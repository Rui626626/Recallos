"""P3 acceptance tests — 对话记录入库（`recall.py archive`）。

除了常规的行为测试，这里专门钉了两个**实测踩出来的坑**：

1. ``test_multiframe_zstd_is_fully_read`` —— DSH 的会话文件是**多个 zstd frame 顺序
   拼接**的。用 `ZstdDecompressor().decompress()` 只会拿到第一个 frame 然后静默截断；
   必须用 `stream_reader` 读到底。这条测试用两段 frame 拼一个文件来守住它。
2. ``test_failures_are_recorded`` —— 无法解析的文件必须出现在输出里，不允许静默跳过。

Run with:

    python recallos/tests/test_p3.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
CLI = os.path.join(PROJECT, "bin", "recall.py")
sys.path.insert(0, PROJECT)

from brain.cli import archive as archive_mod  # noqa: E402

try:
    import zstandard  # type: ignore
    HAVE_ZSTD = True
except ImportError:  # pragma: no cover
    HAVE_ZSTD = False


def run_cli(root: str, *args: str, expect: int | None = 0) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["RECALLOS_ROOT"] = root
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, CLI, *args], capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env, cwd=PROJECT)
    if expect is not None and proc.returncode != expect:
        raise AssertionError(
            f"recall.py {' '.join(args)} exited {proc.returncode} (expected {expect})\n"
            f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}")
    return proc


def cli_json(root: str, *args: str, expect: int | None = 0) -> dict:
    return json.loads(run_cli(root, *args, "--json", expect=expect).stdout)


# ------------------------------------------------------------------ fixtures


def dsh_records() -> list[dict]:
    return [
        {"type": "session", "version": "4", "id": "session-aaaa1111-bbbb-2222",
         "createdAt": "1791708867948", "cwd": "C:/somewhere"},
        {"type": "user/message",
         "data": {"role": "user",
                  "content": [{"type": "text", "text": "帮我看看这份复习提纲"}]}},
        {"type": "tool/call", "data": {"name": "Read", "input": {"path": "x"}}},
        {"type": "assistant/message",
         "data": {"turn": 1,
                  "message": {"role": "assistant",
                              "content": [{"type": "text",
                                           "text": "提纲的第三部分缺少复习重点。"}]}}},
        {"type": "user/message",
         "data": {"role": "user",
                  "content": [{"type": "text", "text": "那帮我把重点补上"}]}},
        {"type": "assistant/message",
         "data": {"turn": 2,
                  "message": {"role": "assistant",
                              "content": [{"type": "text",
                                           "text": "补好了，重点放在均衡与边际分析。"}]}}},
    ]


def write_jsonl(path: str, records: list[dict]) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return path


class ArchiveBase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="recallos-p3-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.root, ignore_errors=True))
        self.src = os.path.join(self.root, "src")
        os.makedirs(self.src, exist_ok=True)
        run_cli(self.root, "init")

    def out_dir(self) -> str:
        return os.path.join(self.root, "cache", "sessions")


# --------------------------------------------------------------- unit: 解压与解析


class TestDecompress(unittest.TestCase):
    def test_plain_bytes_pass_through(self):
        payload = b'{"a":1}\n'
        self.assertEqual(archive_mod.decompress(payload), payload)

    @unittest.skipUnless(HAVE_ZSTD, "需要 zstandard")
    def test_single_frame(self):
        data = b"hello world\n" * 100
        blob = zstandard.ZstdCompressor().compress(data)
        self.assertEqual(archive_mod.decompress(blob), data)

    @unittest.skipUnless(HAVE_ZSTD, "需要 zstandard")
    def test_multiframe_zstd_is_fully_read(self):
        """回归：多帧拼接的 zstd 必须读全，只读第一帧会静默截断。

        这条守的是一个真实的坑 —— DSH 的 session.v4.jsonl.zstd 就是这样存的，
        用 .decompress() 只会拿到第一帧，档案会莫名其妙地只有一半内容。
        """
        head = b"".join(b'{"type":"user/message"}\n' for _ in range(50))
        tail = b"".join(b'{"type":"assistant/message"}\n' for _ in range(50))
        blob = zstandard.ZstdCompressor().compress(head) + \
            zstandard.ZstdCompressor().compress(tail)
        out = archive_mod.decompress(blob)
        self.assertIn(b"user/message", out)
        self.assertIn(b"assistant/message", out)
        self.assertEqual(out.count(b"\n"), 100)


class TestRender(unittest.TestCase):
    def test_headings_are_per_turn(self):
        md, stats = archive_mod.render_session(dsh_records(), "C:/x/session.v4.jsonl.zstd")
        self.assertIn("## 第 1 轮 · 用户", md)
        self.assertIn("## 第 1 轮 · 助手", md)
        self.assertIn("## 第 2 轮 · 用户", md)
        self.assertEqual(stats["turn"], 2)
        self.assertEqual(stats["assistant"], 2)

    def test_header_records_provenance(self):
        md, _ = archive_mod.render_session(dsh_records(), "C:/x/session.v4.jsonl.zstd")
        self.assertIn("C:/x/session.v4.jsonl.zstd", md)
        self.assertIn("session-aaaa1111-bbbb-2222", md)
        self.assertIn("2026-", md)

    def test_tool_events_are_skipped_not_dumped(self):
        md, stats = archive_mod.render_session(dsh_records(), "x")
        self.assertNotIn("tool/call", md)
        self.assertGreaterEqual(stats["skipped"], 1)

    def test_generic_role_content_records(self):
        recs = [{"role": "user", "content": "第一句"},
                {"role": "assistant", "content": "第二句"}]
        md, stats = archive_mod.render_session(recs, "chat.jsonl")
        self.assertIn("第 1 轮 · 用户", md)
        self.assertIn("第一句", md)
        self.assertIn("第二句", md)

    def test_timestamp_millisecond_parsing(self):
        self.assertTrue(archive_mod._ts_to_date("1791708867948").startswith("2026-"))
        self.assertEqual(archive_mod._ts_to_date("not-a-number"), "")

    def test_safe_name_is_stable_and_safe(self):
        got = archive_mod._safe_name("C:/a/session-abc/session.v4.jsonl.zstd")
        self.assertEqual(got, "session-abc")
        self.assertNotIn("/", got)
        self.assertNotIn(":", got)


# ------------------------------------------------------------------ black box


class TestArchiveCli(ArchiveBase):
    def test_archive_creates_a_searchable_document(self):
        src = write_jsonl(os.path.join(self.src, "session-abc", "session.v4.jsonl"),
                          dsh_records())
        payload = cli_json(self.root, "archive", src)
        self.assertEqual(payload["scanned"], 1)
        self.assertEqual(len(payload["converted"]), 1)
        self.assertEqual(payload["converted"][0]["turn"], 2)

        stats = cli_json(self.root, "index", "--stats")
        self.assertEqual(stats["counts"]["docs"], 1)

        hits = cli_json(self.root, "search", "复习提纲")["hits"]
        self.assertTrue(hits)
        self.assertIn("第 1 轮", hits[0]["heading_path"],
                      "检索结果必须能说出这是第几轮")

    def test_dry_run_writes_nothing(self):
        src = write_jsonl(os.path.join(self.src, "s.jsonl"), dsh_records())
        cli_json(self.root, "archive", src, "--dry-run")
        self.assertFalse(os.path.exists(self.out_dir()))
        self.assertEqual(cli_json(self.root, "index", "--stats")["counts"]["docs"], 0)

    def test_second_run_is_idempotent(self):
        src = write_jsonl(os.path.join(self.src, "s.jsonl"), dsh_records())
        cli_json(self.root, "archive", src)
        second = cli_json(self.root, "archive", src)
        by = second["ingest"]["by_status"]
        self.assertEqual(by.get("new", 0), 0, "第二次不应产生新文档")
        self.assertEqual(cli_json(self.root, "index", "--stats")["counts"]["docs"], 1)

    def test_directory_input_is_discovered(self):
        write_jsonl(os.path.join(self.src, "a", "session.v4.jsonl"), dsh_records())
        write_jsonl(os.path.join(self.src, "b", "session.v4.jsonl"), dsh_records())
        payload = cli_json(self.root, "archive", self.src)
        self.assertEqual(payload["scanned"], 2)

    def test_plain_markdown_passes_through(self):
        md = os.path.join(self.src, "笔记.md")
        with open(md, "w", encoding="utf-8") as fh:
            fh.write("# 手动笔记\n\n这是手写的对话摘要。\n")
        payload = cli_json(self.root, "archive", md)
        self.assertEqual(payload["passthrough"], [md])
        self.assertEqual(len(payload["converted"]), 0)
        self.assertEqual(cli_json(self.root, "index", "--stats")["counts"]["docs"], 1)

    def test_failures_are_recorded(self):
        """铁律：失败必须留痕，不允许静默跳过。"""
        bad = os.path.join(self.src, "broken.jsonl")
        with open(bad, "w", encoding="utf-8") as fh:
            fh.write("this is not json at all\nnor is this\n")
        proc = run_cli(self.root, "archive", bad, expect=None)
        self.assertNotEqual(proc.returncode, 0)
        payload = json.loads(run_cli(self.root, "archive", bad, "--json",
                                     expect=None).stdout)
        self.assertEqual(len(payload["failures"]), 1)
        self.assertIn("JSON", payload["failures"][0]["error"])

    def test_missing_path_is_a_clean_error(self):
        proc = run_cli(self.root, "archive", os.path.join(self.root, "nope"),
                       expect=2)
        self.assertIn("nothing to archive", proc.stdout + proc.stderr)

    def test_generic_chat_export_json(self):
        path = os.path.join(self.src, "export.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"messages": [
                {"role": "user", "content": "聊聊贴现率"},
                {"role": "assistant", "content": "贴现率是把未来现金流折成现值用的。"},
            ]}, fh, ensure_ascii=False)
        cli_json(self.root, "archive", path)
        self.assertTrue(cli_json(self.root, "search", "贴现率")["hits"])

    @unittest.skipUnless(HAVE_ZSTD, "需要 zstandard")
    def test_zstd_session_end_to_end(self):
        """整条链路走一遍真实压缩格式。"""
        payload = b"".join(
            json.dumps(r, ensure_ascii=False).encode("utf-8") + b"\n"
            for r in dsh_records())
        mid = len(payload) // 2
        blob = (zstandard.ZstdCompressor().compress(payload[:mid])
                + zstandard.ZstdCompressor().compress(payload[mid:]))
        path = os.path.join(self.src, "session-zz", "session.v4.jsonl.zstd")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(blob)
        out = cli_json(self.root, "archive", path)
        self.assertEqual(out["converted"][0]["turn"], 2)
        self.assertTrue(cli_json(self.root, "search", "均衡与边际分析")["hits"])

    def test_archive_never_modifies_transcript_files(self):
        import hashlib
        src = write_jsonl(os.path.join(self.src, "s.jsonl"), dsh_records())
        with open(src, "rb") as fh:
            before = hashlib.sha256(fh.read()).hexdigest()
        cli_json(self.root, "archive", src)
        with open(src, "rb") as fh:
            after = hashlib.sha256(fh.read()).hexdigest()
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main(verbosity=2)
