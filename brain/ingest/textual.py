"""Plain text, Markdown and HTML.

Markdown is treated as a format rather than flat text: heading levels are
tracked so every segment carries a `heading_path`. That path is what makes a
retrieved snippet readable in isolation ("第二次课 › 反向传播") without loading
the whole document.

A parser raises `ParseError` only when there is genuinely nothing readable. An
empty file is a caller-level decision — the pipeline turns it into a
`content_hash` of `None` rather than treating it as a parse failure.
"""

from __future__ import annotations

import os
import re
from html.parser import HTMLParser

from ..core.chunk import Segment, split_paragraphs
from .base import ParseError, ParsedDoc, read_text_file, register

__all__ = []

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FRONTMATTER = re.compile(r"\A---[ \t]*\n.*?\n---[ \t]*\n", re.DOTALL)
_FENCE = re.compile(r"^\s*(```|~~~)")

_BLOCK_TAGS = {
    "p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
    "section", "article", "header", "footer", "blockquote", "pre", "table",
    "ul", "ol", "dl", "dt", "dd", "hr", "figcaption",
}
_SKIP_TAGS = {"script", "style", "noscript", "template", "svg"}


@register(".txt", ".text", ".log")
def parse_text(path: str) -> ParsedDoc:
    raw, enc = read_text_file(path)
    segments = [Segment(p) for p in split_paragraphs(_FRONTMATTER.sub("", raw))]
    return ParsedDoc(
        title=os.path.splitext(os.path.basename(path))[0],
        segments=segments,
        extractor="text",
        meta={"encoding": enc},
    )


@register(".md", ".markdown", ".mdx")
def parse_markdown(path: str) -> ParsedDoc:
    raw, enc = read_text_file(path)
    body = _FRONTMATTER.sub("", raw)
    stem = os.path.splitext(os.path.basename(path))[0]

    stack: list[tuple[int, str]] = []
    segments: list[Segment] = []
    buffer: list[str] = []
    in_fence = False

    def heading_path() -> str:
        return " › ".join(h for _, h in stack)

    def flush() -> None:
        if not buffer:
            return
        block = "\n".join(buffer).strip()
        buffer.clear()
        if not block:
            return
        for piece in split_paragraphs(block):
            segments.append(Segment(piece, heading_path=heading_path()))

    for line in body.splitlines():
        if _FENCE.match(line):
            # Fenced code stays as data; only its content is preserved verbatim
            # so that a code sample remains searchable and citable.
            in_fence = not in_fence
            buffer.append(line)
            continue
        match = None if in_fence else _HEADING.match(line)
        if match:
            flush()
            level = len(match.group(1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, match.group(2).strip()))
            continue
        if not in_fence and not line.strip():
            flush()
            continue
        buffer.append(line)
    flush()

    title = stack[0][1] if stack and stack[0][0] == 1 else stem
    return ParsedDoc(title=title, segments=segments, extractor="markdown",
                     meta={"encoding": enc})


class _TextExtractor(HTMLParser):
    """Minimal, dependency-free HTML → text.

    Block-level tags become paragraph breaks, headings become `## ` lines so the
    markdown sectioning above can be reused, and script/style content is
    dropped. Enough for saved articles and app-generated pages; not a browser.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._skip = 0
        self._title: list[str] = []
        self._in_title = False
        self._heading: list[str] | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip += 1
            return
        if self._skip:
            return
        if tag == "title":
            self._in_title = True
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._heading = []
        elif tag in _BLOCK_TAGS:
            self._out.append("\n\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
            return
        if self._skip:
            return
        if tag == "title":
            self._in_title = False
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            if self._heading is not None:
                text = "".join(self._heading).strip()
                if text:
                    self._out.append(f"\n\n## {text}\n\n")
                self._heading = None
        elif tag in _BLOCK_TAGS:
            self._out.append("\n\n")

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self._in_title:
            self._title.append(data)
        elif self._heading is not None:
            self._heading.append(data)
        else:
            self._out.append(data)

    @property
    def text(self) -> str:
        joined = "".join(self._out)
        joined = re.sub(r"[ \t\u00a0]+", " ", joined)
        return re.sub(r"\n{3,}", "\n\n", joined).strip()

    @property
    def title(self) -> str:
        return "".join(self._title).strip()


@register(".html", ".htm", ".xhtml")
def parse_html(path: str) -> ParsedDoc:
    raw, enc = read_text_file(path)
    parser = _TextExtractor()
    try:
        parser.feed(raw)
        parser.close()
    except Exception as exc:  # HTMLParser is tolerant; anything here is fatal
        raise ParseError(f"html parse failed: {exc}") from exc

    text = parser.text
    if not text:
        raise ParseError(
            f"{os.path.basename(path)}: no readable text "
            "(script/style/boilerplate only)")

    stack: list[tuple[int, str]] = []
    segments: list[Segment] = []
    for block in split_paragraphs(text):
        match = _HEADING.match(block.strip())
        if match:
            level = len(match.group(1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, match.group(2).strip()))
            continue
        segments.append(Segment(block, heading_path=" › ".join(h for _, h in stack)))

    title = parser.title or (stack[0][1] if stack else os.path.basename(path))
    return ParsedDoc(title=title[:200], segments=segments, extractor="html",
                     meta={"encoding": enc})
