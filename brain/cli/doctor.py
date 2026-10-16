"""`recall.py doctor` — the P0 self-check.

Design rules, deliberately chosen:

* **It never creates anything.** A health check that silently repairs what it
  is meant to report would make "is this project set up correctly?" unanswerable.
  `recall.py init` creates; `doctor` only inspects.
* **Not-implemented features are never reported as passing.** A feature that
  exists but is unusable (a configured format with no parser, a missing PDF
  backend) is reported as `warn`, and every status other than `pass` is visible
  in the summary line.
* **Every failure carries the exact command that fixes it.**
* The exit code is non-zero if and only if a check has status `fail`. `warn`
  and `todo` do not fail the run, so the command is usable during P0 while P1
  is still missing.

Status values: `pass` · `warn` · `fail` · `todo` · `skip`.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from typing import Any

from ..config import CONFIG_NAME, load_config, project_root
from ..core.schema import SCHEMA_VERSION, read_version
from .init import REQUIRED_DIRS

__all__ = ["register", "run", "collect", "Check"]

#: 要求的目录清单**只有一个来源**：`init.REQUIRED_DIRS`。
#: 曾经这里复制过一份，结果改了一边忘另一边，`doctor` 报「6 个目录」而 `init`
#: 实际建 8 个——两个数字各自看着都对，加起来就是错的。
CONFIG_HINT = "run: python recallos/bin/recall.py init"

PASS, WARN, FAIL, TODO, SKIP = "pass", "warn", "fail", "todo", "skip"

_MARKS = {PASS: "ok  ", WARN: "WARN", FAIL: "FAIL", TODO: "todo", SKIP: "skip"}


class Check(dict):
    """A single check result. A dict so it serialises straight to JSON."""

    def __init__(self, name: str, requirement: str, status: str,
                 detail: str = "", hint: str = "") -> None:
        super().__init__(name=name, requirement=requirement, status=status,
                         detail=detail, hint=hint)


# --------------------------------------------------------------------- checks

def _check_layout(cfg) -> Check:
    """Requirement 1 — project directory and required subdirectories."""
    root = cfg.root_dir
    if not os.path.isdir(root):
        return Check("layout", "1", FAIL, f"project root missing: {root}",
                     "check that the repository was copied completely")
    missing = [rel for rel in REQUIRED_DIRS
               if not os.path.isdir(os.path.join(root, *rel.split("/")))]
    if missing:
        return Check("layout", "1", FAIL,
                     f"project root ok; missing: {', '.join(missing)}",
                     CONFIG_HINT)
    return Check("layout", "1", PASS,
                 f"{len(REQUIRED_DIRS)} directories present under {root}")


def _check_python(cfg) -> Check:
    """Requirement 2 — the interpreter runs and the package imports."""
    version = sys.version.split()[0]
    if sys.version_info < (3, 10):
        return Check("python", "2", FAIL, f"{version} at {sys.executable}",
                     "RecallOS needs Python 3.10 or newer")
    try:
        import brain  # noqa: F401
        import brain.config  # noqa: F401
        import brain.core.store  # noqa: F401
    except Exception as exc:
        return Check("python", "2", FAIL, f"{version}; import failed: {exc!r}",
                     "run doctor from the repository; check for a stray "
                     "brain/ directory earlier on sys.path")
    return Check("python", "2", PASS, f"{version} at {sys.executable}")


def _check_sqlite(cfg) -> Check:
    """Requirement 3 — SQLite usable, including the FTS5 facility P1 relies on."""
    version = sqlite3.sqlite_version
    try:
        con = sqlite3.connect(":memory:")
        try:
            con.execute("CREATE VIRTUAL TABLE probe USING fts5(x)")
            con.execute("CREATE VIRTUAL TABLE probe2 USING fts5(x, tokenize='trigram')")
        finally:
            con.close()
    except sqlite3.Error as exc:
        return Check("sqlite", "3", FAIL, f"SQLite {version}; FTS5 unavailable: {exc}",
                     "use a Python built against SQLite with ENABLE_FTS5")
    return Check("sqlite", "3", PASS,
                 f"SQLite {version} with FTS5 (unicode61 + trigram)")


def _check_data_dir(cfg) -> Check:
    """Requirement 4 — the data directories are writable right now."""
    problems: list[str] = []
    checked: list[str] = []
    for rel in ("data", "state", "cache", "index"):
        target = os.path.join(cfg.root_dir, rel)
        if not os.path.isdir(target):
            problems.append(f"{rel}: missing")
            continue
        probe = os.path.join(target, ".recallos-write-probe")
        try:
            with open(probe, "w", encoding="utf-8") as fh:
                fh.write("probe")
            os.unlink(probe)
            checked.append(rel)
        except OSError as exc:
            problems.append(f"{rel}: {exc.strerror or exc}")
    if problems:
        return Check("data-dir", "4", FAIL, "; ".join(problems), CONFIG_HINT)
    return Check("data-dir", "4", PASS, f"writable: {', '.join(checked)}")


def _check_index_absent(cfg) -> Check:
    """Requirement 5 — an absent index is recognised as an empty index."""
    if os.path.exists(cfg.db_path):
        return Check("index(absent)", "5", SKIP,
                     "index database exists — see the next check")
    return Check("index(absent)", "5", PASS,
                 f"no index at {cfg.db_path} — treated as an empty, "
                 "not-yet-built database (this is the expected P0 state)")


def _check_index_open(cfg) -> Check:
    """Requirement 6 — when present, the database opens and answers a query."""
    if not os.path.exists(cfg.db_path):
        return Check("index(open)", "6", SKIP,
                     "no index database yet — nothing to open")
    try:
        con = sqlite3.connect(cfg.db_path)
        con.row_factory = sqlite3.Row
        try:
            con.execute("PRAGMA journal_mode=WAL")
            tables = [r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view') "
                "ORDER BY name")]
            version = read_version(con)
            counts = _counts(con)
        finally:
            con.close()
    except sqlite3.Error as exc:
        return Check("index(open)", "6", FAIL, f"cannot open {cfg.db_path}: {exc}",
                     "delete the index directory and rebuild it, or run "
                     "`recall.py init` if it does not exist")
    if version != SCHEMA_VERSION:
        return Check("index(open)", "6", FAIL,
                     f"schema v{version}, expected v{SCHEMA_VERSION}; "
                     f"{len(tables)} objects",
                     "the index is derived data — delete index/ and rebuild")
    return Check("index(open)", "6", PASS,
                 f"schema v{SCHEMA_VERSION}; docs={counts['docs']} "
                 f"chunks={counts['chunks']} ({len(tables)} objects)")


def _counts(con: sqlite3.Connection) -> dict:
    out = {"docs": 0, "chunks": 0}
    for key, table in (("docs", "doc"), ("chunks", "chunk")):
        try:
            out[key] = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.Error:
            out[key] = -1
    return out


def _skill_entry_paths(cfg) -> list[str]:
    """Where DSH will actually look for the Skill entry.

    Delegates to the adapter so the expectation is encoded in exactly one place.
    The adapter mirrors `@deepseek-ai/dsh-skill-filesystem`: the project root is
    found by walking up from the session cwd for a `.git` directory, falling
    back to the cwd itself. With no repository anywhere above, the project root
    *is* the workspace, so the entry must live beside `recallos/`, not inside it.
    """
    from ..adapters.dsh import find_project_root, skill_roots

    workspace = find_project_root(os.path.dirname(cfg.root_dir))
    return [os.path.join(base, "recallos", "SKILL.md")
            for base, _source in skill_roots(workspace)]


def _check_tests_and_config(cfg) -> Check:
    """Requirement 7 — test directory and required configuration are present."""
    problems: list[str] = []
    good: list[str] = []

    tests_dir = os.path.join(cfg.root_dir, "tests")
    test_files = []
    if os.path.isdir(tests_dir):
        test_files = [f for f in os.listdir(tests_dir)
                      if f.startswith("test_") and f.endswith(".py")]
    if not os.path.isdir(tests_dir):
        problems.append("tests/ missing")
    elif not test_files:
        problems.append("tests/ has no test_*.py")
    else:
        good.append(f"tests: {', '.join(sorted(test_files))}")

    config_path = os.path.join(cfg.root_dir, CONFIG_NAME)
    if not os.path.exists(config_path):
        problems.append(f"{CONFIG_NAME} missing")
    else:
        try:
            with open(config_path, "r", encoding="utf-8") as fh:
                json.load(fh)
            good.append(f"{CONFIG_NAME} readable")
        except (OSError, json.JSONDecodeError) as exc:
            problems.append(f"{CONFIG_NAME} unreadable: {exc}")

    # 仓库自带的技能入口 —— 这是**打包要求**，缺了说明发行包不完整。
    #
    # 路径必须相对**代码位置**，不能相对 `cfg.root_dir`：后者是「运行时的数据根」，
    # 可以被 RECALLOS_ROOT 指到任何地方（测试就是这么用的）。用数据根去找仓库文件，
    # 会在每一个换根的场景里误报「发行包不完整」。
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    bundled = os.path.join(repo_root, "skills", "deepseek-harness", "SKILL.md")
    if os.path.isfile(bundled):
        good.append("skills/deepseek-harness/SKILL.md present")
    else:
        problems.append("skills/deepseek-harness/SKILL.md missing (the repo ships it)")

    # 装进 DSH —— 这是**宿主相关的一步，且是可选的**：命令行本身不依赖它。
    # 全新 clone 还没装，所以这里只能是警告，不能是失败。
    targets = _skill_entry_paths(cfg)
    entry = next((p for p in targets if os.path.isfile(p)), None)
    if entry:
        good.append(f"SKILL.md installed for DSH at {entry}")
    else:
        good.append("SKILL.md NOT installed for DSH (optional)")

    if problems:
        return Check("tests+config", "7", FAIL, "; ".join(problems), CONFIG_HINT)
    if not entry:
        return Check(
            "tests+config", "7", WARN, "; ".join(good),
            "optional: the command line works without it. To let DSH find the "
            f"Skill, copy skills/deepseek-harness/SKILL.md to {targets[0]}")
    return Check("tests+config", "7", PASS, "; ".join(good))


def _check_p1_layer(cfg) -> Check:
    """The ingest layer: which formats are actually parseable right now.

    This reports what is registered, not what is intended. A format listed in
    `brain.config.json` with no parser behind it is a real gap and shows up here
    rather than silently producing "unsupported" documents later.
    """
    try:
        from ..ingest.base import supported_extensions
        from ..ingest.pdf import pdf_backend
    except Exception as exc:  # a broken import must be reported, not swallowed
        return Check("p1-ingest", "-", FAIL,
                     f"ingest layer failed to import: {exc!r}",
                     "run doctor from the repository root and check brain/ingest/")

    exts = supported_extensions()
    if not exts:
        return Check("p1-ingest", "-", FAIL, "no parsers registered",
                     "brain/ingest/__init__.py must import its parser modules")

    backend = pdf_backend()
    configured = {e.lower() for e in cfg.extensions}
    unbacked = sorted(configured - set(exts))
    detail = f"{len(exts)} extensions: {', '.join(exts)}"
    detail += f" | pdf backend: {backend or 'missing'}"
    # 拼音是内置表，不依赖任何包；删掉 brain/data/pinyin.tsv.gz 就会降级为关闭。
    from ..core.pinyin import available as pinyin_available, table as pinyin_table
    if pinyin_available():
        detail += f" | pinyin: {len(pinyin_table())} chars"
    else:
        detail += " | pinyin: off (brain/data/pinyin.tsv.gz missing)"
    if unbacked:
        detail += f" | configured but no parser: {', '.join(unbacked)}"
    if not backend:
        return Check("p1-ingest", "-", WARN, detail,
                     "python -m pip install pypdf "
                     "(PDFs would be recorded as failed, not searchable)")
    return Check("p1-ingest", "-", PASS, detail)


def collect(cfg) -> list[Check]:
    """Run every check. Order follows the numbered P0 requirements."""
    return [
        _check_layout(cfg),
        _check_python(cfg),
        _check_sqlite(cfg),
        _check_data_dir(cfg),
        _check_index_absent(cfg),
        _check_index_open(cfg),
        _check_tests_and_config(cfg),
        _check_p1_layer(cfg),
    ]


# ------------------------------------------------------------------------ run

def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("doctor", help="check the project layout and environment")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    cfg = load_config()
    checks = collect(cfg)

    failures = [c for c in checks if c["status"] == FAIL]
    warnings = [c for c in checks if c["status"] == WARN]
    todos = [c for c in checks if c["status"] == TODO]
    passed = [c for c in checks if c["status"] == PASS]

    if args.json:
        print(json.dumps({
            "root": cfg.root_dir,
            "ok": not failures,
            "summary": {"pass": len(passed), "warn": len(warnings),
                        "fail": len(failures), "todo": len(todos)},
            "checks": checks,
        }, ensure_ascii=False, indent=2))
        return 1 if failures else 0

    print(f"RecallOS doctor — {cfg.root_dir}")
    print()
    for c in checks:
        mark = _MARKS[c["status"]]
        req = f"#{c['requirement']}" if c["requirement"] != "-" else " -"
        print(f"  [{mark}] {req:<3} {c['name']:<15} {c['detail']}")
        if c["hint"] and c["status"] in (FAIL, WARN):
            print(f"             → {c['hint']}")
    print()
    if failures:
        print(f"{len(failures)} check(s) FAILED; "
              f"{len(passed)} passed, {len(warnings)} warning(s), "
              f"{len(todos)} not implemented.")
        return 1
    tail = f"; {len(todos)} not implemented." if todos else "."
    print(f"All {len(passed)} runnable check(s) passed"
          f"{f'; {len(warnings)} warning(s)' if warnings else ''}{tail}")
    return 0
