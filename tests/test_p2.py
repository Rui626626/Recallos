"""P2 acceptance tests — retrieval, fuzzy matching, alias map, context budget.

Two layers, deliberately:

* **Unit** tests for `brain/core/fuzzy.py`. The fuzzy rules are pure functions, so
  they can be pinned exactly — thresholds, coverage ladders, alias directionality.
* **Black-box** tests that drive the real CLI in a subprocess against a throwaway
  root and assert on exit codes and JSON, never on prose.

The single most important case here is ``test_alias_bridges_languages``: a document
whose text contains **no Chinese at all** must still be found by its Chinese name.
That is the documented R1 limitation, and it is only solvable with an alias map —
statistical matching has nothing to match against.

Run with:

    python recallos/tests/test_p2.py
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

from brain.core import fuzzy  # noqa: E402

# --------------------------------------------------------------------------- helpers


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


# --------------------------------------------------------------------- unit: fuzzy


class FuzzyUnits(unittest.TestCase):
    def test_cjk_chars_and_runs(self):
        self.assertEqual(fuzzy.cjk_chars("复习abc资料"), ["复", "习", "资", "料"])
        self.assertEqual(fuzzy.cjk_runs("复习abc资料"), ["复习", "资料"])

    def test_latin_words_lowercased(self):
        self.assertEqual(fuzzy.latin_words("The Phillips Curve!"),
                         ["the", "phillips", "curve"])

    def test_latin_trigrams(self):
        self.assertEqual(fuzzy.latin_trigrams("abcd"), {"abc", "bcd"})
        self.assertEqual(fuzzy.latin_trigrams("ab"), {"ab"})

    def test_char_bigrams(self):
        self.assertEqual(fuzzy.char_bigrams("通货膨胀"), {"通货", "货膨", "膨胀"})
        self.assertEqual(fuzzy.char_bigrams("书"), {"书"})

    def test_similarity_exact_is_one(self):
        """精确匹配必须拿到满分 —— 否则它和一个错别字命中排不出先后。"""
        self.assertAlmostEqual(
            fuzzy.similarity("通货膨胀", "通货膨胀是指物价总水平持续上涨"), 1.0, places=2)

    def test_similarity_typo_is_lower_but_alive(self):
        exact = fuzzy.similarity("通货膨胀", "通货膨胀是指物价总水平持续上涨")
        typo = fuzzy.similarity("通或膨胀", "通货膨胀是指物价总水平持续上涨")
        self.assertGreater(typo, 0.4, "一个字写错不该把命中打没")
        self.assertLess(typo, exact, "错别字必须排在精确匹配后面")

    def test_similarity_unrelated_is_low(self):
        self.assertLess(fuzzy.similarity("通货膨胀", "大别山精神与革命传统教育"), 0.2)

    def test_min_coverage_ladder(self):
        self.assertEqual(fuzzy.min_coverage(2), 1.0)
        self.assertEqual(fuzzy.min_coverage(4), 0.66)
        self.assertEqual(fuzzy.min_coverage(8), 0.5)
        self.assertEqual(fuzzy.min_coverage(20), 0.4)

    def test_two_char_query_needs_full_coverage(self):
        """两个字里只中一个的召回几乎必然是噪声，必须拦住。"""
        self.assertFalse(fuzzy.passes_threshold("复习", "这里只提到一个复字而已"))
        self.assertTrue(fuzzy.passes_threshold("复习", "考前复习资料整理"))

    def test_looks_like_garbage(self):
        """「一个可搜索字符都没有」才算乱码。

        `asdfghjkl` 是**合法**的拉丁词，只是库里没有 —— 它走正常流程并返回空结果，
        不应该被当成乱码。两者对用户的含义不同：前者是「你打错符号了」，后者是
        「库里确实没有」。
        """
        self.assertTrue(fuzzy.looks_like_garbage("!!!---???"))
        self.assertTrue(fuzzy.looks_like_garbage(""))
        self.assertTrue(fuzzy.looks_like_garbage("   "))
        self.assertFalse(fuzzy.looks_like_garbage("asdfghjkl!!!"))
        self.assertFalse(fuzzy.looks_like_garbage("复习"))
        self.assertFalse(fuzzy.looks_like_garbage("inflation"))

    def test_probe_terms(self):
        p = fuzzy.probe_terms("通货膨胀 gini")
        self.assertEqual(p["literal"], "通货膨胀 gini")
        self.assertEqual(p["cjk"], ["通", "货", "膨", "胀"])
        self.assertIn("gin", p["trigram"])
        self.assertEqual(p["words"], ["gini"])

    def test_alias_key_normalises(self):
        self.assertEqual(fuzzy.alias_key("  Phillips  Curve "), "phillips curve")
        self.assertEqual(fuzzy.alias_key("菲利普斯"), "菲利普斯")

    def test_expand_query_both_directions(self):
        merged = {"菲利普斯": ["phillips", "phillips curve"]}
        self.assertEqual(fuzzy.expand_query("菲利普斯", merged),
                         ["phillips", "phillips curve"])
        self.assertIn("菲利普斯", fuzzy.expand_query("phillips", merged))

    def test_expand_query_no_identity(self):
        merged = {"菲利普斯": ["phillips"]}
        self.assertNotIn("菲利普斯", fuzzy.expand_query("菲利普斯", merged))

    def test_seed_aliases_cover_the_documented_gap(self):
        """R1 的具体例子：搜「菲利普斯」必须能找到 the Phillips curve。"""
        self.assertIn("菲利普斯", fuzzy.SEED_ALIASES)
        self.assertIn("phillips", fuzzy.SEED_ALIASES["菲利普斯"])

    def test_learn_pairs_chinese_then_english(self):
        pairs = fuzzy.learn_pairs("基尼系数（Gini Coefficient）用于衡量分配不平等。")
        self.assertEqual(pairs.get("基尼系数"), ["Gini Coefficient"])

    def test_learn_pairs_english_then_chinese(self):
        pairs = fuzzy.learn_pairs("The Gini Coefficient (基尼系数) measures inequality.")
        self.assertEqual(pairs.get("基尼系数"), ["The Gini Coefficient"])

    def test_learn_pairs_ignores_same_language_parens(self):
        self.assertEqual(fuzzy.learn_pairs("复习资料（整理版）"), {})

    def test_merge_learned_is_idempotent(self):
        doc = fuzzy.empty_alias_doc()
        pairs = {"基尼系数": ["Gini Coefficient"]}
        self.assertEqual(fuzzy.merge_learned(doc, pairs), 1)
        self.assertEqual(fuzzy.merge_learned(doc, pairs), 0)

    def test_merge_learned_never_touches_hand_written(self):
        doc = fuzzy.empty_alias_doc()
        doc["aliases"] = {"我自己写的": ["mine"]}
        fuzzy.merge_learned(doc, {"别的": ["other"]})
        self.assertEqual(doc["aliases"], {"我自己写的": ["mine"]})
        self.assertIn("别的", doc["learned"])


# ------------------------------------------------------------------ black box: CLI


CORPUS = {
    # 中文笔记，没有任何英文
    "笔记/通货膨胀.md": (
        "# 通货膨胀\n\n"
        "通货膨胀是指物价总水平在一段时间内持续上涨。\n"
        "按成因可分为需求拉动、成本推动与结构性通胀。\n\n"
        "## 相关笔记\n\n"
        "与 滞涨 同时出现时政策会左右为难。\n"
    ),
    # 中文笔记，带一处中英对照（供 alias learn 学）
    "笔记/基尼系数.md": (
        "# 基尼系数\n\n"
        "基尼系数（Gini Coefficient）是衡量收入分配不平等的常用指标。\n"
        "取值 0 到 1，越大越不平等。\n"
    ),
    # 纯英文笔记：正文里一个汉字都没有 —— 中英互指只能靠别名表
    "笔记/the Phillips curve.md": (
        "# The Phillips Curve\n\n"
        "The Phillips curve describes a short-run inverse relationship between "
        "unemployment and the rate of price increases.\n"
        "In the long run the curve is vertical.\n"
    ),
    # 回归夹具：标题与小标题**都不出现在正文里**。
    # 只索引正文的实现在这条上会返回空 —— 那是实测发现的真实盲区。
    "笔记/冷僻标题的文档.md": (
        "# 冷僻标题的文档\n\n"
        "这一段正文刻意不提标题里的任何字，也不提下面小标题里的任何字。\n"
        "它只讲一件无关的事：天气与季节变化对农作物的影响。\n\n"
        "## 独特小节名称\n\n"
        "这一节同样，正文只说结论，不重复上面那五个字。\n"
    ),
}


class P2Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="recallos-p2-")
        self.addCleanup(self._cleanup)
        src = os.path.join(self.root, "src")
        os.makedirs(src, exist_ok=True)
        self.src = src
        for rel, body in CORPUS.items():
            path = os.path.join(src, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body)
        run_cli(self.root, "init")
        run_cli(self.root, "add", src)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)

    def titles(self, *args, **kw):
        payload = cli_json(self.root, *args, **kw)
        return [h["title"] for h in payload["hits"]], payload


class TestExactSearch(P2Base):
    def test_exact_hit(self):
        titles, payload = self.titles("search", "通货膨胀")
        self.assertIn("通货膨胀", titles)
        self.assertGreaterEqual(payload["stages"]["exact"], 1)

    def test_no_hit_returns_exit_1(self):
        run_cli(self.root, "search", "完全不存在的主题", expect=1)

    def test_empty_query_is_not_an_error(self):
        out = cli_json(self.root, "search", "   ")
        self.assertEqual(out["hits"], [])


class TestFuzzySearch(P2Base):
    def test_typo_still_finds(self):
        titles, payload = self.titles("search", "通或膨胀")
        self.assertIn("通货膨胀", titles)
        self.assertTrue(payload["fuzzy_used"])

    def test_partial_finds(self):
        titles, _ = self.titles("search", "基尼")
        self.assertIn("基尼系数", titles)

    def test_garbage_returns_nothing(self):
        payload = cli_json(self.root, "search", "!!!---???", expect=1)
        self.assertEqual(payload["hits"], [])
        self.assertTrue(payload["garbage_query"])

    def test_unknown_latin_token_returns_nothing(self):
        """合法但不存在的词：返回空，且**不算乱码**（含义不同）。"""
        payload = cli_json(self.root, "search", "asdfghjkl!!!", expect=1)
        self.assertEqual(payload["hits"], [])
        self.assertFalse(payload["garbage_query"])

    def test_no_fuzzy_disables_the_third_stage(self):
        payload = cli_json(self.root, "search", "通或膨胀", "--no-fuzzy", expect=1)
        self.assertEqual(payload["hits"], [])
        self.assertFalse(payload["fuzzy_used"])

    def test_unrelated_query_does_not_pad_results(self):
        """噪声门槛：一个八竿子打不着的词不能靠模糊匹配硬凑出结果。"""
        payload = cli_json(self.root, "search", "大别山革命传统教育", expect=1)
        self.assertEqual(payload["hits"], [])

    def test_alias_bridges_languages(self):
        """R1 验收：纯英文文档必须能被中文名搜到。

        这是整套设计里唯一不能用统计方法解决的一类，只能靠别名表 —— 所以这条
        测试失败就意味着「中英互指」这个能力没了。
        """
        titles, payload = self.titles("search", "菲利普斯")
        self.assertIn("The Phillips Curve", titles)
        self.assertIn("phillips", payload["expanded"])
        self.assertTrue(all(h["why"].startswith("alias") for h in payload["hits"]))

    def test_alias_works_from_the_other_side(self):
        titles, payload = self.titles("search", "gini")
        self.assertIn("基尼系数", titles)


class TestHeadingsAreSearchable(P2Base):
    """回归：**标题与小标题必须可被搜到**。

    这两条守的是一个实测发现的真实盲区。原来的 `replace_chunks` 只把 `chunk.text`
    写进 FTS，而文档标题和 `heading_path` 都不在正文里，于是：

    * 搜文档自己的标题 → 返回空
    * 搜某一节的名字 → 返回空

    对一个「找回我保存过的东西」的工具来说，这等于废掉一半。夹具里的标题与小标题
    刻意不出现在正文中，只索引正文的实现必然失败。
    """

    def test_document_title_is_searchable(self):
        titles, _ = self.titles("search", "冷僻标题的文档")
        self.assertIn("冷僻标题的文档", titles)

    def test_section_heading_is_searchable(self):
        titles, payload = self.titles("search", "独特小节名称")
        self.assertIn("冷僻标题的文档", titles)
        self.assertIn("独特小节名称", payload["hits"][0]["heading_path"])

    def test_short_heading_works_through_the_two_char_channel(self):
        """小标题里抽两个字也必须能命中（走逐字索引那条通道）。"""
        titles, _ = self.titles("search", "小节名称")
        self.assertIn("冷僻标题的文档", titles)


class TestPinyin(P2Base):
    """拼音：输入拼音找中文，以及**同音错字容错**。

    拼音表是随包数据（`brain/data/pinyin.tsv.gz`），不依赖任何第三方库；
    删掉那个文件就自动降级为「不索引拼音」，而不是报错。
    """

    def test_table_is_bundled_and_non_empty(self):
        from brain.core import pinyin
        self.assertTrue(pinyin.available(), "内置拼音表应当随包发布")
        self.assertGreater(len(pinyin.table()), 20000)

    def test_pinyin_projection(self):
        from brain.core import pinyin
        self.assertEqual(pinyin.to_pinyin("通货膨胀"), "tonghuopengzhang")
        self.assertEqual(pinyin.to_pinyin("abc"), "")
        self.assertEqual(pinyin.to_pinyin("abc", keep_other=True), "abc")

    def test_homophones_share_a_pinyin(self):
        """同音字必须落到同一个拼音串上 —— 这是同音容错的全部依据。"""
        from brain.core import pinyin
        self.assertEqual(pinyin.to_pinyin("直接税"), pinyin.to_pinyin("直接水"))

    def test_indexed_projection_contains_the_pinyin(self):
        from brain.core.pinyin import indexed_projection
        got = indexed_projection("通货膨胀", "通货膨胀 › 发生机制", "物价总水平上涨")
        self.assertIn("通货膨胀", got)
        self.assertIn("tonghuopengzhang", got)
        self.assertIn("物价总水平上涨", got)

    def test_pinyin_query_finds_chinese(self):
        titles, payload = self.titles("search", "tonghuopengzhang")
        self.assertIn("通货膨胀", titles)
        self.assertEqual(payload["stages"]["pinyin"], 1)

    def test_homophone_typo_finds_the_right_document(self):
        """「通霍膨胀」与「通货膨胀」同音（huo），字面只重合一半。"""
        titles, payload = self.titles("search", "通霍膨胀")
        self.assertIn("通货膨胀", titles)
        self.assertTrue(any(h["why"] == "fuzzy:pinyin" for h in payload["hits"]),
                        "应当有一条是拼音证据命中的")

    def test_missing_table_degrades_instead_of_crashing(self):
        """拼音表丢了必须只是「没有拼音」，不能把检索整体搞崩。"""
        from brain.core import pinyin
        original = pinyin.TABLE_PATH
        try:
            pinyin.TABLE_PATH = os.path.join(self.root, "does-not-exist.tsv.gz")
            pinyin.table.cache_clear()
            self.assertFalse(pinyin.available())
            self.assertEqual(pinyin.to_pinyin("通货膨胀"), "")
            # 中文检索照常工作
            titles, _ = self.titles("search", "通货膨胀")
            self.assertIn("通货膨胀", titles)
        finally:
            pinyin.TABLE_PATH = original
            pinyin.table.cache_clear()
        self.assertTrue(pinyin.available())


class TestContextBudget(P2Base):
    def test_render_carries_locators(self):
        out = run_cli(self.root, "search", "通货膨胀", "--render").stdout
        self.assertIn("定位", out)
        self.assertIn("doc:", out)
        self.assertIn("命中方式", out)

    def test_json_hits_carry_locator_and_why(self):
        payload = cli_json(self.root, "search", "通货膨胀")
        h = payload["hits"][0]
        self.assertTrue(h["locator"].startswith("doc:"))
        self.assertIn(h["why"], ("exact", "fuzzy") + tuple(
            f"alias:{x}" for x in payload["expanded"]) or h["why"].startswith("alias:"))

    def test_tiny_budget_truncates(self):
        payload = cli_json(self.root, "search", "通货膨胀", "--budget", "5")
        self.assertTrue(payload["truncated"])
        self.assertGreater(payload["dropped_by_budget"], 0)
        self.assertLessEqual(payload["token_estimate"], 5)

    def test_budget_never_exceeded(self):
        for budget in (5, 20, 100, 2500):
            payload = cli_json(self.root, "search", "通货膨胀", "--budget", str(budget))
            self.assertLessEqual(payload["token_estimate"], budget,
                                 f"budget={budget} was exceeded")

    def test_full_text_flag(self):
        payload = cli_json(self.root, "search", "基尼系数", "--full")
        self.assertTrue(any(i["full"] for i in payload["items"]))

    def test_render_is_markdown_with_sources(self):
        out = run_cli(self.root, "search", "基尼系数", "--render").stdout
        self.assertIn("# 检索结果：基尼系数", out)
        self.assertIn("来源", out)


class TestAliasCli(P2Base):
    def test_list_shows_seed_and_file(self):
        payload = cli_json(self.root, "alias", "list")
        self.assertIn("菲利普斯", payload["effective"])
        self.assertTrue(payload["path"].endswith("aliases.json"))

    def test_add_creates_a_working_alias(self):
        run_cli(self.root, "alias", "add", "物价总水平", "inflation")
        titles, _ = self.titles("search", "物价总水平")
        self.assertTrue(titles, "新加的别名应当能让查询命中")

    def test_rm_removes(self):
        run_cli(self.root, "alias", "add", "临时词", "tempword")
        run_cli(self.root, "alias", "rm", "临时词")
        payload = cli_json(self.root, "alias", "list")
        self.assertNotIn("临时词", payload["effective"])

    def test_learn_finds_pairs_in_the_corpus(self):
        run_cli(self.root, "alias", "learn")
        payload = cli_json(self.root, "alias", "list")
        self.assertIn("基尼系数", payload["learned"])
        self.assertIn("Gini Coefficient", payload["learned"]["基尼系数"])

    def test_learn_dry_run_changes_nothing(self):
        run_cli(self.root, "alias", "learn", "--dry-run")
        payload = cli_json(self.root, "alias", "list")
        self.assertEqual(payload["learned"], {})

    def test_where_prints_a_path(self):
        out = run_cli(self.root, "alias", "where").stdout.strip()
        self.assertTrue(out.endswith("aliases.json"))

    def test_aliases_live_outside_the_index(self):
        """别名表是用户数据，不能只存在于索引里（索引可删，别名不能丢）。"""
        run_cli(self.root, "alias", "add", "持久词", "persistentword")
        run_cli(self.root, "alias", "list")   # 与索引无关
        index_db = os.path.join(self.root, "index", "recall.db")
        os.unlink(index_db)
        payload = cli_json(self.root, "alias", "list")
        self.assertIn("持久词", payload["effective"])


class TestSafety(P2Base):
    def test_search_never_touches_the_original_files(self):
        import hashlib
        before = {}
        for dirpath, _d, files in os.walk(self.src):
            for name in files:
                full = os.path.join(dirpath, name)
                with open(full, "rb") as fh:
                    before[full] = hashlib.sha256(fh.read()).hexdigest()
        run_cli(self.root, "search", "通货膨胀")
        run_cli(self.root, "search", "通或膨胀")
        run_cli(self.root, "alias", "learn")
        after = {}
        for dirpath, _d, files in os.walk(self.src):
            for name in files:
                full = os.path.join(dirpath, name)
                with open(full, "rb") as fh:
                    after[full] = hashlib.sha256(fh.read()).hexdigest()
        self.assertEqual(before, after)

    def test_search_without_index_gives_a_usable_message(self):
        empty = tempfile.mkdtemp(prefix="recallos-p2-empty-")
        self.addCleanup(lambda: __import__("shutil").rmtree(empty, ignore_errors=True))
        run_cli(empty, "init")
        proc = run_cli(empty, "search", "任何东西", expect=2)
        self.assertIn("add", proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
