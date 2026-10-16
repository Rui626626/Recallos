"""`recall.py search` — 检索并返回受预算约束、带出处的上下文。

设计上只做一件事：**把最相关的少量内容，连同可核查的出处，交出去。**
它不生成结论——生成是 `compose` 的事，两者分开是为了让「我看到了什么」和
「我写了什么」互不混淆。

两种模式
--------
* **索引模式**（默认）：搜已经收进来的东西，快、可回查。
* **现场模式**（`--in <目录>`）：**不建索引**，直接遍历目录现读现搜。
  给刚下载的人一个零设置的第一步；代价是每次都重新解析，大目录会慢。

退出码：
    0  有命中（或查询为空但语法正确）
    1  没命中（不是错误，但脚本需要能区分）
    2  用法错误 / 索引不可用
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from ..config import ensure_dirs, load_config
from ..core import fuzzy
from ..core.context import build_context, render
from ..core.retrieve import retrieve
from ..core.store import Store

__all__ = ["register", "run"]


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("search", help="search the index and return budgeted context")
    p.add_argument("query", help="what to look for")
    p.add_argument("--limit", type=int, default=None,
                   help="maximum hits to return (default from config)")
    p.add_argument("--budget", type=int, default=None,
                   help="token budget for the assembled context")
    p.add_argument("--deep", action="append", default=[], metavar="CHUNK_ID",
                   help="return this chunk in full (repeatable)")
    p.add_argument("--full", action="store_true",
                   help="return every surviving hit in full")
    p.add_argument("--render", action="store_true",
                   help="print a citable markdown block instead of JSON")
    p.add_argument("--no-fuzzy", action="store_true",
                   help="exact + aliases only; skip typo-tolerant recall")
    p.add_argument("--include-expired", action="store_true",
                   help="also search memory that has been marked expired")
    p.add_argument("--explain", action="store_true",
                   help="report how many hits each stage contributed")
    p.add_argument("--in", dest="live_in", action="append", default=[],
                   metavar="DIR",
                   help="search this directory directly, without building an "
                        "index first (repeatable)")
    p.add_argument("--max-files", type=int, default=None,
                   help="cap on files parsed in --in mode (default 400)")
    p.add_argument("--time-limit", type=float, default=120.0,
                   help="give up after this many seconds in --in mode "
                        "(default 120; parsing PDFs is slow)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=run)


def _live(cfg, args) -> int:
    """`--in` 模式：不读也不写索引，直接搜目录。"""
    from ..core.live import DEFAULT_MAX_FILES, live_search

    missing = [p for p in args.live_in if not os.path.exists(p)]
    for p in missing:
        print(f"路径不存在：{p}", file=sys.stderr)
    targets = [p for p in args.live_in if os.path.exists(p)]
    if not targets:
        return 2

    alias_map = (fuzzy.load_aliases(cfg).get("_merged") or {})
    quiet = args.json or not sys.stderr.isatty()

    def progress(done: int, cap: int, path: str) -> None:
        if quiet:
            return
        sys.stderr.write(f"\r  现场解析 [{done}/{cap}] {os.path.basename(path)[:52]:<52}")
        sys.stderr.flush()

    payload = live_search(cfg, args.query, targets,
                          limit=args.limit or cfg.search_limit,
                          max_files=args.max_files or DEFAULT_MAX_FILES,
                          alias_map=alias_map,
                          deadline_s=args.time_limit,
                          progress=None if quiet else progress)
    if not quiet:
        sys.stderr.write("\r" + " " * 78 + "\r")

    hits = payload["hits"]
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if hits else 1

    print(f"search: {payload['query']}   [现场模式 · 未建索引]")
    print(f"  目录    {', '.join(payload['roots'])}")
    print(f"  扫描    {payload['scanned']} 个文件，解析成功 {payload['parsed']}，"
          f"失败 {payload['failed']}，耗时 {payload['elapsed_s']}s")
    if payload["truncated"]:
        print("  ⚠ 已截断")
    if not hits:
        print("  没有命中。")
        if payload.get("note"):
            print(f"  {payload['note']}")
        for f in payload["failures"][:5]:
            print(f"  ! {os.path.basename(f['path'])}: {f['error'][:90]}")
        return 1

    print(f"  {len(hits)} 条，覆盖 {payload['doc_count']} 个文件")
    for i, h in enumerate(hits, 1):
        mark = {"exact": "=", "fuzzy": "~"}.get(h["why"].split(":")[0], "+")
        where = h["rel_path"]
        if h["heading_path"]:
            where += f" › {h['heading_path']}"
        page_note = f"第 {h['page']} 页"
        # 页码常常已经出现在标题路径里（PDF 抽取器就是这么写的），
        # 再补一次会得到「› 第 1 页（第 1 页）」这种废话。
        if h["page"] and page_note not in where:
            where += f"（{page_note}）"
        print(f"\n  [{i}]{mark} {h['title'] or '(无标题)'}")
        print(f"      {where}")
        print(f"      why={h['why']}"
              + (f" sim={h['similarity']:.2f}" if h["why"] == "fuzzy" else ""))
        print(f"      {' '.join(h['snippet'].split())[:200]}")
    print("\n  提示：这是现场搜的，没有存任何索引。想长期好用先建库："
          "recall.py add <目录>")
    return 0


def run(args: argparse.Namespace) -> int:
    cfg = load_config()
    ensure_dirs(cfg)

    # `--in` 走现场模式：不打开索引，也不建索引。
    if args.live_in:
        return _live(cfg, args)

    alias_doc = fuzzy.load_aliases(cfg)
    alias_map = alias_doc.get("_merged") or {}

    store = Store.open(cfg.db_path)
    try:
        counts = store.counts()
        if not counts.get("docs"):
            # 空库不是错误，是一个**还没做第一步**的状态。给出的必须是能直接
            # 照抄的两条路，而不是一句「请先建库」。
            print("索引里还没有任何资料。两条路，任选一条：\n"
                  "\n"
                  "  1) 先直接搜一个目录（不建索引，立刻出结果）：\n"
                  '     recall.py search "你的查询" --in "C:\\某个文件夹"\n'
                  "\n"
                  "  2) 先收进来（之后检索更快，且每次都能回查出处）：\n"
                  '     recall.py add "C:\\某个文件夹"\n'
                  "\n"
                  "两者都不会改动你的原文件。", file=sys.stderr)
            return 2

        result = retrieve(
            store, args.query,
            limit=args.limit or cfg.search_limit,
            alias_map=alias_map,
            fuzzy_enabled=not args.no_fuzzy,
            # 过期记忆放在 _expired/ 下，默认按路径排除；--include-expired 放回来。
            exclude_paths=[] if args.include_expired else [cfg.expired_memory_dir],
        )

        deep = list(args.deep)
        if args.full:
            deep = [h.hit.chunk_id for h in result.hits] + deep

        payload = build_context(
            result.hits,
            budget=args.budget or cfg.token_budget,
            snippet_chars=cfg.snippet_chars,
            max_chunks_per_doc=cfg.max_chunks_per_doc,
            deep=deep,
        )
    finally:
        store.close()

    items = payload.pop("items")
    rendered = render({**payload, "items": items}, query=result.query)
    payload["items"] = [i.to_dict() for i in items]
    # `hits` 是**预算裁剪前**的完整命中列表，`items` 是裁剪后的上下文。
    # 两个都给，调用方才能看见「有多少被预算丢掉了」。
    payload["hits"] = [h.to_dict() for h in result.hits]
    payload["query"] = result.query
    payload["expanded"] = result.expanded
    payload["stages"] = result.stages
    payload["fuzzy_used"] = result.fuzzy_used
    payload["rejected_by_threshold"] = result.rejected
    payload["garbage_query"] = result.garbage
    payload["note"] = result.note
    payload["rendered"] = rendered

    # 退出码看的是「**检索**有没有命中」，不是「预算裁完还剩几条」。
    # 预算给小了导致上下文被裁光，是用法问题而不是「没找到」——用 1 报到脚本里
    # 会被误读成「库里没有这份资料」。
    found = True if not result.query else bool(result.hits)

    if args.render and not args.json:
        sys.stdout.write(rendered)
        return 0 if found else 1

    if not args.json:
        _print_human(payload, items, explain=args.explain)
        return 0 if found else 1

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if found else 1


def _print_human(payload: dict, items: list, explain: bool = False) -> None:
    q = payload["query"]
    print(f"search: {q}")
    if payload.get("expanded"):
        print(f"  别名扩展: {' , '.join(payload['expanded'])}")
    if explain:
        st = payload.get("stages") or {}
        print("  各段召回: " + "  ".join(f"{k}={v}" for k, v in st.items()))
        if payload.get("fuzzy_used"):
            print(f"  被相似度阈值丢弃: {payload.get('rejected_by_threshold', 0)}")
    if not items:
        print("  没有命中。")
        if payload.get("garbage_query"):
            print("  （查询里既没有汉字也没有拉丁字母，按乱码处理）")
        if payload.get("note"):
            print(f"  {payload['note']}")
        return

    print(f"  {len(items)} 条，覆盖 {payload['doc_count']} 份资料，"
          f"约 {payload['token_estimate']} tokens（预算 {payload['token_budget']}）")
    if payload.get("truncated"):
        print(f"  已裁剪：单篇上限丢弃 {payload['dropped_by_doc_cap']}，"
              f"预算丢弃 {payload['dropped_by_budget']}"
              f"（检索共命中 {len(payload.get('hits') or [])} 条，预算调小不会改变命中集合）")
    for i, it in enumerate(items, 1):
        where = it.source_ref
        if it.heading_path:
            where += f" › {it.heading_path}"
        if it.page:
            where += f"（第 {it.page} 页）"
        mark = {"exact": "=", "fuzzy": "~"}.get(it.why.split(":")[0], "+")
        print(f"\n  [{i}]{mark} {it.doc_title or '(无标题)'}")
        print(f"      {where}")
        print(f"      why={it.why}"
              + (f" sim={it.similarity:.2f}" if it.why == "fuzzy" else ""))
        body = it.text.replace("\n", " ").strip()
        print(f"      {body[:200]}")
    print("\n  提示：加 --render 得到带 locator 的 markdown，可逐条回查。")
