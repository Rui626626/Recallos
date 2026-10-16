"""Parser contract and registry.

A parser turns one file into a `ParsedDoc`: a title plus a list of `Segment`s.
It never touches the database and never writes to disk, which keeps every parser
trivially testable and keeps the pipeline's failure handling in one place.

Two rules:

* Raise `ParseError` for anything expected — a corrupt package, an unreadable
  encoding, a scanned PDF with no text layer. The pipeline records the reason.
* Anything else escaping a parser is a bug and is recorded as such, but must not
  abort the surrounding batch.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, Optional

from ..core.chunk import Segment

__all__ = ["ParsedDoc", "ParseError", "register", "parser_for",
           "supported_extensions", "read_text_file", "parser_name"]


class ParseError(Exception):
    """Raised when a file cannot be turned into text, with a human reason."""


@dataclass
class ParsedDoc:
    title: str
    segments: list[Segment] = field(default_factory=list)
    extractor: str = ""
    meta: dict = field(default_factory=dict)

    @property
    def text(self) -> str:
        return "\n\n".join(s.text for s in self.segments)

    @property
    def char_count(self) -> int:
        return sum(len(s.text) for s in self.segments)

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()


_REGISTRY: dict[str, Callable[[str], ParsedDoc]] = {}


def register(*extensions: str) -> Callable:
    def deco(fn: Callable[[str], ParsedDoc]) -> Callable[[str], ParsedDoc]:
        for ext in extensions:
            key = ext.lower() if ext.startswith(".") else "." + ext.lower()
            _REGISTRY[key] = fn
        return fn

    return deco


def parser_for(path: str) -> Optional[Callable[[str], ParsedDoc]]:
    return _REGISTRY.get(os.path.splitext(path)[1].lower())


def parser_name(fn: Optional[Callable]) -> str:
    """Extractor key for a parser function, e.g. `markdown`, `pdf`."""
    if fn is None:
        return ""
    return getattr(fn, "__name__", "unknown").removeprefix("parse_")


def supported_extensions() -> list[str]:
    return sorted(_REGISTRY)


# Chinese documents on this machine mix UTF-8 and GBK. `utf-8-sig` must precede
# `utf-8` so a BOM is consumed rather than kept as a stray character; `latin-1`
# is a total function over byte strings, so the chain always terminates.
_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "big5", "cp1252", "latin-1")


def read_text_file(path: str, limit_bytes: Optional[int] = None) -> tuple[str, str]:
    """Return `(text, encoding)`. Never raises on unexpected bytes."""
    with open(path, "rb") as fh:
        raw = fh.read(limit_bytes) if limit_bytes else fh.read()
    for enc in _ENCODINGS:
        try:
            return raw.decode(enc), enc
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("latin-1", errors="replace"), "latin-1"
