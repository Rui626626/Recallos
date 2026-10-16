"""Chinese-aware chunking.

Design choices that matter later:

* **No fixed-width window by default.** Chunks break on paragraph boundaries,
  falling back to sentence boundaries for an oversized paragraph, and only to a
  hard character window when even a single sentence does not fit. That last
  fallback matters: a 200 KB file with no punctuation would otherwise become one
  gigantic chunk and blow the retrieval context budget on its own.
* **Deterministic.** Given the same segments and parameters, chunk text, order
  and hashes are identical — this is what lets a rebuild reproduce identical
  chunk ids.
* **Content-addressed identity.** A chunk carries `chunk_hash` (of its
  normalised text) and `occurrence` (which duplicate of that text it is). Its id
  is derived from those, never from its position, so inserting a paragraph does
  not renumber everything below it.
* **No overlap.** Deliberate: overlapping windows make `char_start/char_end`
  non-monotonic and complicate offset-based citations, for a retrieval gain that
  paragraph-aligned chunks mostly already provide. Add it behind a config flag
  once there is evidence it helps.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

from .normalize import content_hash as _normalized_hash

__all__ = [
    "Segment", "Chunk", "chunk_segments", "split_paragraphs", "split_sentences",
    "TARGET_CHARS", "MAX_CHARS",
]

TARGET_CHARS = 900
MAX_CHARS = 1600

_PARA_SPLIT = re.compile(r"\n\s*\n+")
# CJK full stops, ASCII sentence enders, and newlines all terminate a sentence.
_SENT_SPLIT = re.compile(r"(?<=[。！？!?；;])|(?<=\.)\s+|\n+")


@dataclass
class Segment:
    """One contiguous run of text from a parser, with whatever locality it knows."""

    text: str
    heading_path: str = ""
    page: Optional[int] = None
    ts_start: Optional[float] = None
    ts_end: Optional[float] = None


@dataclass
class Chunk:
    ordinal: int
    text: str
    heading_path: str = ""
    page: Optional[int] = None
    ts_start: Optional[float] = None
    ts_end: Optional[float] = None
    char_start: int = 0
    char_end: int = 0
    chunk_hash: str = ""
    occurrence: int = 0

    @property
    def locator_suffix(self) -> str:
        """`<hash10>@<ordinal>` — what goes after `doc:<id>#h=`."""
        return f"{self.chunk_hash[:10]}@{self.ordinal}"

    def finalize(self) -> "Chunk":
        self.text = self.text.strip()
        self.chunk_hash = _normalized_hash(self.text) or ""
        return self


@dataclass
class _Unit:
    """A paragraph, sentence or window with its offset in the document."""

    text: str
    heading_path: str = ""
    page: Optional[int] = None
    ts_start: Optional[float] = None
    ts_end: Optional[float] = None
    offset: int = 0


def split_paragraphs(text: str) -> list[str]:
    if not text or not text.strip():
        return []
    return [p.strip() for p in _PARA_SPLIT.split(text) if p and p.strip()]


def split_sentences(text: str) -> list[str]:
    if not text:
        return []
    parts = [p.strip() for p in _SENT_SPLIT.split(text) if p and p.strip()]
    return parts or [text.strip()]


def _hard_split(text: str, limit: int) -> list[str]:
    """Last-resort windowing. Only reached when one sentence exceeds `limit`."""
    if limit <= 0:
        return [text]
    return [text[i:i + limit] for i in range(0, len(text), limit)]


def _explode(seg: Segment, max_chars: int) -> list[_Unit]:
    """Segment → units, shrinking the split granularity until everything fits."""
    units: list[_Unit] = []
    base = 0

    def emit(piece: str) -> None:
        nonlocal base
        if not piece or not piece.strip():
            return
        units.append(
            _Unit(piece, seg.heading_path, seg.page, seg.ts_start, seg.ts_end, base)
        )
        base += len(piece) + 1

    for para in split_paragraphs(seg.text):
        if len(para) <= max_chars:
            emit(para)
            continue
        for sentence in split_sentences(para):
            if len(sentence) <= max_chars:
                emit(sentence)
                continue
            for window in _hard_split(sentence, max_chars):
                emit(window)
    return units


def chunk_segments(
    segments: Sequence[Segment],
    target_chars: int = TARGET_CHARS,
    max_chars: int = MAX_CHARS,
) -> list[Chunk]:
    """Pack units into chunks, flushing on size or locality change."""
    units: list[_Unit] = []
    for seg in segments:
        units.extend(_explode(seg, max_chars))
    if not units:
        return []

    chunks: list[Chunk] = []
    buf: list[_Unit] = []
    buf_len = 0
    buf_key: Optional[tuple] = None

    def flush() -> None:
        nonlocal buf, buf_len, buf_key
        if not buf:
            return
        first, last = buf[0], buf[-1]
        chunk = Chunk(
            ordinal=len(chunks),
            text="\n\n".join(u.text for u in buf),
            heading_path=first.heading_path,
            page=first.page,
            ts_start=first.ts_start,
            ts_end=last.ts_end,
            char_start=first.offset,
            char_end=last.offset + len(last.text),
        ).finalize()
        if chunk.chunk_hash:
            chunks.append(chunk)
        buf = []
        buf_len = 0
        buf_key = None

    for unit in units:
        key = (unit.heading_path, unit.page)
        if buf and buf_key != key:
            flush()
        if buf and buf_len + len(unit.text) > target_chars:
            flush()
        if not buf:
            buf_key = key
        buf.append(unit)
        buf_len += len(unit.text)
    flush()

    _assign_occurrences(chunks)
    return chunks


def _assign_occurrences(chunks: list[Chunk]) -> None:
    """Number repeated identical texts within one document.

    `occurrence` is what keeps the id unique when a document legitimately
    contains the same paragraph twice, without reintroducing a positional
    component.
    """
    seen: dict[str, int] = {}
    for chunk in chunks:
        n = seen.get(chunk.chunk_hash, 0)
        chunk.occurrence = n
        seen[chunk.chunk_hash] = n + 1


def chunk_lengths(chunks: Iterable[Chunk]) -> list[int]:
    return [len(c.text) for c in chunks]
