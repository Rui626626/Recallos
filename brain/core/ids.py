"""Stable identifiers.

Two rules, and everything else follows from them:

1. **A document id is derived from its source, never from insertion order.**
   The same file always gets the same id, so re-running an import cannot create
   a second record for it.

2. **A chunk id is derived from its text, never from its position.**
   `doc_id + chunk_hash + occurrence`. Inserting a paragraph at the top of a
   document must not change the identity of the paragraphs below it — otherwise
   every stored citation (`doc:x#h=…`) silently starts pointing at different
   words.

   `occurrence` disambiguates a text that legitimately appears more than once in
   one document.

Ids are truncated to 48 bits. For a personal corpus that is far beyond the
birthday bound (~16.7 M items), and `store.integrity()` checks for a collision
rather than assuming one cannot happen.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

__all__ = ["short_hash", "source_key", "doc_id", "chunk_id", "normalize_ref", "slugify"]

_SEP = "\x00"  # cannot occur in a path or a hash
ID_LEN = 12


def short_hash(*parts: str, length: int = ID_LEN) -> str:
    h = hashlib.sha1()
    for i, part in enumerate(parts):
        if i:
            h.update(_SEP.encode("utf-8"))
        h.update((part or "").encode("utf-8"))
    return h.hexdigest()[:length]


def normalize_ref(ref: str) -> str:
    """Comparison form of a source reference.

    On Windows, paths are case-insensitive and `/` and `\\` are interchangeable,
    so both spellings must map to one key. The **original** string is stored
    alongside and is what gets displayed — normalisation is only for identity.
    """
    r = (ref or "").strip().replace("\\", "/")
    if _is_windows_path(r):
        r = r.casefold()
        if len(r) > 3 and r.endswith("/"):
            r = r.rstrip("/")
    return r


def _is_windows_path(r: str) -> bool:
    return bool(re.match(r"^[a-zA-Z]:/", r)) or r.startswith("//")


def source_key(source_kind: str, source_ref: str) -> str:
    return f"{source_kind}\x1f{normalize_ref(source_ref)}"


def doc_id(source_kind: str, source_ref: str) -> str:
    """Stable id for one tracked source. Depends only on kind + normalised ref."""
    return "d" + short_hash(source_kind, normalize_ref(source_ref))


def chunk_id(doc: str, chunk_hash: str, occurrence: int = 0) -> str:
    """Content-addressed chunk id. Position is deliberately absent."""
    return "c" + short_hash(doc, chunk_hash, str(occurrence))


_SLUG_STRIP = re.compile(r"[^\w\u4e00-\u9fff]+", re.UNICODE)


def slugify(text: str, max_len: int = 40) -> str:
    """Readable, filesystem-safe slug that keeps CJK characters intact."""
    t = unicodedata.normalize("NFKC", text or "").strip().lower()
    t = _SLUG_STRIP.sub("-", t).strip("-")
    return (t[:max_len].rstrip("-")) or "untitled"
