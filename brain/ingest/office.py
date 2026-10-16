"""Office formats that are really just ZIP + XML.

DOCX, PPTX and XLSX are all OOXML packages, so `zipfile` plus
`xml.etree.ElementTree` covers them with **no third-party dependency**. That
matters twice: nothing has to be installed here, and the eventual WPS port may
not be allowed to install arbitrary packages either.

XLSX is the one exception — `openpyxl` is used when present because date and
number formatting is genuinely fiddly — and a missing backend is a clear
`ParseError` naming the fix, never an import-time crash.
"""

from __future__ import annotations

import os
import re
import zipfile
from xml.etree import ElementTree as ET

from ..core.chunk import Segment
from .base import ParseError, ParsedDoc, register

__all__ = []

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"

_HEADING_STYLE = re.compile(r"(?:heading|标题)\s*([1-6])", re.IGNORECASE)
_SLIDE_NUM = re.compile(r"slide(\d+)\.xml$")

ROWS_PER_SEGMENT = 20


def _zip_members(path: str) -> list[str]:
    try:
        with zipfile.ZipFile(path) as z:
            return z.namelist()
    except zipfile.BadZipFile as exc:
        raise ParseError(
            f"{os.path.basename(path)}: not a valid Office package "
            "(the file is corrupt or is not really an OOXML document)") from exc


def _zip_root(path: str, member: str) -> ET.Element:
    try:
        with zipfile.ZipFile(path) as z:
            payload = z.read(member)
    except KeyError as exc:
        raise ParseError(f"{os.path.basename(path)}: missing {member}") from exc
    except zipfile.BadZipFile as exc:
        raise ParseError(f"{os.path.basename(path)}: not a valid Office package") from exc
    try:
        return ET.fromstring(payload)
    except ET.ParseError as exc:
        raise ParseError(f"{os.path.basename(path)}: malformed XML in {member}") from exc


# ------------------------------------------------------------------------- DOCX

@register(".docx")
def parse_docx(path: str) -> ParsedDoc:
    root = _zip_root(path, "word/document.xml")
    stack: list[tuple[int, str]] = []
    segments: list[Segment] = []

    for para in root.iter(W + "p"):
        text = "".join(t.text or "" for t in para.iter(W + "t")).strip()
        if not text:
            continue
        style = para.find(f"{W}pPr/{W}pStyle")
        level = 0
        if style is not None:
            match = _HEADING_STYLE.search(style.get(W + "val") or "")
            if match:
                level = int(match.group(1))
        if level:
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, text))
            continue
        segments.append(Segment(text, heading_path=" › ".join(h for _, h in stack)))

    if not segments:
        raise ParseError(
            f"{os.path.basename(path)}: no paragraph text found "
            "(the document may contain only images or drawings)")
    title = stack[0][1] if stack else os.path.splitext(os.path.basename(path))[0]
    return ParsedDoc(title=title[:200], segments=segments, extractor="docx")


# ------------------------------------------------------------------------- PPTX

@register(".pptx")
def parse_pptx(path: str) -> ParsedDoc:
    slides = sorted(
        (m for m in _zip_members(path) if _SLIDE_NUM.search(m)),
        key=lambda m: int(_SLIDE_NUM.search(m).group(1)),  # type: ignore[union-attr]
    )
    if not slides:
        raise ParseError(f"{os.path.basename(path)}: no slides found")

    segments: list[Segment] = []
    first_title = ""
    for member in slides:
        number = int(_SLIDE_NUM.search(member).group(1))  # type: ignore[union-attr]
        root = _zip_root(path, member)
        # One segment per paragraph, so a bullet list stays retrievable as a unit.
        for para in root.iter(A + "p"):
            text = "".join(t.text or "" for t in para.iter(A + "t")).strip()
            if text:
                segments.append(Segment(text, heading_path=f"第 {number} 页"))
                if not first_title:
                    first_title = text

    if not segments:
        raise ParseError(
            f"{os.path.basename(path)}: no readable slide text "
            "(the deck may be image-only)")
    title = first_title[:120] or os.path.splitext(os.path.basename(path))[0]
    return ParsedDoc(title=title, segments=segments, extractor="pptx",
                     meta={"slides": len(slides)})


# ------------------------------------------------------------------------- XLSX

@register(".xlsx", ".xlsm")
def parse_xlsx(path: str) -> ParsedDoc:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover - openpyxl is present here
        raise ParseError(
            "xlsx needs openpyxl: python -m pip install openpyxl") from exc

    try:
        workbook = load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:
        raise ParseError(
            f"{os.path.basename(path)}: cannot open workbook: {exc}") from exc

    segments: list[Segment] = []
    sheet_names: list[str] = []
    try:
        for sheet in workbook.worksheets:
            sheet_names.append(sheet.title)
            buffer: list[str] = []

            def flush() -> None:
                if buffer:
                    segments.append(Segment("\n".join(buffer), heading_path=sheet.title))
                    buffer.clear()

            for row in sheet.iter_rows(values_only=True):
                cells = [str(c).strip() for c in row if c is not None and str(c).strip()]
                if cells:
                    buffer.append(" | ".join(cells))
                if len(buffer) >= ROWS_PER_SEGMENT:
                    flush()
            flush()
    finally:
        try:
            workbook.close()
        except Exception:
            pass

    if not segments:
        raise ParseError(f"{os.path.basename(path)}: no non-empty cells")
    title = sheet_names[0] if sheet_names else os.path.splitext(os.path.basename(path))[0]
    return ParsedDoc(title=title[:120], segments=segments, extractor="xlsx",
                     meta={"sheets": len(sheet_names)})
