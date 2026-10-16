"""The derived index: opening, writing, searching, integrity checking.

`index/recall.db` is disposable — everything here is regenerated from
`data/docs/*.json` plus `cache/`. That is why there is no migration logic: an
unrecognised schema version is dropped and recreated.

Two rules the write paths exist to enforce:

* **A failure is always recorded.** No path may return "failed" without a
  durable row carrying the reason. A silent failure is worse than a loud one.
* **Identity is the normalised source key.** Not the raw reference string —
  otherwise two spellings of one Windows path fight over a single primary key.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Optional, Sequence

from . import schema as schema_mod
from .chunk import Chunk
from .ids import normalize_ref, source_key
from .pinyin import indexed_projection
from .tokenize import as_phrase, cjk_segment, has_cjk, longest_cjk_run
from .versions import NORM_VERSION, chunker_version as _chunker_version

__all__ = ["Store", "SearchHit", "open_store", "DISPLAY_OPEN", "DISPLAY_CLOSE"]

SNIPPET_MARK_OPEN = "\x01"
SNIPPET_MARK_CLOSE = "\x02"
# Real-world notes are full of `[[wiki links]]` and `**emphasis**`; marking a
# hit with brackets produced unreadable `[[[[双重]]]]` output. Control
# characters cannot occur in extracted text, so they are safe as internal
# markers and are converted to these display glyphs before anything leaves.
DISPLAY_OPEN = "«"
DISPLAY_CLOSE = "»"
SNIPPET_CJK_WIDTH = 120


def _present(text: str) -> str:
    if not text:
        return text or ""
    return text.replace(SNIPPET_MARK_OPEN, DISPLAY_OPEN).replace(
        SNIPPET_MARK_CLOSE, DISPLAY_CLOSE)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@dataclass
class SearchHit:
    chunk_id: str
    doc_id: str
    ordinal: int
    title: str
    source_ref: str
    source_kind: str
    heading_path: str
    page: Optional[int]
    snippet: str
    score: float
    channel: str
    chunk_hash: str = ""
    occurrence: int = 0
    #: 切片原文。**不参与 to_dict()** —— 它只在进程内用于模糊重排，避免把整段
    #: 正文塞进 `--json` 输出。
    text: str = ""

    def to_dict(self) -> dict:
        return {
            "chunk_id": self.chunk_id,
            "doc_id": self.doc_id,
            "ordinal": self.ordinal,
            "title": self.title,
            "source_ref": self.source_ref,
            "source_kind": self.source_kind,
            "heading_path": self.heading_path,
            "page": self.page,
            "snippet": self.snippet,
            "score": round(self.score, 4),
            "channel": self.channel,
            "locator": f"doc:{self.doc_id}#h={self.chunk_hash[:10]}@{self.ordinal}",
        }


class Store:
    """Thin, explicit wrapper around one SQLite connection."""

    def __init__(self, conn: sqlite3.Connection, db_path: str) -> None:
        self.conn = conn
        self.db_path = db_path

    # ---------------------------------------------------------------- lifecycle

    @classmethod
    def open(cls, db_path: str, create: bool = True) -> "Store":
        os.makedirs(os.path.dirname(os.path.abspath(db_path)) or ".", exist_ok=True)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        if create and schema_mod.read_version(conn) != schema_mod.SCHEMA_VERSION:
            schema_mod.drop_all(conn)
            schema_mod.create_schema(conn)
            conn.commit()
        return cls(conn, db_path)

    def close(self) -> None:
        try:
            self.conn.commit()
        finally:
            self.conn.close()

    def reset(self) -> None:
        """Drop every object we own and recreate the schema in place.

        Used instead of relying on the database *file* being deleted: on Windows
        a lingering handle can keep it alive, and reusing stale rows would make
        a rebuild silently wrong.
        """
        schema_mod.drop_all(self.conn)
        schema_mod.create_schema(self.conn)
        self.conn.commit()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------- meta

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )

    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        try:
            row = self.conn.execute(
                "SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        except sqlite3.OperationalError:
            return default
        return row[0] if row else default

    # ------------------------------------------------------------------- doc

    def upsert_doc(
        self,
        *,
        doc_id: str,
        source_kind: str,
        source_ref: str,
        title: str,
        content_hash: Optional[str],
        doc_hash: Optional[str],
        mtime: Optional[float],
        size: Optional[int],
        status: str,
        extractor: str,
        char_count: int,
        chunk_count: int,
        # --- P1 additions; every one has a safe default so older callers keep
        # --- working unchanged.
        source_key_value: Optional[str] = None,
        mtime_ns: int = 0,
        file_hash: Optional[str] = None,
        norm_version: Optional[str] = None,
        chunker_version_value: Optional[str] = None,
        extractor_version: Optional[str] = None,
        duplicate_of: Optional[str] = None,
        error: Optional[str] = None,
        missing_at: Optional[str] = None,
        clear_error: bool = False,
    ) -> None:
        ts = now_iso()
        self.conn.execute(
            """
            INSERT INTO doc (doc_id, source_kind, source_key, source_ref, title,
                             mtime, mtime_ns, size, file_hash, content_hash, doc_hash,
                             status, duplicate_of, missing_at, error, error_at,
                             norm_version, chunker_version, extractor,
                             extractor_version, char_count, chunk_count,
                             imported_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(doc_id) DO UPDATE SET
              source_kind=excluded.source_kind,
              source_key=excluded.source_key,
              source_ref=excluded.source_ref,
              title=excluded.title,
              mtime=excluded.mtime, mtime_ns=excluded.mtime_ns, size=excluded.size,
              file_hash=excluded.file_hash,
              content_hash=excluded.content_hash, doc_hash=excluded.doc_hash,
              status=excluded.status, duplicate_of=excluded.duplicate_of,
              missing_at=excluded.missing_at,
              error=excluded.error,
              error_at=CASE WHEN excluded.error IS NULL THEN NULL
                            ELSE excluded.updated_at END,
              norm_version=excluded.norm_version,
              chunker_version=excluded.chunker_version,
              extractor=excluded.extractor,
              extractor_version=excluded.extractor_version,
              char_count=excluded.char_count, chunk_count=excluded.chunk_count,
              updated_at=excluded.updated_at
            """,
            (
                doc_id, source_kind,
                source_key_value or source_key(source_kind, source_ref),
                source_ref, title,
                mtime, mtime_ns, size, file_hash, content_hash, doc_hash,
                status, duplicate_of, missing_at,
                None if clear_error else error, ts if (error and not clear_error) else None,
                norm_version or NORM_VERSION,
                chunker_version_value or _chunker_version(900, 1600),
                extractor, extractor_version,
                char_count, chunk_count, ts, ts,
            ),
        )

    def set_error(self, doc_id: str, message: str) -> None:
        """Record a failure. Always paired with an upsert so the row exists."""
        self.conn.execute(
            "UPDATE doc SET error=?, error_at=?, updated_at=? WHERE doc_id=?",
            (message[:4000], now_iso(), now_iso(), doc_id),
        )

    def set_status(self, doc_id: str, status: str, error: str = "") -> None:
        self.conn.execute(
            "UPDATE doc SET status=?, updated_at=? WHERE doc_id=?",
            (status, now_iso(), doc_id),
        )
        if error:
            self.set_error(doc_id, error)

    def mark_missing(self, doc_id: str, when: Optional[str] = None) -> None:
        """Mark a source as gone **without deleting the record.**

        The record is the only evidence that lets a later re-appearance be
        recognised as a rename or a move rather than as a brand new document.
        """
        self.conn.execute(
            "UPDATE doc SET status='missing', missing_at=?, updated_at=? WHERE doc_id=?",
            (when or now_iso(), now_iso(), doc_id),
        )

    def clear_missing(self, doc_id: str) -> None:
        self.conn.execute(
            "UPDATE doc SET missing_at=NULL WHERE doc_id=?", (doc_id,))

    # ------------------------------------------------------------------ reads

    def doc_by_id(self, doc_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM doc WHERE doc_id=?", (doc_id,)).fetchone()

    def get_doc(self, source_kind: str, source_ref: str) -> Optional[sqlite3.Row]:
        """Look up by normalised key — the raw spelling is only for display."""
        return self.conn.execute(
            "SELECT * FROM doc WHERE source_kind=? AND source_key=?",
            (source_kind, source_key(source_kind, source_ref)),
        ).fetchone()

    def docs_by_file_hash(self, file_hash: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM doc WHERE file_hash=? ORDER BY source_ref", (file_hash,)
        ).fetchall()

    def move_candidates(self, file_hash: str) -> list[sqlite3.Row]:
        """Documents whose bytes match but whose file is no longer on disk.

        Candidates are ordered deterministically so that an ambiguous match
        resolves the same way on every run: earliest disappearance first, then
        lexicographic path.
        """
        return self.conn.execute(
            """SELECT * FROM doc
               WHERE file_hash=? AND missing_at IS NOT NULL
               ORDER BY missing_at ASC, source_ref ASC""",
            (file_hash,),
        ).fetchall()

    def docs_by_content_hash(self, content_hash: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM doc WHERE content_hash=? ORDER BY source_ref",
            (content_hash,),
        ).fetchall()

    def chunk(self, chunk_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM chunk WHERE chunk_id=?", (chunk_id,)).fetchone()

    def counts(self) -> dict:
        out = {}
        for name, sql in (
            ("docs", "SELECT COUNT(*) FROM doc"),
            ("chunks", "SELECT COUNT(*) FROM chunk"),
            ("fts_trigram", "SELECT COUNT(*) FROM chunk_fts"),
            ("fts_cjk", "SELECT COUNT(*) FROM chunk_fts_cjk"),
        ):
            try:
                out[name] = self.conn.execute(sql).fetchone()[0]
            except sqlite3.OperationalError:
                out[name] = -1
        return out

    def status_counts(self) -> dict:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) FROM doc GROUP BY status").fetchall()
        return {r[0]: r[1] for r in rows}

    # --------------------------------------------------------------- history

    def record_source_history(self, doc_id: str, source_kind: str, source_ref: str,
                              reason: str, note: str = "") -> None:
        ts = now_iso()
        self.conn.execute(
            """
            INSERT INTO source_history
              (doc_id, source_kind, source_ref, source_key, reason,
               first_seen_at, last_seen_at, note)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(doc_id, source_key) DO UPDATE SET
              source_ref=excluded.source_ref,
              reason=excluded.reason,
              last_seen_at=excluded.last_seen_at,
              note=excluded.note
            """,
            (doc_id, source_kind, source_ref, source_key(source_kind, source_ref),
             reason, ts, ts, note or None),
        )

    def source_history(self, doc_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM source_history WHERE doc_id=? ORDER BY first_seen_at",
            (doc_id,),
        ).fetchall()

    def record_revision(self, doc_id: str, content_hash: Optional[str],
                        file_hash: Optional[str], char_count: int,
                        chunk_count: int) -> None:
        if not content_hash:
            return
        self.conn.execute(
            """INSERT INTO doc_revision
                 (doc_id, content_hash, file_hash, char_count, chunk_count, recorded_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(doc_id, content_hash) DO NOTHING""",
            (doc_id, content_hash, file_hash, char_count, chunk_count, now_iso()),
        )

    def revisions(self, doc_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM doc_revision WHERE doc_id=? ORDER BY recorded_at",
            (doc_id,),
        ).fetchall()

    # ---------------------------------------------------------------- chunks

    def replace_chunks(self, doc_id: str, chunks: Sequence[Chunk],
                       chunk_ids: Sequence[str]) -> None:
        """Delete then reinsert. Identical input yields identical rows.

        **索引的文本是 `标题 + 标题路径 + 正文`，不只是正文。** 只索引正文会导致
        两个致命盲区（都是实测发现的）：

        * 文档标题不在任何切片正文里 → **搜文档自己的标题返回空**
        * 小标题只存在于 `heading_path` 里 → **搜某一节的名字也返回空**

        对「找回我保存过的东西」这个用途来说，这两条就等于工具废了一半。标题放前面
        既让 bm25 给出应有的权重，又不动正文命中的 snippet（FTS5 取最佳匹配区域）。

        另外还会附上一段**汉字拼音的连写**（`通货膨胀` → `tonghuopengzhang`），于是
        输入拼音能搜到中文，同音错字（飞利浦斯 / 菲利普斯）也能互相命中。
        这段的可开关性由数据文件本身决定：删掉 `brain/data/pinyin.tsv.gz`，
        `pinyin_projection` 就返回空串，索引自动退回「只有中文」的形态。
        """
        if len(chunks) != len(chunk_ids):
            raise ValueError("chunk/id length mismatch")
        row = self.conn.execute(
            "SELECT title FROM doc WHERE doc_id=?", (doc_id,)).fetchone()
        title = (row["title"] or "") if row is not None else ""

        self.conn.execute("DELETE FROM chunk WHERE doc_id=?", (doc_id,))
        self.conn.execute("DELETE FROM chunk_fts WHERE doc_id=?", (doc_id,))
        self.conn.execute("DELETE FROM chunk_fts_cjk WHERE doc_id=?", (doc_id,))
        for chunk, cid in zip(chunks, chunk_ids):
            self.conn.execute(
                """INSERT INTO chunk (chunk_id, doc_id, ordinal, heading_path, text,
                                      page, ts_start, ts_end, char_start, char_end,
                                      chunk_hash, occurrence)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (cid, doc_id, chunk.ordinal, chunk.heading_path, chunk.text,
                 chunk.page, chunk.ts_start, chunk.ts_end,
                 chunk.char_start, chunk.char_end,
                 chunk.chunk_hash, chunk.occurrence),
            )
            indexed = indexed_projection(title, chunk.heading_path, chunk.text)
            self.conn.execute(
                "INSERT INTO chunk_fts (text, chunk_id, doc_id) VALUES (?,?,?)",
                (indexed, cid, doc_id),
            )
            self.conn.execute(
                "INSERT INTO chunk_fts_cjk (text, chunk_id, doc_id) VALUES (?,?,?)",
                (cjk_segment(indexed), cid, doc_id),
            )

    def delete_chunks(self, doc_id: str) -> None:
        for table in ("chunk_fts", "chunk_fts_cjk", "chunk"):
            self.conn.execute(f"DELETE FROM {table} WHERE doc_id=?", (doc_id,))

    def chunk_ids(self) -> list[str]:
        return [r[0] for r in self.conn.execute(
            "SELECT chunk_id FROM chunk ORDER BY chunk_id")]

    # ----------------------------------------------------------------- search

    def search(self, query: str, limit: int = 20) -> list[SearchHit]:
        """Two-channel retrieval.

        Channel 1 is the trigram index (substring semantics, 3+ characters).
        Channel 2 is the per-character index, used when channel 1 finds nothing
        and the query contains CJK — the only way a 2-character query works.

        A query *error* propagates: silently returning "no results" would hide a
        bug behind a plausible answer.
        """
        q = (query or "").strip()
        if not q:
            return []

        hits: dict[str, SearchHit] = {}
        if longest_cjk_run(q) >= 3 or not has_cjk(q):
            for hit in self._fts_search("chunk_fts", as_phrase(q), limit, "trigram"):
                hits.setdefault(hit.chunk_id, hit)

        if len(hits) < limit and has_cjk(q):
            seg = cjk_segment(q)
            if seg:
                for hit in self._fts_search("chunk_fts_cjk", as_phrase(seg), limit,
                                            "cjk", manual_snippet=q):
                    hits.setdefault(hit.chunk_id, hit)

        return sorted(hits.values(), key=lambda h: h.score)[:limit]

    def _fts_search(self, table: str, match: str, limit: int, channel: str,
                    manual_snippet: str = "") -> list[SearchHit]:
        if manual_snippet:
            sql = f"""SELECT c.chunk_id, c.doc_id, c.ordinal, c.heading_path, c.page,
                             c.text, c.chunk_hash, c.occurrence,
                             d.title, d.source_ref, d.source_kind,
                             bm25({table}) AS score
                      FROM {table} f
                      JOIN chunk c ON c.chunk_id = f.chunk_id
                      JOIN doc   d ON d.doc_id   = c.doc_id
                      WHERE {table} MATCH ?
                      ORDER BY score LIMIT ?"""
            rows = self.conn.execute(sql, (match, limit)).fetchall()
            return [self._hit(r, channel, manual_snippet=manual_snippet) for r in rows]

        sql = f"""SELECT c.chunk_id, c.doc_id, c.ordinal, c.heading_path, c.page,
                         c.text, c.chunk_hash, c.occurrence,
                         d.title, d.source_ref, d.source_kind,
                         snippet({table}, 0, ?, ?, '…', 24) AS snip,
                         bm25({table}) AS score
                  FROM {table} f
                  JOIN chunk c ON c.chunk_id = f.chunk_id
                  JOIN doc   d ON d.doc_id   = c.doc_id
                  WHERE {table} MATCH ?
                  ORDER BY score LIMIT ?"""
        rows = self.conn.execute(
            sql, (SNIPPET_MARK_OPEN, SNIPPET_MARK_CLOSE, match, limit)).fetchall()
        return [self._hit(r, channel) for r in rows]

    def search_any(self, terms: Sequence[str], limit: int = 60) -> list[SearchHit]:
        """**OR 检索**：任意一个 term 命中即召回。模糊匹配的召回层。

        短语匹配要求「原样相邻」，一个错别字就全盘落空；这里把查询拆成探针词后
        用 OR 放宽召回，精度交给上层的重叠度重排把关。

        `terms` 会被逐字面引用（`as_phrase`），所以用户输入永远不会被当成查询语法。
        """
        cleaned: list[str] = []
        seen: set[str] = set()
        for t in terms or ():
            v = (t or "").strip()
            if v and v not in seen:
                seen.add(v)
                cleaned.append(v)
        if not cleaned:
            return []

        match = " OR ".join(as_phrase(t) for t in cleaned)
        hits: dict[str, SearchHit] = {}
        for hit in self._fts_search("chunk_fts_cjk", match, limit, "fuzzy-cjk",
                                    manual_snippet=cleaned[0]):
            hits.setdefault(hit.chunk_id, hit)
        if len(hits) < limit:
            for hit in self._fts_search("chunk_fts", match, limit, "fuzzy-tri",
                                        manual_snippet=cleaned[0]):
                hits.setdefault(hit.chunk_id, hit)
        return list(hits.values())

    @staticmethod
    def _hit(row: sqlite3.Row, channel: str, manual_snippet: str = "") -> SearchHit:
        if manual_snippet:
            snip = _window_snippet(row["text"], manual_snippet, SNIPPET_CJK_WIDTH)
        else:
            snip = row["snip"] if "snip" in row.keys() else ""
        keys = row.keys()
        return SearchHit(
            chunk_id=row["chunk_id"],
            doc_id=row["doc_id"],
            ordinal=row["ordinal"],
            title=row["title"] or "",
            source_ref=row["source_ref"],
            source_kind=row["source_kind"],
            heading_path=row["heading_path"] or "",
            page=row["page"],
            snippet=_present(snip),
            score=float(row["score"]) if row["score"] is not None else 0.0,
            channel=channel,
            chunk_hash=row["chunk_hash"] or "",
            occurrence=row["occurrence"] or 0,
            text=(row["text"] or "") if "text" in keys else "",
        )

    # -------------------------------------------------------------- integrity

    def integrity(self) -> dict:
        """Structural checks. An empty dict means "nothing wrong found"."""
        problems: dict[str, Any] = {}
        c = self.counts()
        if c["chunks"] != c["fts_trigram"]:
            problems["fts_trigram_mismatch"] = {"chunks": c["chunks"], "fts": c["fts_trigram"]}
        if c["chunks"] != c["fts_cjk"]:
            problems["fts_cjk_mismatch"] = {"chunks": c["chunks"], "fts": c["fts_cjk"]}

        def count(sql: str, *params) -> int:
            try:
                return self.conn.execute(sql, params).fetchone()[0]
            except sqlite3.OperationalError:
                return -1

        orphan = count("SELECT COUNT(*) FROM chunk "
                       "WHERE doc_id NOT IN (SELECT doc_id FROM doc)")
        if orphan:
            problems["orphan_chunks"] = orphan

        dangling = count("SELECT COUNT(*) FROM doc WHERE chunk_count > 0 AND doc_id "
                         "NOT IN (SELECT DISTINCT doc_id FROM chunk)")
        if dangling:
            problems["docs_without_chunks"] = dangling

        # A document claiming to be indexed but holding no chunks would be
        # invisible to search while looking healthy in the counts.
        empty_indexed = count(
            "SELECT COUNT(*) FROM doc WHERE status='indexed' AND chunk_count=0")
        if empty_indexed:
            problems["indexed_without_chunks"] = empty_indexed

        # Content hashes must never be compared when empty — that would make
        # every contentless document a duplicate of every other.
        empty_hash = count(
            "SELECT COUNT(*) FROM doc WHERE content_hash IS NOT NULL AND content_hash=''")
        if empty_hash:
            problems["empty_content_hash"] = empty_hash

        # Two ids are 48 bits; a collision would silently merge two documents.
        collisions = self.conn.execute(
            """SELECT chunk_id, COUNT(DISTINCT chunk_hash) AS n FROM chunk
               GROUP BY chunk_id HAVING n > 1""").fetchall()
        if collisions:
            problems["chunk_id_collision"] = len(collisions)

        missing_versions = count(
            "SELECT COUNT(*) FROM doc WHERE norm_version='' OR chunker_version=''")
        if missing_versions:
            problems["docs_without_version_stamps"] = missing_versions

        dangling_dup = count(
            "SELECT COUNT(*) FROM doc WHERE duplicate_of IS NOT NULL AND duplicate_of "
            "NOT IN (SELECT doc_id FROM doc)")
        if dangling_dup:
            problems["dangling_duplicate_of"] = dangling_dup

        return problems

    def vacuum_into(self, target: str) -> str:
        os.makedirs(os.path.dirname(os.path.abspath(target)) or ".", exist_ok=True)
        self.conn.execute("VACUUM INTO ?", (target,))
        return target


def _window_snippet(text: str, query: str, width: int) -> str:
    """Manual snippet for the per-character index, whose stored text is
    space-separated and therefore unreadable if fed to `snippet()`."""
    if not text:
        return ""
    probe = "".join(ch for ch in query if not ch.isspace())
    pos = text.find(probe)
    if pos < 0:
        return text[:width] + ("…" if len(text) > width else "")
    start = max(0, pos - 30)
    end = min(len(text), pos + len(probe) + width)
    head = "…" if start > 0 else ""
    tail = "…" if end < len(text) else ""
    return (f"{head}{text[start:pos]}{SNIPPET_MARK_OPEN}{text[pos:pos + len(probe)]}"
            f"{SNIPPET_MARK_CLOSE}{text[pos + len(probe):end]}{tail}")


def open_store(db_path: str, create: bool = True) -> Store:
    return Store.open(db_path, create=create)
