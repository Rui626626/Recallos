"""`recall.py verify` — 检查一份文件里的每个出处还能不能回查到。

存在的理由：**出处只有在能被验证时才叫出处。** 一份成品写完、索引重建过、原文件
改过之后，里面的 locator 可能已经指向不存在的切片。这个命令让「不可核查」变成一条
能跑出来的结论，而不是一句承诺。

它读的是文件里那段机器可读清单（`<!-- recallos:locators ... -->`），没有清单时
退回全文扫描。任何 markdown 都能验——不只是 `compose` 生成的。

退出码：0 全部可解析；1 有对不上的；2 用法错误。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from ..config import load_config
from ..core.locator import extract_locators, verify_locators
from ..core.store import Store

__all__ = ["register", "run"]


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("verify", help="check that every locator in a file still resolves")
    p.add_argument("paths", nargs="+", help="markdown files that carry locators")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    cfg = load_config()
    if not os.path.exists(cfg.db_path):
        print("索引不存在，无法回查。先跑 `recall.py add <路径>` 或 `recall.py index "
              "--rebuild`", file=sys.stderr)
        return 2

    results: list[dict] = []
    all_ok = True
    store = Store.open(cfg.db_path, create=False)
    try:
        for path in args.paths:
            if not os.path.isfile(path):
                results.append({"path": path, "ok": False, "error": "文件不存在",
                                "checked": 0, "resolved": 0,
                                "broken": [], "missing_doc": []})
                all_ok = False
                continue
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            locators = extract_locators(text)
            if not locators:
                results.append({"path": path, "ok": False,
                                "error": "文件里没有找到任何 locator",
                                "checked": 0, "resolved": 0,
                                "broken": [], "missing_doc": []})
                all_ok = False
                continue
            outcome = verify_locators(store, locators)
            outcome["path"] = path
            results.append(outcome)
            all_ok = all_ok and outcome["ok"]
    finally:
        store.close()

    if args.json:
        print(json.dumps({"ok": all_ok, "files": results},
                         ensure_ascii=False, indent=2))
        return 0 if all_ok else 1

    for r in results:
        name = os.path.basename(r.get("path", "?"))
        if r.get("error"):
            print(f"  [FAIL] {name}: {r['error']}")
            continue
        mark = "ok  " if r["ok"] else "FAIL"
        print(f"  [{mark}] {name}: {r['resolved']}/{r['checked']} 条出处可回查")
        for b in r["broken"][:5]:
            print(f"         ! {b.get('locator')}: {b.get('why')}")
        for b in r["missing_doc"][:5]:
            print(f"         ! {b.get('locator')}: {b.get('why')}")
    return 0 if all_ok else 1
