"""P1 acceptance tests — import, update, move, failure handling, rebuild.

Black-box: every case drives the real command line in a subprocess against a
throwaway root, then asserts on real files, real database rows and exit codes.
No assertion depends on a model's description of what happened, and no test
mocks the code under test.

Each test builds its own fixture tree, so a test that mutates a file cannot
affect any other test.

Run with:

    python recallos/tests/test_p1.py
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
CLI = os.path.join(PROJECT, "bin", "recall.py")
sys.path.insert(0, HERE)
sys.path.insert(0, PROJECT)

import make_fixtures  # noqa: E402

from brain.core.store import Store  # noqa: E402


def run_cli(root: str, *args: str,
            expect: int | None = 0) -> subprocess.CompletedProcess:
    """Run the real CLI against `root`. `expect=None` skips the exit-code check."""
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


def snapshot(tree: str) -> list[tuple[str, int, int, str]]:
    """(relative path, size, mtime_ns, sha256) for every file under `tree`."""
    out: list[tuple[str, int, int, str]] = []
    for dirpath, _dirs, files in os.walk(tree):
        for name in files:
            full = os.path.join(dirpath, name)
            st = os.stat(full)
            with open(full, "rb") as fh:
                digest = hashlib.sha256(fh.read()).hexdigest()
            out.append((os.path.relpath(full, tree), st.st_size,
                        st.st_mtime_ns, digest))
    return sorted(out)


class P1Base(unittest.TestCase):

    def setUp(self) -> None:
        # Mirror the real layout: a workspace holding both the skill entry and
        # the project directory, so `doctor`'s skill check is exercisable.
        self.tmp = tempfile.mkdtemp(prefix="recallos-p1-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = os.path.join(self.tmp, "recallos")
        os.makedirs(self.root)

        entry = os.path.join(self.tmp, ".dsh", "skills", "recallos")
        os.makedirs(entry)
        with open(os.path.join(entry, "SKILL.md"), "w", encoding="utf-8") as fh:
            fh.write("---\nname: recallos\ndescription: test stub\n---\n")

        cfg = {
            "version": 1,
            "roots": [],
            "extensions": [".md", ".txt", ".docx", ".pptx", ".pdf"],
            "excludes": ["**/~$*"],
            "chunk": {"target_chars": 900, "max_chars": 1600},
            "search": {"default_limit": 20, "max_chunks_per_doc": 3,
                       "token_budget": 2500, "snippet_chars": 240},
            "paths": {},
        }
        with open(os.path.join(self.root, "brain.config.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False)
        run_cli(self.root, "init", "--json")

        # `doctor` requires the tests directory to hold a test file.
        tests_dir = os.path.join(self.root, "tests")
        os.makedirs(tests_dir, exist_ok=True)
        with open(os.path.join(tests_dir, "test_placeholder.py"), "w",
                  encoding="utf-8") as fh:
            fh.write("def test_ok():\n    assert True\n")

        self.src = os.path.join(self.root, "src")
        self.files = make_fixtures.build(self.src)

    # ------------------------------------------------------------------ helpers

    def read(self, *parts: str) -> str:
        with open(os.path.join(self.root, *parts), encoding="utf-8") as fh:
            return fh.read()

    def backdate(self, tree: str, seconds: float = 10.0) -> None:
        """Move every file's mtime into the past.

        The stat shortcut deliberately distrusts timestamps newer than a short
        grace window (a coarse filesystem clock can stamp two edits inside one
        tick), so freshly created fixtures never take it. Backdating lets a test
        exercise the shortcut without inventing a fake clock.
        """
        stamp = time.time() - seconds
        for dirpath, _dirs, files in os.walk(tree):
            for name in files:
                os.utime(os.path.join(dirpath, name), (stamp, stamp))

    def import_good(self, expect: int = 0) -> dict:
        """Import the fixture directories that are expected to succeed."""
        return cli_json(self.root, "add", *self.files["good_dirs"], expect=expect)

    def import_all(self, expect: int | None = None) -> dict:
        return cli_json(self.root, "add", self.src, expect=expect)

    def rows(self, sql: str, *params) -> list[sqlite3.Row]:
        con = sqlite3.connect(os.path.join(self.root, "index", "recall.db"))
        con.row_factory = sqlite3.Row
        try:
            return list(con.execute(sql, params))
        finally:
            con.close()

    def scalar(self, sql: str, *params):
        rows = self.rows(sql, *params)
        return rows[0][0] if rows else None

    def doc(self, source_ref: str) -> sqlite3.Row:
        hits = self.rows("SELECT * FROM doc WHERE source_ref=?", source_ref)
        self.assertEqual(len(hits), 1, f"expected exactly one doc for {source_ref}")
        return hits[0]

    def counts(self) -> list[int]:
        return [self.scalar(f"SELECT COUNT(*) FROM {t}")
                for t in ("doc", "chunk", "chunk_fts", "chunk_fts_cjk")]

    def store(self) -> Store:
        return Store.open(os.path.join(self.root, "index", "recall.db"))


class TestFormatsAndIdempotence(P1Base):

    def test_01_every_available_format_imports(self) -> None:
        """markdown / txt / docx / pptx（以及装了后端时的 pdf）各自产出可检索切片。

        PDF 是**可选**的：没装 pypdf/PyMuPDF 时工具承诺的是「记录为解析失败并说明
        原因」，而不是正常解析。所以这里按实际可用的后端断言，而不是硬要求五格式
        齐全——后者会让一个健康的无依赖环境判为失败。
        """
        self.import_good()

        extractors = {r["extractor"] for r in self.rows(
            "SELECT DISTINCT extractor FROM doc WHERE status='indexed'")}
        for expected in ("markdown", "text", "docx", "pptx"):
            self.assertIn(expected, extractors)

        if "pdf" in self.files.get("skipped_optional", []):
            # 没有后端：断言降级行为，而不是假装它成功了
            self.assertFalse(any(e.startswith("pdf:") for e in extractors),
                             "没有 PDF 后端时不该出现 pdf: 抽取器")
            self.assertNotIn("paper.pdf", os.listdir(self.files["materials_dir"]))
        else:
            self.assertTrue(any(e.startswith("pdf:") for e in extractors), extractors)

        docs = self.scalar("SELECT COUNT(*) FROM doc")
        archives = [f for f in os.listdir(os.path.join(self.root, "data", "docs"))
                    if f.endswith(".json")]
        self.assertEqual(len(archives), docs)

        cache_root = os.path.join(self.root, "cache")
        self.assertGreater(sum(len(f) for _d, _s, f in os.walk(cache_root)), 0)

        chunks, fts, cjk = (self.scalar("SELECT COUNT(*) FROM chunk"),
                            self.scalar("SELECT COUNT(*) FROM chunk_fts"),
                            self.scalar("SELECT COUNT(*) FROM chunk_fts_cjk"))
        self.assertGreater(chunks, 0)
        self.assertEqual(chunks, fts)
        self.assertEqual(chunks, cjk)
        self.assertEqual(cli_json(self.root, "index", "--stats")["integrity"], {})

    def test_02_reimport_changes_nothing(self) -> None:
        # Backdate *before* the first import, so the recorded timestamp is
        # already outside the freshness grace window. Backdating afterwards
        # would change mtime and defeat the very shortcut under test.
        self.backdate(self.files["materials_dir"])
        self.backdate(self.files["archive_dir"])
        self.import_good()

        before = self.counts()
        sources_before = self.read("state", "sources.json")

        summary = self.import_good()
        self.assertEqual(before, self.counts())
        self.assertEqual(summary["chunks_written"], 0)
        by = summary["by_status"]
        # 期望值从夹具**实际内容**算出来，而不是写死 8。写死的话，少一个可选格式
        # （比如没装 PDF 后端就没生成 paper.pdf）就会让这条与快捷路径无关的测试
        # 莫名其妙地失败。空文件走的是 empty 而不是 skipped，所以减 1。
        indexable = [
            name
            for directory in self.files["good_dirs"]
            for name in os.listdir(directory)
            if os.path.splitext(name)[1].lower()
            in (".md", ".txt", ".docx", ".pptx", ".pdf")
        ]
        self.assertGreaterEqual(by.get("skipped", 0), len(indexable) - 1,
                                f"the cheap stat shortcut did not engage: {by}")
        # The authoritative file must be byte-identical too, or every run looks
        # like a change and `data/` stops being diffable.
        self.assertEqual(sources_before, self.read("state", "sources.json"))

        # A third run is still a no-op.
        self.assertEqual(self.import_good()["chunks_written"], 0)
        self.assertEqual(before, self.counts())

    def test_03_content_update_keeps_identity(self) -> None:
        self.import_good()
        target = self.files["lecture02"]
        before = self.doc(target)
        others_before = {r["source_ref"]: r["content_hash"] for r in self.rows(
            "SELECT source_ref, content_hash FROM doc")}

        with open(target, "a", encoding="utf-8") as fh:
            fh.write("\n## 新增章节\n\n这一段是后加的，用来验证增量更新。\n")
        os.utime(target, None)
        time.sleep(0.01)

        summary = self.import_good()
        self.assertGreaterEqual(summary["by_status"].get("updated", 0), 1)

        after = self.doc(target)
        self.assertEqual(after["doc_id"], before["doc_id"], "the id must not change")
        self.assertNotEqual(after["content_hash"], before["content_hash"])
        self.assertNotEqual(after["file_hash"], before["file_hash"])
        self.assertEqual(after["imported_at"], before["imported_at"])
        self.assertGreaterEqual(after["chunk_count"], before["chunk_count"])

        others_after = {r["source_ref"]: r["content_hash"] for r in self.rows(
            "SELECT source_ref, content_hash FROM doc")}
        changed = [ref for ref in others_before
                   if ref != target and others_before[ref] != others_after.get(ref)]
        self.assertEqual(changed, [], f"unrelated documents changed: {changed}")

        revisions = self.rows("SELECT * FROM doc_revision WHERE doc_id=?",
                              before["doc_id"])
        self.assertGreaterEqual(len(revisions), 2)
        self.assertGreaterEqual(
            len(os.listdir(os.path.join(self.root, "cache", before["doc_id"]))), 1)

    def test_04_bytes_changed_content_same_is_metadata_only(self) -> None:
        self.import_good()
        target = self.files["pptx"]
        before = self.doc(target)
        chunks_before = self.scalar(
            "SELECT COUNT(*) FROM chunk WHERE doc_id=?", before["doc_id"])

        make_fixtures.write_pptx(target, variant=1)  # same text, different bytes
        os.utime(target, None)
        time.sleep(0.01)

        self.import_good()
        after = self.doc(target)
        self.assertEqual(after["content_hash"], before["content_hash"])
        self.assertNotEqual(after["file_hash"], before["file_hash"])
        self.assertEqual(after["status"], "metadata_only")
        self.assertEqual(
            self.scalar("SELECT COUNT(*) FROM chunk WHERE doc_id=?", before["doc_id"]),
            chunks_before)

    def test_05_identical_copy_is_not_indexed_twice(self) -> None:
        self.import_good()
        rows = self.rows(
            """SELECT doc_id, source_ref, status, duplicate_of, chunk_count
               FROM doc WHERE source_ref LIKE '%lecture01%' ORDER BY source_ref""")
        self.assertEqual(len(rows), 2, [dict(r) for r in rows])
        self.assertEqual(sorted(r["status"] for r in rows), ["duplicate", "indexed"])
        duplicate = next(r for r in rows if r["status"] == "duplicate")
        canonical = next(r for r in rows if r["status"] == "indexed")
        self.assertEqual(duplicate["duplicate_of"], canonical["doc_id"])
        self.assertEqual(duplicate["chunk_count"], 0)
        self.assertEqual(
            self.scalar("SELECT COUNT(*) FROM chunk WHERE doc_id=?",
                        duplicate["doc_id"]), 0)

    def test_06_chinese_queries_reach_the_right_documents(self) -> None:
        self.import_good()
        store = self.store()
        try:
            for query in ("机器学习三要素", "复习资料", "复习", "通货膨胀"):
                hits = store.search(query, limit=20)
                self.assertTrue(hits, f"{query!r} should have matched something")
                for hit in hits:
                    self.assertIn(hit.channel, ("trigram", "cjk"))
                    self.assertNotIn("\x01", hit.snippet)
                    self.assertNotIn("\x02", hit.snippet)
            for query in ("zzz不存在的词组qqq", ""):
                self.assertEqual(store.search(query, limit=20), [],
                                 f"{query!r} should not have matched")
        finally:
            store.close()

    def test_07_rebuild_from_archives_reproduces_identical_chunk_ids(self) -> None:
        self.import_good()
        before = [r[0] for r in self.rows(
            "SELECT chunk_id FROM chunk ORDER BY chunk_id")]
        hashes = {r["doc_id"]: r["content_hash"] for r in self.rows(
            "SELECT doc_id, content_hash FROM doc")}
        self.assertTrue(before)

        shutil.rmtree(os.path.join(self.root, "index"), ignore_errors=True)
        self.assertFalse(os.path.exists(
            os.path.join(self.root, "index", "recall.db")))

        summary = cli_json(self.root, "index", "--rebuild")
        self.assertGreater(summary["restored"], 0)

        after = [r[0] for r in self.rows(
            "SELECT chunk_id FROM chunk ORDER BY chunk_id")]
        self.assertEqual(before, after, "rebuild produced different chunk ids")
        self.assertEqual(
            {r["doc_id"]: r["content_hash"] for r in self.rows(
                "SELECT doc_id, content_hash FROM doc")}, hashes)
        self.assertEqual(cli_json(self.root, "index", "--stats")["integrity"], {})

    def test_08_rebuild_from_archives_reads_no_original_files(self) -> None:
        """Decisive: move the whole source tree away, then rebuild."""
        self.import_good()
        before = [r[0] for r in self.rows(
            "SELECT chunk_id FROM chunk ORDER BY chunk_id")]

        hidden = self.src + "-hidden"
        os.rename(self.src, hidden)
        try:
            cli_json(self.root, "index", "--rebuild")
            after = [r[0] for r in self.rows(
                "SELECT chunk_id FROM chunk ORDER BY chunk_id")]
            self.assertEqual(before, after)
        finally:
            os.rename(hidden, self.src)

    def test_09_rebuild_from_source_reads_the_original_files(self) -> None:
        self.import_good()
        before = [r[0] for r in self.rows(
            "SELECT chunk_id FROM chunk ORDER BY chunk_id")]
        cli_json(self.root, "index", "--rebuild", "--from-source")
        after = [r[0] for r in self.rows(
            "SELECT chunk_id FROM chunk ORDER BY chunk_id")]
        self.assertEqual(before, after)


class TestFailureHandling(P1Base):

    def test_10_empty_broken_and_unsupported(self) -> None:
        proc = run_cli(self.root, "add", self.src, expect=1)  # broken.docx fails

        empty = self.doc(self.files["empty"])
        self.assertEqual(empty["status"], "empty")
        self.assertIsNone(empty["content_hash"])
        self.assertEqual(empty["chunk_count"], 0)

        broken = self.doc(self.files["broken"])
        self.assertEqual(broken["status"], "failed")
        self.assertTrue((broken["error"] or "").strip())
        self.assertIsNotNone(broken["error_at"])

        # An unsupported extension is never scanned at all.
        self.assertEqual(self.scalar(
            "SELECT COUNT(*) FROM doc WHERE source_ref LIKE '%.xyz'"), 0)

        # Everything else still imported despite the failure.
        self.assertGreaterEqual(self.scalar(
            "SELECT COUNT(*) FROM doc WHERE status='indexed'"), 6)

        journal = self.read("state", "imports.jsonl")
        self.assertIn("broken.docx", journal)
        self.assertNotIn("Traceback", proc.stderr)

    def test_11_a_failed_document_recovers_on_the_next_run(self) -> None:
        run_cli(self.root, "add", self.src, expect=1)
        self.assertEqual(self.doc(self.files["broken"])["status"], "failed")

        make_fixtures.write_docx(self.files["broken"])
        os.utime(self.files["broken"], None)
        time.sleep(0.01)

        cli_json(self.root, "add", self.src)  # now expected to exit 0
        fixed = self.doc(self.files["broken"])
        self.assertEqual(fixed["status"], "indexed")
        self.assertGreater(fixed["chunk_count"], 0)
        self.assertIsNone(fixed["error"])

    def test_12_a_locked_index_fails_cleanly_and_changes_nothing(self) -> None:
        self.import_good()
        docs_before = self.scalar("SELECT COUNT(*) FROM doc")

        blocker = sqlite3.connect(os.path.join(self.root, "index", "recall.db"))
        blocker.execute("PRAGMA journal_mode=WAL")
        blocker.execute("BEGIN EXCLUSIVE")
        try:
            proc = run_cli(self.root, "add", self.files["archive_dir"], expect=1)
            self.assertIn("index database", proc.stderr.lower())
            self.assertNotIn("Traceback", proc.stderr)
        finally:
            blocker.rollback()
            blocker.close()

        self.assertEqual(self.scalar("SELECT COUNT(*) FROM doc"), docs_before,
                         "a locked index must not leave partial rows")


class TestMoveAndRename(P1Base):

    def test_13_a_renamed_file_keeps_its_document_id(self) -> None:
        self.import_good()
        original = self.files["lecture02"]
        before = self.doc(original)

        # Rename *within* the imported tree, so the next import sees the new
        # path and the old one gone — which is exactly the situation the move
        # rule exists for.
        new_path = os.path.join(self.files["materials_dir"], "lecture02-renamed.md")
        os.rename(original, new_path)
        time.sleep(0.01)

        self.import_good()

        self.assertEqual(self.scalar(
            "SELECT COUNT(*) FROM doc WHERE doc_id=?", before["doc_id"]), 1,
            "the moved document must not become a second record")
        after = self.doc(new_path)
        self.assertEqual(after["doc_id"], before["doc_id"])
        self.assertEqual(after["content_hash"], before["content_hash"])
        self.assertEqual(after["status"], "indexed")

        history = self.rows(
            "SELECT * FROM source_history WHERE doc_id=? AND reason='moved'",
            before["doc_id"])
        self.assertTrue(history, "the move was not recorded in source_history")

        store = self.store()
        try:
            hits = store.search("反向传播", limit=10)
            self.assertTrue(hits)
            self.assertTrue(any(h.source_ref == new_path for h in hits),
                            [h.source_ref for h in hits])
        finally:
            store.close()

    def test_14_a_missing_file_is_marked_not_forgotten(self) -> None:
        self.import_good()
        victim = self.files["notes"]
        doc_id = self.doc(victim)["doc_id"]

        os.rename(victim, victim + ".gone")
        self.import_good()

        row = self.doc(victim)
        self.assertEqual(row["status"], "missing")
        self.assertIsNotNone(row["missing_at"])
        self.assertEqual(row["doc_id"], doc_id, "the record must survive")

        registry = json.loads(self.read("state", "sources.json"))
        refs = [e["source_ref"] for e in registry["sources"]]
        self.assertIn(victim, refs, "a missing source must stay registered")


class TestSafety(P1Base):

    def test_15_original_files_are_never_touched(self) -> None:
        before = snapshot(self.src)

        self.import_good()
        self.import_good()
        cli_json(self.root, "index", "--rebuild")
        cli_json(self.root, "index", "--rebuild", "--from-source")

        self.assertEqual(before, snapshot(self.src),
                         "import or rebuild modified the original files")

    def test_16_doctor_reports_the_ingest_layer_honestly(self) -> None:
        """导入层自检要如实报告，包括「没装 PDF 后端」这个状态。

        `p1-ingest` 在缺后端时给的是 **warn**（工具能用，只是 PDF 读不了），
        这不是失败。硬断言 pass 等于要求每台机器都装可选包。
        """
        payload = cli_json(self.root, "doctor")
        check = next(c for c in payload["checks"] if c["name"] == "p1-ingest")
        self.assertIn("extensions", check["detail"])
        self.assertIn("pdf backend", check["detail"])

        if "pdf" in self.files.get("skipped_optional", []):
            self.assertEqual(check["status"], "warn", check)
            self.assertIn("missing", check["detail"])
            self.assertIn("pypdf", check.get("hint", ""))
        else:
            self.assertEqual(check["status"], "pass", check)


class TestExtensionCoverage(unittest.TestCase):
    """「有解析器但永远不会被扫到」是一类静默丢失，必须被守住。

    真实发生过：`.log` / `.mdx` / `.xhtml` / `.xlsm` 早就注册了解析器，但
    `DEFAULT_EXTENSIONS` 里没有它们 —— 于是这些文件既不会被扫描，也不会报错，
    用户看到的就是「我明明有这份文件，工具说没有」。
    """

    def test_every_registered_extension_is_scanned_by_default(self):
        from brain.config import DEFAULT_EXTENSIONS
        from brain.ingest.base import supported_extensions

        scanned = {e.lower() for e in DEFAULT_EXTENSIONS}
        registered = set(supported_extensions())
        missing = sorted(registered - scanned)
        self.assertEqual(missing, [],
                         "这些扩展名有解析器却不在 DEFAULT_EXTENSIONS 里，"
                         "永远不会被扫描到：" + ", ".join(missing))


class TestCsvIngest(unittest.TestCase):
    """CSV 是高频格式：表格单元格必须能被搜到，且摘录要像表格而不是一串逗号。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="recallos-p1-csv-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = os.path.join(self.tmp, "data-root")
        self.src = os.path.join(self.tmp, "资料")
        os.makedirs(self.src, exist_ok=True)
        with open(os.path.join(self.src, "预算.csv"), "w",
                  encoding="utf-8", newline="\n") as fh:
            fh.write("科目,金额\n研发预算,1200000\n市场投放,300000\n")

    def test_csv_cells_are_searchable(self):
        run_cli(self.root, "add", self.src)
        payload = cli_json(self.root, "search", "研发预算")
        self.assertTrue(payload["hits"], "CSV 里的中文单元格应当可搜")
        hit = payload["hits"][0]
        self.assertTrue(hit["source_ref"].endswith("预算.csv"))
        # 单元格用 " | " 连接，摘录里应当看得到分隔符而不是原始逗号
        self.assertIn(" | ", hit["snippet"])

    def test_csv_is_reported_as_a_supported_format(self):
        from brain.ingest.base import supported_extensions
        self.assertIn(".csv", supported_extensions())


class TestCacheDirectoriesAreSkipped(unittest.TestCase):
    """IM / 系统缓存目录必须被剪掉，否则 `add Desktop` 会把几万张缓存图当资料扫。

    实测用户桌面：一万多张图片、五千多个深层目录，几乎全在企业微信（WXWork）缓存里。
    扫它们的代价是「很慢」，收益是零——而且会把「不支持格式」的统计污染成噪音。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="recallos-p1-cache-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = os.path.join(self.tmp, "data-root")
        self.src = os.path.join(self.tmp, "Desktop")
        cache = os.path.join(self.src, "WXWork", "cache")
        os.makedirs(cache, exist_ok=True)
        # 缓存里放一份「看起来该命中」的文件：它必须搜不到
        with open(os.path.join(cache, "藏在缓存里的笔记.md"), "w",
                  encoding="utf-8", newline="\n") as fh:
            fh.write("# 缓存里的机密内容\n")
        with open(os.path.join(self.src, "真正资料.md"), "w",
                  encoding="utf-8", newline="\n") as fh:
            fh.write("# 真正的资料\n")

    def test_files_under_a_cache_directory_are_not_indexed(self):
        run_cli(self.root, "add", self.src)
        self.assertEqual(cli_json(self.root, "search", "缓存里的机密内容",
                                  expect=None)["hits"], [],
                         "缓存目录里的文件不该被索引")
        self.assertTrue(cli_json(self.root, "search", "真正的资料")["hits"],
                        "缓存目录外的文件必须正常入库")

    def test_the_exclude_patterns_really_match_cache_paths(self):
        """直接量一下 glob，而不是相信「写了就等于生效」——这类静默失效最难发现。"""
        from brain.config import DEFAULT_EXCLUDES
        from brain.pipeline import _matches_any

        for path in (r"T:/samples/Desktop/WXWork",
                     r"T:/samples/Desktop/WXWork/cache/a.jpg",
                     r"T:/samples/AppData/Local/Temp/a.md",
                     r"T:/samples/WeChat Files/a.md"):
            self.assertTrue(_matches_any(path, DEFAULT_EXCLUDES), f"应当跳过：{path}")
        self.assertFalse(_matches_any(r"T:/samples/Documents/笔记.md", DEFAULT_EXCLUDES))


if __name__ == "__main__":
    unittest.main(verbosity=2)
