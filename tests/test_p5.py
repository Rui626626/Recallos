"""P5 acceptance tests — 零设置检索（`search --in`）与技能安装（`install-skill`）。

这一批测的是「别人下载后直接能用」这件事。它有两个可验证的含义：

1. **没有索引也能搜** —— 全新 clone、一次 `add` 都没跑过，给出一个目录就该出结果。
2. **技能能装到对的地方** —— 装错地方不会报错，只会**永远不生效**，所以必须靠断言钉住。

另外这里守着两个实测踩出来的打包坑：

* `install.ps1` 必须带 UTF-8 BOM（PowerShell 5.1 无 BOM 时按 ANSI 解码）
* `install.sh` 必须纯 LF（一个 CR 就让 `/bin/sh` 报 `'\r': not found`）

Run with:

    python recallos/tests/test_p5.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
CLI = os.path.join(PROJECT, "bin", "recall.py")
sys.path.insert(0, PROJECT)

from brain.adapters import dsh  # noqa: E402
from brain.cli import install_skill  # noqa: E402


def run_cli(root: str, *args: str, expect: int | None = 0,
            extra_env: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["RECALLOS_ROOT"] = root
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("DSH_HOME", None)
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run([sys.executable, CLI, *args], capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          env=env, cwd=PROJECT)
    if expect is not None and proc.returncode != expect:
        raise AssertionError(
            f"recall.py {' '.join(args)} exited {proc.returncode} (expected {expect})\n"
            f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}")
    return proc


def cli_json(root: str, *args: str, expect: int | None = 0,
             extra_env: dict | None = None) -> dict:
    return json.loads(run_cli(root, *args, "--json", expect=expect,
                              extra_env=extra_env).stdout)


SOURCES = {
    "笔记/通货膨胀.md": (
        "# 通货膨胀\n\n通货膨胀是物价总水平持续上涨。\n\n"
        "## 发生机制\n\n需求拉动、成本推动与结构性三类。\n"
    ),
    "笔记/the Phillips curve.md": (
        "# The Phillips Curve\n\n"
        "A short-run inverse relationship between unemployment and price increases.\n"
    ),
}


class LiveBase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="recallos-p5-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.src = os.path.join(self.root, "资料")
        for rel, body in SOURCES.items():
            path = os.path.join(self.src, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body)
        # 一个它读不了的格式，用来验证「不支持的类型被跳过」而不是报错
        with open(os.path.join(self.src, "照片.jpg"), "wb") as fh:
            fh.write(b"\xff\xd8\xff\xe0not-a-real-jpeg")
        # 一个坏掉的 docx（zip 头对，内容不对），用来验证失败留痕
        with open(os.path.join(self.src, "坏文件.docx"), "wb") as fh:
            fh.write(b"PK\x03\x04this is not a valid docx package")


class TestLiveSearch(LiveBase):
    def test_searches_without_any_index(self):
        """核心承诺：一次 add 都没跑过，也要能出结果。"""
        runs = os.path.join(self.root, "index")
        self.assertFalse(os.path.exists(runs))

        payload = cli_json(self.root, "search", "通货膨胀", "--in", self.src)
        self.assertEqual(payload["mode"], "live")
        self.assertTrue(payload["hits"])
        self.assertIn("通货膨胀", payload["hits"][0]["title"])
        self.assertEqual(payload["hits"][0]["why"], "exact")

    def test_does_not_create_a_database(self):
        cli_json(self.root, "search", "通货膨胀", "--in", self.src)
        self.assertFalse(os.path.exists(os.path.join(self.root, "index", "recall.db")),
                         "--in 模式不该顺手建一个索引库")

    def test_unsupported_types_are_skipped(self):
        payload = cli_json(self.root, "search", "通货膨胀", "--in", self.src)
        scanned = payload["scanned"]
        self.assertNotIn("照片.jpg",
                         [os.path.basename(h["path"]) for h in payload["hits"]])
        self.assertEqual(payload["scanned"], 3,
                         "只该扫到 2 个 md + 1 个坏 docx，jpg 不算")

    def test_unreadable_file_is_recorded_not_swallowed(self):
        """铁律：失败必须留痕。坏 docx 要被记下来并说明原因。"""
        payload = cli_json(self.root, "search", "通货膨胀", "--in", self.src)
        self.assertEqual(payload["failed"], 1)
        self.assertIn("坏文件.docx",
                      [os.path.basename(f["path"]) for f in payload["failures"]])
        self.assertTrue(payload["failures"][0]["error"])

    def test_finds_an_english_document(self):
        payload = cli_json(self.root, "search", "Phillips", "--in", self.src)
        self.assertIn("Phillips", json.dumps(payload["hits"], ensure_ascii=False))

    def test_fuzzy_and_pinyin_work_in_live_mode(self):
        typo = cli_json(self.root, "search", "通霍膨胀", "--in", self.src)
        self.assertTrue(typo["hits"], "现场模式也要有同音容错")
        py = cli_json(self.root, "search", "tonghuopengzhang", "--in", self.src)
        self.assertTrue(py["hits"], "现场模式也要支持拼音")

    def test_garbage_query_returns_nothing(self):
        payload = cli_json(self.root, "search", "!!!---???", "--in", self.src,
                           expect=1)
        self.assertEqual(payload["hits"], [])

    def test_no_hit_is_exit_1(self):
        run_cli(self.root, "search", "完全不存在的主题", "--in", self.src, expect=1)

    def test_max_files_truncation_is_reported(self):
        """上限被触发时必须说出来。

        注意这里**不要求有命中**：只扫 1 个文件时扫到哪一个是目录遍历顺序决定的，
        可能恰好是那个读不了的 docx。所以退出码不固定，但「截断了」这件事必须报。
        """
        payload = cli_json(self.root, "search", "通货膨胀", "--in", self.src,
                           "--max-files", "1", expect=None)
        self.assertTrue(payload["truncated"])
        self.assertIn("上限", payload["note"])
        self.assertEqual(payload["scanned"], 1)

    def test_locator_is_distinguishable_from_index_locators(self):
        """live 的出处没法回查，所以**不能**长得像可以回查的那种。"""
        payload = cli_json(self.root, "search", "通货膨胀", "--in", self.src)
        loc = payload["hits"][0]["locator"]
        self.assertTrue(loc.startswith("live:"))
        self.assertNotIn("doc:", loc.split("@")[0][:4])

    def test_rel_path_is_relative_to_the_searched_directory(self):
        payload = cli_json(self.root, "search", "通货膨胀", "--in", self.src)
        rel = payload["hits"][0]["rel_path"]
        self.assertNotIn("..", rel, f"应当是相对被搜目录的路径，实际 {rel}")
        self.assertIn("通货膨胀.md", rel)

    def test_multiple_in_flags(self):
        other = os.path.join(self.root, "别的")
        os.makedirs(other, exist_ok=True)
        with open(os.path.join(other, "滞涨.md"), "w", encoding="utf-8") as fh:
            fh.write("# 滞涨\n\n经济停滞与通货膨胀并存。\n")
        payload = cli_json(self.root, "search", "滞涨", "--in", self.src,
                           "--in", other)
        self.assertEqual(len(payload["roots"]), 2)
        self.assertTrue(payload["hits"])

    def test_original_files_are_untouched(self):
        import hashlib

        def snap():
            out = {}
            for dirpath, _d, files in os.walk(self.src):
                for name in files:
                    full = os.path.join(dirpath, name)
                    with open(full, "rb") as fh:
                        out[full] = hashlib.sha256(fh.read()).hexdigest()
            return out

        before = snap()
        cli_json(self.root, "search", "通货膨胀", "--in", self.src)
        cli_json(self.root, "search", "通霍膨胀", "--in", self.src)
        self.assertEqual(before, snap())

    def test_missing_directory_is_a_clean_error(self):
        proc = run_cli(self.root, "search", "任何东西",
                       "--in", os.path.join(self.root, "不存在"), expect=2)
        self.assertIn("不存在", proc.stdout + proc.stderr)

    def test_empty_index_message_offers_the_live_route(self):
        """空库不是死路：提示里必须给出 --in 这条能立刻照抄的命令。"""
        proc = run_cli(self.root, "search", "任何东西", expect=2)
        text = proc.stdout + proc.stderr
        self.assertIn("--in", text)
        self.assertIn("add", text)

    def test_human_output_mentions_live_mode(self):
        out = run_cli(self.root, "search", "通货膨胀", "--in", self.src).stdout
        self.assertIn("现场模式", out)
        self.assertIn("扫描", out)
        self.assertIn("耗时", out)


class TestInstallSkill(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="recallos-p5-skill-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = os.path.join(self.tmp, "data-root")
        os.makedirs(self.root, exist_ok=True)
        self.dsh_home = os.path.join(self.tmp, "dshhome")
        os.makedirs(self.dsh_home, exist_ok=True)

    def test_install_to_an_explicit_directory(self):
        target_root = os.path.join(self.tmp, "wherever")
        payload = cli_json(self.root, "install-skill", "--to", target_root)
        self.assertEqual(payload["action"], "write")
        self.assertEqual(payload["status"], "installed")
        installed = os.path.join(target_root, "recallos", "SKILL.md")
        self.assertTrue(os.path.isfile(installed))
        with open(installed, encoding="utf-8") as fh:
            self.assertTrue(fh.read().startswith("---\nname: recallos"))

    def test_second_run_is_idempotent(self):
        target_root = os.path.join(self.tmp, "twice")
        cli_json(self.root, "install-skill", "--to", target_root)
        second = cli_json(self.root, "install-skill", "--to", target_root)
        self.assertEqual(second["status"], "up-to-date")
        self.assertEqual(second["action"], "skip")

    def test_refuses_to_clobber_a_different_file(self):
        """用户可能自己改过技能。没让覆盖就不许覆盖。"""
        target_root = os.path.join(self.tmp, "mine")
        target = os.path.join(target_root, "recallos", "SKILL.md")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("---\nname: recallos\ndescription: 我自己改过的\n---\n")
        proc = run_cli(self.root, "install-skill", "--to", target_root, expect=1)
        self.assertIn("--force", proc.stdout + proc.stderr)
        with open(target, encoding="utf-8") as fh:
            self.assertIn("我自己改过的", fh.read(), "不许动用户改过的文件")

    def test_force_overwrites(self):
        target_root = os.path.join(self.tmp, "force")
        target = os.path.join(target_root, "recallos", "SKILL.md")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("旧的\n")
        payload = cli_json(self.root, "install-skill", "--to", target_root,
                           "--force")
        self.assertEqual(payload["action"], "write")
        with open(target, encoding="utf-8") as fh:
            self.assertIn("name: recallos", fh.read())

    def test_user_scope_respects_dsh_home(self):
        payload = cli_json(self.root, "install-skill", "--user",
                           extra_env={"DSH_HOME": self.dsh_home})
        self.assertEqual(payload["scope"], "user")
        self.assertTrue(os.path.isfile(
            os.path.join(self.dsh_home, "skills", "recallos", "SKILL.md")))

    def test_user_root_is_not_nested_twice(self):
        """回归：曾经算成 `~/.dsh/.dsh/skills` —— 一个永远不会被扫描的路径。

        后果非常隐蔽：项目级安装一切正常，只有 `--user` 会「报告成功但永远不生效」。
        """
        payload = cli_json(self.root, "install-skill", "--user",
                           extra_env={"DSH_HOME": self.dsh_home})
        self.assertNotIn(f"{os.sep}.dsh{os.sep}.dsh{os.sep}",
                         payload["target"])
        self.assertEqual(os.path.dirname(os.path.dirname(payload["target"])),
                         os.path.join(self.dsh_home, "skills"))

    def test_adapted_roots_never_nest_dsh_twice(self):
        roots = [p for p, _ in dsh.skill_roots(self.root)]
        for path in roots:
            self.assertNotIn(f"{os.sep}.dsh{os.sep}.dsh{os.sep}",
                             path + os.sep, f"技能根不该出现两层 .dsh: {path}")

    def test_reports_other_roots_and_undo(self):
        out = run_cli(self.root, "install-skill",
                      "--to", os.path.join(self.tmp, "x")).stdout
        self.assertIn("撤销", out)
        self.assertIn("删除", out)


class TestAnchorSelection(unittest.TestCase):
    """技能装在哪：锚点推断 + 「绝不放第二份」。

    `default_anchor` / `existing_install` 都接受一个显式的仓库路径，所以这组测试
    可以在临时目录里跑，不去碰这台机器上真实的技能位置。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="recallos-p5-anchor-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = os.path.join(self.tmp, "RecallOS")
        os.makedirs(self.repo, exist_ok=True)

    def _install_at(self, root: str) -> str:
        target = os.path.join(root, "recallos", "SKILL.md")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("---\nname: recallos\ndescription: x\n---\n")
        return target

    def test_repo_with_git_anchors_to_the_repo(self):
        os.makedirs(os.path.join(self.repo, ".git"), exist_ok=True)
        anchor, reason = install_skill.default_anchor(self.repo)
        self.assertEqual(anchor, self.repo)
        self.assertIn(".git", reason)

    def test_repo_without_git_anchors_to_the_parent(self):
        anchor, reason = install_skill.default_anchor(self.repo)
        self.assertEqual(anchor, self.tmp)
        self.assertIn("parent", reason)

    def test_existing_install_is_found(self):
        target = self._install_at(os.path.join(self.tmp, ".dsh", "skills"))
        found = install_skill.existing_install(self.repo)
        self.assertIsNotNone(found)
        self.assertEqual(found[0], target)

    def test_no_existing_install_returns_none(self):
        self.assertIsNone(install_skill.existing_install(self.repo))

    def test_existing_install_wins_over_a_fresh_anchor(self):
        """回归：仓库一旦 `git init`，锚点会变成仓库自己。

        如果不管「已经装过一份」，就会在现有那份旁边**再放一份**。两份必然漂移，
        而且漂移是静默的——用户以为改动生效了，DSH 读的却是另一份。
        """
        installed = self._install_at(os.path.join(self.tmp, ".dsh", "skills"))
        os.makedirs(os.path.join(self.repo, ".git"), exist_ok=True)

        # 锚点确实变成了仓库……
        anchor, _ = install_skill.default_anchor(self.repo)
        self.assertEqual(anchor, self.repo)
        # ……但「已经装在哪」仍然指向工作区那一份，调用方据此原地更新。
        found = install_skill.existing_install(self.repo)
        self.assertEqual(found[0], installed)
        self.assertNotIn(self.repo, found[0])


class TestPackagingInvariants(unittest.TestCase):
    """打包坑的回归测试。这两条一旦破了，问题会以「莫名其妙」的形式出现。"""

    def test_install_sh_is_pure_lf(self):
        path = os.path.join(PROJECT, "install.sh")
        with open(path, "rb") as fh:
            data = fh.read()
        self.assertEqual(data.count(b"\r"), 0,
                         "install.sh 必须纯 LF；一个 CR 就让 /bin/sh 报 "
                         "'\\r': not found")

    def test_install_ps1_has_a_utf8_bom(self):
        path = os.path.join(PROJECT, "install.ps1")
        with open(path, "rb") as fh:
            head = fh.read(3)
        self.assertEqual(head, b"\xef\xbb\xbf",
                         "install.ps1 必须带 UTF-8 BOM：PowerShell 5.1 在无 BOM 时"
                         "按 ANSI 解码，将来一旦加入中文就会乱码")

    def test_bundled_skill_has_usable_frontmatter(self):
        path = os.path.join(PROJECT, "skills", "deepseek-harness", "SKILL.md")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertTrue(text.startswith("---\n"), "frontmatter 必须在最前面")
        end = text.index("\n---", 3)
        front = text[:end]
        self.assertIn("name: recallos", front)
        self.assertIn("description:", front)
        # 描述太短就触发不了技能，等于装了个没人认出来的东西
        desc = front.split("description:", 1)[1].strip()
        self.assertGreater(len(desc), 40)

    def test_bundled_skill_has_no_author_paths(self):
        """技能是要发给别人的，里面不许出现我这台机器的路径。"""
        path = os.path.join(PROJECT, "skills", "deepseek-harness", "SKILL.md")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        for needle in ("C:\\Users\\", "/Users/", "/home/"):
            self.assertNotIn(needle, text,
                             f"技能文件里不该出现作者机器的绝对路径：{needle}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
