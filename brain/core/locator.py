"""定位符（locator）的解析与回查。

每一条检索命中都带一个定位符：

    doc:<doc_id>#h=<chunk_hash 前 10 位>@<ordinal>

它的唯一职责是**让「出处」可核查**。一份成品里写了十个出处，就应该能逐个回到数据库
里那一行——否则出处在读者眼里和装饰没有区别。

`h=` 段只取 chunk_hash 的前 10 位：够用且肉眼可读，完整哈希仍然是切片的主键。
回查时**以 ordinal 定位、以 hash 前 10 位校验**：ordinal 能定位到行，hash 能发现
「内容变了但序号没变」这种情况。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from .store import Store

__all__ = ["Locator", "LOCATOR_RE", "parse_locators", "extract_locators", "verify_locators"]

#: 允许 hash 段缺失或长短不一（写成 8 位、10 位、完整都可以）。
LOCATOR_RE = re.compile(r"doc:([0-9a-fA-F]+)(?:#h=([0-9a-fA-F]*))?@(\d+)")

#: 成品文件里机器可读的出处块。
BLOCK_OPEN = "<!-- recallos:locators"
BLOCK_CLOSE = "-->"


@dataclass(frozen=True)
class Locator:
    doc_id: str
    chunk_hash: str
    ordinal: int
    raw: str = ""

    def __str__(self) -> str:
        h = self.chunk_hash[:10]
        return f"doc:{self.doc_id}#h={h}@{self.ordinal}" if h else \
            f"doc:{self.doc_id}@{self.ordinal}"

    def to_dict(self) -> dict:
        return {"doc_id": self.doc_id, "chunk_hash": self.chunk_hash,
                "ordinal": self.ordinal, "locator": str(self)}


def parse_locators(text: str) -> list[Locator]:
    out: list[Locator] = []
    seen: set[str] = set()
    for m in LOCATOR_RE.finditer(text or ""):
        loc = Locator(doc_id=m.group(1).lower(), chunk_hash=(m.group(2) or "").lower(),
                      ordinal=int(m.group(3)), raw=m.group(0))
        key = str(loc)
        if key not in seen:
            seen.add(key)
            out.append(loc)
    return out


def extract_locators(markdown: str) -> list[Locator]:
    """优先读机器可读块；没有块时退回全文扫描。"""
    text = markdown or ""
    if BLOCK_OPEN in text:
        body = text.split(BLOCK_OPEN, 1)[1]
        body = body.split(BLOCK_CLOSE, 1)[0]
        found = parse_locators(body)
        if found:
            return found
    return parse_locators(text)


def verify_locators(store: Store, locators: Iterable[Locator]) -> dict:
    """逐个回查。返回 `{ok, checked, resolved, broken[], missing_doc[]}`。

    判定规则：
    * doc_id 不存在 → `missing_doc`
    * doc 存在但该 ordinal 的切片不存在 → `broken`（内容变了/被重建过）
    * hash 前缀给了但对不上 → `broken`（同序号不同内容）
    """
    checked = 0
    resolved = 0
    broken: list[dict] = []
    missing_doc: list[dict] = []

    for loc in locators:
        checked += 1
        row = store.conn.execute(
            "SELECT 1 FROM doc WHERE doc_id=?", (loc.doc_id,)).fetchone()
        if row is None:
            missing_doc.append({**loc.to_dict(), "why": "文档不在索引里"})
            continue
        hit = store.conn.execute(
            "SELECT chunk_hash FROM chunk WHERE doc_id=? AND ordinal=?",
            (loc.doc_id, loc.ordinal)).fetchone()
        if hit is None:
            broken.append({**loc.to_dict(), "why": "该序号没有切片"})
            continue
        if loc.chunk_hash and not (hit["chunk_hash"] or "").startswith(loc.chunk_hash):
            broken.append({**loc.to_dict(), "why": "切片内容已变化（哈希对不上）",
                           "actual": (hit["chunk_hash"] or "")[:10]})
            continue
        resolved += 1

    return {
        "ok": not broken and not missing_doc,
        "checked": checked,
        "resolved": resolved,
        "broken": broken,
        "missing_doc": missing_doc,
    }
