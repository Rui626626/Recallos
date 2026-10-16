"""Source references and the canonical locator grammar.

A locator is the one string that ties a memory card or a generated output back
to the exact place in the exact document it came from. It is deliberately
**content-addressed**, because a positional locator (`#chunk=5`) stops meaning
what it meant as soon as the document is edited.

    doc:<doc_id>#h=<chunk_hash[:10]>          the canonical, stable form
    doc:<doc_id>#h=<hash>@<ordinal>           same chunk, with the readable position
    doc:<doc_id>#page=<n>                     a page (PDFs)
    doc:<doc_id>#ts=<start>-<end>             a time range (audio/video)
    session:<session_id>#evt=<seq>            a DSH session event
    url:<url>                                 a web source

`parse_locator()` returns `None` for anything it does not recognise. Callers
must treat that as an orphan — never as a silent success.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

__all__ = [
    "SourceRef", "FILE", "URL", "SESSION", "TEXT", "NOTE", "KINDS",
    "make_file_source", "locator_chunk", "locator_page", "locator_ts",
    "locator_session", "parse_locator",
]

FILE = "file"
URL = "url"
SESSION = "session"
TEXT = "text"
NOTE = "note"

KINDS = (FILE, URL, SESSION, TEXT, NOTE)


@dataclass(frozen=True)
class SourceRef:
    kind: str
    ref: str

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"unknown source kind: {self.kind!r}")

    @property
    def display(self) -> str:
        if self.kind == FILE:
            return os.path.basename(self.ref) or self.ref
        return self.ref

    def __str__(self) -> str:
        return f"{self.kind}:{self.ref}"


def make_file_source(path: str) -> SourceRef:
    """Absolute path: the process working directory is not part of identity."""
    return SourceRef(FILE, os.path.abspath(path))


def locator_chunk(doc_id: str, chunk_hash: str, ordinal: Optional[int] = None) -> str:
    short = (chunk_hash or "")[:10]
    if ordinal is None:
        return f"doc:{doc_id}#h={short}"
    return f"doc:{doc_id}#h={short}@{ordinal}"


def locator_page(doc_id: str, page: int) -> str:
    return f"doc:{doc_id}#page={page}"


def locator_ts(doc_id: str, start: float, end: float) -> str:
    return f"doc:{doc_id}#ts={start:.3f}-{end:.3f}"


def locator_session(session_id: str, seq: int) -> str:
    return f"session:{session_id}#evt={seq}"


def parse_locator(locator: str) -> Optional[dict]:
    """Parse the grammar above. `None` means unrecognised."""
    if not locator or ":" not in locator:
        return None
    head, _, tail = locator.partition(":")

    if head == "doc":
        doc, _, frag = tail.partition("#")
        if not doc:
            return None
        out: dict = {"kind": "doc", "doc_id": doc}
        if not frag:
            return out
        key, _, value = frag.partition("=")
        if key == "h" and value:
            ordinal = None
            if "@" in value:
                value, _, raw = value.partition("@")
                if raw.isdigit():
                    ordinal = int(raw)
            out["chunk_hash"] = value
            if ordinal is not None:
                out["ordinal"] = ordinal
            return out
        if key == "page" and value.isdigit():
            out["page"] = int(value)
            return out
        if key == "ts" and "-" in value:
            a, _, b = value.partition("-")
            try:
                out["ts"] = (float(a), float(b))
            except ValueError:
                return None
            return out
        return None

    if head == "session":
        sid, _, frag = tail.partition("#")
        if not sid:
            return None
        if not frag:
            return {"kind": "session", "session_id": sid}
        key, _, value = frag.partition("=")
        if key == "evt" and value.isdigit():
            return {"kind": "session", "session_id": sid, "evt": int(value)}
        return None

    if head == "url" and tail:
        return {"kind": "url", "url": tail}

    return None
