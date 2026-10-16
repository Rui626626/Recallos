#!/usr/bin/env python3
"""重新生成 `brain/data/pinyin.tsv.gz`（内置拼音表）。

为什么需要这个脚本
------------------
项目承诺「零依赖」，但拼音搜索需要一张 汉字→拼音 表。解决办法是**一次性生成、
随包发布**，而不是让每个用户去装 pypinyin。这个脚本就是那次生成的完整记录——
任何人想验证表格是怎么来的、或者想换一个数据源重新生成，都可以直接跑它。

用法
----
    python tools/build_pinyin_table.py                 # 用已安装的 pypinyin
    python tools/build_pinyin_table.py --whl <path>    # 从 .whl 解压后临时导入

输出：`brain/data/pinyin.tsv.gz`（gzip 压缩的 `汉字<TAB>拼音` 文本）。

数据约定
--------
* 只保留基本区 U+4E00–U+9FFF 与扩展 A U+3400–U+4DBF
* 无调拼音，全小写
* 多音字取**第一个**读音 —— 这是刻意的：目的是容忍同音错字，不是做注音
"""

from __future__ import annotations

import argparse
import gzip
import os
import sys
import tempfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TARGET = HERE.parent / "brain" / "data" / "pinyin.tsv.gz"

KEEP_RANGES = (("\u4e00", "\u9fff"), ("\u3400", "\u4dbf"))


def _ensure_importable(whl: str | None) -> None:
    if whl is None:
        return
    dest = Path(tempfile.mkdtemp(prefix="pypinyin-"))
    with zipfile.ZipFile(whl) as z:
        z.extractall(dest)
    sys.path.insert(0, str(dest))
    print(f"  extracted {whl} -> {dest}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--whl", help="path to a pypinyin .whl to import from")
    ap.add_argument("--out", default=str(TARGET), help="output .tsv.gz path")
    args = ap.parse_args()

    _ensure_importable(args.whl)

    try:
        import pypinyin
        from pypinyin import Style, pinyin
    except ImportError:
        print("pypinyin not importable. Install it, or pass --whl <path>.",
              file=sys.stderr)
        return 2

    print(f"  pypinyin {pypinyin.__version__}")
    source = pypinyin.pinyin_dict.pinyin_dict
    print(f"  dictionary entries: {len(source)}")

    def keep(ch: str) -> bool:
        return any(lo <= ch <= hi for lo, hi in KEEP_RANGES)

    rows: list[tuple[str, str]] = []
    for code in source:
        ch = chr(code)
        if not keep(ch):
            continue
        got = pinyin(ch, style=Style.NORMAL, errors="ignore")
        if not got or not got[0]:
            continue
        py = got[0][0]
        if py.isalpha():
            rows.append((ch, py.lower()))

    rows.sort()
    print(f"  kept: {len(rows)} characters")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(f"{ch}\t{py}\n" for ch, py in rows).encode("utf-8")
    # mtime=0 so the same input always produces the same bytes (reproducible builds)
    blob = gzip.compress(payload, compresslevel=9, mtime=0)
    out.write_bytes(blob)
    print(f"  wrote {out}  ({len(payload)} raw -> {len(blob)} gzip bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
