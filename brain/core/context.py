"""上下文组装：把检索结果裁到预算内，并渲染成「可引用」的形式。

两个硬约束，都来自计划里写的验收标准：

1. **返回内容必须有上限。** 没有上限的检索会把一堆无关内容一次性塞进对话，
   既费钱又淹没有效信息。预算按 `estimate_tokens` 估算（偏保守），超了就丢尾部。
2. **每条都必须能回查。** 每一项都带 `locator`（`doc:<id>#h=<hash>@<ordinal>`），
   成品里的每个出处都能拿它回到具体的切片。

同一份文档默认最多贡献 `max_chunks_per_doc` 条——否则一篇长文档会把别的文档全挤掉，
而「找回资料」要的是覆盖面，不是单篇的完整度。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

from .tokenize import estimate_tokens

__all__ = ["ContextItem", "build_context", "render"]

#: 渲染时每条片段最多显示多少字符（防止单条撑爆阅读体验）。
RENDER_SNIPPET_CHARS = 320


@dataclass
class ContextItem:
    doc_id: str
    chunk_id: str
    doc_title: str
    source_ref: str
    source_kind: str
    heading_path: str
    page: Optional[int]
    ordinal: int
    locator: str
    text: str
    full: bool
    why: str
    rank: float
    similarity: float
    tokens: int = 0

    def to_dict(self) -> dict:
        return {
            "doc_id": self.doc_id,
            "chunk_id": self.chunk_id,
            "title": self.doc_title,
            "source_ref": self.source_ref,
            "source_kind": self.source_kind,
            "heading_path": self.heading_path,
            "page": self.page,
            "ordinal": self.ordinal,
            "locator": self.locator,
            "why": self.why,
            "rank": round(self.rank, 4),
            "similarity": round(self.similarity, 4),
            "full": self.full,
            "tokens": self.tokens,
            "text": self.text,
        }


def build_context(ranked: Sequence, *, budget: int = 2500, snippet_chars: int = 240,
                  max_chunks_per_doc: int = 3,
                  deep: Iterable[str] = ()) -> dict:
    """按预算裁剪检索结果。

    `ranked` 是 `retrieve.Ranked` 序列（按置信度降序）。`deep` 里的 chunk_id 会返回
    切片全文，不受 `snippet_chars` 限制——但**仍然受总预算约束**，预算面前人人平等。
    """
    deep_set = set(deep or ())
    items: list[ContextItem] = []
    used_tokens = 0
    per_doc: dict[str, int] = {}
    dropped_by_doc_cap = 0
    dropped_by_budget = 0

    for r in ranked:
        hit = r.hit
        if per_doc.get(hit.doc_id, 0) >= max_chunks_per_doc:
            dropped_by_doc_cap += 1
            continue

        want_full = hit.chunk_id in deep_set
        body = hit.text or hit.snippet or ""
        text = body if want_full else _clip(hit.snippet or body, snippet_chars)
        cost = estimate_tokens(text)

        # 预算不够时**整条丢弃**，不截半条——半条切片既读不懂也可能掐掉关键句。
        if used_tokens + cost > budget:
            dropped_by_budget += 1
            continue

        used_tokens += cost
        per_doc[hit.doc_id] = per_doc.get(hit.doc_id, 0) + 1
        items.append(ContextItem(
            doc_id=hit.doc_id,
            chunk_id=hit.chunk_id,
            doc_title=hit.title,
            source_ref=hit.source_ref,
            source_kind=hit.source_kind,
            heading_path=hit.heading_path,
            page=hit.page,
            ordinal=hit.ordinal,
            locator=f"doc:{hit.doc_id}#h={hit.chunk_hash[:10]}@{hit.ordinal}",
            text=text,
            full=want_full,
            why=r.why,
            rank=r.rank,
            similarity=r.similarity,
            tokens=cost,
        ))

    docs = sorted({i.doc_id for i in items})
    return {
        "items": items,
        "token_estimate": used_tokens,
        "token_budget": budget,
        "truncated": bool(dropped_by_doc_cap or dropped_by_budget),
        "dropped_by_doc_cap": dropped_by_doc_cap,
        "dropped_by_budget": dropped_by_budget,
        "doc_count": len(docs),
        "doc_ids": docs,
    }


def _clip(text: str, width: int) -> str:
    t = (text or "").strip()
    if width <= 0 or len(t) <= width:
        return t
    return t[:width].rstrip() + "…"


def render(payload: dict, query: str) -> str:
    """渲染成一段可引用的 markdown。

    **每条都带 locator**，这是「出处回查」的基础：成品里引用了哪一条，就能拿
    locator 回到那个切片，不需要相信任何叙述。
    """
    items: list[ContextItem] = list(payload.get("items") or [])
    head = [
        f"# 检索结果：{query}",
        "",
        f"命中 {len(items)} 条，覆盖 {payload.get('doc_count', 0)} 份资料，"
        f"约 {payload.get('token_estimate', 0)} tokens"
        f"（预算 {payload.get('token_budget', 0)}）。",
    ]
    if payload.get("truncated"):
        head.append(
            f"已裁剪：因单篇上限丢弃 {payload.get('dropped_by_doc_cap', 0)} 条，"
            f"因预算丢弃 {payload.get('dropped_by_budget', 0)} 条。")
    if not items:
        head += ["", "没有命中。可以试：换一个说法；把常见叫法写进 `aliases.json`；"
                     "确认相关文件已用 `recall.py add` 导入。"]
        return "\n".join(head) + "\n"

    head.append("")
    for i, it in enumerate(items, 1):
        where = it.source_ref
        if it.heading_path:
            where += f" › {it.heading_path}"
        if it.page:
            where += f"（第 {it.page} 页）"
        head.append(f"## [{i}] {it.doc_title or '(无标题)'}")
        head.append("")
        head.append(f"- 来源：`{where}`")
        head.append(f"- 定位：`{it.locator}`")
        head.append(f"- 命中方式：{it.why}"
                    + (f"（相似度 {it.similarity:.2f}）" if it.why == "fuzzy" else ""))
        head.append("")
        head.append(it.text)
        head.append("")
    return "\n".join(head).rstrip() + "\n"
