"""`recall.py alias` — 维护「你平时怎么叫 → 文档里怎么写」的映射表。

为什么需要它：错别字和少字多字靠重叠度能救回来，但**跨语言指代救不回来**。
文档标题是 `the Phillips curve`、正文里一个汉字都没有，那么搜「菲利普斯」在统计上
**无解**——没有任何可以匹配的东西。这类只能靠一张映射表，没有别的办法。

表分两段，职责分开：

* `aliases`  用户手写。`alias add` 写这里。
* `learned`  从文档正文的 `中文（English）` 成对写法自动学来。`alias learn` 写这里。

分开的原因：`learn` 可以反复跑（幂等），但**绝不能覆盖用户手写的内容**。
"""

from __future__ import annotations

import argparse
import json
import sys

from ..config import ensure_dirs, load_config
from ..core import fuzzy
from ..core.store import Store

__all__ = ["register", "run"]


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("alias", help="manage the 中文/English alias map")
    sub2 = p.add_subparsers(dest="alias_command", metavar="<action>")

    sp = sub2.add_parser("list", help="show the effective alias map")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=run_list)

    sp = sub2.add_parser("add", help="add one 叫法 → 写法 mapping")
    sp.add_argument("key", help="how you say it (中文或英文都行)")
    sp.add_argument("values", nargs="+", help="how the documents write it")
    sp.set_defaults(func=run_add)

    sp = sub2.add_parser("rm", help="remove a key from the hand-written map")
    sp.add_argument("key")
    sp.set_defaults(func=run_rm)

    sp = sub2.add_parser("learn", help="learn 中文（English） pairs from indexed text")
    sp.add_argument("--dry-run", action="store_true", help="report, change nothing")
    sp.set_defaults(func=run_learn)

    sp = sub2.add_parser("where", help="print the alias file path")
    sp.set_defaults(func=run_where)

    p.set_defaults(func=lambda a: (p.print_help(), 0)[1])


# --------------------------------------------------------------------- actions


def _load():
    cfg = load_config()
    ensure_dirs(cfg)
    doc = fuzzy.load_aliases(cfg)
    if not doc.get("_merged"):
        fuzzy.rebuild_merged(doc)
    return cfg, doc


def run_where(args: argparse.Namespace) -> int:
    cfg, _ = _load()
    print(cfg.aliases_path)
    return 0


def run_list(args: argparse.Namespace) -> int:
    cfg, doc = _load()
    hand = doc.get("aliases") or {}
    learned = doc.get("learned") or {}
    merged = doc.get("_merged") or {}

    if args.json:
        print(json.dumps({"path": cfg.aliases_path, "aliases": hand,
                          "learned": learned, "effective": merged},
                         ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    print(f"alias file: {cfg.aliases_path}")
    print(f"  手写 {len(hand)} 条 / 学到 {len(learned)} 条 / 种子 "
          f"{len(fuzzy.SEED_ALIASES)} 条 → 生效 {len(merged)} 条")
    if not merged:
        print("  （空）")
        return 0
    for key in sorted(merged):
        print(f"  {key:<14} → {', '.join(merged[key])}")
    if not hand:
        print("\n提示：`recall.py alias add 菲利普斯 phillips` 把常见叫法加进来。")
    return 0


def run_add(args: argparse.Namespace) -> int:
    cfg, doc = _load()
    key = args.key.strip()
    if not key:
        print("key 不能为空", file=sys.stderr)
        return 2
    values = [v.strip() for v in args.values if v.strip()]
    if not values:
        print("至少要给一个写法", file=sys.stderr)
        return 2

    hand = doc.setdefault("aliases", {})
    bucket = hand.setdefault(key, [])
    have = {fuzzy.alias_key(x) for x in bucket}
    added = []
    for v in values:
        if fuzzy.alias_key(v) not in have:
            bucket.append(v)
            have.add(fuzzy.alias_key(v))
            added.append(v)
    path = fuzzy.save_aliases(cfg, doc)
    print(f"alias add: {key} += {', '.join(added) if added else '（已存在，无变化）'}")
    print(f"  写入 {path}")
    return 0


def run_rm(args: argparse.Namespace) -> int:
    cfg, doc = _load()
    key = args.key.strip()
    removed = []
    for section in ("aliases", "learned"):
        bucket = doc.get(section) or {}
        for k in list(bucket):
            if fuzzy.alias_key(k) == fuzzy.alias_key(key):
                bucket.pop(k)
                removed.append(f"{section}:{k}")
    if not removed:
        print(f"没找到别名键：{key}", file=sys.stderr)
        return 1
    path = fuzzy.save_aliases(cfg, doc)
    print("alias rm: 移除 " + ", ".join(removed))
    print(f"  写入 {path}")
    return 0


def run_learn(args: argparse.Namespace) -> int:
    cfg, doc = _load()
    if not __import__("os").path.exists(cfg.db_path):
        print("索引还不存在，先跑 `recall.py add <路径>`", file=sys.stderr)
        return 2

    store = Store.open(cfg.db_path)
    try:
        pairs: dict[str, list[str]] = {}
        for row in store.conn.execute("SELECT text FROM chunk"):
            for zh, values in fuzzy.learn_pairs(row["text"] or "").items():
                bucket = pairs.setdefault(zh, [])
                have = {fuzzy.alias_key(x) for x in bucket}
                for v in values:
                    if fuzzy.alias_key(v) not in have:
                        bucket.append(v)
                        have.add(fuzzy.alias_key(v))
    finally:
        store.close()

    total = sum(len(v) for v in pairs.values())
    print(f"alias learn: 从正文里找到 {total} 组「中文（English）」对照，"
          f"涉及 {len(pairs)} 个中文词")
    for zh in sorted(pairs)[:20]:
        print(f"  {zh:<16} → {', '.join(pairs[zh])}")
    if len(pairs) > 20:
        print(f"  ... 其余 {len(pairs) - 20} 个见 --dry-run 之外的 JSON")

    if args.dry_run:
        print("  （--dry-run，未写入）")
        return 0

    added = fuzzy.merge_learned(doc, pairs)
    path = fuzzy.save_aliases(cfg, doc)
    print(f"  新增 {added} 条 → {path}")
    if added == 0:
        print("  （没有新东西。成对写法要求中英紧挨在同一个括号里，语料里可能确实没有。）")
    return 0


def run(args: argparse.Namespace) -> int:  # pragma: no cover - 由子命令分发
    return int(getattr(args, "func", lambda a: 0)(args))
