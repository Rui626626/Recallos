#!/usr/bin/env python3
"""recall.py — the single entry point for RecallOS.

Everything the system can do is reachable from here, so any host (DSH, a
scheduled task, a WPS skill, a person in a terminal) only needs to know how to
run one command. No daemon, no service, no background state.

Current stage: **P2 — retrieval.**

    python recallos/bin/recall.py init                     # create the layout
    python recallos/bin/recall.py doctor                   # verify it
    python recallos/bin/recall.py add <path...>            # import
    python recallos/bin/recall.py index                    # index statistics
    python recallos/bin/recall.py index --rebuild          # rebuild from archives
    python recallos/bin/recall.py search "<query>"         # retrieve (exact+alias+fuzzy)
    python recallos/bin/recall.py alias list|add|learn     # 中文/English alias map

`search` returns a budgeted, citable context: every hit carries a `locator` that can be
resolved back to the exact chunk. `add` only ever reads your files.
"""

from __future__ import annotations

import argparse
import os
import sys

# Make `import brain...` work no matter which directory the caller is in.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from brain.cli import add as add_cli        # noqa: E402
from brain.cli import alias as alias_cli    # noqa: E402
from brain.cli import archive as archive_cli  # noqa: E402
from brain.cli import compose as compose_cli  # noqa: E402
from brain.cli import doctor as doctor_cli  # noqa: E402
from brain.cli import index as index_cli    # noqa: E402
from brain.cli import init as init_cli      # noqa: E402
from brain.cli import install_skill as install_skill_cli  # noqa: E402
from brain.cli import memory as memory_cli  # noqa: E402
from brain.cli import scope as scope_cli    # noqa: E402
from brain.cli import search as search_cli  # noqa: E402
from brain.cli import verify as verify_cli  # noqa: E402

__version__ = "0.7.2"

_COMMANDS = (init_cli, doctor_cli, scope_cli, add_cli, index_cli, search_cli,
             alias_cli, archive_cli, compose_cli, verify_cli, memory_cli,
             install_skill_cli)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="recall.py",
        description="RecallOS — a personal, local-first memory and material index.",
    )
    parser.add_argument("--version", action="version", version=f"recallos {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    for module in _COMMANDS:
        module.register(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        sys.stderr.write("\ninterrupted\n")
        raise SystemExit(130)
