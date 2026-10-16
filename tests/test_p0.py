"""P0 acceptance tests — layout, self-check and the index foundation.

Every case drives the real command line in a subprocess (or the real `Store`
class) against a throwaway root, then asserts on **real files and real database
rows**. No assertion depends on a model's description of what happened, and no
test mocks the code under test.

Run with:

    python recallos/tests/test_p0.py
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)          # …/default-workspace/recallos
SKILL_NAME = "recallos"

# Use the real package, but a disposable data root.
sys.path.insert(0, PROJECT)

from brain.adapters.dsh import find_project_root, skill_entry_path, skill_roots  # noqa: E402
from brain.core import schema as schema_mod                                      # noqa: E402
from brain.core.store import Store                                               # noqa: E402

CLI = os.path.join(PROJECT, "bin", "recall.py")


def run_cli(root: str | None, *args: str, expect: int | None = 0,
            cwd: str | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    if root is None:
        env.pop("RECALLOS_ROOT", None)
    else:
        env["RECALLOS_ROOT"] = root
    proc = subprocess.run(
        [sys.executable, CLI, *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, cwd=cwd or PROJECT,
    )
    if expect is not None and proc.returncode != expect:
        raise AssertionError(
            f"recall.py {' '.join(args)} exited {proc.returncode} (expected {expect})\n"
            f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
        )
    return proc


def cli_json(root: str | None, *args: str, expect: int | None = 0) -> dict:
    return json.loads(run_cli(root, *args, "--json", expect=expect).stdout)


def find_check(payload: dict, name: str) -> dict:
    for check in payload["checks"]:
        if check["name"] == name:
            return check
    raise AssertionError(f"no check named {name!r} in {[c['name'] for c in payload['checks']]}")


class P0TestBase(unittest.TestCase):
    """Builds a sandbox that mirrors the real layout.

        <tmp>/.dsh/skills/recallos/SKILL.md     ← the host's project skill root
        <tmp>/recallos/                          ← RECALLOS_ROOT
    """

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="recallos-p0-")
        self.root = os.path.join(self.tmp, "recallos")
        os.makedirs(self.root)
        entry = os.path.join(self.tmp, ".dsh", "skills", SKILL_NAME)
        os.makedirs(entry)
        with open(os.path.join(entry, "SKILL.md"), "w", encoding="utf-8") as fh:
            fh.write("---\nname: recallos\ndescription: test stub\n---\n")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def add_placeholder_test_file(self) -> None:
        """Make the tests-directory check satisfiable in the sandbox."""
        tests = os.path.join(self.root, "tests")
        os.makedirs(tests, exist_ok=True)
        with open(os.path.join(tests, "test_smoke.py"), "w", encoding="utf-8") as fh:
            fh.write("def test_ok():\n    assert True\n")


class TestInit(P0TestBase):

    def test_01_init_creates_the_layout_in_an_empty_root(self) -> None:
        """Empty project → every required directory really exists on disk."""
        self.assertFalse(os.path.exists(os.path.join(self.root, "data")))

        payload = cli_json(self.root, "init")

        for rel in ("data", "data/docs", "index", "state", "cache", "tests"):
            path = os.path.join(self.root, *rel.split("/"))
            self.assertTrue(os.path.isdir(path), f"{rel} was not created")

        config = os.path.join(self.root, "brain.config.json")
        self.assertTrue(os.path.isfile(config))
        with open(config, encoding="utf-8") as fh:
            json.load(fh)  # must be valid JSON on disk

        self.assertEqual(payload["config"], "created")
        self.assertTrue(payload["config_ok"])
        self.assertIn("data", payload["dirs"]["created"])

    def test_02_init_does_not_overwrite_existing_user_data(self) -> None:
        """A second init must leave every existing byte alone."""
        cli_json(self.root, "init")

        # A user file inside data/, plus a hand-edited configuration file.
        note = os.path.join(self.root, "data", "profile.md")
        with open(note, "w", encoding="utf-8") as fh:
            fh.write("# 我的画像\n不要覆盖我\n")
        config = os.path.join(self.root, "brain.config.json")
        with open(config, "w", encoding="utf-8") as fh:
            fh.write('{"version": 1, "roots": [], "custom": "hand-edited"}')

        payload = cli_json(self.root, "init")

        with open(note, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "# 我的画像\n不要覆盖我\n")
        with open(config, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["custom"], "hand-edited")
        self.assertEqual(payload["config"], "exists")
        self.assertEqual(payload["dirs"]["created"], [])
        self.assertIn("data", payload["dirs"]["existing"])

    def test_03_missing_directory_is_detected(self) -> None:
        """Deleting a required directory must be reported, not silently fixed."""
        cli_json(self.root, "init")
        self.add_placeholder_test_file()

        shutil.rmtree(os.path.join(self.root, "state"))

        payload = cli_json(self.root, "doctor", expect=1)
        layout = find_check(payload, "layout")
        self.assertEqual(layout["status"], "fail")
        self.assertIn("state", layout["detail"])
        self.assertIn("init", layout["hint"])

        # doctor must not have rebuilt it: it reports, it does not repair.
        self.assertFalse(os.path.isdir(os.path.join(self.root, "state")))

    def test_04_doctor_fails_when_configuration_is_absent(self) -> None:
        """A missing brain.config.json is a failure with a fix command."""
        cli_json(self.root, "init")
        self.add_placeholder_test_file()
        os.unlink(os.path.join(self.root, "brain.config.json"))

        payload = cli_json(self.root, "doctor", expect=1)
        check = find_check(payload, "tests+config")
        self.assertEqual(check["status"], "fail")
        self.assertIn("brain.config.json", check["detail"])

    def test_05_doctor_fails_when_the_skill_entry_is_missing(self) -> None:
        """Skill 入口是两件事，状态必须分开报。

        * 仓库自带的 `skills/deepseek-harness/SKILL.md` —— **打包要求**，缺了算失败
        * 装进 DSH 的那一份 —— **可选的宿主集成**，缺了只能是警告

        旧版本把第二件事也当成失败，后果是「全新 clone 一跑 doctor 就报 FAIL」。
        那不是环境有问题，而是把可选步骤当成了必需步骤——按项目的铁律，
        这种误报和「把没实现的说成通过」是同一类错误，方向相反而已。
        """
        cli_json(self.root, "init")
        self.add_placeholder_test_file()
        shutil.rmtree(os.path.join(self.tmp, ".dsh"))

        payload = cli_json(self.root, "doctor", expect=0)
        check = find_check(payload, "tests+config")
        self.assertEqual(check["status"], "warn", check)
        self.assertIn("SKILL.md", check["detail"])
        # 仓库自带的那一份必须被认出来——这是真的打包要求。
        self.assertIn("skills/deepseek-harness/SKILL.md present", check["detail"])
        # 而且原因要说清楚：命令行不依赖它。
        self.assertIn("optional", (check.get("hint") or "").lower())


class TestDoctor(P0TestBase):

    def test_06_doctor_passes_on_a_healthy_root(self) -> None:
        cli_json(self.root, "init")
        self.add_placeholder_test_file()

        payload = cli_json(self.root, "doctor", expect=0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["summary"]["fail"], 0)

        # Requirements 2, 3 and 4 must be genuinely exercised, not skipped.
        self.assertEqual(find_check(payload, "python")["status"], "pass")
        sqlite_check = find_check(payload, "sqlite")
        self.assertEqual(sqlite_check["status"], "pass")
        self.assertIn("FTS5", sqlite_check["detail"])
        self.assertEqual(find_check(payload, "data-dir")["status"], "pass")

    def test_07_absent_database_is_reported_as_empty_not_missing(self) -> None:
        """Requirement 5 — no database yet is a normal, explicit state."""
        cli_json(self.root, "init")
        self.add_placeholder_test_file()

        db_path = os.path.join(self.root, "index", "recall.db")
        self.assertFalse(os.path.exists(db_path))

        payload = cli_json(self.root, "doctor", expect=0)
        absent = find_check(payload, "index(absent)")
        opened = find_check(payload, "index(open)")

        self.assertEqual(absent["status"], "pass")
        self.assertIn("empty", absent["detail"])
        self.assertEqual(opened["status"], "skip")
        self.assertIn("nothing to open", opened["detail"])

    def test_08_existing_database_opens_and_answers_a_query(self) -> None:
        """Requirement 6 — connect, read the schema, count rows."""
        cli_json(self.root, "init")
        self.add_placeholder_test_file()
        db_path = os.path.join(self.root, "index", "recall.db")

        # Build a real database through the real Store class.
        store = Store.open(db_path)
        try:
            store.upsert_doc(
                doc_id="d000000000001", source_kind="file",
                source_ref=os.path.join(self.tmp, "a.md"),
                title="t", content_hash="h", doc_hash="h",
                mtime=1.0, size=2, status="indexed", extractor="test",
                char_count=3, chunk_count=0,
            )
            store.conn.commit()
            self.assertEqual(store.counts()["docs"], 1)
        finally:
            store.close()

        payload = cli_json(self.root, "doctor", expect=0)
        opened = find_check(payload, "index(open)")
        self.assertEqual(opened["status"], "pass")
        self.assertIn(f"schema v{schema_mod.SCHEMA_VERSION}", opened["detail"])
        self.assertIn("docs=1", opened["detail"])

        self.assertEqual(find_check(payload, "index(absent)")["status"], "skip")

    def test_09_unknown_schema_version_fails_with_a_rebuild_hint(self) -> None:
        """A stale derived index must fail loudly instead of being trusted."""
        cli_json(self.root, "init")
        self.add_placeholder_test_file()
        db_path = os.path.join(self.root, "index", "recall.db")

        con = sqlite3.connect(db_path)
        try:
            con.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            con.execute("INSERT INTO meta VALUES('schema_version','999')")
            con.commit()
        finally:
            con.close()

        payload = cli_json(self.root, "doctor", expect=1)
        opened = find_check(payload, "index(open)")
        self.assertEqual(opened["status"], "fail")
        self.assertIn("999", opened["detail"])
        self.assertIn("index/", opened["hint"])

    def test_10_doctor_exit_code_is_nonzero_on_failure_and_zero_on_success(self) -> None:
        """Requirement 8 — the exit code is the machine-readable verdict."""
        cli_json(self.root, "init")
        self.add_placeholder_test_file()
        run_cli(self.root, "doctor", expect=0)

        shutil.rmtree(os.path.join(self.root, "cache"))
        proc = run_cli(self.root, "doctor", expect=1)
        self.assertIn("FAILED", proc.stdout)

    def test_11_statuses_are_never_inflated(self) -> None:
        """A check that is not `pass` must never be counted as one.

        This test previously asserted that the ingest layer was reported as
        `todo`, which was the truthful status while P1 was unimplemented. P1 now
        exists, so the durable property is asserted instead: the summary
        arithmetic must agree with the per-check statuses, and a `pass` must
        carry evidence rather than a claim.
        """
        cli_json(self.root, "init")
        self.add_placeholder_test_file()

        payload = cli_json(self.root, "doctor", expect=0)
        checks = payload["checks"]

        counted_pass = sum(1 for c in checks if c["status"] == "pass")
        self.assertEqual(payload["summary"]["pass"], counted_pass)
        # Every check must fall into exactly one reported bucket — a status
        # that never reaches the summary would let a problem go uncounted.
        skipped = sum(1 for c in checks if c["status"] == "skip")
        self.assertEqual(
            payload["summary"]["pass"] + payload["summary"]["warn"]
            + payload["summary"]["fail"] + payload["summary"]["todo"] + skipped,
            len(checks),
        )

        p1 = find_check(payload, "p1-ingest")
        self.assertIn(p1["status"], ("pass", "warn", "fail"))
        if p1["status"] == "pass":
            # Passing must name what it actually found.
            self.assertIn("extensions", p1["detail"])
            self.assertIn("pdf backend", p1["detail"])


class TestIndexFoundation(P0TestBase):

    def test_12_database_survives_close_and_reopen(self) -> None:
        """Write, close, reopen a fresh connection, read the same rows back."""
        cli_json(self.root, "init")
        db_path = os.path.join(self.root, "index", "recall.db")

        store = Store.open(db_path)
        try:
            store.upsert_doc(
                doc_id="droundtrip", source_kind="file",
                source_ref=os.path.join(self.tmp, "roundtrip.md"),
                title="往返测试", content_hash="abc123", doc_hash="abc123",
                mtime=42.0, size=99, status="indexed", extractor="test",
                char_count=7, chunk_count=0,
            )
            store.conn.commit()
        finally:
            store.close()

        self.assertTrue(os.path.isfile(db_path))
        self.assertGreater(os.path.getsize(db_path), 0)

        # A brand-new process-level connection, not the object we closed.
        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        try:
            row = con.execute(
                "SELECT doc_id, title, content_hash, status FROM doc WHERE doc_id=?",
                ("droundtrip",),
            ).fetchone()
        finally:
            con.close()

        self.assertIsNotNone(row)
        self.assertEqual(row["title"], "往返测试")
        self.assertEqual(row["content_hash"], "abc123")
        self.assertEqual(row["status"], "indexed")

        # And the schema version survived too.
        store = Store.open(db_path)
        try:
            self.assertEqual(schema_mod.read_version(store.conn), schema_mod.SCHEMA_VERSION)
            self.assertEqual(store.counts()["docs"], 1)
        finally:
            store.close()

    def test_13_store_reset_empties_the_database_in_place(self) -> None:
        """The derived index must be wipeable without deleting the file."""
        cli_json(self.root, "init")
        db_path = os.path.join(self.root, "index", "recall.db")

        store = Store.open(db_path)
        try:
            store.upsert_doc(
                doc_id="dtmp", source_kind="file", source_ref="x",
                title="t", content_hash="h", doc_hash="h", mtime=0.0, size=0,
                status="indexed", extractor="t", char_count=0, chunk_count=0,
            )
            store.conn.commit()
            self.assertEqual(store.counts()["docs"], 1)

            store.reset()
            self.assertEqual(store.counts()["docs"], 0)
            self.assertEqual(schema_mod.read_version(store.conn),
                             schema_mod.SCHEMA_VERSION)
        finally:
            store.close()

    def test_14_schema_is_stable_and_declares_its_version(self) -> None:
        cli_json(self.root, "init")
        db_path = os.path.join(self.root, "index", "recall.db")
        store = Store.open(db_path)
        try:
            tables = {r[0] for r in store.conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
        finally:
            store.close()
        for expected in ("doc", "chunk", "chunk_fts", "chunk_fts_cjk", "meta"):
            self.assertIn(expected, tables)
        # Assert the property, not a literal: the store must report whatever
        # the module declares, so bumping the schema cannot silently pass.
        self.assertGreaterEqual(schema_mod.SCHEMA_VERSION, 1)
        store = Store.open(db_path)
        try:
            self.assertEqual(schema_mod.read_version(store.conn),
                             schema_mod.SCHEMA_VERSION)
        finally:
            store.close()


class TestHostAdapter(P0TestBase):

    def test_15_skill_entry_path_matches_the_verified_host_rule(self) -> None:
        """The adapter must point at the project skill root, not inside code.

        `find_project_root(cwd)` answers "what would DSH treat as the project
        root if the session cwd were `cwd`" — with no `.git` anywhere it falls
        back to `cwd` itself, which is why the caller passes the *workspace*
        directory rather than the code directory.
        """
        expected = os.path.join(self.tmp, ".dsh", "skills", SKILL_NAME, "SKILL.md")
        self.assertEqual(find_project_root(self.tmp), self.tmp)
        self.assertEqual(skill_entry_path(self.tmp, SKILL_NAME), expected)
        self.assertTrue(os.path.isfile(expected))

        roots = [p for p, _ in skill_roots(self.tmp)]
        self.assertEqual(roots[0], os.path.join(self.tmp, ".dsh", "skills"))

        # Decisive point: the entry must NOT live inside the code directory.
        self.assertFalse(expected.startswith(self.root + os.sep),
                         "SKILL.md would never be discovered inside the code tree")

    def test_17_the_shipped_skill_can_be_installed(self) -> None:
        """发行包里带着完整的技能，而且**装得进宿主会扫的位置**。

        旧版本断言「这台机器上技能已经装好了」——那是开发者的个人状态，不是交付
        要求。刚 clone 的人本来就没装（装技能是可选的一步），断言必然失败。

        现在测的是**机制**：技能文件在不在、frontmatter 完不完整、装到指定位置后
        位置对不对。这样在任何一台干净的机器上都成立。
        """
        bundled = os.path.join(PROJECT, "skills", "deepseek-harness", "SKILL.md")
        self.assertTrue(os.path.isfile(bundled), "发行包里应当带着技能文件")
        with open(bundled, encoding="utf-8") as fh:
            head = fh.read(400)
        self.assertTrue(head.startswith("---"), "SKILL.md needs YAML frontmatter")
        self.assertIn("name: recallos", head)

        # 真的装一次，验证落点正确
        target_root = os.path.join(self.tmp, "skill-root")
        run_cli(None, "install-skill", "--to", target_root, expect=0)
        placed = os.path.join(target_root, SKILL_NAME, "SKILL.md")
        self.assertTrue(os.path.isfile(placed), f"技能没装到 {placed}")
        with open(placed, encoding="utf-8") as fh:
            self.assertIn("name: recallos", fh.read(400))
        # 决定性的一点：技能绝不能装在代码目录**里面**，那样宿主永远发现不了。
        self.assertFalse(placed.startswith(PROJECT + os.sep),
                         "SKILL.md would never be discovered inside the code tree")

    def test_16_the_shipped_project_can_be_healthy(self) -> None:
        """交付出去的仓库**能**通过自检 —— 而不是「我这台机器已经配好了」。

        这个区别是实测撞出来的：旧版本直接断言 `doctor` 退 0，于是每一个刚
        `git clone` 下来的人都会看到这条失败——因为 `data/`、`index/` 这些目录
        都被 `.gitignore` 排除了，克隆出来的仓库本来就没有，得先跑 `init`。

        现在先跑 `init`（幂等，产物全在 `.gitignore` 里），再要求全绿。
        如果发行包本身是坏的（配置缺失、文件读不了），这条仍然会失败。
        """
        run_cli(None, "init", expect=0)
        payload = cli_json(None, "doctor", expect=0)
        self.assertTrue(payload["ok"], json.dumps(payload, ensure_ascii=False))
        self.assertEqual(payload["root"], PROJECT)

        # 这一项在干净克隆上应当是 pass 或 warn，**绝不能是 fail**：
        # 「把技能装进 DSH」是可选的一步，没装只是警告。而发行包自带的
        # skills/deepseek-harness/SKILL.md 必须被认出来——那是交付要求。
        check = find_check(payload, "tests+config")
        self.assertNotEqual(check["status"], "fail", check)
        self.assertIn("skills/deepseek-harness/SKILL.md present", check["detail"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
