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
import re
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


class TestConsentedScope(unittest.TestCase):
    """一次性授权：问一次、记住，之后一句话就能搜。

    这条修的是一个**产品层面的自相矛盾**：原来的铁律是「只处理明确给出的路径」，
    被实现成了「每次检索前问用户目录」。可用户说得清目录，就说明他知道文件在哪 ——
    那还要这个工具干什么？工具的用途恰恰是他不想记得。

    正确形状是「一次性授权 + 记住 + 硬边界」，这组测试同时钉住三件事：

    1. 没授权时**不扫盘**，并且明确告诉用户怎么授权；
    2. 授权之后 `search "<查询>"` 直接出结果，不再问；
    3. **范围之外永远读不到** —— 这是铁律真正要保护的东西。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="recallos-p5-scope-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = os.path.join(self.tmp, "data-root")
        os.makedirs(self.root, exist_ok=True)

        self.home = os.path.join(self.tmp, "home")
        self.documents = os.path.join(self.home, "Documents")
        self.desktop = os.path.join(self.home, "Desktop")
        self.outside = os.path.join(self.tmp, "outside")
        for path in (self.documents, self.desktop, self.outside):
            os.makedirs(path, exist_ok=True)

        self._write(os.path.join(self.documents, "个人简历.md"),
                    "# 张伟 简历\n\n求职意向：数据分析。\n")
        self._write(os.path.join(self.outside, "独有.md"),
                    "# 机密甲\n\n这一份只在范围之外。\n")

    @staticmethod
    def _write(path: str, body: str) -> None:
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(body)

    def _config(self) -> dict:
        path = os.path.join(self.root, "brain.config.json")
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    # ------------------------------------------------------------------ 未授权

    def test_without_a_scope_search_never_scans_and_says_how(self):
        proc = run_cli(self.root, "search", "简历", expect=2)
        text = proc.stdout + proc.stderr
        self.assertIn("scope default", text, "空库提示必须给出一次性授权这条路")
        self.assertIn("--in", text)
        self.assertIn("add", text)

    def test_scope_show_says_nothing_is_authorised_yet(self):
        payload = cli_json(self.root, "scope", "show")
        self.assertEqual(payload["scope"], [])
        self.assertFalse(payload["configured"])

    # -------------------------------------------------------------------- 授权

    def test_scope_default_authorises_only_directories_that_exist(self):
        payload = cli_json(self.root, "scope", "default", "--home", self.home)
        self.assertEqual(payload["action"], "default")
        self.assertEqual(len(payload["scope"]), 2)
        self.assertIn(os.path.abspath(self.documents), payload["scope"])
        self.assertIn(os.path.abspath(self.desktop), payload["scope"])

    def test_scope_is_persisted_into_the_config(self):
        cli_json(self.root, "scope", "set", self.documents)
        roots = [r["path"] for r in self._config()["roots"]]
        self.assertEqual(roots, [os.path.abspath(self.documents)])

    def test_a_missing_directory_is_reported_and_changes_nothing(self):
        proc = run_cli(self.root, "scope", "set",
                       os.path.join(self.tmp, "根本不存在"), expect=2)
        self.assertIn("不存在", proc.stderr + proc.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.root, "brain.config.json")),
                         "全部路径都无效时不该写出配置")

    def test_dry_run_does_not_write(self):
        cli_json(self.root, "scope", "default", "--home", self.home, "--dry-run")
        self.assertFalse(os.path.exists(os.path.join(self.root, "brain.config.json")))

    # ------------------------------------------------------- 一句话就能搜到

    def test_search_uses_the_scope_without_asking_again(self):
        """这是本次改动的核心：授权之后一条命令出结果，用户不用再报路径。"""
        cli_json(self.root, "scope", "default", "--home", self.home)
        payload = cli_json(self.root, "search", "简历")
        self.assertTrue(payload["via_scope"], "应当说明这次用的是已授权范围")
        self.assertEqual(payload["mode"], "live")
        self.assertTrue(payload["hits"], "授权过的目录里应当搜到简历")
        self.assertEqual(os.path.basename(payload["hits"][0]["path"]), "个人简历.md")

    def test_search_never_reads_outside_the_scope(self):
        """铁律真正要保护的东西：**范围之外一个字节都不读**。"""
        cli_json(self.root, "scope", "set", self.documents)
        payload = cli_json(self.root, "search", "机密甲", expect=None)
        self.assertEqual(payload["hits"], [], "范围之外的文件不该被搜到")

    def test_granting_a_directory_makes_it_searchable(self):
        cli_json(self.root, "scope", "set", self.documents)
        cli_json(self.root, "scope", "add", self.outside)
        payload = cli_json(self.root, "search", "机密甲", expect=None)
        self.assertTrue(payload["hits"], "授权之后就该搜得到")
        self.assertEqual(os.path.basename(payload["hits"][0]["path"]), "独有.md")

    def test_revoking_a_directory_takes_access_away(self):
        cli_json(self.root, "scope", "set", self.documents, self.outside)
        self.assertTrue(cli_json(self.root, "search", "机密甲", expect=None)["hits"])
        cli_json(self.root, "scope", "rm", self.outside)
        revoked = cli_json(self.root, "search", "机密甲", expect=None)
        self.assertEqual(revoked["hits"], [], "撤销之后就不该再读到")

    def test_scope_does_not_touch_a_built_index(self):
        """有索引时走索引，不因为存在 scope 就改走现场模式。"""
        cli_json(self.root, "scope", "set", self.documents)
        cli_json(self.root, "add", self.documents)
        payload = cli_json(self.root, "search", "简历")
        self.assertNotIn("via_scope", payload, "有索引时不该退回现场模式")
        self.assertIn("stages", payload, "索引模式才会给出四段召回的统计")
        self.assertTrue(payload["hits"])


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


class TestInstalledSkillIsSelfLocating(unittest.TestCase):
    """回归：技能装出去之后，AI 还找得到 `recall.py` 吗？

    真实故障：用户下载之后，AI 读到技能说明却找不到命令行，于是回答
    「只有技能说明，`recall.py` 工具和索引库都还没装」，然后一直要用户给目录。
    而工具其实装好了、模糊检索完全可用 —— 它只是**不知道仓库在哪**。

    根因有两层，两层都要钉住：

    1. `install-skill` 只是把 SKILL.md 原样复制，副本里没有任何绝对路径；
    2. 副本离开仓库后，「本文件所在仓库的根目录」指向技能目录本身，是句假话。

    所以这里断言**行为**：照技能里那条命令、从一个毫不相干的目录跑，必须真出结果。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="recallos-p5-locate-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = os.path.join(self.tmp, "data-root")
        os.makedirs(self.root, exist_ok=True)
        self.target_root = os.path.join(self.tmp, "skills")
        self.installed = os.path.join(self.target_root, "recallos", "SKILL.md")

    def _install(self, *extra: str) -> dict:
        return cli_json(self.root, "install-skill", "--to", self.target_root, *extra)

    def _text(self) -> str:
        with open(self.installed, encoding="utf-8") as fh:
            return fh.read()

    def test_installed_skill_records_the_absolute_entry_path(self):
        """技能里必须写着本机入口的**绝对路径**，而且那条路径真的存在。"""
        payload = self._install()
        self.assertEqual(payload["target"], self.installed)
        text = self._text()
        entry = install_skill.entry_path()
        self.assertIn(entry, text, "装好的技能里没有本机入口的绝对路径")
        self.assertTrue(os.path.isfile(entry))

    def test_the_command_in_the_skill_runs_from_any_directory(self):
        """照技能里那条命令、从无关目录跑一次 —— 这是用户真正经历的路径。"""
        self._install()
        text = self._text()
        found = re.findall(r'python3? "([^"]*bin[\\/]recall\.py)"', text)
        self.assertTrue(found, "技能里没有一条可直接照抄的绝对路径命令")
        entry = found[0]
        proc = subprocess.run([sys.executable, entry, "--version"],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", cwd=self.tmp)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("recallos", proc.stdout)

    def test_no_claim_that_the_file_lives_in_the_repository(self):
        """副本已经不在仓库里了，所以「本文件所在仓库的根目录」必须消失。"""
        self._install()
        text = self._text()
        self.assertNotIn("本文件所在仓库的根目录就是它", text)
        self.assertIn(install_skill.ENTRY_MARK, text)
        self.assertIn("不要 `cd`", text)

    def test_reinstall_refreshes_a_stale_path(self):
        """仓库搬过家：正文没动，只更新本机块，且**不需要 --force**。"""
        self._install()
        stale = os.path.join(self.tmp, "搬走了", "bin", "recall.py")
        text = self._text().replace(install_skill.entry_path(), stale)
        with open(self.installed, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        payload = self._install()
        self.assertEqual(payload["status"], "refreshed")
        self.assertEqual(payload["action"], "write")
        self.assertNotIn(stale, self._text())
        self.assertIn(install_skill.entry_path(), self._text())

    def test_reinstall_after_a_pull_is_reported_as_an_upgrade(self):
        """`git pull` 之后技能正文变了：报错必须说「版本更新过」。

        装好的副本不会随仓库更新 —— 用户升级时必然遇到「内容不同」，
        如果把这句话写成「你可能改过」，他会以为升级会毁掉自己的东西，
        于是停在一个用着旧技能的中间状态。
        """
        self._install()
        stale = self._text().replace("第 0 步", "旧版本里的小标题")
        with open(self.installed, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(stale)

        proc = run_cli(self.root, "install-skill", "--to", self.target_root,
                       expect=1)
        text = proc.stdout + proc.stderr
        self.assertIn("版本", text, "必须点明这是版本不一致，不是用户乱改")
        self.assertIn("--force", text)

        payload = self._install("--force")
        self.assertEqual(payload["status"], "outdated")
        self.assertIn("第 0 步", self._text(), "升级后应当是随包版本的正文")
        self.assertNotIn("旧版本里的小标题", self._text())

    def test_legacy_copy_needs_force_and_says_why(self):
        """老版本装出来的副本没有本机块：必须拒绝，并说清加什么参数。"""
        os.makedirs(os.path.dirname(self.installed), exist_ok=True)
        with open(self.installed, "w", encoding="utf-8") as fh:
            fh.write("---\nname: recallos\ndescription: 老副本\n---\n\n# RecallOS\n")
        proc = run_cli(self.root, "install-skill", "--to", self.target_root, expect=1)
        self.assertIn("--force", proc.stdout + proc.stderr)
        self.assertIn("本机路径", proc.stdout + proc.stderr)
        payload = self._install("--force")
        self.assertEqual(payload["action"], "write")
        self.assertIn(install_skill.entry_path(), self._text())

    def test_doctor_catches_a_skill_pointing_at_nothing(self):
        """技能指着一个不存在的入口时，`doctor` 必须喊出来 —— 而不是等用户撞上。"""
        workspace = os.path.join(self.tmp, "ws")
        entry_dir = os.path.join(workspace, ".dsh", "skills", "recallos")
        os.makedirs(entry_dir, exist_ok=True)
        ghost = os.path.join(workspace, "moved", "bin", "recall.py")
        with open(os.path.join(entry_dir, "SKILL.md"), "w", encoding="utf-8") as fh:
            fh.write("---\nname: recallos\ndescription: x\n---\n"
                     f"{install_skill.WHERE_BEGIN}\n"
                     f"{install_skill.ENTRY_MARK} {ghost} -->\n"
                     f"{install_skill.WHERE_END}\n")
        data_root = os.path.join(workspace, "data-root")
        payload = cli_json(data_root, "doctor", expect=1)
        check = next(c for c in payload["checks"] if c["name"] == "skill-path")
        self.assertEqual(check["status"], "fail")
        self.assertIn(ghost, check["detail"])
        self.assertIn("install-skill", check["hint"])


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

    def test_posix_launcher_is_pure_lf_and_self_locating(self):
        """`bin/recall` 没有扩展名，换行符不会自动被 `.gitattributes` 的 *.sh 规则管到。

        一个 CR 就会让 `/bin/sh` 报 `'\r': not found`，而且只在别人 clone 出来时
        才暴露 —— 本地跑测试看到的是工作区里的那份。
        """
        path = os.path.join(PROJECT, "bin", "recall")
        with open(path, "rb") as fh:
            data = fh.read()
        self.assertEqual(data.count(b"\r"), 0, "bin/recall 必须纯 LF")
        self.assertTrue(data.startswith(b"#!/bin/sh"), "必须有可执行的 shebang")
        self.assertIn(b'dirname -- "$0"', data, "必须按自身位置定位仓库")

    def test_windows_launcher_uses_its_own_directory(self):
        """`%~dp0` 是唯一能在任意 cwd 下正确解析仓库位置的写法。"""
        path = os.path.join(PROJECT, "bin", "recall.cmd")
        with open(path, "rb") as fh:
            data = fh.read()
        self.assertIn(b"\r\n", data, "cmd 脚本必须 CRLF")
        self.assertIn(b"%~dp0recall.py", data)

    def test_bundled_template_is_renderable(self):
        """模板必须带界标，渲染两次结果一致 —— 否则每次安装都会「内容不同」。"""
        with open(os.path.join(PROJECT, "skills", "deepseek-harness", "SKILL.md"),
                  encoding="utf-8") as fh:
            template = fh.read()
        self.assertIn(install_skill.WHERE_BEGIN, template)
        self.assertIn(install_skill.WHERE_END, template)
        rendered = install_skill.render_skill(template, os.path.join(PROJECT, "x"))
        self.assertEqual(rendered, install_skill.render_skill(rendered,
                                                              os.path.join(PROJECT, "x")))
        self.assertIn(os.path.join(PROJECT, "x", "bin", "recall.py"), rendered)

    def test_skill_never_shows_a_bare_relative_command(self):
        """技能里不许出现 `python bin/recall.py …` 这种「看着能跑」的命令。

        它只在「cwd 恰好是仓库父目录」时成立，而技能是装在技能根里的 ——
        AI 照抄过去必然报「文件不存在」，然后把这句失败解释成「工具还没装」。
        这条 bug 的起点就是这个形状，所以直接在模板层面禁掉。

        唯一允许的例外：明确跟着 `cd Recallos` 的安装引导（那时 cwd 是对的），
        以及**用来禁止它**的那句话本身（引用坏例子不算使用坏例子）。
        """
        with open(os.path.join(PROJECT, "skills", "deepseek-harness", "SKILL.md"),
                  encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        offenders = []
        for number, line in enumerate(lines, 1):
            if not re.search(r"python3?\s+(bin|recallos)[\\/]recall\.py", line):
                continue
            if "cd " in line:          # 安装引导：先 cd 进仓库，相对路径是对的
                continue
            if any(word in line for word in ("不要", "禁止", "不许")):
                continue               # 这是在禁止这种写法，不是在教它
            offenders.append(f"{number}: {line.strip()}")
        self.assertEqual(offenders, [],
                         "技能里出现了没有基准目录的相对命令（AI 会照抄并失败）：\n"
                         + "\n".join(offenders))

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
