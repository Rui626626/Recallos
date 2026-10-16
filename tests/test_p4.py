"""P4 acceptance tests — 成品汇编、出处回查、长期记忆。

三条计划里的验收标准在这里逐条钉死：

* 第 5 步：成品文件真实存在 / 每个出处都能在数据库里验证到 / 成品本身能被再次搜到
* 第 6 步：同一主题记两次 → 只有 1 个文件且多 1 行修订 / 过期后默认搜不到、加参数能搜到 /
  导出后现有记忆插件能读

外加一条不在计划里但很重要的性质：**索引重建之后 locator 仍然可解析**。
chunk_id 是内容寻址的，所以重建不该让已发出的出处失效——这条一旦破了，
「出处可核查」就是一句空话。

Run with:

    python recallos/tests/test_p4.py
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

from brain.core import memory as mem  # noqa: E402

CORPUS = {
    "经济学/通货膨胀.md": (
        "# 通货膨胀\n\n通货膨胀是物价总水平持续上涨。\n\n"
        "## 发生机制\n\n需求拉动、成本推动与结构性三类。与滞涨并存时政策左右为难。\n"
    ),
    "经济学/滞涨.md": (
        "# 滞涨\n\n经济停滞与通货膨胀同时发生。\n\n## 政策困境\n\n"
        "紧缩治通胀则增长与就业更差，扩张治停滞则通胀更凶。\n"
    ),
    "经济学/the Phillips curve.md": (
        "# The Phillips Curve\n\n"
        "A short-run inverse relationship between unemployment and price increases.\n"
    ),
}


def run_cli(root: str, *args: str, expect: int | None = 0) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["RECALLOS_ROOT"] = root
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run([sys.executable, CLI, *args], capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          env=env, cwd=PROJECT)
    if expect is not None and proc.returncode != expect:
        raise AssertionError(
            f"recall.py {' '.join(args)} exited {proc.returncode} (expected {expect})\n"
            f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}")
    return proc


def cli_json(root: str, *args: str, expect: int | None = 0) -> dict:
    return json.loads(run_cli(root, *args, "--json", expect=expect).stdout)


class P4Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="recallos-p4-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.src = os.path.join(self.root, "src")
        for rel, body in CORPUS.items():
            path = os.path.join(self.src, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body)
        run_cli(self.root, "init")
        run_cli(self.root, "add", self.src)

    def artifact(self) -> str:
        d = os.path.join(self.root, "artifacts")
        files = sorted(os.listdir(d))
        assert files, "artifacts/ 里没有文件"
        return os.path.join(d, files[0])


# ------------------------------------------------------------------ compose


class TestCompose(P4Base):
    def test_creates_a_real_file(self):
        out = cli_json(self.root, "compose", "通货膨胀")
        self.assertTrue(os.path.isfile(out["path"]))
        self.assertGreater(os.path.getsize(out["path"]), 200)

    def test_every_locator_resolves(self):
        out = cli_json(self.root, "compose", "通货膨胀")
        self.assertTrue(out["verification"]["ok"], out["verification"])
        self.assertEqual(out["verification"]["checked"], out["verification"]["resolved"])
        self.assertGreater(out["verification"]["checked"], 0)

    def test_artifact_is_searchable_again(self):
        """闭环：成品本身要能被再次搜到。"""
        out = cli_json(self.root, "compose", "滞涨")
        title = os.path.basename(out["path"])[:-3]
        hits = cli_json(self.root, "search", "政策困境")["hits"]
        self.assertTrue(any(title in h["source_ref"] or "复习材料" in h["title"]
                            for h in hits),
                        f"成品没进索引：{title}")

    def test_no_index_flag(self):
        """--no-index 只写文件，不动索引。"""
        before = cli_json(self.root, "index", "--stats")["counts"]["docs"]
        out = cli_json(self.root, "compose", "通货膨胀", "--no-index",
                       "--out", os.path.join(self.root, "noindex.md"))
        self.assertIsNone(out["indexed"])
        self.assertTrue(os.path.isfile(out["path"]))
        after = cli_json(self.root, "index", "--stats")["counts"]["docs"]
        self.assertEqual(before, after, "--no-index 不应该往索引里加文档")

    def test_kinds_produce_different_bodies(self):
        a = cli_json(self.root, "compose", "通货膨胀", "--kind", "outline", "--out",
                     os.path.join(self.root, "a.md"))
        b = cli_json(self.root, "compose", "通货膨胀", "--kind", "cards", "--out",
                     os.path.join(self.root, "b.md"))
        with open(a["path"], encoding="utf-8") as fh:
            ta = fh.read()
        with open(b["path"], encoding="utf-8") as fh:
            tb = fh.read()
        self.assertIn("大纲", ta)
        self.assertIn("卡片", tb)
        self.assertNotEqual(ta, tb)

    def test_no_hits_is_not_an_error_with_a_file(self):
        proc = run_cli(self.root, "compose", "完全不存在的主题", expect=1)
        self.assertIn("没有命中", proc.stdout + proc.stderr)

    def test_artifact_declares_it_is_not_authorship(self):
        """成品必须自己说清楚：这是汇编，不是原创结论。"""
        out = cli_json(self.root, "compose", "通货膨胀")
        with open(out["path"], encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("资料汇编，不是原创结论", text)
        self.assertIn("## 结论", text)
        self.assertIn("<!-- 由人（或 Agent）补写", text)


# ------------------------------------------------------------------ verify


class TestVerify(P4Base):
    def test_ok_on_fresh_artifact(self):
        out = cli_json(self.root, "compose", "通货膨胀")
        r = cli_json(self.root, "verify", out["path"])
        self.assertTrue(r["ok"])
        self.assertEqual(r["files"][0]["resolved"], r["files"][0]["checked"])

    def test_survives_index_rebuild(self):
        """内容寻址的 chunk_id 必须能扛住索引重建，否则出处就是一次性的。"""
        out = cli_json(self.root, "compose", "通货膨胀")
        run_cli(self.root, "index", "--rebuild")
        r = cli_json(self.root, "verify", out["path"], expect=None)
        self.assertTrue(r["ok"], r["files"][0])

    def test_broken_locator_is_reported(self):
        path = os.path.join(self.root, "fake.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# 假成品\n\n<!-- recallos:locators\n"
                     "doc:deadbeefdeadbeef#h=0000000000@0\n-->\n")
        r = cli_json(self.root, "verify", path, expect=1)
        self.assertFalse(r["ok"])
        self.assertEqual(len(r["files"][0]["missing_doc"]), 1)

    def test_file_without_locators_fails(self):
        path = os.path.join(self.root, "empty.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# 没有出处\n")
        r = cli_json(self.root, "verify", path, expect=1)
        self.assertFalse(r["ok"])
        self.assertIn("没有找到任何 locator", r["files"][0]["error"])

    def test_missing_file(self):
        r = cli_json(self.root, "verify", os.path.join(self.root, "nope.md"), expect=1)
        self.assertFalse(r["ok"])


# ------------------------------------------------------------------ memory


class TestMemory(P4Base):
    def test_remember_creates_one_file(self):
        out = cli_json(self.root, "memory", "remember", "用户在做 AI 比赛",
                       "--topic", "ai比赛")
        self.assertTrue(os.path.isfile(out["path"]))
        self.assertEqual(out["entry"]["revisions"], 1)

    def test_same_topic_twice_is_one_file_with_two_revisions(self):
        """第 6 步验收：同一主题保存两次 → 只有 1 个文件，多了 1 行修订历史。"""
        cli_json(self.root, "memory", "remember", "第一版结论", "--topic", "选题")
        out = cli_json(self.root, "memory", "remember", "第二版结论", "--topic", "选题")

        files = [f for f in os.listdir(os.path.join(self.root, "data", "memory"))
                 if f.endswith(".md")]
        self.assertEqual(files, ["选题.md"], f"应当只有一个文件，实际 {files}")
        self.assertEqual(out["entry"]["revisions"], 2)
        self.assertEqual(len(out["entry"]["history"]), 2,
                         "修订历史应当有 2 行（新旧各一）")
        # 历史是最新在最前的**完整版本日志**
        self.assertIn("第二版结论", out["entry"]["history"][0])
        self.assertIn("第一版结论", out["entry"]["history"][1],
                      "旧结论必须留在历史里，不能被删掉")
        self.assertEqual(out["entry"]["conclusion"], "第二版结论")

    def test_repeated_identical_text_does_not_inflate_revisions(self):
        """把同一句话再粘一次不该产生新版本——那是噪声，不是历史。"""
        cli_json(self.root, "memory", "remember", "一模一样的内容", "--topic", "重复")
        out = cli_json(self.root, "memory", "remember", "一模一样的内容", "--topic", "重复")
        self.assertEqual(out["entry"]["revisions"], 1)
        self.assertEqual(len(out["entry"]["history"]), 1)

    def test_different_topics_are_different_files(self):
        cli_json(self.root, "memory", "remember", "甲", "--topic", "主题甲")
        cli_json(self.root, "memory", "remember", "乙", "--topic", "主题乙")
        files = [f for f in os.listdir(os.path.join(self.root, "data", "memory"))
                 if f.endswith(".md")]
        self.assertEqual(len(files), 2)

    def test_same_text_lands_in_the_same_file(self):
        cli_json(self.root, "memory", "remember", "同一句话")
        cli_json(self.root, "memory", "remember", "同一句话")
        files = [f for f in os.listdir(os.path.join(self.root, "data", "memory"))
                 if f.endswith(".md")]
        self.assertEqual(len(files), 1)

    def test_expired_is_hidden_by_default(self):
        """第 6 步验收：过期后默认搜不到，加参数能搜到。"""
        cli_json(self.root, "memory", "remember",
                 "用户的比赛选题是知识库检索类 Skill", "--topic", "比赛选题")
        self.assertTrue(cli_json(self.root, "search", "比赛选题")["hits"])

        out = cli_json(self.root, "memory", "expire", "比赛选题")
        self.assertIn("_expired", out["path"])

        default = cli_json(self.root, "search", "比赛选题", expect=1)
        self.assertEqual(default["hits"], [], "过期记忆不该被默认搜到")

        with_expired = cli_json(self.root, "search", "比赛选题", "--include-expired")
        self.assertTrue(with_expired["hits"], "加 --include-expired 应当能搜到")

    def test_restore_brings_it_back(self):
        cli_json(self.root, "memory", "remember", "临时结论内容", "--topic", "临时的")
        cli_json(self.root, "memory", "expire", "临时的")
        cli_json(self.root, "memory", "restore", "临时的")
        self.assertTrue(cli_json(self.root, "search", "临时结论内容")["hits"])

    def test_remember_reactivates_an_expired_topic(self):
        cli_json(self.root, "memory", "remember", "旧结论", "--topic", "复活")
        cli_json(self.root, "memory", "expire", "复活")
        out = cli_json(self.root, "memory", "remember", "新结论", "--topic", "复活")
        self.assertTrue(out["reactivated_from"])
        self.assertEqual(out["entry"]["status"], "active")
        self.assertFalse(os.path.exists(os.path.join(
            self.root, "data", "memory", "_expired", "复活.md")))

    def test_list_and_show(self):
        cli_json(self.root, "memory", "remember", "要点内容", "--topic", "列表演示")
        listed = cli_json(self.root, "memory", "list")
        self.assertEqual(listed["count"], 1)
        shown = cli_json(self.root, "memory", "show", "列表演示")
        self.assertEqual(shown["conclusion"], "要点内容")

    def test_missing_topic_is_a_clean_error(self):
        run_cli(self.root, "memory", "show", "不存在的主题", expect=1)
        run_cli(self.root, "memory", "expire", "不存在的主题", expect=1)

    def test_export_matches_the_plugin_format(self):
        """第 6 步验收：导出后现有记忆插件能读。

        DSH 记忆插件的文件形态是固定的 frontmatter（id/scope/path/saved_at/tags）
        加一个 H1 与正文。这里逐字段验证，并用一个「只认这几个键」的读取器读回来。
        """
        cli_json(self.root, "memory", "remember", "导出用内容", "--topic", "导出示意",
                 "--tag", "alpha", "--tag", "beta")
        target = os.path.join(self.root, "exported")
        out = cli_json(self.root, "memory", "export", "--to", target, "--scope", "user")
        self.assertEqual(out["count"], 1)

        path = out["files"][0]
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertTrue(text.startswith("---\n"))

        # 用插件那套键自己解析一遍（不依赖本项目的解析器）
        lines = text.splitlines()
        end = lines.index("---", 1)
        keys = {}
        for raw in lines[1:end]:
            k, v = raw.split(":", 1)
            keys[k.strip()] = v.strip()
        for required in ("id", "scope", "path", "saved_at", "tags"):
            self.assertIn(required, keys, f"缺少插件需要的键：{required}")
        self.assertEqual(keys["scope"], "user")
        self.assertIn("alpha", keys["tags"])
        self.assertTrue(keys["saved_at"].endswith("Z"))
        self.assertRegex(keys["id"], r"^\d{8}-\d{6}-")
        self.assertIn("导出用内容", "\n".join(lines[end:]))

    def test_export_never_overwrites(self):
        cli_json(self.root, "memory", "remember", "内容", "--topic", "重名")
        target = os.path.join(self.root, "exported")
        cli_json(self.root, "memory", "export", "--to", target)
        second = cli_json(self.root, "memory", "export", "--to", target)
        self.assertEqual(len(os.listdir(target)), 2,
                         "第二次导出必须另起一个文件，不能覆盖")
        self.assertTrue(any(f.endswith("-2.md") for f in os.listdir(target)))

    def test_memory_is_authoritative_not_only_in_the_index(self):
        """记忆是权威数据：索引没了它还在，重建后还能搜到。"""
        cli_json(self.root, "memory", "remember", "权威性验证内容", "--topic", "权威")
        shutil.rmtree(os.path.join(self.root, "index"))
        self.assertTrue(os.path.isfile(
            os.path.join(self.root, "data", "memory", "权威.md")))
        run_cli(self.root, "index", "--rebuild", "--from-source")
        self.assertTrue(cli_json(self.root, "search", "权威性验证内容")["hits"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
