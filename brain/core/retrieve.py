"""检索编排：精确 → 别名扩展 → 模糊 OR → 相似度重排。

分成四段是刻意的。每一段都只在**上一段召回不足**时才跑，而且每一段都会说明自己
贡献了什么（`why` / `stages`），这样「为什么这条命中了」永远能回答，不会变成一个
黑盒打分。

    第 1 段  精确短语   ── 原样相邻出现，置信度最高
    第 2 段  别名扩展   ── 「菲利普斯」→「phillips」这类跨语言/跨叫法的等价
    第 3 段  模糊 OR    ── 拆成探针词，任一命中即召回，容错别字与少字多字
    第 4 段  相似度重排 ── 用重叠率把第 3 段召回里蹭分的丢掉

第 3 段的召回**必然**包含噪声（OR 的代价），所以第 4 段不是可选项：没有它，模糊
搜索就等于「把库里含任意一个字的切片全倒出来」。阈值是动态的，见 `fuzzy.min_coverage`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

from . import fuzzy
from .pinyin import indexed_projection, looks_like_pinyin, pinyin_projection
from .store import SearchHit, Store

__all__ = ["Ranked", "Retrieval", "retrieve"]


@dataclass
class Ranked:
    hit: SearchHit
    rank: float
    why: str
    similarity: float = 0.0
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = self.hit.to_dict()
        d["why"] = self.why
        d["rank"] = round(self.rank, 4)
        d["similarity"] = round(self.similarity, 4)
        return d


@dataclass
class Retrieval:
    query: str
    hits: list[Ranked] = field(default_factory=list)
    expanded: list[str] = field(default_factory=list)
    stages: dict = field(default_factory=dict)
    fuzzy_used: bool = False
    rejected: int = 0
    garbage: bool = False
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "query": self.query,
            "expanded": self.expanded,
            "stages": self.stages,
            "fuzzy_used": self.fuzzy_used,
            "rejected_by_threshold": self.rejected,
            "garbage": self.garbage,
            "note": self.note,
            "hits": [h.to_dict() for h in self.hits],
        }


#: 各段的基础置信度。精确 > 别名 > 模糊，模糊段再用相似度缩放。
_CONF_EXACT = 1.0
_CONF_ALIAS = 0.85


def _merge(bucket: dict[str, Ranked], hit: SearchHit, rank: float, why: str,
           similarity: float = 0.0, detail: Optional[dict] = None) -> None:
    """同一个切片被多段召回时保留置信度最高的那次。"""
    prev = bucket.get(hit.chunk_id)
    if prev is None or rank > prev.rank:
        bucket[hit.chunk_id] = Ranked(hit=hit, rank=rank, why=why,
                                      similarity=similarity, detail=detail or {})


def retrieve(store: Store, query: str, *, limit: int = 20,
             alias_map: Optional[dict] = None, fuzzy_enabled: bool = True,
             fuzzy_candidates: int = 90,
             exclude_paths: Sequence[str] = ()) -> Retrieval:
    """跑完整条检索流水线。`store.search` 的异常**照常抛出**——静默返回「没找到」
    会把 bug 伪装成一个看起来合理的答案。

    `exclude_paths` 是路径片段黑名单：`source_ref` 命中任何一段的切片都会被剔除。
    记忆的「过期」就是靠它实现的（过期记忆放在 `data/memory/_expired/` 下），
    这样不需要给索引加列，也不会碰 33KB 的导入管线。
    """
    q = fuzzy.normalize_query(query)
    out = Retrieval(query=q)

    if not q:
        out.note = "空查询"
        return out
    if fuzzy.looks_like_garbage(q):
        # 乱码/纯符号：必须返回空，绝不能靠模糊匹配硬凑出几条。
        out.garbage = True
        out.note = "查询里既没有汉字也没有拉丁字母，按乱码处理，返回空"
        return out

    fragments = tuple(f.replace("\\", "/") for f in (exclude_paths or ()) if f)

    def _excluded(hit: SearchHit) -> bool:
        if not fragments:
            return False
        ref = (hit.source_ref or "").replace("\\", "/")
        return any(frag in ref for frag in fragments)

    bucket: dict[str, Ranked] = {}

    # ---- 第 1 段：精确 ------------------------------------------------------
    exact = store.search(q, limit=limit)
    out.stages["exact"] = len(exact)
    for hit in exact:
        _merge(bucket, hit, _CONF_EXACT, "exact")

    # ---- 第 2 段：别名扩展 --------------------------------------------------
    alias_map = alias_map or {}
    expansions = fuzzy.expand_query(q, alias_map)
    out.expanded = expansions
    alias_hits = 0
    if expansions and (len(bucket) < limit or not bucket):
        for term in expansions:
            for hit in store.search(term, limit=limit):
                _merge(bucket, hit, _CONF_ALIAS, f"alias:{term}")
                alias_hits += 1
    out.stages["alias"] = alias_hits

    # ---- 第 3 段：模糊 OR ---------------------------------------------------
    probes = fuzzy.probe_terms(q)
    # 英文查询靠三元组容忍拼写错误；这里把「原词」也放进去，否则正确拼写反而
    # 只能靠三元组命中，排名会失真。
    probe_list: list[str] = []
    if probes["cjk"]:
        probe_list.extend(probes["cjk"])
    if probes["trigram"]:
        probe_list.extend(probes["trigram"])
    if probes["literal"] and len(probes["literal"]) >= 2:
        probe_list.append(probes["literal"])

    for term in expansions:
        sub = fuzzy.probe_terms(term)
        probe_list.extend(sub["cjk"] or [])
        probe_list.extend(sub["trigram"] or [])
        if len(term) >= 2:
            probe_list.append(term)

    # 拼音：把查询里的汉字转成拼音连写再探一次。
    # 「飞利浦斯」→ `feilipusi`，与索引里「菲利普斯」的拼音相同 → 同音字命中。
    # 用一个整串探针而不是「逐字展开同音字集合」：后者会组合爆炸，前者只要索引里
    # 有拼音投影就能一次对上。索引里没有拼音表时这里是空串，自动跳过。
    pinyin_query = False
    py_query = pinyin_projection(q)
    if py_query and len(py_query) >= 4:
        probe_list.append(py_query)
        pinyin_query = True
    elif looks_like_pinyin(q):
        # 用户直接敲了拼音：原串本身就是探针（`tonghuopengzhang` → 通货膨胀）。
        probe_list.append(q.lower())
        pinyin_query = True
    out.stages["pinyin"] = 1 if pinyin_query else 0

    fuzzy_hits = 0
    if fuzzy_enabled and len(bucket) < limit and probe_list:
        candidates = store.search_any(probe_list, limit=fuzzy_candidates)
        out.stages["fuzzy_candidates"] = len(candidates)
        out.fuzzy_used = True
        for hit in candidates:
            if hit.chunk_id in bucket:
                continue
            # **判定必须看索引里那份文本**（标题 + 标题路径 + 正文 + 拼音），
            # 而不是只看正文。否则靠拼音召回上来的候选会被阈值全部否掉。
            haystack = indexed_projection(hit.title, hit.heading_path,
                                          hit.text or hit.snippet or "")

            # 拼音命中要单独放行：同音字（飞利浦斯 / 菲利普斯）在**字面**上只有一半
            # 重合，字符覆盖率过不了阈值，但它们的拼音完全一致 —— 这正是拼音支持的
            # 意义所在。字面阈值不能用来否决拼音证据。
            py_hit = False
            if pinyin_query and fuzzy.cjk_chars(q):
                py_q = pinyin_projection(q)
                if py_q and py_q in pinyin_projection(haystack):
                    py_hit = True

            if not py_hit and not fuzzy.passes_threshold(q, haystack):
                out.rejected += 1
                continue

            sim = fuzzy.similarity(q, haystack)
            why = "fuzzy:pinyin" if py_hit else "fuzzy"
            # 拼音命中是强证据（整串相同），给一个不低于字面精确命中的置信度
            rank = max(sim, 0.9) if py_hit else sim
            _merge(bucket, hit, rank, why, similarity=sim,
                   detail=fuzzy.overlap_score(q, haystack))
            fuzzy_hits += 1
    out.stages["fuzzy"] = fuzzy_hits

    ranked = sorted(bucket.values(), key=lambda r: (-r.rank, r.hit.score))
    if fragments:
        kept = [r for r in ranked if not _excluded(r.hit)]
        out.stages["excluded_by_path"] = len(ranked) - len(kept)
        ranked = kept
    out.hits = ranked[:limit]
    out.stages["total"] = len(ranked)
    if not ranked:
        out.note = ("没有命中。可以试：换一个说法；把常见叫法写进 aliases.json；"
                    "确认相关文件已经用 `recall.py add` 导入过"
                    + ("；过期的记忆默认不返回，加 --include-expired 可以搜到"
                       if fragments else ""))
    return out
