"""现场检索（live 模式）：**不建索引，直接搜一个目录**。

为什么需要它
------------
「先建库，再搜索」在逻辑上完全正确，但它把「我想试试这东西」变成了「我得先花时间
把资料收进来」。对一个刚下载的人，这个门槛足以让他直接放弃。

live 模式把第一步变成零：给一个目录 + 一句查询，立刻出结果。不需要 `init`、不需要
`add`、不需要数据库。

代价（必须说清楚，而且要在输出里报出来）
------------------------------------------
每次查询都要**重新遍历、重新解析、重新切块**。所以：

* 适合「小目录 / 先试试 / 一次性找一份文件」
* **不是**长期方案 —— 同样的内容搜第二遍仍然要重新解析一遍
* 大目录会明显变慢，所以有 `max_files` 上限；被截断时会明确报出来，不假装搜完了

live 与索引的分工：**live 负责「立刻能用」，索引负责「长期好用」。** 两者共用同一套
抽取器、切块器和打分规则，所以同一个查询在两个模式下的排序是一致的——不会出现
「试的时候能找到、建库之后反而找不到」这种荒唐事。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional, Sequence

from ..config import Config
from ..ingest.base import ParseError, parser_for, parser_name
from ..pipeline import scan_files
from . import fuzzy
from .chunk import chunk_segments
from .pinyin import indexed_projection, looks_like_pinyin, pinyin_projection

__all__ = ["LiveHit", "live_search"]

#: 一次 live 查询最多解析多少个文件。超了会被截断并明确报告。
DEFAULT_MAX_FILES = 400


@dataclass
class LiveHit:
    path: str
    rel_path: str
    title: str
    heading_path: str
    page: Optional[int]
    ordinal: int
    snippet: str
    rank: float
    why: str
    similarity: float = 0.0

    @property
    def locator(self) -> str:
        """live 模式的定位符。

        刻意与索引模式的 `doc:<id>#h=<hash>@<n>` 长得不一样：它**没法**用
        `recall.py verify` 回查（文件还没进库），所以不能长得像可以。
        """
        return f"live:{self.path}@{self.ordinal}"

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "rel_path": self.rel_path,
            "title": self.title,
            "heading_path": self.heading_path,
            "page": self.page,
            "ordinal": self.ordinal,
            "snippet": self.snippet,
            "rank": round(self.rank, 4),
            "why": self.why,
            "similarity": round(self.similarity, 4),
            "locator": self.locator,
        }


def _clip(text: str, width: int) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= width else t[:width].rstrip() + "…"


def _window(text: str, needle: str, width: int) -> str:
    """把命中位置附近的文字抠出来，并用 «» 标出命中的那一处。"""
    body = text or ""
    if not needle:
        return _clip(body, width)
    idx = body.lower().find(needle.lower())
    if idx < 0:
        return _clip(body, width)
    start = max(0, idx - width // 3)
    end = min(len(body), start + width)
    return ("…" if start > 0 else "") + \
        body[start:idx] + "«" + body[idx:idx + len(needle)] + "»" + \
        body[idx + len(needle):end] + ("…" if end < len(body) else "")


def _relative_to_any(path: str, roots: Sequence[str]) -> str:
    """相对**被搜的那个目录**给路径，而不是相对项目根。

    相对项目根会得到一串 `..\\..\\..\\Desktop\\...`，对读者毫无意义；相对被搜的
    那个目录才是「在这个文件夹里的哪个位置」，一眼能懂。
    """
    ap = os.path.abspath(path)
    for root in roots:
        try:
            common = os.path.commonpath([ap, root])
        except ValueError:            # 不同盘符
            continue
        if common == root:
            return os.path.relpath(ap, root)
    return ap


def _score(query: str, haystack: str, *, expansions: Sequence[str],
           pinyin_query: bool) -> Optional[tuple[float, str, float]]:
    """给一个切片打分。返回 `(rank, why, similarity)`，不达标返回 None。

    判定顺序刻意与索引模式一致：精确 → 别名 → 拼音 → 模糊。
    """
    low = haystack.lower()
    q = query.lower()
    if q and q in low:
        return 1.0, "exact", 1.0
    for term in expansions:
        if term and term.lower() in low:
            return 0.85, f"alias:{term}", 0.85
    if pinyin_query:
        py_q = pinyin_projection(query)
        if py_q and py_q in pinyin_projection(haystack):
            sim = fuzzy.similarity(query, haystack)
            return max(sim, 0.9), "fuzzy:pinyin", sim
    if not fuzzy.passes_threshold(query, haystack):
        return None
    sim = fuzzy.similarity(query, haystack)
    return sim, "fuzzy", sim


def live_search(cfg: Config, query: str, roots: Sequence[str], *,
                limit: int = 20,
                max_files: int = DEFAULT_MAX_FILES,
                alias_map: Optional[dict] = None,
                progress: Optional[Callable[[int, int, str], None]] = None,
                deadline_s: Optional[float] = None) -> dict:
    """直接在一个目录里搜，不读也不写索引。

    返回一个带 `hits` / `scanned` / `failures` / `truncated` / `elapsed_s` 的字典。
    **失败必须留痕**：解析不了的文件进 `failures`，绝不静默跳过。
    """
    started = time.time()
    q = fuzzy.normalize_query(query)
    out: dict = {
        "query": q, "mode": "live", "roots": [os.path.abspath(r) for r in roots],
        "scanned": 0, "parsed": 0, "failed": 0, "failures": [],
        "truncated": False, "max_files": max_files,
        "hits": [], "elapsed_s": 0.0, "note": "",
    }
    if not q:
        out["note"] = "空查询"
        return out
    if fuzzy.looks_like_garbage(q):
        out["note"] = "查询里既没有汉字也没有拉丁字母，按乱码处理，返回空"
        return out

    expansions = fuzzy.expand_query(q, alias_map or {})
    pinyin_query = bool(pinyin_projection(q)) or looks_like_pinyin(q)

    hits: list[LiveHit] = []
    abs_roots = [os.path.abspath(r) for r in roots]
    files = scan_files(cfg, list(roots))
    for path in files:
        if out["scanned"] >= max_files:
            out["truncated"] = True
            break
        out["scanned"] += 1
        if progress:
            progress(out["scanned"], max_files, path)

        parser = parser_for(path)
        if parser is None:
            continue
        try:
            parsed = parser(path)
        except ParseError as exc:
            out["failed"] += 1
            out["failures"].append({"path": path, "error": str(exc),
                                    "extractor": parser_name(parser)})
            continue
        except Exception as exc:                    # 解析器的意外崩溃也要留痕
            out["failed"] += 1
            out["failures"].append({"path": path,
                                    "error": f"{type(exc).__name__}: {exc}",
                                    "extractor": parser_name(parser)})
            continue
        if parsed.is_empty:
            continue

        out["parsed"] += 1
        try:
            chunks = chunk_segments(parsed.segments,
                                    target_chars=cfg.chunk_target_chars,
                                    max_chars=cfg.chunk_max_chars)
        except Exception as exc:
            out["failures"].append({"path": path,
                                    "error": f"切块失败 {type(exc).__name__}: {exc}",
                                    "extractor": parser_name(parser)})
            continue

        rel = _relative_to_any(path, abs_roots)
        for chunk in chunks:
            haystack = indexed_projection(parsed.title, chunk.heading_path, chunk.text)
            scored = _score(q, haystack, expansions=expansions,
                            pinyin_query=pinyin_query)
            if scored is None:
                continue
            rank, why, sim = scored
            hits.append(LiveHit(
                path=path, rel_path=rel, title=parsed.title,
                heading_path=chunk.heading_path, page=chunk.page,
                ordinal=chunk.ordinal,
                snippet=_window(chunk.text, q, cfg.snippet_chars),
                rank=rank, why=why, similarity=sim,
            ))

        if deadline_s is not None and (time.time() - started) > deadline_s:
            out["truncated"] = True
            out["note"] = (f"超过时间上限 {deadline_s:.0f}s，提前停止"
                           f"（已扫描 {out['scanned']} 个文件）")
            break

    # 每个文件最多贡献 3 条，避免一篇长文档把别的全挤掉（与索引模式一致）
    per_file: dict[str, int] = {}
    kept: list[LiveHit] = []
    for hit in sorted(hits, key=lambda h: -h.rank):
        if per_file.get(hit.path, 0) >= cfg.max_chunks_per_doc:
            continue
        per_file[hit.path] = per_file.get(hit.path, 0) + 1
        kept.append(hit)
        if len(kept) >= limit:
            break

    out["hits"] = [h.to_dict() for h in kept]
    out["total_matched"] = len(hits)
    out["doc_count"] = len({h.path for h in kept})
    out["elapsed_s"] = round(time.time() - started, 2)
    if out["truncated"] and not out["note"]:
        cap = max_files
        out["note"] = (f"目录里的文件超过上限 {cap}，只搜了前 {cap} 个。"
                       f"要搜全，建议先建库：recall.py add <目录>")
    if not kept:
        out["note"] = out["note"] or (
            "没有命中。可以试：换一个说法；确认目录里确实有这类文件"
            "（只认 md/txt/html/docx/pptx/xlsx/pdf 等文本类格式）")
    return out
