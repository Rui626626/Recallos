"""Text normalisation — the input to `content_hash`.

The rule this module exists to enforce: **two texts that a reader would call
the same must hash the same.** Line endings, trailing spaces, zero-width
characters and runs of blank lines differ for reasons that have nothing to do
with the meaning of a document, so they are removed before hashing.

Two consequences worth being explicit about:

* `content_hash` is `None` for text with no content. A hash of the empty
  string would be *identical for every empty document*, which would make all of
  them look like duplicates of each other. `None` means "never compare this".
* The hash input is prefixed with `NORM_VERSION`. When the rules below change,
  every previously stored hash becomes incomparable — prefixing the version
  makes that detectable instead of silently wrong.
"""

from __future__ import annotations

import hashlib
import re

from .versions import NORM_VERSION

__all__ = ["normalize_text", "content_hash", "normalize_for_match"]

# Zero-width and BOM characters: invisible, and they break exact matching.
_INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"), None)
_BLANK_RUN = re.compile(r"\n{3,}")
_SPACE_RUN = re.compile(r"[ \t]{2,}")


def normalize_text(text: str) -> str:
    """Canonical form used for hashing and for chunk text.

    Deliberately conservative: no case folding, no Unicode NFKC, no punctuation
    rewriting. Those would make genuinely different documents collide, and
    `content_hash` decides whether a document is "the same material".
    """
    if not text:
        return ""
    t = text.replace("\r\n", "\n").replace("\r", "\n")
    t = t.translate(_INVISIBLE)
    t = t.replace("\u00a0", " ").replace("\u3000", " ")
    # Strip per-line trailing whitespace (editors add it; it is not content).
    t = "\n".join(line.rstrip() for line in t.split("\n"))
    t = _BLANK_RUN.sub("\n\n", t)
    return t.strip()


def normalize_for_match(text: str) -> str:
    """Stronger normalisation, used only for *detecting near-identical chunks*.

    Never used for `content_hash` — collapsing whitespace would merge documents
    that differ only in spacing, and that difference can be meaningful in code
    and tables.
    """
    return _SPACE_RUN.sub(" ", normalize_text(text))


def content_hash(text: str) -> str | None:
    """sha256 over the version-prefixed normalised text.

    Returns `None` when there is nothing to hash. Callers must treat `None` as
    "this document never participates in duplicate detection".
    """
    normalized = normalize_text(text)
    if not normalized:
        return None
    payload = f"{NORM_VERSION}\x00{normalized}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
