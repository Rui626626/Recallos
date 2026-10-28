"""The ingest pipeline: scan → fingerprint → parse → chunk → index.

Four properties this module exists to guarantee, each of which was a defect in
the first draft and each of which now has a test:

1. **The authoritative layer never lags the derived layer.** Every document
   writes its archive and registers the source *before* committing its database
   row, so a crash can never leave an indexed document that a rebuild cannot
   find again.
2. **A failure is always recorded.** No path returns "failed" without first
   writing a durable row carrying the reason.
3. **A version change forces re-processing.** Bumping the normaliser, the
   chunker parameters or an extractor bypasses the "unchanged" shortcuts, so the
   index can never be silently stale.
4. **Renames are recognised.** A path whose bytes match a *missing* record, with
   no other candidate, is a move — not new material.

Statuses a document can hold:

    indexed        parsed and searchable
    duplicate      content-identical to a canonical document; metadata kept,
                   chunks removed to avoid bloat
    metadata_only  the file's bytes changed but the indexable text did not
    empty          no indexable content (content_hash is NULL; never deduped)
    pending        registered but its chunks are missing
    missing        the source is no longer on disk (record kept on purpose)
    failed         a parser rejected it; `doc.error` holds the reason
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from typing import Iterator, Optional, Sequence

from .config import Config
from .core import ids as ids_mod
from .core.chunk import Segment, chunk_segments
from .core.hashing import fingerprint, hash_file, stat_is_trusted
from .core.normalize import content_hash as content_hash_of
from .core.normalize import normalize_text
from .core.schema import META_LAST_RUN_AT, META_LAST_RUN_MODE, META_LAST_RUN_STATE
from .core.store import Store
from .core.versions import NORM_VERSION, chunker_version, extractor_version
from .ingest import textual as _textual  # noqa: F401  (registers parsers)
from .ingest.base import ParseError, ParsedDoc, parser_for, parser_name
from .core.source import FILE, locator_chunk
from .state import Journal, SourceRegistry, now_iso

__all__ = [
    "IngestResult", "scan_files", "ingest_file", "ingest_paths",
    "apply_dedup", "rebuild_from_archive", "rebuild_from_source",
]

INDEXED = "indexed"
DUPLICATE = "duplicate"
METADATA_ONLY = "metadata_only"
EMPTY = "empty"
PENDING = "pending"
MISSING = "missing"
FAILED = "failed"

MAX_CACHED_VERSIONS = 3
_SKIP_STATUSES = (INDEXED, DUPLICATE)


@dataclass
class IngestResult:
    path: str
    status: str
    doc_id: str = ""
    title: str = ""
    chunks: int = 0
    char_count: int = 0
    error: str = ""
    extractor: str = ""
    reason: str = ""
    note: str = ""

    def to_dict(self) -> dict:
        d: dict = {"path": self.path, "status": self.status, "doc_id": self.doc_id,
                   "chunks": self.chunks, "chars": self.char_count}
        if self.title:
            d["title"] = self.title
        if self.extractor:
            d["extractor"] = self.extractor
        if self.reason:
            d["reason"] = self.reason
        if self.note:
            d["note"] = self.note
        if self.error:
            d["error"] = self.error[:500]
        return d


# --------------------------------------------------------------------- scanning

def _glob_to_re(pattern: str) -> re.Pattern:
    """Glob where `**` crosses separators and `*` does not."""
    p = pattern.replace("\\", "/")
    out = ["^"]
    # 开头的 `**/` 必须是**可选**前缀。
    #
    # 踩过：`**/WXWork/**` 编译成 `^.*/WXWork/.*$`，要求前面至少有一个字符。
    # 排除规则现在按「相对目标目录」匹配，路径可能就以 `WXWork/` 开头 —— 于是
    # 这条规则在相对路径下**静默失效**，缓存目录照样被扫。写成 `(?:.*/)?` 之后，
    # 绝对路径与相对路径都能命中。
    if p.startswith("**/"):
        out.append("(?:.*/)?")
        p = p[3:]
    i = 0
    while i < len(p):
        ch = p[i]
        if ch == "*":
            if p[i:i + 2] == "**":
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(ch))
        i += 1
    out.append("$")
    return re.compile("".join(out))


def _matches_any(path: str, patterns: Sequence[str]) -> bool:
    posix = path.replace("\\", "/")
    base = os.path.basename(posix)
    candidates = (posix, posix + "/")
    for pat in patterns:
        if fnmatch.fnmatch(base, pat):
            return True
        rx = _glob_to_re(pat)
        if any(rx.match(c) for c in candidates):
            return True
    return False


def _rel(dirpath: str, name: str, base: str) -> str:
    """`(dirpath/name)` 相对 `base` 的 posix 路径，用于匹配排除规则。"""
    rel = os.path.relpath(os.path.join(dirpath, name), base)
    return rel.replace("\\", "/")


def scan_files(cfg: Config, targets: Sequence[str],
               follow_symlinks: bool = False) -> Iterator[str]:
    """Yield absolute paths of indexable files under `targets`.

    Directories are walked once and pruned by `cfg.excludes`. The caller's own
    tree is never special-cased: indexing something is always an explicit act.
    """
    exts = set(cfg.extensions)
    seen: set[str] = set()

    for target in targets:
        ap = os.path.abspath(target)
        if os.path.isfile(ap):
            if os.path.splitext(ap)[1].lower() in exts and ap not in seen:
                seen.add(ap)
                yield ap
            continue
        if not os.path.isdir(ap):
            continue
        for dirpath, dirnames, filenames in os.walk(ap, followlinks=follow_symlinks):
            # 排除规则只看**目标目录之下**的相对路径。
            #
            # 为什么不是绝对路径：用户显式点名 `add "D:\备份\AppData"` 时，绝对路径里
            # 含 `AppData` 会被一刀切掉 —— 他亲手选的目录反而读不到，而且报「没找到」。
            # 规则要剪的是「大目录里顺手带上的缓存」，不是「用户明确要的东西」。
            # （测试套件自己就全在临时目录里，这个 bug 一改就立刻暴露。）
            dirnames[:] = [
                d for d in dirnames
                if not _matches_any(_rel(dirpath, d, ap), cfg.excludes)
            ]
            for name in filenames:
                full = os.path.join(dirpath, name)
                if _matches_any(_rel(dirpath, name, ap), cfg.excludes):
                    continue
                if os.path.splitext(name)[1].lower() not in exts:
                    continue
                if full in seen:
                    continue
                seen.add(full)
                yield full


# ---------------------------------------------------------------- single ingest

def ingest_file(store: Store, registry: SourceRegistry, journal: Journal,
                cfg: Config, path: str, *, force: bool = False,
                dry_run: bool = False, allow_move: bool = True) -> IngestResult:
    ap = os.path.abspath(path)
    fp = fingerprint(ap)
    if fp is None:
        return IngestResult(path=ap, status=MISSING, error="file not found")

    parser = parser_for(ap)
    ext = os.path.splitext(ap)[1].lower()
    if parser is None:
        return IngestResult(path=ap, status="unsupported",
                            error=f"no parser for {ext}")

    ex_name = parser_name(parser)
    ex_version = extractor_version(ex_name)
    chunker_ver = chunker_version(cfg.chunk_target_chars, cfg.chunk_max_chars)

    existing = store.get_doc(FILE, ap)
    did = existing["doc_id"] if existing else ids_mod.doc_id(FILE, ap)

    # ---- cheap shortcut: only when stat AND every version stamp agree -------
    if existing and not force:
        trusted, reason = stat_is_trusted(
            recorded_mtime_ns=existing["mtime_ns"],
            recorded_size=existing["size"], fp=fp)
        versions_ok = _versions_ok(existing, NORM_VERSION, chunker_ver, ex_version)
        if trusted and versions_ok and existing["status"] in _SKIP_STATUSES:
            return IngestResult(path=ap, status="skipped", doc_id=did,
                                chunks=existing["chunk_count"],
                                char_count=existing["char_count"] or 0,
                                reason=reason)

    # ---- byte identity -----------------------------------------------------
    try:
        file_hash = hash_file(ap)
    except OSError as exc:
        return _record_failure(store, journal, did, ap, fp, None,
                               f"cannot read file: {exc}", dry_run)

    # A path whose bytes match a *missing* record is a rename or a move.
    note = ""
    moved_from = ""
    if existing is None and allow_move and file_hash:
        candidates = store.move_candidates(file_hash)
        if candidates:
            chosen = candidates[0]
            did = chosen["doc_id"]
            moved_from = chosen["source_ref"]
            if len(candidates) > 1:
                note = (f"{len(candidates)} records shared this content; chose "
                        f"{chosen['source_ref']} (earliest missing, then path order)")
            existing = chosen

    # ---- parse -------------------------------------------------------------
    try:
        parsed: ParsedDoc = parser(ap)
    except ParseError as exc:
        return _record_failure(store, journal, did, ap, fp, file_hash,
                               str(exc), dry_run)
    except Exception as exc:  # a parser bug must not kill the batch
        return _record_failure(store, journal, did, ap, fp, file_hash,
                               f"{type(exc).__name__}: {exc}", dry_run)

    normalized_len = len(normalize_text(parsed.text))
    chash = content_hash_of(parsed.text)  # None when there is nothing to index

    # ---- comparison against the current record -----------------------------
    # Skipped entirely for a move: the record exists, but it describes a path
    # that is gone, so "nothing changed" would be the wrong answer.
    if (existing is not None and not force and not moved_from
            and existing["doc_id"] == did):
        versions_ok = _versions_ok(existing, NORM_VERSION, chunker_ver, ex_version)
        same_bytes = existing["file_hash"] == file_hash
        same_content = (chash is not None and existing["content_hash"] == chash)

        if same_bytes and versions_ok and existing["status"] in _SKIP_STATUSES:
            if not dry_run:
                _reaffirm(store, registry, journal, cfg, did, ap, parsed, fp,
                          file_hash, chash, existing, ex_version, chunker_ver,
                          status=existing["status"])
            return IngestResult(path=ap, status="unchanged", doc_id=did,
                                title=parsed.title, chunks=existing["chunk_count"],
                                char_count=existing["char_count"] or 0,
                                extractor=parsed.extractor)

        if same_content and versions_ok:
            # Bytes differ (re-save, new metadata) but the searchable text is
            # identical: keep the chunks, refresh the file identity.
            if not dry_run:
                _reaffirm(store, registry, journal, cfg, did, ap, parsed, fp,
                          file_hash, chash, existing, ex_version, chunker_ver,
                          status=METADATA_ONLY)
            return IngestResult(path=ap, status=METADATA_ONLY, doc_id=did,
                                title=parsed.title, chunks=existing["chunk_count"],
                                char_count=normalized_len,
                                extractor=parsed.extractor)

    # ---- no indexable content ---------------------------------------------
    if chash is None:
        if not dry_run:
            _write_archive(cfg, _archive_record(
                did, ap, parsed, ex_version, chunker_ver, chash, file_hash, fp,
                char_count=0, chunk_count=0, status=EMPTY))
            registry.add(FILE, ap, did, {"ext": ext, "extractor": parsed.extractor})
            registry.save()
            store.upsert_doc(
                doc_id=did, source_kind=FILE, source_ref=ap, title=parsed.title,
                content_hash=None, doc_hash=None, mtime=fp.mtime, size=fp.size,
                mtime_ns=fp.mtime_ns, file_hash=file_hash, status=EMPTY,
                extractor=parsed.extractor, extractor_version=ex_version,
                chunker_version_value=chunker_ver, norm_version=NORM_VERSION,
                char_count=0, chunk_count=0, clear_error=True, missing_at=None)
            store.delete_chunks(did)
            if moved_from:
                registry.remove(FILE, moved_from)
                registry.save()
                store.record_source_history(did, FILE, ap, "moved", note)
                store.clear_missing(did)
            journal.append({"path": ap, "doc_id": did, "status": EMPTY})
            store.conn.commit()
        return IngestResult(path=ap, status="moved" if moved_from else EMPTY,
                            doc_id=did, title=parsed.title, chunks=0,
                            char_count=0, extractor=parsed.extractor,
                            note=note,
                            reason="no indexable content")

    # ---- chunk -------------------------------------------------------------
    chunks = chunk_segments(parsed.segments,
                            target_chars=cfg.chunk_target_chars,
                            max_chars=cfg.chunk_max_chars)
    chunk_ids = [ids_mod.chunk_id(did, c.chunk_hash, c.occurrence) for c in chunks]

    if dry_run:
        return IngestResult(path=ap, status="updated" if existing else "new",
                            doc_id=did, title=parsed.title, chunks=len(chunks),
                            char_count=normalized_len, extractor=parsed.extractor)

    # ---- authoritative layer FIRST, then the derived layer -----------------
    _write_archive(cfg, _archive_record(
        did, ap, parsed, ex_version, chunker_ver, chash, file_hash, fp,
        char_count=normalized_len, chunk_count=len(chunks), status=INDEXED))
    _write_cache(cfg, did, chash, parsed)

    registry.add(FILE, ap, did, {"ext": ext, "extractor": parsed.extractor})
    if moved_from:
        # The registry tracks what is *currently* tracked; where a document has
        # been is `source_history`'s job.
        registry.remove(FILE, moved_from)
    registry.save()

    store.upsert_doc(
        doc_id=did, source_kind=FILE, source_ref=ap, title=parsed.title,
        content_hash=chash, doc_hash=chash, mtime=fp.mtime, size=fp.size,
        mtime_ns=fp.mtime_ns, file_hash=file_hash, status=INDEXED,
        extractor=parsed.extractor, extractor_version=ex_version,
        chunker_version_value=chunker_ver, norm_version=NORM_VERSION,
        char_count=normalized_len, chunk_count=len(chunks),
        clear_error=True, missing_at=None)
    store.replace_chunks(did, chunks, chunk_ids)
    store.record_revision(did, chash, file_hash, normalized_len, len(chunks))
    store.record_source_history(
        did, FILE, ap,
        "moved" if moved_from else ("updated" if existing else "new"), note)
    store.clear_missing(did)

    if moved_from:
        status = "moved"
    elif existing is None:
        status = "new"
    elif not _versions_ok(existing, NORM_VERSION, chunker_ver, ex_version):
        status = "reindexed"
    else:
        status = "updated"

    journal.append({"path": ap, "doc_id": did, "status": status,
                    "chunks": len(chunks), "chars": normalized_len,
                    "extractor": parsed.extractor})
    store.conn.commit()

    return IngestResult(path=ap, status=status, doc_id=did, title=parsed.title,
                        chunks=len(chunks), char_count=normalized_len,
                        extractor=parsed.extractor, note=note)


def _versions_ok(row, norm_version: str, chunker_ver: str,
                 ex_version: str) -> bool:
    return (row["norm_version"] == norm_version
            and row["chunker_version"] == chunker_ver
            and row["extractor_version"] == ex_version)


def _reaffirm(store: Store, registry: SourceRegistry, journal: Journal,
              cfg: Config, did: str, ap: str, parsed: ParsedDoc, fp,
              file_hash: str, chash: Optional[str], existing, ex_version: str,
              chunker_ver: str, *, status: str) -> None:
    """Refresh identity/stat fields without touching the chunks."""
    store.upsert_doc(
        doc_id=did, source_kind=FILE, source_ref=ap, title=parsed.title,
        content_hash=chash, doc_hash=chash, mtime=fp.mtime, size=fp.size,
        mtime_ns=fp.mtime_ns, file_hash=file_hash, status=status,
        extractor=parsed.extractor, extractor_version=ex_version,
        chunker_version_value=chunker_ver, norm_version=NORM_VERSION,
        duplicate_of=existing["duplicate_of"],
        char_count=existing["char_count"], chunk_count=existing["chunk_count"])
    registry.add(FILE, ap, did, {"ext": os.path.splitext(ap)[1].lower(),
                                 "extractor": parsed.extractor})
    registry.save()
    journal.append({"path": ap, "doc_id": did, "status": status})
    store.conn.commit()


def _record_failure(store: Store, journal: Journal, doc_id: str, path: str,
                    fp, file_hash: Optional[str], message: str,
                    dry_run: bool) -> IngestResult:
    """Always write the row first, so a failure can never be lost.

    The first draft called `set_status()` without an upsert; for a document seen
    for the first time that UPDATE matched zero rows and the failure vanished.
    """
    if not dry_run:
        store.upsert_doc(
            doc_id=doc_id, source_kind=FILE, source_ref=path,
            title=os.path.basename(path), content_hash=None, doc_hash=None,
            mtime=fp.mtime, size=fp.size, mtime_ns=fp.mtime_ns,
            file_hash=file_hash, status=FAILED, extractor="",
            char_count=0, chunk_count=0, error=message)
        store.set_error(doc_id, message)
        journal.append({"path": path, "doc_id": doc_id, "status": "failed",
                        "error": message[:500]})
        store.conn.commit()
    return IngestResult(path=path, status="failed", doc_id=doc_id, error=message)


# ------------------------------------------------------------------ batch entry

def ingest_paths(store: Store, registry: SourceRegistry, journal: Journal,
                 cfg: Config, targets: Sequence[str], *, force: bool = False,
                 dry_run: bool = False, prune: bool = False,
                 progress=None) -> dict:
    files = list(scan_files(cfg, targets))
    results: list[IngestResult] = []

    store.set_meta(META_LAST_RUN_STATE, "running")
    store.set_meta(META_LAST_RUN_AT, now_iso())
    store.set_meta(META_LAST_RUN_MODE, "add")
    store.conn.commit()

    if not dry_run:
        # Mark vanished sources *before* processing, not after. A rename can
        # only be recognised while the old record already reads as missing —
        # doing this at the end of the run would make every move look like a
        # brand new document.
        _sweep_missing(store, registry, journal, files, prune=prune)

    for i, path in enumerate(files, 1):
        if progress:
            progress(i, len(files), path)
        results.append(ingest_file(store, registry, journal, cfg, path,
                                   force=force, dry_run=dry_run))

    if not dry_run:
        apply_dedup(store)
        _sweep_missing(store, registry, journal, files, prune=prune)
        registry.save()
        store.set_meta(META_LAST_RUN_STATE, "complete")
        store.conn.commit()

    summary = _summarize(files, results)
    summary["dedup"] = dedup_stats(store)
    return summary


def _sweep_missing(store: Store, registry: SourceRegistry, journal: Journal,
                   seen: Sequence[str], prune: bool = False) -> list[str]:
    """Mark registered sources that were not on disk as missing.

    Removal is opt-in (`--prune`) precisely because the record is the evidence a
    later rename needs.
    """
    seen_set = {os.path.normcase(os.path.abspath(p)) for p in seen}
    gone: list[str] = []
    for entry in registry.all():
        ref = entry.get("source_ref", "")
        if os.path.normcase(os.path.abspath(ref)) in seen_set:
            registry.clear_missing(FILE, ref)
            continue
        if os.path.isfile(ref):
            continue
        gone.append(ref)
        row = store.get_doc(FILE, ref)
        if prune:
            if row is not None:
                store.delete_chunks(row["doc_id"])
                store.conn.execute("DELETE FROM doc WHERE doc_id=?", (row["doc_id"],))
            registry.remove(FILE, ref)
            journal.append({"path": ref, "status": "pruned"})
        else:
            if row is not None and row["status"] != MISSING:
                store.mark_missing(row["doc_id"])
                journal.append({"path": ref, "doc_id": row["doc_id"],
                                "status": "missing"})
            registry.mark_missing(FILE, ref)
    return gone


def _summarize(files: Sequence[str], results: Sequence[IngestResult]) -> dict:
    by_status: dict[str, int] = {}
    for r in results:
        by_status[r.status] = by_status.get(r.status, 0) + 1
    return {
        "scanned": len(files),
        "processed": len(results),
        "by_status": by_status,
        "chunks_written": sum(r.chunks for r in results
                              if r.status in ("new", "moved", "updated",
                                              "reindexed")),
        "failures": [r.to_dict() for r in results if r.status == "failed"],
        "moves": [r.to_dict() for r in results if r.status == "moved"],
        "results": [r.to_dict() for r in results],
    }


# --------------------------------------------------------------------- dedup

def apply_dedup(store: Store) -> dict:
    """Mark content-identical documents whose chunks are redundant.

    The canonical copy is the one with the smallest `source_ref` — a choice that
    depends only on stored values, so a rebuild picks exactly the same winner.
    Writes only when the outcome actually changes, so a repeated import does not
    churn the database.
    """
    marked = 0
    reset = 0
    groups = store.conn.execute(
        """SELECT doc_hash FROM doc
           WHERE doc_hash IS NOT NULL AND doc_hash <> '' AND status <> ?
           GROUP BY doc_hash HAVING COUNT(*) > 1""",
        (FAILED,)).fetchall()

    for row in groups:
        members = store.conn.execute(
            "SELECT doc_id, source_ref, status, duplicate_of FROM doc "
            "WHERE doc_hash=? ORDER BY source_ref", (row["doc_hash"],)).fetchall()
        canonical = members[0]["doc_id"]
        for member in members[1:]:
            if member["status"] == DUPLICATE and member["duplicate_of"] == canonical:
                continue  # already correct — do not rewrite
            store.delete_chunks(member["doc_id"])
            store.conn.execute(
                "UPDATE doc SET status=?, duplicate_of=?, chunk_count=0, updated_at=? "
                "WHERE doc_id=?",
                (DUPLICATE, canonical, now_iso(), member["doc_id"]))
            marked += 1
        if members[0]["status"] == DUPLICATE:
            store.conn.execute(
                "UPDATE doc SET status=?, duplicate_of=NULL, updated_at=? WHERE doc_id=?",
                (PENDING, now_iso(), canonical))
            reset += 1

    orphans = store.conn.execute(
        """SELECT doc_id FROM doc
           WHERE status=? AND (duplicate_of IS NULL
                OR duplicate_of NOT IN (SELECT doc_id FROM doc))""",
        (DUPLICATE,)).fetchall()
    for row in orphans:
        store.conn.execute(
            "UPDATE doc SET status=?, duplicate_of=NULL, updated_at=? WHERE doc_id=?",
            (PENDING, now_iso(), row["doc_id"]))
        reset += 1

    store.conn.commit()
    return {"marked": marked, "reset": reset}


def dedup_stats(store: Store) -> dict:
    row = store.conn.execute(
        "SELECT COUNT(*) FROM doc WHERE status=?", (DUPLICATE,)).fetchone()
    return {"duplicates": row[0] if row else 0}


# ------------------------------------------------------------------- rebuild

def rebuild_from_archive(store: Store, cfg: Config, *, progress=None) -> dict:
    """Recreate the index from `data/docs/*.json` + `cache/`.

    Reads **no original files**, which is what makes it work when documents have
    moved, been archived, or live on a disconnected drive. Documents whose
    cached segments are unavailable are reported, so the caller can fall back to
    `rebuild_from_source` for just those.
    """
    archives = _load_archives(cfg)
    restored = 0
    need_source: list[str] = []
    failures: list[dict] = []

    for i, record in enumerate(archives, 1):
        if progress:
            progress(i, len(archives), record.get("source_ref", ""))
        doc_id = record.get("doc_id")
        chash = record.get("content_hash")
        if not doc_id:
            failures.append({"archive": "?", "error": "archive has no doc_id"})
            continue
        if not chash:
            _restore_doc_row(store, record, chunk_count=0, status=EMPTY)
            restored += 1
            continue
        segments = _read_cache(cfg, doc_id, chash)
        if segments is None:
            need_source.append(record.get("source_ref", doc_id))
            continue
        chunks = chunk_segments(segments, target_chars=cfg.chunk_target_chars,
                                max_chars=cfg.chunk_max_chars)
        chunk_ids = [ids_mod.chunk_id(doc_id, c.chunk_hash, c.occurrence)
                     for c in chunks]
        _restore_doc_row(store, record, chunk_count=len(chunks), status=INDEXED)
        store.replace_chunks(doc_id, chunks, chunk_ids)
        store.record_revision(doc_id, chash, record.get("file_hash"),
                              record.get("char_count") or 0, len(chunks))
        store.record_source_history(doc_id, FILE, record.get("source_ref", ""),
                                    "rebuild")
        restored += 1

    apply_dedup(store)
    store.conn.commit()
    return {"archives": len(archives), "restored": restored,
            "need_source": need_source, "failures": failures}


def rebuild_from_source(store: Store, registry: SourceRegistry, journal: Journal,
                        cfg: Config, *, progress=None) -> dict:
    """Re-extract from the original files. Used when an extractor changed.

    Sources that no longer exist are marked missing — **not** forgotten, because
    the record is what lets a later rename be recognised.
    """
    entries = [e for e in registry.all() if e.get("source_kind") == FILE]
    results: list[IngestResult] = []
    missing: list[str] = []

    store.set_meta(META_LAST_RUN_STATE, "running")
    store.set_meta(META_LAST_RUN_MODE, "rebuild-from-source")
    store.conn.commit()

    for i, entry in enumerate(entries, 1):
        path = entry.get("source_ref", "")
        if progress:
            progress(i, len(entries), path)
        if not os.path.isfile(path):
            missing.append(path)
            row = store.get_doc(FILE, path)
            if row is not None:
                store.mark_missing(row["doc_id"])
            registry.mark_missing(FILE, path)
            journal.append({"path": path, "doc_id": entry.get("doc_id", ""),
                            "status": "missing"})
            continue
        results.append(ingest_file(store, registry, journal, cfg, path,
                                   force=True, allow_move=False))

    apply_dedup(store)
    registry.save()
    store.set_meta(META_LAST_RUN_STATE, "complete")
    store.conn.commit()

    summary = _summarize([e.get("source_ref", "") for e in entries], results)
    summary["missing"] = missing
    return summary


# ------------------------------------------------------------------ artefacts

def _archive_path(cfg: Config, doc_id: str) -> str:
    return os.path.join(cfg.docs_dir(), f"{doc_id}.json")


def _archive_record(doc_id: str, source_ref: str, parsed: ParsedDoc,
                    ex_version: str, chunker_ver: str,
                    content_hash: Optional[str], file_hash: Optional[str],
                    fp, *, char_count: int, chunk_count: int,
                    status: str) -> dict:
    return {
        "doc_id": doc_id,
        "source_kind": FILE,
        "source_ref": source_ref,
        "source_key": ids_mod.source_key(FILE, source_ref),
        "title": parsed.title,
        "extractor": parsed.extractor,
        "extractor_version": ex_version,
        "content_hash": content_hash,
        "file_hash": file_hash,
        "mtime": fp.mtime,
        "mtime_ns": fp.mtime_ns,
        "size": fp.size,
        "char_count": char_count,
        "chunk_count": chunk_count,
        "norm_version": NORM_VERSION,
        "chunker_version": chunker_ver,
        "status": status,
        "meta": parsed.meta,
    }


def _write_archive(cfg: Config, record: dict) -> str:
    directory = cfg.docs_dir()
    os.makedirs(directory, exist_ok=True)
    target = _archive_path(cfg, record["doc_id"])

    revisions: list[dict] = []
    if os.path.exists(target):
        try:
            with open(target, "r", encoding="utf-8") as fh:
                revisions = json.load(fh).get("revisions") or []
        except (OSError, json.JSONDecodeError):
            revisions = []

    if record.get("content_hash"):
        entry = {"at": now_iso(), "content_hash": record["content_hash"],
                 "file_hash": record.get("file_hash"),
                 "char_count": record.get("char_count"),
                 "chunk_count": record.get("chunk_count")}
        revisions = [r for r in revisions
                     if r.get("content_hash") != entry["content_hash"]]
        revisions.append(entry)
        revisions = revisions[-MAX_CACHED_VERSIONS:]

    payload = {**record, "revisions": revisions}
    _atomic_write_json(target, payload)
    return target


def _load_archives(cfg: Config) -> list[dict]:
    directory = cfg.docs_dir()
    if not os.path.isdir(directory):
        return []
    out: list[dict] = []
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(directory, name), "r", encoding="utf-8") as fh:
                out.append(json.load(fh))
        except (OSError, json.JSONDecodeError):
            continue  # unreadable archives are surfaced by `doctor`
    return out


def _cache_dir(cfg: Config, doc_id: str) -> str:
    return os.path.join(cfg.cache_dir, doc_id)


def _write_cache(cfg: Config, doc_id: str, content_hash: str,
                 parsed: ParsedDoc) -> None:
    """Persist the segments — not just flat text.

    Re-chunking needs the heading paths and page numbers that came from the
    parse; flattening to text would lose them and make an archive rebuild
    produce different metadata than the original run.
    """
    directory = _cache_dir(cfg, doc_id)
    os.makedirs(directory, exist_ok=True)
    payload = {
        "doc_id": doc_id,
        "content_hash": content_hash,
        "title": parsed.title,
        "extractor": parsed.extractor,
        "meta": parsed.meta,
        "segments": [
            {"text": s.text, "heading_path": s.heading_path, "page": s.page,
             "ts_start": s.ts_start, "ts_end": s.ts_end}
            for s in parsed.segments
        ],
    }
    _atomic_write_json(os.path.join(directory, f"{content_hash}.json"), payload)
    _prune_cache(cfg, doc_id, keep=MAX_CACHED_VERSIONS)


def _read_cache(cfg: Config, doc_id: str,
                content_hash: str) -> Optional[list[Segment]]:
    path = os.path.join(_cache_dir(cfg, doc_id), f"{content_hash}.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    return [
        Segment(text=s.get("text", ""), heading_path=s.get("heading_path", ""),
                page=s.get("page"), ts_start=s.get("ts_start"),
                ts_end=s.get("ts_end"))
        for s in payload.get("segments") or []
    ]


def _prune_cache(cfg: Config, doc_id: str, keep: int) -> None:
    directory = _cache_dir(cfg, doc_id)
    if not os.path.isdir(directory):
        return
    files = [f for f in os.listdir(directory) if f.endswith(".json")]
    if len(files) <= keep:
        return
    files.sort(key=lambda f: os.path.getmtime(os.path.join(directory, f)),
               reverse=True)
    for name in files[keep:]:
        try:
            os.unlink(os.path.join(directory, name))
        except OSError:
            pass


def clear_cache(cfg: Config) -> None:
    if os.path.isdir(cfg.cache_dir):
        shutil.rmtree(cfg.cache_dir, ignore_errors=True)
    os.makedirs(cfg.cache_dir, exist_ok=True)


# -------------------------------------------------------------------- helpers

def _restore_doc_row(store: Store, record: dict, *, chunk_count: int,
                     status: str) -> None:
    store.upsert_doc(
        doc_id=record["doc_id"],
        source_kind=record.get("source_kind", FILE),
        source_ref=record.get("source_ref", ""),
        title=record.get("title", ""),
        content_hash=record.get("content_hash"),
        doc_hash=record.get("content_hash"),
        mtime=record.get("mtime"), size=record.get("size"),
        mtime_ns=record.get("mtime_ns") or 0,
        file_hash=record.get("file_hash"),
        status=status,
        extractor=record.get("extractor", ""),
        extractor_version=record.get("extractor_version"),
        chunker_version_value=record.get("chunker_version"),
        norm_version=record.get("norm_version"),
        char_count=record.get("char_count") or 0,
        chunk_count=chunk_count,
        clear_error=True)


def _atomic_write_json(path: str, payload: dict) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise
