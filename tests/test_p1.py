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

    def test_01_five_formats_import(self) -> None:
        """Markdown / txt / docx / pptx / pdf each produce searchable chunks."""
        self.import_good()

        extractors = {r["extractor"] for r in self.rows(
            "SELECT DISTINCT extractor FROM doc WHERE status='indexed'")}
        for expected in ("markdown", "text", "docx", "pptx"):
            self.assertIn(expected, extractors)
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
        self.assertGreaterEqual(by.get("skipped", 0), 8,
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

    def test_16_doctor_reports_the_ingest_layer_as_working(self) -> None:
        payload = cli_json(self.root, "doctor")
        check = next(c for c in payload["checks"] if c["name"] == "p1-ingest")
        self.assertEqual(check["status"], "pass", check)
        self.assertIn("extensions", check["detail"])
        self.assertIn("pdf backend", check["detail"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
