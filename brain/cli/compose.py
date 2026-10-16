"""`recall.py compose` — 把检索结果汇编成一份带出处的成品。

一句话说清楚它是什么、不是什么
--------------------------------
它是**汇编器，不是作者**。没有模型参与，所以它**不会替你写结论**——它把检索到的
原文按结构排好、逐条标上可回查的出处，然后在末尾留一个空白的「结论」小节等人
（或 Agent）补写。

这么设计是因为另一条路更糟：让一个离线工具假装会写文章，产出的东西读起来像结论、
实际上无法核查，正好破坏了这个项目最值钱的性质——**每一句都能回到出处**。

成品必须满足三条（来自计划里的验收标准）：

1. 文件真实存在
2. 里面**每一个**出处都能回查（写完立刻自查，见 `--verify`）
3. 成品本身能被再次搜到（写完自动入库，形成闭环）

用 `recall.py verify <文件>` 可以随时对任何成品重跑第 2 条。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime

from ..config import ensure_dirs, load_config
from ..core import fuzzy
from ..core.context import ContextItem, build_context
from ..core.locator import extract_locators, verify_locators
from ..core.retrieve import retrieve
from ..core.store import Store
from ..pipeline import ingest_paths
from ..state import Journal, SourceRegistry

__all__ = ["register", "run"]

#: 四种形态。区别只在**分组方式与标题措辞**，内容一律来自原文。
KINDS = {
    "review": "复习材料",
    "outline": "大纲",
    "report": "资料报告",
    "cards": "知识卡片",
}


def _slug(text: str, fallback: str = "artifact") -> str:
    s = re.sub(r"[\\/:*?\"<>|]", "", text or "").strip()
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"[^\w\u4e00-\u9fff\-]", "", s).strip("-")
    return (s[:60].strip("-") or fallback).lower()


def _cite_line(item: ContextItem) -> str:
    where = item.source_ref
    if item.heading_path:
        where += f" › {item.heading_path}"
    if item.page:
        where += f"（第 {item.page} 页）"
    return where


def render_artifact(query: str, kind: str, items: list[ContextItem],
                    meta: dict) -> str:
    """按形态汇编正文。每一段都必须紧跟着自己的出处。"""
    label = KINDS.get(kind, kind)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    out: list[str] = [
        f"# {label}：{query}",
        "",
        f"> 由 RecallOS `compose` 汇编 · {now} · 形态 `{kind}`",
        f"> 出处 {len(items)} 条，覆盖 {meta.get('doc_count', 0)} 份资料。",
        "> **这是资料汇编，不是原创结论。** 正文按检索命中的原文排列，每条都带出处；",
        "> 结论小节留空，由人（或 Agent）在**不超出上述出处**的前提下补写。",
        "",
        "## 出处清单",
        "",
        "| # | 资料 | 位置 | 定位 |",
        "|---|------|------|------|",
    ]
    for i, it in enumerate(items, 1):
        out.append(f"| {i} | {it.doc_title or '(无标题)'} | "
                   f"`{_cite_line(it)}` | `{it.locator}` |")
    out += ["", "---", "", "## 正文", ""]

    if kind == "outline":
        out += _render_outline(items)
    elif kind == "cards":
        out += _render_cards(items)
    elif kind == "report":
        out += _render_report(items)
    else:
        out += _render_review(items)

    out += [
        "",
        "---",
        "",
        "## 结论",
        "",
        "<!-- 由人（或 Agent）补写。约束：只能使用上面已列出的出处，"
        "不得引入未出现的内容；每句话末尾标出 [n]。 -->",
        "",
        "---",
        "",
        "## 机器可读出处",
        "",
        "<!-- 下面是本文件的出处清单，`recall.py verify` 只读这一段。"
        "请勿手工编辑，改动会导致回查失败。 -->",
        "",
        "<!-- recallos:locators",
    ]
    for it in items:
        out.append(it.locator)
    out += ["-->", ""]
    return "\n".join(out)


def _render_review(items: list[ContextItem]) -> list[str]:
    out: list[str] = []
    for i, it in enumerate(items, 1):
        out.append(f"### {i}. {it.doc_title or '(无标题)'}")
        out.append("")
        out.append(f"出处：`{_cite_line(it)}`　定位：`{it.locator}`")
        out.append("")
        out.append(it.text)
        out.append("")
    return out


def _render_report(items: list[ContextItem]) -> list[str]:
    """按资料分组，每条一个要点句。"""
    by_doc: dict[str, list[tuple[int, ContextItem]]] = {}
    for i, it in enumerate(items, 1):
        by_doc.setdefault(it.doc_title or it.doc_id, []).append((i, it))
    out: list[str] = []
    for title, group in by_doc.items():
        out.append(f"### {title}")
        out.append("")
        out.append(f"来源：`{group[0][1].source_ref}`")
        out.append("")
        for idx, it in group:
            first = it.text.strip().splitlines()[0] if it.text.strip() else ""
            out.append(f"- {first}　`[{idx}]` `{it.locator}`")
        out.append("")
    return out


def _render_cards(items: list[ContextItem]) -> list[str]:
    out: list[str] = []
    for i, it in enumerate(items, 1):
        term = it.heading_path.split("›")[-1].strip() if it.heading_path else \
            (it.doc_title or f"要点 {i}")
        out.append(f"### 卡片 {i}：{term}")
        out.append("")
        out.append(it.text.strip())
        out.append("")
        out.append(f"— 出处：`{_cite_line(it)}`　定位：`{it.locator}`")
        out.append("")
    return out


def _render_outline(items: list[ContextItem]) -> list[str]:
    """按资料 → 标题路径聚合成大纲树，叶子挂一行摘录。"""
    tree: dict[str, dict[str, list[ContextItem]]] = {}
    for it in items:
        doc = it.doc_title or it.doc_id
        path = it.heading_path or "(正文)"
        tree.setdefault(doc, {}).setdefault(path, []).append(it)

    out: list[str] = []
    for doc, paths in tree.items():
        out.append(f"### {doc}")
        out.append("")
        for path, group in paths.items():
            out.append(f"- **{path}**")
            for it in group:
                first = it.text.strip().splitlines()[0] if it.text.strip() else ""
                out.append(f"  - {first[:120]}　`{it.locator}`")
        out.append("")
    return out


# --------------------------------------------------------------------- CLI


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("compose",
                       help="assemble retrieved material into a citable artifact")
    p.add_argument("query", help="what the artifact is about")
    p.add_argument("--kind", choices=sorted(KINDS), default="review",
                   help="artifact shape (default review)")
    p.add_argument("--out", default=None, help="output path (default artifacts/<slug>.md)")
    p.add_argument("--budget", type=int, default=None, help="token budget")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--no-index", action="store_true",
                   help="do not import the artifact back into the index")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    cfg = load_config()
    ensure_dirs(cfg)
    alias_doc = fuzzy.load_aliases(cfg)

    store = Store.open(cfg.db_path)
    try:
        if not store.counts().get("docs"):
            print("索引里还没有资料，先 `recall.py add <路径>`", file=sys.stderr)
            return 2

        result = retrieve(store, args.query, limit=args.limit or cfg.search_limit,
                          alias_map=alias_doc.get("_merged") or {},
                          exclude_paths=[cfg.expired_memory_dir])
        if not result.hits:
            print(f"没有命中，无法汇编：{args.query}", file=sys.stderr)
            if result.note:
                print(f"  {result.note}", file=sys.stderr)
            return 1

        payload = build_context(
            result.hits,
            budget=args.budget or cfg.token_budget,
            snippet_chars=cfg.snippet_chars,
            max_chunks_per_doc=cfg.max_chunks_per_doc,
        )
        items: list[ContextItem] = list(payload["items"])
        if not items:
            print("命中都被预算裁掉了；把 --budget 调大再试。", file=sys.stderr)
            return 2

        markdown = render_artifact(result.query, args.kind, items, payload)

        out_path = args.out or os.path.join(
            cfg.artifacts_dir, f"{_slug(args.query, 'artifact')}-{args.kind}.md")
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(markdown)

        # 验收第 2 条：写完立刻自查每一个出处（只信文件里那一段机器可读清单，
        # 不信内存里的对象——文件才是交付物）
        verification = verify_locators(store, extract_locators(markdown))
    finally:
        store.close()

    indexed = None
    if not args.no_index:
        # 验收第 3 条：成品本身能被再次搜到（闭环）
        store = Store.open(cfg.db_path)
        registry = SourceRegistry(cfg.sources_path)
        journal = Journal(cfg.journal_path)
        try:
            summary = ingest_paths(store, registry, journal, cfg, [out_path], force=False)
            indexed = {"chunks_written": summary["chunks_written"],
                       "by_status": summary["by_status"]}
        finally:
            store.close()

    rel = os.path.relpath(out_path, cfg.root_dir)
    if args.json:
        print(json.dumps({
            "query": result.query,
            "kind": args.kind,
            "path": out_path,
            "relative_path": rel,
            "items": len(items),
            "doc_count": payload["doc_count"],
            "token_estimate": payload["token_estimate"],
            "sources": [{"locator": i.locator, "title": i.doc_title,
                         "source_ref": i.source_ref, "why": i.why}
                        for i in items],
            "verification": verification,
            "indexed": indexed,
            "stages": result.stages,
        }, ensure_ascii=False, indent=2))
    else:
        print(f"compose: {result.query}  →  {KINDS[args.kind]}")
        print(f"  成品    {rel}")
        print(f"  出处    {len(items)} 条 / {payload['doc_count']} 份资料 / "
              f"约 {payload['token_estimate']} tokens")
        v = verification
        print(f"  回查    {v['resolved']}/{v['checked']} 条可解析"
              + ("" if v["ok"] else "  ← 有问题！"))
        for b in (v["broken"] + v["missing_doc"])[:5]:
            print(f"      ! {b.get('locator')}: {b.get('why')}")
        if indexed:
            print(f"  已入库  {indexed['chunks_written']} 个切片"
                  f"（成品可被再次搜到）")
        else:
            print("  未入库（--no-index）")

    return 0 if verification["ok"] else 1
