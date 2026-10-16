"""`recall.py memory` — 长期记忆：用户画像、项目记忆、决策记录。

记忆是**权威数据**（`data/memory/`），不是索引的附属物。索引删掉、cache 清空，
记忆都必须还在——所以本模块只读写 `.md` 文件，索引只是让它可被搜到的一个投影。

    remember <文本>     记一件事；同一主题再记 → 只有一个文件，修订 +1
    list                列出所有主题（含已过期）
    show <主题>         看某个主题的当前结论与修订历史
    expire <主题>       标记过期：移入 _expired/，检索默认不再返回
    restore <主题>      取消过期
    export              导出成现有记忆插件能直接读的格式

关于「过期」：实现方式是**移动文件 + 改 frontmatter**，然后重新导入并 `--prune`。
之所以要 prune，是因为移动会留下一条指向旧路径的 `missing` 记录——不 prune 的话，
那条陈旧的行仍然会被检索命中，出现「明明标了过期却还搜得到」的假象。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from ..config import ensure_dirs, load_config
from ..core import memory as mem
from ..core.store import Store
from ..pipeline import ingest_paths
from ..state import Journal, SourceRegistry

__all__ = ["register", "run"]


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("memory", help="long-term memory: remember / list / expire / export")
    sub2 = p.add_subparsers(dest="memory_command", metavar="<action>")

    sp = sub2.add_parser("remember", help="记住一件事（同主题就地更新）")
    sp.add_argument("text")
    sp.add_argument("--topic", help="主题键；省略时由文本推导（同一句话 → 同一文件）")
    sp.add_argument("--title")
    sp.add_argument("--tag", action="append", default=[])
    sp.add_argument("--no-index", action="store_true", help="只写文件，不导入索引")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=run_remember)

    sp = sub2.add_parser("list", help="列出所有主题")
    sp.add_argument("--active-only", action="store_true")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=run_list)

    sp = sub2.add_parser("show", help="看一个主题的当前结论与修订历史")
    sp.add_argument("topic")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=run_show)

    for action, help_text in (("expire", "标记为过期（检索默认不再返回）"),
                              ("restore", "取消过期")):
        sp = sub2.add_parser(action, help=help_text)
        sp.add_argument("topic")
        sp.add_argument("--no-index", action="store_true")
        sp.add_argument("--json", action="store_true")
        sp.set_defaults(func=run_expire if action == "expire" else run_restore)

    sp = sub2.add_parser("export", help="导出成现有记忆插件能读的格式")
    sp.add_argument("--to", default=None,
                    help="目标目录（默认 <root>/data/memory/export）")
    sp.add_argument("--dsh", action="store_true",
                    help="直接导出到 DSH 记忆插件目录（会写进你的记忆库）")
    sp.add_argument("--scope", default="user", choices=["user", "project"])
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=run_export)

    sp = sub2.add_parser("where", help="打印记忆目录")
    sp.set_defaults(func=run_where)

    p.set_defaults(func=lambda a: (p.print_help(), 0)[1])


# --------------------------------------------------------------------- helpers


def _load():
    cfg = load_config()
    ensure_dirs(cfg)
    os.makedirs(cfg.memory_dir, exist_ok=True)
    return cfg


def _index_memory(cfg, paths: list[str], prune: bool = False) -> dict:
    store = Store.open(cfg.db_path)
    registry = SourceRegistry(cfg.sources_path)
    journal = Journal(cfg.journal_path)
    try:
        summary = ingest_paths(store, registry, journal, cfg, paths, force=False,
                               prune=prune)
    finally:
        store.close()
    return {"chunks_written": summary["chunks_written"],
            "by_status": summary["by_status"], "failures": summary["failures"]}


# --------------------------------------------------------------------- actions


def run_remember(args: argparse.Namespace) -> int:
    cfg = _load()
    try:
        outcome = mem.remember(cfg, args.text, topic=args.topic,
                               tags=args.tag, title=args.title)
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    entry: mem.MemoryEntry = outcome["entry"]
    indexed = None
    if not args.no_index:
        indexed = _index_memory(cfg, [outcome["path"]])

    if args.json:
        print(json.dumps({"topic": outcome["topic"], "path": outcome["path"],
                          "existed": outcome["existed"],
                          "reactivated_from": outcome["reactivated_from"],
                          "entry": entry.to_dict(), "indexed": indexed},
                         ensure_ascii=False, indent=2))
        return 0

    verb = "更新" if outcome["existed"] else "新建"
    print(f"memory remember: {verb}主题 `{entry.topic}`")
    print(f"  文件    {os.path.relpath(outcome['path'], cfg.root_dir)}")
    print(f"  修订    {entry.revisions} 版" +
          ("（旧结论留在同文件的修订历史里，没有删除）" if entry.revisions > 1 else ""))
    if outcome["reactivated_from"]:
        print("  注意    这个主题之前是过期状态，这次记录把它重新激活了")
    if indexed:
        print(f"  已入库  {indexed['chunks_written']} 个切片（可被 search 找到）")
    return 0


def run_list(args: argparse.Namespace) -> int:
    cfg = _load()
    entries = mem.list_entries(cfg)
    if args.active_only:
        entries = [e for e in entries if e["status"] == mem.STATUS_ACTIVE]
    if args.json:
        print(json.dumps({"memory_dir": cfg.memory_dir, "count": len(entries),
                          "entries": entries}, ensure_ascii=False, indent=2))
        return 0
    if not entries:
        print("还没有任何记忆。用 `recall.py memory remember \"...\"` 记一条。")
        return 0
    actives = [e for e in entries if e["status"] == mem.STATUS_ACTIVE]
    expired = [e for e in entries if e["status"] != mem.STATUS_ACTIVE]
    print(f"memory: {len(actives)} 条有效 / {len(expired)} 条已过期"
          f"　（{os.path.relpath(cfg.memory_dir, cfg.root_dir)}）")
    for e in entries:
        flag = "" if e["status"] == mem.STATUS_ACTIVE else "  [已过期]"
        print(f"  {e['topic']:<28} {e['revisions']:>2} 版  {e['title'][:34]}{flag}")
    return 0


def run_show(args: argparse.Namespace) -> int:
    cfg = _load()
    found = mem.find_entry(cfg, args.topic)
    if not found:
        print(f"没有这个主题：{args.topic}", file=sys.stderr)
        return 1
    path, entry = found
    if args.json:
        print(json.dumps({"path": path, **entry.to_dict()},
                         ensure_ascii=False, indent=2))
        return 0
    print(f"# {entry.title}")
    print(f"主题 {entry.topic}　状态 {entry.status}　修订 {entry.revisions} 版")
    print(f"文件 {os.path.relpath(path, cfg.root_dir)}")
    print()
    print(entry.conclusion)
    if len(entry.history) > 1:
        print("\n修订历史（旧结论保留在这里，没有删除）：")
        for line in entry.history:
            print(f"  {line}")
    return 0


def _status_change(args: argparse.Namespace, status: str) -> int:
    cfg = _load()
    try:
        outcome = mem.set_status(cfg, args.topic, status)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    indexed = None
    if not args.no_index:
        # prune 是必须的：移动留下的旧记录如果不清掉，那条陈旧的行仍会被搜到，
        # 就会出现「标了过期却还能搜到」的假象。
        indexed = _index_memory(cfg, [cfg.memory_dir], prune=True)

    if args.json:
        print(json.dumps({"topic": outcome["entry"].topic, "path": outcome["path"],
                          "status": status, "indexed": indexed},
                         ensure_ascii=False, indent=2))
        return 0
    verb = "已标记过期" if status == mem.STATUS_EXPIRED else "已恢复有效"
    print(f"memory: 主题 `{outcome['entry'].topic}` {verb}")
    print(f"  文件  {os.path.relpath(outcome['path'], cfg.root_dir)}")
    if status == mem.STATUS_EXPIRED:
        print("  检索  默认不再返回；要看到它加 --include-expired")
    return 0


def run_expire(args: argparse.Namespace) -> int:
    return _status_change(args, mem.STATUS_EXPIRED)


def run_restore(args: argparse.Namespace) -> int:
    return _status_change(args, mem.STATUS_ACTIVE)


def run_export(args: argparse.Namespace) -> int:
    cfg = _load()
    if args.dsh:
        target = os.path.join(os.path.expanduser("~"), ".dsh", "memory", "memories")
    else:
        target = args.to or os.path.join(cfg.memory_dir, "export")

    entries = mem.list_entries(cfg, include_expired=False)
    written: list[str] = []
    for e in entries:
        full = mem.MemoryEntry(**{k: e[k] for k in
                                  ("topic", "title", "created", "updated",
                                   "revisions", "status", "tags", "conclusion",
                                   "history")})
        written.append(mem.export_entry(full, target, scope=args.scope))

    if args.json:
        print(json.dumps({"target": target, "count": len(written),
                          "files": written}, ensure_ascii=False, indent=2))
        return 0
    print(f"memory export → {target}")
    print(f"  导出 {len(written)} 条（只导有效记忆；已过期的需要先 restore）")
    for path in written[:20]:
        print(f"    {os.path.basename(path)}")
    if len(written) > 20:
        print(f"    ... 其余 {len(written) - 20} 条")
    if args.dsh:
        print("  已写入 DSH 记忆插件目录：现有插件的 list/read 应该能找到它们")
    else:
        print("  提示：加 --dsh 可直接导出到 ~/.dsh/memory/memories/（现有插件能读）")
    return 0


def run_where(args: argparse.Namespace) -> int:
    cfg = _load()
    print(cfg.memory_dir)
    return 0


def run(args: argparse.Namespace) -> int:  # pragma: no cover - 由子命令分发
    return int(getattr(args, "func", lambda a: 0)(args))
