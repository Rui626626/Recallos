"""`recall.py add` — import files and directories."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

from ..config import Config, ensure_dirs, load_config
from ..core.store import Store
from ..pipeline import ingest_paths
from ..state import Journal, SourceRegistry

__all__ = ["register", "run"]


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("add", help="import files or directories")
    p.add_argument("paths", nargs="*",
                   help="files or directories; omit to use the configured roots")
    p.add_argument("--force", action="store_true",
                   help="re-parse even when nothing appears to have changed")
    p.add_argument("--dry-run", action="store_true",
                   help="report what would happen without writing anything")
    p.add_argument("--prune", action="store_true",
                   help="also forget sources whose file has disappeared "
                        "(by default they are only marked missing)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=run)


def _progress(i: int, total: int, path: str) -> None:
    if sys.stderr.isatty():
        sys.stderr.write(f"\r  [{i}/{total}] {os.path.basename(path)[:60]:<60}")
        sys.stderr.flush()


def run(args: argparse.Namespace) -> int:
    cfg: Config = load_config()
    ensure_dirs(cfg)

    targets = list(args.paths) or cfg.enabled_roots()
    if not targets:
        print("nothing to import: no paths given and no roots configured in "
              "brain.config.json", file=sys.stderr)
        return 2

    if args.paths:
        missing = [t for t in targets if not os.path.exists(t)]
        if missing:
            print("path(s) not found: " + ", ".join(missing), file=sys.stderr)
            targets = [t for t in targets if os.path.exists(t)]
            if not targets:
                return 2
    else:
        missing = [t for t in targets if not os.path.exists(t)]
        if missing:
            print("configured root(s) missing: " + ", ".join(missing), file=sys.stderr)
            targets = [t for t in targets if os.path.exists(t)]
            if not targets:
                return 2

    store = Store.open(cfg.db_path)
    registry = SourceRegistry(cfg.sources_path)
    journal = Journal(cfg.journal_path)

    try:
        summary = ingest_paths(
            store, registry, journal, cfg, targets,
            force=args.force, dry_run=args.dry_run, prune=args.prune,
            progress=None if (args.quiet or args.json) else _progress)
    except sqlite3.Error as exc:
        # A locked or unwritable index is an operational condition, not a bug:
        # report it plainly instead of showing a traceback.
        print(f"import aborted: the index database could not be written: {exc}",
              file=sys.stderr)
        print("nothing was changed in the index; the authoritative files under "
              "data/ are unaffected.", file=sys.stderr)
        return 1
    finally:
        if not (args.quiet or args.json) and sys.stderr.isatty():
            sys.stderr.write("\r" + " " * 80 + "\r")
        store.close()

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 1 if summary["failures"] else 0

    if not args.quiet:
        by = summary["by_status"]
        print(f"scanned {summary['scanned']} file(s)"
              + ("  [dry run]" if args.dry_run else ""))
        for key in ("new", "moved", "reindexed", "updated", "metadata_only",
                    "unchanged", "skipped", "empty", "duplicate", "failed",
                    "unsupported"):
            if by.get(key):
                print(f"  {key:<14} {by[key]}")
        print(f"  chunks written  {summary['chunks_written']}")
        if summary.get("dedup", {}).get("duplicates"):
            print(f"  duplicate copies  {summary['dedup']['duplicates']}")
        for mv in summary.get("moves", [])[:10]:
            print(f"  ~ moved: {mv['path']}")
        for failure in summary["failures"][:20]:
            print(f"  ! {os.path.basename(failure['path'])}: "
                  f"{failure.get('error', '')[:110]}")
        if len(summary["failures"]) > 20:
            print(f"  ... and {len(summary['failures']) - 20} more failures")

    return 0 if not summary["failures"] else 1
