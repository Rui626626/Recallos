"""Generates the P1 test fixtures.

Everything is built here rather than committed, so the suite stays small and
each file's expected content is visible in one place. `build()` is
deterministic: running it twice produces byte-identical files.

The DOCX/PPTX/PDF fixtures are **hand-built minimal packages**. That is enough
to exercise the parsers (which read specific parts of the OOXML/PDF structure)
without pulling in a document-authoring library. They are not intended to be
opened in Word.
"""

from __future__ import annotations

import os
import zipfile

LECTURE_01 = """\
# 第一次课：人工智能导论

## 课程目标

本课程介绍人工智能的基本概念、发展历史与主要分支。

## 机器学习三要素

老师强调机器学习三要素：数据、模型、算法。三者缺一不可。

## 相关笔记

[[宏观经济学导论]]、[[信息经济导论]]
"""

LECTURE_02 = """\
# 第二次课：神经网络与反向传播

## 感知机

感知机是最简单的神经网络模型，只能解决线性可分问题。

## 反向传播

反向传播算法通过链式法则计算梯度。

## 复习建议

重点复习资料整理方法，考试会考这一部分。
"""

NOTES_UTF8 = """\
随手记的一些结论。

图书馆四楼的研讨间需要提前一天预约。

概率论的贝叶斯公式比想象中重要，要反复练。
"""

NOTES_GBK = "编码回退测试：这一行是 GBK 编码的中文内容。\n\n第二段用来验证分段。\n"

DECK_SLIDES = [
    "人工智能：一个划时代的重大技术变革",
    "第一部分 技术演进",
    "第二部分 产业应用",
]

DOCX_TITLE = "宏观经济学复习提纲"
DOCX_BODY = [
    ("Heading1", DOCX_TITLE),
    ("Heading2", "第一章 国民收入核算"),
    ("Normal", "GDP 是最终产品与服务的市场价值总和。"),
    ("Normal", "名义 GDP 与实际 GDP 的差别在于价格基期。"),
    ("Heading2", "第二章 通货膨胀"),
    ("Normal", "通货膨胀按程度可分为温和、奔腾、超级和滞涨。"),
]

PDF_TEXT = "RecallOS archival retention policy"


# ------------------------------------------------------------------ builders

def _write(path: str, text: str, encoding: str = "utf-8") -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding=encoding, newline="\n") as fh:
        fh.write(text)
    return path


def _write_bytes(path: str, payload: bytes) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(payload)
    return path


_DOCX_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""

_DOCX_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""


def _docx_document(paragraphs: list[tuple[str, str]]) -> str:
    ns = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    body = []
    for style, text in paragraphs:
        body.append(
            f'<w:p><w:pPr><w:pStyle w:val="{style}"/></w:pPr>'
            f"<w:r><w:t>{text}</w:t></w:r></w:p>")
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f"<w:document {ns}><w:body>{''.join(body)}</w:body></w:document>")


def write_docx(path: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _DOCX_CONTENT_TYPES)
        z.writestr("_rels/.rels", _DOCX_RELS)
        z.writestr("word/document.xml", _docx_document(DOCX_BODY))
    return path


def write_pptx(path: str, variant: int = 0) -> str:
    """Build a minimal deck.

    `variant` changes the package bytes **without changing the slide text**,
    which is what the `metadata_only` case needs: `zipfile.writestr` on a fixed
    string is deterministic, so re-running with `variant=0` produces identical
    bytes and would not exercise the path at all.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for i, text in enumerate(DECK_SLIDES, 1):
            z.writestr(
                f"ppt/slides/slide{i}.xml",
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
                'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
                f"<p:cSld><p:spTree><p:sp><p:txBody>"
                f"<a:p><a:r><a:t>{text}</a:t></a:r></a:p>"
                f"</p:txBody></p:sp></p:spTree></p:cSld></p:sld>")
        # Extra metadata that no parser reads, but that changes the file hash.
        z.writestr("docProps/app.xml",
                   '<?xml version="1.0" encoding="UTF-8"?>'
                   f"<Properties><Revision>{variant}</Revision></Properties>")
    return path


def write_pdf(path: str, text: str = PDF_TEXT) -> str:
    """A valid single-page PDF with a standard font.

    Hand-built so the PDF parser has a real, self-contained input: `pypdf`
    cannot synthesise text without a font resource, and committing a binary
    fixture would hide what the test expects.
    """
    content = f"BT /F1 24 Tf 72 700 Td ({text}) Tj ET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n"
        + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_at}\n%%EOF\n").encode()
    return _write_bytes(path, bytes(out))


def build(root: str) -> dict:
    """Create the fixture tree under `root`; return name → path.

    The corrupt package lives in its own directory so that importing the *good*
    fixtures is expected to exit 0, and only a run that deliberately includes
    the broken directory exits non-zero.
    """
    files = {
        "lecture01": _write(os.path.join(root, "materials", "lecture01.md"), LECTURE_01),
        "lecture02": _write(os.path.join(root, "materials", "lecture02.md"), LECTURE_02),
        "notes": _write(os.path.join(root, "materials", "notes.txt"), NOTES_UTF8),
        "notes_gbk": _write(os.path.join(root, "materials", "notes-gbk.txt"),
                            NOTES_GBK, encoding="gb18030"),
        "docx": write_docx(os.path.join(root, "materials", "report.docx")),
        "pptx": write_pptx(os.path.join(root, "materials", "deck.pptx")),
        "pdf": write_pdf(os.path.join(root, "materials", "paper.pdf")),
        "empty": _write(os.path.join(root, "materials", "empty.md"), ""),
        "unsupported": _write(os.path.join(root, "materials", "unsupported.xyz"), "x"),
        # Byte-identical to lecture01 → must be detected as a duplicate.
        "lecture01_copy": _write(os.path.join(root, "archive", "lecture01-copy.md"),
                                 LECTURE_01),
    }
    broken = os.path.join(root, "broken", "broken.docx")
    _write_bytes(broken, b"PK\x03\x04this is not a real zip archive at all")
    files["broken"] = broken
    files["materials_dir"] = os.path.join(root, "materials")
    files["archive_dir"] = os.path.join(root, "archive")
    files["broken_dir"] = os.path.join(root, "broken")
    files["root"] = root
    # `good_dirs` is what an import that is expected to exit 0 should be given.
    files["good_dirs"] = [files["materials_dir"], files["archive_dir"]]
    return files


if __name__ == "__main__":  # pragma: no cover
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else "tests/fixtures"
    for key, value in sorted(build(target).items()):
        print(f"{key}: {value}")
