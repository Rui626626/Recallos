"""`recall.py init` — create the minimal layout, never overwrite anything.

Idempotent and additive by construction:

* a directory that already exists is left untouched and reported as `exists`;
* a configuration file that already exists is **never** rewritten, because it
  is the one file a user is expected to edit by hand;
* nothing outside the project root is ever touched.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from ..config import CONFIG_NAME, load_config, project_root

__all__ = ["register", "run", "REQUIRED_DIRS"]

# Relative to the project root. `tests/` ships with the repository, so it is
# listed for completeness rather than created empty.
REQUIRED_DIRS = (
    "data",
    "data/docs",
    # 长期记忆与汇编成品在首次 `init` 时就建好，这样第一次 `memory remember`
    # 或 `compose` 不会因为「目录不存在」而给出一个与用法无关的报错。
    "data/memory",
    "index",
    "state",
    "cache",
    "artifacts",
    "tests",
)

DEFAULT_CONFIG = {
    "version": 1,
    "roots": [],
    "extensions": [
        ".md", ".markdown", ".txt", ".text",
        ".docx", ".pptx", ".xlsx",
        ".pdf", ".html", ".htm",
    ],
    "excludes": [
        "**/~$*", "**/*.tmp", "**/.git/**", "**/node_modules/**",
        "**/__pycache__/**", "**/.venv/**", "**/venv/**",
    ],
    "chunk": {"target_chars": 900, "max_chars": 1600},
    "search": {
        "default_limit": 20,
        "max_chunks_per_doc": 3,
        "token_budget": 2500,
        "snippet_chars": 240,
    },
    "paths": {},
}


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("init", help="create the project layout (never overwrites)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=run)


def ensure_layout(root: str) -> dict:
    """Create the standard directories. Returns created / existing lists.

    Kept importable so a future host can call it directly, and so tests can
    assert on the real filesystem rather than on printed text.
    """
    created: list[str] = []
    existing: list[str] = []
    for rel in REQUIRED_DIRS:
        target = os.path.join(root, *rel.split("/"))
        if os.path.isdir(target):
            existing.append(rel)
            continue
        os.makedirs(target, exist_ok=True)
        created.append(rel)
    return {"created": created, "existing": existing}


def run(args: argparse.Namespace) -> int:
    root = project_root()
    result = ensure_layout(root)

    config_path = os.path.join(root, CONFIG_NAME)
    config_state = "exists"
    if not os.path.exists(config_path):
        with open(config_path, "w", encoding="utf-8") as fh:
            json.dump(DEFAULT_CONFIG, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        config_state = "created"

    # Load it back through the normal path so a malformed file is caught here
    # rather than at the next command.
    try:
        load_config(root)
        config_ok = True
        config_error = ""
    except (OSError, json.JSONDecodeError) as exc:
        config_ok = False
        config_error = str(exc)

    if args.json:
        print(json.dumps(
            {"root": root, "dirs": result, "config": config_state,
             "config_ok": config_ok, "config_error": config_error},
            ensure_ascii=False, indent=2))
        return 0 if config_ok else 1

    print(f"RecallOS init — {root}")
    for rel in result["created"]:
        print(f"  created  {rel}")
    for rel in result["existing"]:
        print(f"  exists   {rel}")
    if config_state == "created":
        print(f"  created  {CONFIG_NAME}")
    else:
        print(f"  exists   {CONFIG_NAME} (left untouched)")
    if not config_ok:
        print(f"  ! {CONFIG_NAME} is not readable: {config_error}")
        return 1
    # 提示里给绝对路径：`init` 可以在任意 cwd 里跑，相对路径抄过去就是错的。
    from .install_skill import entry_path
    print(f'\nnext: "{sys.executable}" "{entry_path()}" doctor')
    return 0
