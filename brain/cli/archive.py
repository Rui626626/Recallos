"""`recall.py archive` — 把对话记录转成可检索的档案。

支持的输入
----------
* **DSH 会话**：`session.v4.jsonl.zstd`（zstd 压缩的 JSONL，事件流）
* **通用 JSONL/JSON**：每行/每个元素带 `role` + `content` 的聊天导出
* **已经是纯文本**（`.md` / `.txt`）：不做转换，直接交给 `add` 管线

转换产物
--------
写到 `cache/sessions/<名字>.md`，**按轮次切成标题**：

    ## 第 3 轮 · 用户
    ## 第 3 轮 · 助手

这样切片会自然对齐到轮次，检索结果里就能说出「这在第几次对话的第几轮」——
这正是「我忘了在哪次聊天里说过」这个痛点的解药。

产物放在 `cache/` 而不是 `data/`：它是**可重抽的派生物**（原文还在会话目录里），
重跑 `archive` 幂等。真正入库的权威副本由 `ingest_paths` 写进 `data/docs/`。

⚠️ 对话记录属于**隐私内容**。本命令**只处理你明确给出的路径**，不扫描、不自动发现。
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import tempfile
from typing import Any, Iterable, Iterator, Optional

from ..config import ensure_dirs, load_config
from ..core.store import Store
from ..pipeline import ingest_paths
from ..state import Journal, SourceRegistry

__all__ = ["register", "run"]

#: zstd frame 魔数。DSH 的会话文件是**多个 frame 顺序拼接**的，必须流式读完全部，
#: 只解第一个 frame 会静默截断（这个坑是实测踩出来的，别再简化成 decompress()）。
_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"

CANDIDATE_SUFFIXES = (
    ".jsonl.zstd", ".jsonl.zst", ".ndjson.zstd",
    ".jsonl", ".ndjson", ".json",
    ".md", ".txt",
)


# --------------------------------------------------------------------- 解压


def decompress(blob: bytes) -> bytes:
    """zstandard 包 → zstd 命令行 → 原样返回（明文）。全失败时抛 RuntimeError。"""
    if not blob.startswith(_ZSTD_MAGIC):
        return blob
    try:
        import zstandard  # type: ignore
    except ImportError:
        zstandard = None
    if zstandard is not None:
        dctx = zstandard.ZstdDecompressor()
        with dctx.stream_reader(io.BytesIO(blob)) as reader:
            return reader.read()
    import shutil
    import subprocess
    if shutil.which("zstd"):
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "in.zst")
            dst = os.path.join(td, "out.jsonl")
            with open(src, "wb") as fh:
                fh.write(blob)
            subprocess.run(["zstd", "-d", "-f", "-q", "-o", dst, src], check=True)
            with open(dst, "rb") as fh:
                return fh.read()
    raise RuntimeError(
        "这是 zstd 压缩的会话文件，但本机既没有 zstandard 包也没有 zstd 命令。\n"
        "装一个即可：pip install zstandard")


# --------------------------------------------------------------------- 解析


def text_of(value: Any) -> str:
    """从任意嵌套结构里抽可读文本。

    只认两种东西：`{"type":"text","text":"..."}` 这类带 `text` 的块，以及裸字符串。
    顺序保持原样——拼出来的正文必须还能读。
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return ""
    if isinstance(value, list):
        return "\n".join(p for p in (text_of(v) for v in value) if p)
    if isinstance(value, dict):
        t = value.get("type")
        if t and t not in ("text", "input_text", "output_text", "message"):
            # 工具调用/思考块之类的结构化内容不进正文
            if t in ("tool_use", "tool_call", "tool_result", "thinking", "reasoning"):
                return ""
        for key in ("text", "content", "message", "body"):
            if key in value:
                got = text_of(value[key])
                if got:
                    return got
        return ""
    return ""


def _records_from_document(doc: Any) -> list[dict]:
    if isinstance(doc, list):
        return [o for o in doc if isinstance(o, dict)]
    if isinstance(doc, dict):
        for key in ("messages", "conversation", "turns", "history", "items"):
            v = doc.get(key)
            if isinstance(v, list) and v:
                return [o for o in v if isinstance(o, dict)]
        return [doc]
    return []


def read_records(path: str) -> tuple[list[dict], str]:
    """读一条会话/聊天导出，返回 (记录列表, 错误说明)。错误不抛，交给调用方留痕。"""
    try:
        with open(path, "rb") as fh:
            blob = fh.read()
    except OSError as exc:
        return [], f"读不了：{exc}"
    try:
        raw = decompress(blob)
    except RuntimeError as exc:
        return [], str(exc)
    except Exception as exc:                      # zstd 数据损坏
        return [], f"解压失败：{type(exc).__name__}: {exc}"

    text = raw.decode("utf-8", "replace").strip()
    if not text:
        return [], "空文件"

    # **先试「整个文件是一个 JSON 文档」，再退回 JSONL。** 顺序不能反：单行的
    # JSON 文档（`{"messages":[...]}`）在 JSONL 解析下会变成「一条记录」，把里面
    # 整段对话当成一个对象吞掉。多行的 JSONL 整体不是合法 JSON，自然落到下面。
    if text[:1] in ("{", "["):
        try:
            doc = json.loads(text)
        except json.JSONDecodeError:
            doc = None
        if doc is not None:
            got = _records_from_document(doc)
            if got:
                return got, ""

    records: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            records.append(obj)
        elif isinstance(obj, list):
            records.extend(o for o in obj if isinstance(o, dict))
    if records:
        return records, ""
    return [], "既不是 JSONL 也不是合法 JSON"


def _ts_to_date(value: Any) -> str:
    """DSH 的 createdAt 是**毫秒**时间戳字符串。"""
    try:
        ms = int(str(value).strip())
    except (TypeError, ValueError):
        return ""
    if ms <= 0:
        return ""
    import datetime
    if ms > 10_000_000_000:      # 毫秒
        ms = ms / 1000.0
    return datetime.datetime.fromtimestamp(ms).strftime("%Y-%m-%d %H:%M")


def _role_and_text(rec: dict) -> tuple[str, str]:
    """尽量从一条记录里判断「谁说的」和「说了什么」。兼容 DSH 与通用导出。"""
    rtype = str(rec.get("type") or rec.get("kind") or "")
    data = rec.get("data") if isinstance(rec.get("data"), dict) else rec

    if rtype == "user/message":
        return "用户", text_of(data.get("content"))
    if rtype == "assistant/message":
        msg = data.get("message")
        if isinstance(msg, dict):
            return "助手", text_of(msg.get("content"))
        return "助手", text_of(data.get("content"))
    if rtype == "system/message":
        return "", ""            # 内部系统块，不进档案

    role = str(data.get("role") or rec.get("role") or "").lower()
    if role in ("user", "human"):
        return "用户", text_of(data.get("content", data.get("text")))
    if role in ("assistant", "ai", "model"):
        return "助手", text_of(data.get("content", data.get("text")))
    return "", ""


def render_session(records: Iterable[dict], source_path: str) -> tuple[str, dict]:
    """把记录渲染成按轮次切分的 markdown。"""
    header = next((r for r in records if r.get("type") == "session"), None) or {}
    session_id = str(header.get("id") or "")
    created = _ts_to_date(header.get("createdAt") or "")

    body: list[str] = []
    turn = 0
    user_turns = 0
    assistant_turns = 0
    skipped = 0

    for rec in records:
        kind, text = _role_and_text(rec)
        if not kind:
            skipped += 1
            continue
        text = (text or "").strip()
        if not text:
            continue
        if kind == "用户":
            turn += 1
            user_turns += 1
        elif kind == "助手":
            assistant_turns += 1
        display_turn = turn if turn else 1
        body.append(f"## 第 {display_turn} 轮 · {kind}\n")
        body.append(text)
        body.append("")

    title = f"会话档案 {session_id[-8:] if session_id else os.path.basename(source_path)}"
    if created:
        title += f"（{created}）"

    doc = [
        f"# {title}",
        "",
        f"- 原始文件：`{source_path}`",
        f"- 会话 id：`{session_id or '(未知)'}`",
        f"- 时间：{created or '(未知)'}",
        f"- 轮次：用户 {user_turns} 条 / 助手 {assistant_turns} 条"
        + (f" / 跳过 {skipped} 条非对话事件" if skipped else ""),
        "",
    ]
    doc.extend(body)
    stats = {
        "turn": user_turns,
        "assistant": assistant_turns,
        "skipped": skipped,
        "chars": sum(len(b) for b in body),
    }
    return "\n".join(doc).rstrip() + "\n", stats


# --------------------------------------------------------------------- 发现


def discover(paths: list[str]) -> tuple[list[str], list[str]]:
    """展开成待处理文件清单。显式给的目录会递归查找候选后缀。"""
    files: list[str] = []
    problems: list[str] = []
    for p in paths:
        if os.path.isfile(p):
            files.append(p)
            continue
        if os.path.isdir(p):
            found = []
            for dirpath, dirnames, filenames in os.walk(p):
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                for name in sorted(filenames):
                    low = name.lower()
                    if any(low.endswith(s) for s in CANDIDATE_SUFFIXES):
                        found.append(os.path.join(dirpath, name))
            if not found:
                problems.append(f"{p}: 目录里没有可识别为对话记录的文件")
            files.extend(found)
            continue
        problems.append(f"{p}: 路径不存在")
    return files, problems


def _safe_name(path: str) -> str:
    """会话目录名/文件名拼成安全的输出名，避免同名覆盖。"""
    parent = os.path.basename(os.path.dirname(path)) or "archive"
    stem = os.path.basename(path)
    for suf in CANDIDATE_SUFFIXES:
        if stem.lower().endswith(suf):
            stem = stem[: -len(suf)]
            break
    raw = f"{parent}-{stem}" if stem not in ("session.v4", "session", "") else parent
    return re.sub(r"[^\w\u4e00-\u9fff\-]+", "-", raw).strip("-")[:80] or "archive"


# --------------------------------------------------------------------- CLI


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("archive",
                       help="import chat/agent transcripts as searchable documents")
    p.add_argument("paths", nargs="+",
                   help="transcript files, or directories to search (explicit paths only)")
    p.add_argument("--out", default=None,
                   help="where converted markdown goes (default <cache>/sessions)")
    p.add_argument("--dry-run", action="store_true",
                   help="convert and report, but write nothing")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    cfg = load_config()
    ensure_dirs(cfg)

    out_dir = args.out or os.path.join(cfg.cache_dir, "sessions")
    files, problems = discover(list(args.paths))
    if not files:
        for p in problems:
            print(p, file=sys.stderr)
        print("nothing to archive: give a transcript file or a directory that holds them",
              file=sys.stderr)
        return 2

    converted: list[dict] = []
    passthrough: list[str] = []
    failures: list[dict] = []
    plan: list[dict] = []

    for path in files:
        low = path.lower()
        if low.endswith((".md", ".txt")):
            passthrough.append(path)
            plan.append({"path": path, "mode": "passthrough", "target": path})
            continue

        records, why = read_records(path)
        if why:
            failures.append({"path": path, "error": why})
            continue
        if not records:
            failures.append({"path": path, "error": "没有解析出任何记录"})
            continue

        markdown, stats = render_session(records, path)
        target = os.path.join(out_dir, _safe_name(path) + ".md")
        plan.append({"path": path, "mode": "convert", "target": target, **stats})
        converted.append({"path": path, "target": target, "markdown": markdown, "stats": stats})

    if args.json:
        payload = {
            "scanned": len(files),
            "converted": [{"path": c["path"], "target": c["target"], **c["stats"]}
                          for c in converted],
            "passthrough": passthrough,
            "failures": failures,
            "problems": problems,
            "ingest": None,
            "dry_run": bool(args.dry_run),
        }

    if args.dry_run:
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 1 if failures else 0
        _print_plan(plan, failures, problems, dry_run=True)
        return 1 if failures else 0

    if converted:
        os.makedirs(out_dir, exist_ok=True)
        for c in converted:
            with open(c["target"], "w", encoding="utf-8") as fh:
                fh.write(c["markdown"])

    targets = [c["target"] for c in converted] + passthrough
    store = Store.open(cfg.db_path)
    registry = SourceRegistry(cfg.sources_path)
    journal = Journal(cfg.journal_path)
    try:
        summary = ingest_paths(store, registry, journal, cfg, targets, force=False)
    finally:
        store.close()

    if args.json:
        payload["ingest"] = {
            "scanned": summary["scanned"],
            "by_status": summary["by_status"],
            "chunks_written": summary["chunks_written"],
            "failures": summary["failures"],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_plan(plan, failures, problems, dry_run=False)
        by = summary["by_status"]
        print(f"\n入库：扫描 {summary['scanned']} 个文件，写入 {summary['chunks_written']} 个切片")
        for key in ("new", "updated", "unchanged", "skipped", "duplicate", "failed"):
            if by.get(key):
                print(f"  {key:<12} {by[key]}")
        for f in summary["failures"][:10]:
            print(f"  ! {os.path.basename(f['path'])}: {f.get('error', '')[:100]}")

    # 转换失败的也要留痕（铁律：失败必须留痕，不允许静默跳过）
    for f in failures:
        print(f"  ! {os.path.basename(f['path'])}: {f['error']}", file=sys.stderr)
    return 1 if (failures or summary["failures"]) else 0


def _print_plan(plan: list[dict], failures: list[dict], problems: list[str],
                dry_run: bool) -> None:
    mode = "（dry-run，未写入）" if dry_run else ""
    print(f"archive{mode}")
    for item in plan:
        if item["mode"] == "passthrough":
            print(f"  直接入库  {item['path']}")
        else:
            print(f"  转换      {item['path']}")
            print(f"            → {item['target']}")
            print(f"              {item['turn']} 轮 / {item['assistant']} 条助手 / "
                  f"{item['chars']} 字符")
    for p in problems:
        print(f"  - {p}")
