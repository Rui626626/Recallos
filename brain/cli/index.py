"""`recall.py index` — inspect the index, or rebuild it.

Rebuilds come in two forms, and the default is the one that does *not* touch the
original files:

* `--rebuild`               from the archives in `data/` plus `cache/`
* `--rebuild --from-source` from the original files (used when an extractor
                            changed, or when a cache entry is missing)

Both build into a temporary database and swap it in atomically, so the existing
index stays queryable for the whole run and a failure leaves it untouched.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys

from ..config import ensure_dirs, load_config
from ..core import schema as schema_mod
from ..core.schema import META_LAST_RUN_STATE
from ..core.store import Store
from ..pipeline import rebuild_from_archive, rebuild_from_source
from ..state import Journal, SourceRegistry

__all__ = ["register", "run"]


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("index", help="inspect or rebuild the search index")
    p.add_argument("--rebuild", action="store_true",
                   help="recreate the index from data/ and cache/")
    p.add_argument("--from-source", action="store_true",
                   help="rebuild by re-reading the original files instead")
    p.add_argument("--stats", action="store_true", help="show index statistics")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=run)


def _stats(cfg, as_json: bool) -> int:
    store = Store.open(cfg.db_path)
    try:
        counts = store.counts()
        statuses = store.status_counts()
        problems = store.integrity()
        journal = Journal(cfg.journal_path)
        registry = SourceRegistry(cfg.sources_path)
        payload = {
            "db": cfg.db_path,
            "schema_version": schema_mod.read_version(store.conn),
            "counts": counts,
            "statuses": statuses,
            "integrity": problems,
            "journal": journal.counts(),
            "tracked_sources": len(registry),
            "missing_sources": len(registry.missing()),
            "last_run_state": store.get_meta(META_LAST_RUN_STATE),
        }
    finally:
        store.close()

    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(f"index: {payload['db']}")
    print(f"  schema v{payload['schema_version']}")
    print(f"  docs={counts['docs']}  chunks={counts['chunks']}")
    if statuses:
        print("  status: " + ", ".join(f"{k}={v}" for k, v in sorted(statuses.items())))
    print(f"  tracked sources: {payload['tracked_sources']}"
          + (f" ({payload['missing_sources']} missing)"
             if payload["missing_sources"] else ""))
    if journal.counts():
        print("  journal: " + ", ".join(f"{k}={v}"
                                        for k, v in sorted(journal.counts().items())))
    if payload["last_run_state"] and payload["last_run_state"] != "complete":
        print(f"  ! last run ended as: {payload['last_run_state']}")
    if problems:
        print(f"  integrity: {json.dumps(problems, ensure_ascii=False)}")
    else:
        print("  integrity: ok")
    return 0


def _rebuild(cfg, from_source: bool, as_json: bool) -> int:
    registry = SourceRegistry(cfg.sources_path)
    journal = Journal(cfg.journal_path)

    if not from_source and len(registry) == 0:
        print("nothing to rebuild: state/sources.json is empty.\n"
              "Run `recall.py add <path>` first — a rebuild replays what `add` "
              "has already registered.", file=sys.stderr)
        return 2

    temp_dir = cfg.index_dir
    os.makedirs(temp_dir, exist_ok=True)
    temp_db = os.path.join(temp_dir, f".rebuild-{os.getpid()}.db")

    # The live database is left completely alone until the new one is verified.
    store = Store.open(temp_db)
    try:
        store.set_meta(META_LAST_RUN_STATE, "running")
        if from_source:
            summary = rebuild_from_source(store, registry, journal, cfg)
        else:
            summary = rebuild_from_archive(store, cfg)
            need = summary.get("need_source") or []
            if need:
                summary["note"] = (
                    f"{len(need)} document(s) had no cached text; re-run with "
                    f"--from-source to recover them from the original files")
        store.set_meta(META_LAST_RUN_STATE, "complete")
        store.conn.commit()
        problems = store.integrity()
    except BaseException:
        store.close()
        _discard(temp_db)
        raise

    store.close()

    if problems:
        _discard(temp_db)
        print(f"rebuild aborted: the new index failed its integrity check: "
              f"{json.dumps(problems, ensure_ascii=False)}", file=sys.stderr)
        return 1

    _swap_in(cfg, temp_db)

    if as_json:
        print(json.dumps({**summary, "integrity": problems}, ensure_ascii=False,
                         indent=2))
        return 0

    print(f"rebuilt from {'the original files' if from_source else 'archives'}")
    if "restored" in summary:
        print(f"  archives: {summary['archives']}  restored: {summary['restored']}")
    by = summary.get("by_status") or {}
    for key in ("new", "moved", "reindexed", "updated", "unchanged", "empty",
                "duplicate", "failed"):
        if by.get(key):
            print(f"  {key:<14} {by[key]}")
    if summary.get("missing"):
        print(f"  missing        {len(summary['missing'])} (marked, not forgotten)")
    if summary.get("note"):
        print(f"  note: {summary['note']}")
    print("  integrity: ok")
    return 0


def _swap_in(cfg, temp_db: str) -> None:
    """Atomically replace the live database with the freshly built one."""
    final = cfg.db_path
    for suffix in ("", "-wal", "-shm"):
        stale = final + suffix
        if os.path.exists(stale):
            try:
                os.unlink(stale)
            except OSError:
                pass
    os.replace(temp_db, final)
    _discard(temp_db)


def _discard(temp_db: str) -> None:
    for suffix in ("", "-wal", "-shm"):
        path = temp_db + suffix
        if os.path.exists(path):
            try:
                os.unlink(path)
            except OSError:
                pass


def run(args: argparse.Namespace) -> int:
    cfg = load_config()
    ensure_dirs(cfg)

    if args.from_source and not args.rebuild:
        print("--from-source only applies together with --rebuild", file=sys.stderr)
        return 2

    if args.rebuild:
        return _rebuild(cfg, args.from_source, args.json)

    return _stats(cfg, args.json)
