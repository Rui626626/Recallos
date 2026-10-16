"""拼音支持：内置表，**零依赖**。

用来干什么
----------
1. **拼音搜索**：输入 `tonghuopengzhang` 能找到「通货膨胀」。
2. **同音错字容错**：搜索「飞利浦斯」也能找到「菲利普斯」——两者拼音相同
   （`feilipusi`），而逐字匹配会漏掉它。

怎么实现的
----------
索引时，除了正文本身，还写入一段**汉字拼音的连写**（`通货膨胀` → `tonghuopengzhang`）。
查询时把查询里的汉字也转成拼音，于是「查询拼音」与「索引里的拼音」直接对上，
同音字自动互相命中——不需要逐个汉字展开同音字集合（那会组合爆炸）。

数据从哪来
----------
`brain/data/pinyin.tsv.gz`：`汉字<TAB>无调拼音`，26,703 条，基本区 + 扩展 A。
由 pypinyin 0.55.0 生成，多音字取第一个读音。生成脚本见
`tools/build_pinyin_table.py`（可重新生成，结果可复现）。

**为什么不直接依赖 pypinyin**：那会把一个纯粹的、离线可用的工具变成需要联网装包的
工具。既然表能一次性生成，就把运行时依赖变成随包数据。代价是仓库多 98KB，
换来的是用户不需要装任何东西。
"""

from __future__ import annotations

import gzip
import os
from functools import lru_cache
from typing import Optional

__all__ = ["available", "table", "to_pinyin", "pinyin_projection",
           "indexed_projection", "looks_like_pinyin", "TABLE_PATH"]

TABLE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "data", "pinyin.tsv.gz")

#: 汉字范围（与 fuzzy.py 保持一致）
_CJK_RANGES = ("\u3400-\u4dbf", "\u4e00-\u9fff", "\uf900-\ufaff")


def _is_cjk(ch: str) -> bool:
    for rng in _CJK_RANGES:
        lo, hi = rng.split("-")
        if ord(lo) <= ord(ch) <= ord(hi):
            return True
    return False


@lru_cache(maxsize=1)
def table() -> dict[str, str]:
    """懒加载内置拼音表。文件缺失时返回空表（功能降级，不抛异常）。"""
    out: dict[str, str] = {}
    if not os.path.isfile(TABLE_PATH):
        return out
    try:
        with gzip.open(TABLE_PATH, "rt", encoding="utf-8") as fh:
            for line in fh:
                if "\t" not in line:
                    continue
                ch, _, py = line.rstrip("\n").partition("\t")
                if ch and py:
                    out[ch] = py
    except (OSError, EOFError):
        # 表坏了不能把检索整体拖垮：当作没有拼音支持。
        return {}
    return out


def available() -> bool:
    return bool(table())


def to_pinyin(text: str, keep_other: bool = False) -> str:
    """汉字转拼音连写。

    `keep_other=False`（默认）时非汉字被丢弃，只留下连续的拼音串——这正是
    「繁简/同音都能对上」所需的形式。`keep_other=True` 时非汉字原样保留，
    适合给人看的场景。
    """
    tbl = table()
    if not tbl:
        return ""
    parts: list[str] = []
    for ch in text or "":
        if _is_cjk(ch):
            parts.append(tbl.get(ch, ""))
        elif keep_other:
            parts.append(ch)
    return "".join(parts)


def pinyin_projection(text: str) -> str:
    """给索引用：把文本里的汉字转成一段拼音连写。没有汉字或没有表时返回空串。"""
    if not text:
        return ""
    return to_pinyin(text)


def indexed_projection(title: str, heading_path: str, text: str) -> str:
    """**索引里实际写入的那段可检索文本。**

    这个函数必须被两处共用：写索引的地方（`store.replace_chunks`）和做相似度
    阈值判定的地方（`retrieve`）。曾经只在一处用了它 —— 结果拼音召回上来了、
    却又被阈值全部否掉，因为判定看的是原始正文，而拼音并不在正文里。
    两处不一致的 bug 表现为「明明召回了却返回空」，非常难查。
    """
    base = "\n".join(part for part in (title, heading_path, text) if part)
    py = pinyin_projection(base)
    return f"{base}\n{py}" if py else base


def looks_like_pinyin(query: str) -> bool:
    """查询是不是「全拼音」形态：只有拉丁字母，且长度够。

    用来决定要不要把查询本身当成拼音再探一次。太短的串（如 `gini`）不当作拼音，
    否则会和正常英文词互相干扰。
    """
    q = (query or "").strip()
    if len(q) < 6 or not q.isalpha() or not q.isascii():
        return False
    # 至少能切出两个音节才当拼音，否则 "python" 这类词会被误判
    return len(syllables(q) or []) >= 2


def syllables(text: str) -> Optional[list[str]]:
    """尽力把一串拼音切成音节（贪心最长匹配）。切不动就返回 None。"""
    tbl = table()
    if not tbl:
        return None
    known: set[str] = set()
    for py in tbl.values():
        known.add(py)
    q = (text or "").lower()
    out: list[str] = []
    i = 0
    while i < len(q):
        matched = ""
        for size in range(min(6, len(q) - i), 0, -1):
            chunk = q[i:i + size]
            if chunk in known:
                matched = chunk
                break
        if not matched:
            return None
        out.append(matched)
        i += len(matched)
    return out
