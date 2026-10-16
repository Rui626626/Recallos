"""CJK-aware tokenisation for the secondary FTS index.

SQLite's `unicode61` tokenizer treats a whole run of Han characters as one
token, so `MATCH '复习'` finds nothing inside `课前复习资料整理`. The `trigram`
tokenizer fixes that for queries of 3+ characters but cannot match a 2-character
query at all.

The workaround, verified locally on SQLite 3.51, is a second index whose stored
text is split into per-character tokens for CJK and whole tokens for ASCII.
Queries are split the same way and sent as an FTS5 *phrase* so the characters
must be adjacent — without the quoting, `复 习` becomes `复 AND 习` and would
match documents containing those characters anywhere.
"""

from __future__ import annotations

import re

__all__ = ["cjk_segment", "as_phrase", "has_cjk", "longest_cjk_run", "estimate_tokens"]

# One CJK codepoint, or one ASCII/identifier word. Punctuation and whitespace
# are dropped, mirroring what unicode61 does with them anyway.
_TOKEN = re.compile(
    r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]"
    r"|[A-Za-z0-9_]+"
)
_CJK_CHAR = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]+")


def cjk_segment(text: str) -> str:
    """`课前复习abc` → `课 前 复 习 abc` (order preserved, punctuation dropped)."""
    return " ".join(_TOKEN.findall(text or ""))


def has_cjk(text: str) -> bool:
    return bool(_CJK_CHAR.search(text or ""))


def longest_cjk_run(text: str) -> int:
    """Longest contiguous CJK run. Drives the trigram-vs-unigram routing:
    trigram needs 3+ characters inside a single run."""
    runs = _CJK_RUN.findall(text or "")
    return max((len(r) for r in runs), default=0)


def as_phrase(text: str) -> str:
    """Quote an arbitrary user string as a literal FTS5 phrase.

    Everything the caller types is data, never query syntax — same policy DSH's
    own session search uses. Internal quotes are doubled per FTS5 rules.
    """
    return '"' + (text or "").replace('"', '""') + '"'


# A CJK codepoint costs roughly one token, Latin text roughly a quarter token
# per character. This is an estimate for *budgeting*, never for billing — it is
# deliberately conservative so a payload that passes the budget will not blow
# up on a real tokenizer.
_CJK_COST = 1.0
_OTHER_COST = 0.25


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    cjk = 0
    for ch in text:
        if _CJK_CHAR.match(ch) or "\u3040" <= ch <= "\u30ff" or "\uac00" <= ch <= "\ud7af":
            cjk += 1
    return int(cjk * _CJK_COST + (len(text) - cjk) * _OTHER_COST) + 1
