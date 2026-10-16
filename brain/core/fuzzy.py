"""模糊匹配：别名扩展 + 字符/三元组重叠。纯本地，不需要任何模型或网络。

为什么需要它
------------
`Store.search` 走的是 FTS5 的**短语**匹配（trigram 与逐字两个索引），语义是
「必须原样相邻出现」。这对精确检索是对的，但下面三类**合理**的查询它会一律返回空：

1. **错别字**　　「菲力普斯」 vs 文档里的「菲利普斯」
2. **少字多字**　「菲利普斯曲线」 vs 文档里的「菲利普斯」
3. **中英互指**　「菲利普斯」 vs 只有英文标题 `the Phillips curve` 的笔记

前两类靠「把查询拆成探针词 → 任一命中即召回 → 再按重叠度重排」解决，**不需要
任何外部数据**。第三类**统计方法无解**：文档里根本没有那几个汉字，没有任何可以
匹配的东西，只能靠一张别名表。别名表支持手工维护，也会从文档里自动学习
`中文（English）` 这类成对写法。

边界（明确的取舍，不是 bug）
----------------------------
* **没有 embedding**：本机离线，也不打算把资料发出去。因此**同义词与上下位关系
  不在能力范围内**（「通胀」↔「物价上涨」这种需要外部知识的映射，只能写进别名表）。
* **不做分词**：中文按单字与二元组处理，不引入词典。
* 阈值是可调的常量，宁可少召回也不要召回一堆噪声。
"""

from __future__ import annotations

import json
import os
import re
from typing import Iterable, Optional, Sequence

__all__ = [
    "SEED_ALIASES", "ALIAS_FILE_VERSION",
    "cjk_chars", "cjk_runs", "latin_words", "latin_trigrams", "char_bigrams",
    "overlap_score", "similarity", "min_coverage",
    "normalize_query", "probe_terms", "alias_key",
    "load_aliases", "save_aliases", "expand_query", "learn_pairs",
    "merge_learned", "looks_like_garbage",
]

# --------------------------------------------------------------------------- 常量

ALIAS_FILE_VERSION = 1

#: 起始别名包。**只放「库里确实有、而且确实容易搜不到」的条目**，不做通用词典。
#: 这些是经济学/统计笔记里常见的英文标题 ↔ 中文叫法；用户可直接编辑 aliases.json。
SEED_ALIASES: dict[str, list[str]] = {
    "菲利普斯": ["phillips"],
    "菲利普斯曲线": ["phillips curve"],
    "基尼系数": ["gini", "gini coefficient"],
    "人类发展指数": ["hdi", "human development index"],
    "人力发展指数": ["hdi", "human development index"],
    "国内生产总值": ["gdp", "gross domestic product"],
    "消费价格指数": ["cpi", "consumer price index"],
    "通货膨胀": ["inflation"],
    "滞涨": ["stagflation"],
    "失业率": ["unemployment", "unemployment rate"],
    "折旧": ["depreciation"],
    "直接税": ["direct tax"],
    "间接税": ["indirect tax"],
}

_CJK_RANGES = (
    "\u3400-\u4dbf"      # 扩展 A
    "\u4e00-\u9fff"      # 基本区
    "\uf900-\ufaff"      # 兼容表意
)
_CJK_CHAR = re.compile(f"[{_CJK_RANGES}]")
_CJK_RUN = re.compile(f"[{_CJK_RANGES}]+")
_LATIN_WORD = re.compile(r"[A-Za-z][A-Za-z0-9'’\-]*")

#: 短语匹配失败后允许的最低「查询覆盖率」。查询越短要求越严 —— 两个字里只中一个字
#: 的召回几乎必然是噪声。
_COVERAGE_LADDER = ((2, 1.0), (4, 0.66), (8, 0.5))
_COVERAGE_FLOOR = 0.4

#: 相似度阈值，低于它直接丢弃（宁可少召回）。
FUZZY_MIN_SCORE = 0.34

#: 别名键长度上限，避免把整句话塞进别名表。
_ALIAS_KEY_MAX = 24


# --------------------------------------------------------------------- 基础切分


def cjk_chars(text: str) -> list[str]:
    """按出现顺序返回所有汉字（含重复）。"""
    return _CJK_CHAR.findall(text or "")


def cjk_runs(text: str) -> list[str]:
    """连续的汉字串，例如 `复习abc资料` → `['复习', '资料']`。"""
    return _CJK_RUN.findall(text or "")


def latin_words(text: str) -> list[str]:
    """拉丁词，全部小写（检索用不到大小写区别）。"""
    return [w.lower() for w in _LATIN_WORD.findall(text or "")]


def latin_trigrams(text: str) -> set[str]:
    """拉丁词的三元组，用于容忍拼写错误。短于 3 的词返回自身。"""
    out: set[str] = set()
    for w in latin_words(text):
        if len(w) < 3:
            out.add(w)
            continue
        out.update(w[i:i + 3] for i in range(len(w) - 2))
    return out


def char_bigrams(text: str) -> set[str]:
    """汉字二元组；单个汉字返回自身。"""
    out: set[str] = set()
    for run in cjk_runs(text):
        if len(run) < 2:
            out.add(run)
            continue
        out.update(run[i:i + 2] for i in range(len(run) - 1))
    return out


# --------------------------------------------------------------------- 相似度


def _coverage(query_units: set, haystack_units: set) -> float:
    """查询有多少比例被命中。文档比查询长得多，所以覆盖率比 Jaccard 更适用。"""
    if not query_units:
        return 0.0
    return len(query_units & haystack_units) / len(query_units)


def min_coverage(n_units: int) -> float:
    """按查询规模取动态阈值：短查询要求几乎全中，长查询可以宽松些。"""
    for limit, need in _COVERAGE_LADDER:
        if n_units <= limit:
            return need
    return _COVERAGE_FLOOR


def overlap_score(query: str, text: str) -> dict:
    """把中英两侧的重叠算清楚，返回明细（便于在输出里解释「为什么命中」）。

    **为什么用「相对查询的覆盖率」而不是 Jaccard。** 文档比查询长一两个数量级，
    Jaccard 的分母被文档的 n-gram 数量主导：一个完全精确的匹配（查询原样出现在
    文档里）也只能拿到 0.76，而「查询的字散落在文档各处」反而能蹭到 0.7。两种
    完全不同的情况拿到几乎一样的分，排序就失去意义了。

    改成两个分子分母都只跟查询有关的量：

    * 单字覆盖率 —— 容忍错别字（「通或膨胀」仍然覆盖 3/4）
    * 二元组覆盖率 —— 保证相邻性（散落的字覆盖不了二元组）

    精确匹配 = 1.0，一个字写错 ≈ 0.5，散落的字 ≈ 0.5。区分度回来了。
    """
    q_chars = set(cjk_chars(query))
    q_bigrams = char_bigrams(query)
    q_trigrams = latin_trigrams(query)

    t_chars = set(cjk_chars(text))
    t_bigrams = char_bigrams(text)
    t_trigrams = latin_trigrams(text)

    parts: list[float] = []
    detail: dict = {}

    if q_chars:
        char_cov = _coverage(q_chars, t_chars)
        bigram_cov = _coverage(q_bigrams, t_bigrams)
        cjk_score = char_cov * 0.6 + bigram_cov * 0.4
        parts.append(cjk_score)
        detail["cjk"] = {
            "char_coverage": round(char_cov, 4),
            "bigram_coverage": round(bigram_cov, 4),
            "required_coverage": min_coverage(len(q_chars)),
            "units": len(q_chars),
        }

    if q_trigrams:
        tri_cov = _coverage(q_trigrams, t_trigrams)
        parts.append(tri_cov)
        detail["latin"] = {
            "trigram_coverage": round(tri_cov, 4),
            "units": len(q_trigrams),
        }

    detail["score"] = round(max(parts) if parts else 0.0, 4)
    return detail


def similarity(query: str, text: str) -> float:
    """0..1 的综合相似度。取中/英两侧更高的那个（查询通常只属于一侧）。"""
    return float(overlap_score(query, text)["score"])


def passes_threshold(query: str, text: str) -> bool:
    """动态阈值判定：覆盖率与综合分**都要**过线，避免长文档蹭分。"""
    info = overlap_score(query, text)
    if info["score"] < FUZZY_MIN_SCORE:
        return False
    cjk = info.get("cjk")
    if cjk and cjk["char_coverage"] < cjk["required_coverage"]:
        return False
    latin = info.get("latin")
    if latin and latin["trigram_coverage"] < 0.5:
        return False
    return True


# --------------------------------------------------------------------- 查询处理


def normalize_query(query: str) -> str:
    """去掉多余空白。大小写交由 FTS5 的 unicode61/trigram 处理（都不区分大小写）。"""
    return re.sub(r"\s+", " ", (query or "").strip())


def looks_like_garbage(query: str) -> bool:
    """查询里**一个可搜索字符都没有**（既无汉字也无拉丁字母）。

    这类查询必须直接返回空，而不是靠模糊匹配撞出一堆东西。

    注意它**不等于**「查不到」：`asdfghjkl` 是合法的拉丁词，只是库里没有，走正常
    流程返回空结果。两者对用户的含义不同——前者是「你打错符号了」，后者是
    「库里确实没有」，所以 `--json` 里是两个不同的字段。
    """
    q = query or ""
    return not cjk_chars(q) and not latin_words(q)


def probe_terms(query: str) -> dict:
    """把查询拆成可以放进 FTS 的探针词。

    `cjk` 是单字（逐字索引只认单字），`trigram` 是拉丁词的三元组（容忍拼写错误），
    `literal` 是原串（精确优先）。
    """
    q = normalize_query(query)
    chars: list[str] = []
    seen: set[str] = set()
    for ch in cjk_chars(q):
        if ch not in seen:
            seen.add(ch)
            chars.append(ch)
    return {
        "literal": q,
        "cjk": chars,
        "trigram": sorted(latin_trigrams(q)),
        "words": sorted(set(latin_words(q))),
    }


def alias_key(text: str) -> str:
    """别名表的键一律小写去空白，中英通用。"""
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


# --------------------------------------------------------------------- 别名表


def _aliases_path(cfg) -> str:
    return getattr(cfg, "aliases_path", os.path.join(cfg.root_dir, "aliases.json"))


def empty_alias_doc() -> dict:
    return {
        "version": ALIAS_FILE_VERSION,
        "note": ("把「你平时怎么叫」映射到「文档里怎么写」。两边随便哪边放中文或英文都行。"
                 "示例：{\"菲利普斯\": [\"phillips\", \"phillips curve\"]}"),
        "aliases": {},
        "learned": {},
    }


def load_aliases(cfg) -> dict:
    """读别名表。文件不存在时返回种子集（保证 clone 下来立刻可用）。"""
    path = _aliases_path(cfg)
    doc = empty_alias_doc()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            if isinstance(raw, dict):
                doc.update({k: raw[k] for k in ("version", "note") if k in raw})
                doc["aliases"] = dict(raw.get("aliases") or {})
                doc["learned"] = dict(raw.get("learned") or {})
        except (OSError, json.JSONDecodeError):
            # 文件坏了不能让检索整体失败：退回种子集，用户下次 save 会覆盖。
            pass
    merged = {alias_key(k): list(v) for k, v in SEED_ALIASES.items()}
    for k, v in (doc.get("aliases") or {}).items():
        merged[alias_key(k)] = [str(x) for x in (v or [])]
    doc["_merged"] = merged
    return doc


def save_aliases(cfg, doc: dict) -> str:
    path = _aliases_path(cfg)
    payload = {
        "version": ALIAS_FILE_VERSION,
        "note": doc.get("note") or empty_alias_doc()["note"],
        "aliases": doc.get("aliases") or {},
        "learned": doc.get("learned") or {},
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    return path


def expand_query(query: str, merged: dict) -> list[str]:
    """把查询扩展成若干等价写法。

    支持两个方向：命中别名键 → 追加它的所有值；命中某个值 → 追加它的键。
    命中的键越具体（越长）越好，所以按长度倒序尝试。
    """
    q = normalize_query(query)
    if not q:
        return []
    key = alias_key(q)
    out: list[str] = []

    def add(value: str) -> None:
        v = normalize_query(value)
        if v and alias_key(v) != key and v not in out:
            out.append(v)

    for k in sorted(merged, key=len, reverse=True):
        values = [alias_key(x) for x in (merged.get(k) or [])]
        if key == k:
            for v in (merged.get(k) or []):
                add(v)
            break
        if key in values:
            add(k)
        # 两边都命中时还要把该键的其他写法也带上（如 phillips → 菲利普斯曲线）
        if key == k or key in values:
            for v in (merged.get(k) or []):
                add(v)
    return out


_PAIR_PATTERNS = (
    # 中文（English）  /  中文(English)
    re.compile(rf"([{_CJK_RANGES}]{{2,{_ALIAS_KEY_MAX}}})\s*[（(]\s*"
               r"([A-Za-z][A-Za-z0-9 \-'’\.]{2,40}?)\s*[)）]"),
    # English（中文）
    re.compile(rf"([A-Za-z][A-Za-z0-9 \-'’\.]{{2,40}}?)\s*[（(]\s*"
               rf"([{_CJK_RANGES}]{{2,{_ALIAS_KEY_MAX}}})\s*[)）]"),
)


def learn_pairs(text: str) -> dict[str, list[str]]:
    """从正文里学 `中文（English）` 这类成对写法。

    只学**同一个括号里紧挨着**的对照，不做任何猜测——那种「同段落共现」的统计
    学习会造出大量错别名，宁可学得少。
    """
    found: dict[str, list[str]] = {}
    for pat in _PAIR_PATTERNS:
        for m in pat.finditer(text or ""):
            a, b = normalize_query(m.group(1)), normalize_query(m.group(2))
            if not a or not b or alias_key(a) == alias_key(b):
                continue
            # 统一朝向：键放中文，值放英文
            zh, en = (a, b) if cjk_chars(a) and not cjk_chars(b) else (b, a)
            if not cjk_chars(zh) or cjk_chars(en):
                continue
            if len(zh) > _ALIAS_KEY_MAX:
                continue
            bucket = found.setdefault(zh, [])
            low = alias_key(en)
            if low not in [alias_key(x) for x in bucket]:
                bucket.append(en)
    return found


def merge_learned(doc: dict, pairs: dict[str, list[str]]) -> int:
    """把学到的对照并进 `learned` 段，返回新增条数。**不动用户手写的 aliases。**"""
    learned = doc.setdefault("learned", {})
    added = 0
    for zh, values in (pairs or {}).items():
        bucket = learned.setdefault(zh, [])
        have = {alias_key(x) for x in bucket}
        for v in values:
            if alias_key(v) not in have:
                bucket.append(v)
                have.add(alias_key(v))
                added += 1
    return added


def rebuild_merged(doc: dict) -> dict:
    """把 seed + 手写 + 学到 合成最终的查询用映射。"""
    merged: dict[str, list[str]] = {alias_key(k): list(v) for k, v in SEED_ALIASES.items()}
    for section in ("aliases", "learned"):
        for k, v in (doc.get(section) or {}).items():
            merged.setdefault(alias_key(k), [])
            have = {alias_key(x) for x in merged[alias_key(k)]}
            for item in (v or []):
                if alias_key(item) not in have:
                    merged[alias_key(k)].append(item)
                    have.add(alias_key(item))
    doc["_merged"] = merged
    return merged
