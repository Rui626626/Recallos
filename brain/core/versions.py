"""Version stamps for everything that can change what gets indexed.

Any of these changing means previously stored hashes are no longer comparable,
so the affected documents must be re-processed. Keeping them in one module
makes that rule easy to audit: if a constant here changes, the pipeline
automatically bypasses the "unchanged" shortcuts.

Naming: `n` = normalisation, `k` = chunking, `x` = extraction.
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "NORM_VERSION", "CHUNKER_ALGO", "INDEX_CONTENT_VERSION",
    "chunker_version", "extractor_version", "EXTRACTORS",
]

# Bump when normalize_text() changes in any way that alters its output.
NORM_VERSION: Final[str] = "n1"

# Bump when chunk_segments() changes its boundaries or ordering.
CHUNKER_ALGO: Final[str] = "k1"

# Bump when **what gets written into the FTS tables** changes.
#   ch1: 切片正文
#   ch2: 文档标题 + 标题路径 + 切片正文
#   ch3: 上述内容 + 汉字拼音连写（拼音搜索与同音字容错）
# 从 ch1 到 ch2 修的是一个实测发现的盲区：标题和小标题都不在正文里，只索引正文
# 就会出现「搜文档自己的标题返回空」。老的索引必须重建，靠这个戳自动触发。
INDEX_CONTENT_VERSION: Final[str] = "ch3"

# Per-extractor version. Bump for one format when its parser starts producing
# different text — that is what makes "upgrade the parser, re-index only the
# affected documents" possible instead of re-indexing everything.
EXTRACTORS: Final[dict[str, str]] = {
    "markdown": "1",
    "text": "1",
    "html": "1",
    "docx": "1",
    "pptx": "1",
    "xlsx": "1",
    "pdf": "1",
}


def chunker_version(target_chars: int, max_chars: int) -> str:
    """Fingerprint of the chunking *algorithm, its parameters, and the FTS projection*.

    Parameters belong in here: changing `target_chars` in the config must
    invalidate every stored chunk, and without this the pipeline would report
    "nothing changed" and leave the index silently stale.

    `INDEX_CONTENT_VERSION` belongs here for exactly the same reason — it decides
    what a chunk row projects into the search index, so changing it must rewrite
    every stored chunk.
    """
    return f"{CHUNKER_ALGO}:{INDEX_CONTENT_VERSION}:{target_chars}:{max_chars}"


def extractor_version(extractor: str) -> str:
    """Version for an extractor name like `pdf:pypdf` → keyed on `pdf`."""
    base = (extractor or "").split(":", 1)[0]
    return EXTRACTORS.get(base, "0")
