"""PDF text extraction.

`pypdf` (pure Python) is the primary backend; `PyMuPDF` is used when present
because it is markedly faster on large documents. Both are optional — a missing
backend is a `ParseError` naming the exact fix, never an import-time crash.

Real PDFs vary: a page with no extractable text is normal (it is a scan). Such a
page simply contributes no segments. Only a document with no text on *any* page
is reported as failed, and the message says "scanned" so the caller knows the
answer is OCR, not "the file is broken".
"""

from __future__ import annotations

import os
from typing import Optional

from ..core.chunk import Segment, split_paragraphs
from .base import ParseError, ParsedDoc, register

__all__ = ["extract_pdf", "pdf_backend"]


def pdf_backend() -> Optional[str]:
    """Which backend would be used right now, or `None` if none is installed."""
    for module in ("pymupdf", "fitz"):
        try:
            __import__(module)
            return "pymupdf"
        except ImportError:
            continue
    try:
        __import__("pypdf")
        return "pypdf"
    except ImportError:
        return None


def _extract_pymupdf(path: str) -> list[tuple[int, str]]:
    try:
        import pymupdf as fitz  # type: ignore
    except ImportError:
        import fitz  # type: ignore
    pages: list[tuple[int, str]] = []
    with fitz.open(path) as doc:
        for i, page in enumerate(doc, start=1):
            pages.append((i, page.get_text("text") or ""))
    return pages


def _extract_pypdf(path: str) -> list[tuple[int, str]]:
    # 受保护的导入。调用方其实已经用 `pdf_backend()` 确认过它存在，但这里仍然
    # 自己兜住：这个模块的契约是「缺后端 → ParseError，绝不是导入期崩溃」，
    # 而只靠调用方守规矩，直接调用这个函数的人就会撞到 ImportError。
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ParseError(
            "no PDF backend installed — run: python -m pip install pypdf") from exc

    reader = PdfReader(path)
    if getattr(reader, "is_encrypted", False):
        try:
            reader.decrypt("")
        except Exception as exc:
            raise ParseError(f"encrypted PDF, cannot open: {exc}") from exc
    pages: list[tuple[int, str]] = []
    for i, page in enumerate(reader.pages, start=1):
        try:
            pages.append((i, page.extract_text() or ""))
        except Exception:
            # One unreadable page must not fail the whole document.
            pages.append((i, ""))
    return pages


def extract_pdf(path: str) -> list[tuple[int, str]]:
    backend = pdf_backend()
    if backend == "pymupdf":
        return _extract_pymupdf(path)
    if backend == "pypdf":
        return _extract_pypdf(path)
    raise ParseError("no PDF backend installed — run: python -m pip install pypdf")


@register(".pdf")
def parse_pdf(path: str) -> ParsedDoc:
    try:
        pages = extract_pdf(path)
    except ParseError:
        raise
    except Exception as exc:
        raise ParseError(f"{os.path.basename(path)}: PDF read failed: {exc}") from exc

    segments: list[Segment] = []
    empty_pages = 0
    for number, text in pages:
        paragraphs = split_paragraphs(text)
        if not paragraphs:
            empty_pages += 1
            continue
        for paragraph in paragraphs:
            segments.append(
                Segment(paragraph, heading_path=f"第 {number} 页", page=number))

    if not segments:
        raise ParseError(
            f"{os.path.basename(path)}: no extractable text in {len(pages)} page(s) "
            "— this is a scanned PDF; it needs OCR rather than a text parser")

    stem = os.path.splitext(os.path.basename(path))[0]
    first_line = segments[0].text.strip().splitlines()[0][:120]
    return ParsedDoc(
        title=first_line or stem,
        segments=segments,
        extractor=f"pdf:{pdf_backend()}",
        meta={"pages": len(pages), "empty_pages": empty_pages},
    )
