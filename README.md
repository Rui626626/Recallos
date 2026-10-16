# RecallOS

> 「我不必记得自己保存过什么，AI 会在需要时找到相关资料，并帮我把它们用起来。」

个人 AI 外接大脑：把你散落在硬盘里的笔记、课件、文档和对话记录收进一个可检索的库，
**每条结果都带能回查的出处**。

**当前状态：P5 —— 导入 / 检索 / 对话归档 / 成品汇编 / 长期记忆 / 零设置检索，全部可用。**
161 项黑盒测试。

---

## 真的 30 秒：不建库也能搜

不想先花时间收资料？给它一个目录就直接搜，**不需要 `init`，也不需要 `add`**：

```bash
python bin/recall.py search "通货膨胀" --in ~/Documents
```

这就是「现场模式」：它现场遍历、现场解析、现场打分，不写任何索引。
代价是每次查询都要重新解析（PDF 比较慢），所以有文件数（默认 400）和时间
（默认 120 秒）双上限，被截断时会**明确告诉你**。想长期好用再建库。

## 装了就能用（一条命令）

```bash
git clone https://github.com/Rui626626/Recallos.git
cd Recallos
python bin/recall.py install-skill --user     # 让 AI 认识它，所有工作区都生效
```

装完重启会话，之后你**用大白话就行**——不用记命令：

> 「我之前存过一个讲通货膨胀的笔记」
> 「那个文件放哪了」
> 「帮我用这些资料整一份复习材料」

`install-skill` 会算出 DSH 到底会去哪个目录找技能、报告装在哪、怎么撤销。
已经装好且内容一致时不改动；内容不同且没加 `--force` 时**拒绝覆盖**（你可能自己改过）。

Windows 上一行装全套（自检 + 启动器 + 技能）：

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1
```

macOS / Linux：

```bash
bash install.sh
```

**除了 Python 3.10+ 什么都不需要。** 标准库之外零依赖；`zstandard` 与 `pypdf` 是可选
增强，没有它们时相关格式会被跳过**并留下一条明确的记录**，不会静默失败。

---

## 它能做什么

| 命令 | 作用 |
|---|---|
| `init` | 创建目录骨架（幂等，绝不覆盖已有文件） |
| `doctor` | 自检：Python、SQLite/FTS5、目录读写、索引状态、配置、导入层可用格式 |
| `add <路径...>` | 导入 Markdown / TXT / HTML / Word / PPT / Excel / PDF —— **只读** |
| `archive <路径...>` | 把对话记录（DSH 会话、通用 JSONL/JSON 导出）转成可检索档案 |
| `index --stats` | 查看统计与完整性 |
| `index --rebuild [--from-source]` | 原子重建索引（失败不动原索引） |
| `search <查询>` | 检索，返回带出处、受预算约束的上下文 |
| `search <查询> --in <目录>` | **不建索引**直接搜目录（现场模式） |
| `alias list/add/rm/learn` | 维护「你平时怎么叫 → 文档里怎么写」的映射 |
| `compose <查询>` | 把检索结果汇编成带出处的成品（复习材料 / 大纲 / 报告 / 卡片） |
| `verify <文件...>` | 检查文件里的每个出处还能不能回查到数据库行 |
| `memory remember/list/show/expire/restore/export` | 长期记忆，一个主题一个文件 |
| `install-skill [--user]` | 把随包技能装到 DSH 会扫的位置（报告装在哪、怎么撤销） |

### 检索是四段流水线

1. **精确短语** —— 原样相邻出现，置信度最高
2. **别名扩展** —— 「菲利普斯」→「phillips」这类跨语言/跨叫法的等价
3. **模糊 OR** —— 拆成探针词，任一命中即召回，容**错别字**、**少字多字**与**拼音**
4. **相似度重排** —— 把第 3 段召回里的噪声按覆盖率丢掉

**拼音是内置的，零依赖。** 输入 `tonghuopengzhang` 能找到「通货膨胀」；
同音错字也能容错——搜「直接水」照样找到「直接税」，因为两者拼音都是 `zhijieshui`。
实现方式是索引时附上一段汉字拼音连写，查询时把查询也转成拼音，两边直接对上。
拼音表（26,703 字 / 98 KB）由 `tools/build_pinyin_table.py` 从 pypinyin 一次性生成后
随包发布——**用户不需要装任何东西**。删掉 `brain/data/pinyin.tsv.gz` 就自动关闭。

每一段贡献了多少都报得出来：

```bash
python bin/recall.py search "飞利浦斯" --explain
#   各段召回: exact=0  alias=0  pinyin=1  fuzzy_candidates=6  fuzzy=0  total=0
```

### 每一句都能回到出处

```bash
python bin/recall.py search "通货膨胀" --render
```

```markdown
## [1] 通货膨胀

- 来源：`~/Documents/笔记/通货膨胀.md › 发生机制`
- 定位：`doc:dbc2f3bbcc36f#h=e9354f8a64@0`
- 命中方式：exact
```

`compose` 把这套东西汇编成成品，`verify` 让「不可核查」变成一条能跑出来的结论：

```bash
python bin/recall.py compose "通货膨胀" --kind review
python bin/recall.py verify artifacts/通货膨胀-review.md
#   [ok  ] 通货膨胀-review.md: 4/4 条出处可回查
```

---

## 它做不到什么（请先读这段）

诚实地划出边界，比宣传功能更重要：

* **没有语义检索。** 没有 embedding，也不打算把你的资料发到任何地方。
  所以同义词与上下位关系（「通胀」↔「物价上涨」）**不在能力范围内**。
  注意区分：**同音**（直接水 / 直接税）能容错，**同义**不能。
* **跨语言指代需要别名表。** 一份标题是 `the Phillips curve`、正文全是英文的笔记，
  搜「菲利普斯」在统计上**无解**——文档里没有任何汉字可以匹配（拼音也帮不上：
  拼音表里没有「菲利普斯」这三个字，因为文档里根本没有它们）。这类只能靠
  `aliases.json`（内置了一个经济学常见术语的起始包，也可以 `alias add` 手工加）。
* **PDF 只抽有文字层的**，扫描件不做 OCR。
* **`compose` 不写结论。** 它是汇编器不是作者：正文全部来自原文，结论小节留空给
  人或 Agent 补写。让一个离线工具假装会写文章，产出的东西读起来像结论、实际上无法
  核查，正好破坏这个项目最值钱的性质。
* **不做分词，不改动你的任何文件。** 永远只读。
* **现场模式（`--in`）不是长期方案。** 它每次查询都重新解析一遍，PDF 尤其慢
  （实测：4 个 PDF + 2 个 Word 约 13 秒）。它的价值是「立刻能用」，长期好用要靠建库。
  命中量大、资料多的时候，先 `add` 一次比反复现搜划算得多。

---

## 设计铁律

这七条写在代码注释和测试里，评审时优先盯它们：

1. **原始文件只读。** 永不移动、改名或删除用户的资料。
2. **`data/` 是权威，`index/` 是派生物。** 索引随时可删可重建；
   用户数据绝不能只存在于索引里。
3. **失败必须留痕。** 任何跳过、降级、解析失败都有一行记录和具体原因，
   不允许静默跳过。
4. **不把没实现的功能报告为通过。** `doctor` 区分通过 / 警告 / 未实现。
5. **一切断言落在真实文件与数据库行上**，不接受「模型说成功了」。
6. **检索结果必须带出处。** 没有 `locator` 的引用等于不可核查。
7. **返回内容有预算上限**（默认 2500 tokens），被裁掉的条数明确报出来。

---

## 目录结构

```
recallos/
├── bin/recall.py          命令行入口（唯一真相来源在 brain/cli/__init__.py）
├── brain/
│   ├── data/pinyin.tsv.gz 内置拼音表（26,703 字，随包发布，删掉即关闭拼音）
│   ├── cli/               每个命令一个文件
│   ├── core/              索引、检索、模糊匹配、拼音、别名、上下文、定位符、记忆
│   ├── ingest/            各格式的抽取器
│   ├── adapters/          宿主适配层
│   └── pipeline.py        导入流水线（幂等、可重建）
├── tools/                 构建脚本（拼音表生成等）
├── tests/                 161 项黑盒测试
├── docs/                  设计与审查文档
├── data/                  权威数据：导入档案 + 长期记忆（默认不入库）
├── index/                 SQLite 索引（派生，可删）
├── cache/                 抽取出的正文与转换后的档案（派生，可删）
├── artifacts/             汇编出的成品
├── aliases.json           中英互指映射（本地增量）
└── brain.config.json      配置
```

**运行时会创建的目录都在 `.gitignore` 里**，`data/` 与 `state/` 默认不入库——
你的资料不该被推到公开仓库。

---

## 测试

```bash
python tests/test_p0.py   # 17 项  骨架 / 自检 / 索引基础
python tests/test_p1.py   # 16 项  导入 / 更新 / 改名 / 失败 / 重建
python tests/test_p2.py   # 56 项  检索 / 模糊 / 别名 / 拼音 / 预算 / 标题可搜
python tests/test_p3.py   # 19 项  对话归档 / 多帧 zstd / 失败留痕
python tests/test_p4.py   # 25 项  成品汇编 / 出处回查 / 长期记忆
python tests/test_p5.py   # 28 项  零设置检索 / 技能安装 / 打包不变量
```

全部是黑盒：驱动真实命令行、在临时项目根里跑、断言真实文件与数据库行，
**不 mock 被测代码**。

值得单独一提的两条回归测试：

* `test_multiframe_zstd_is_fully_read` —— DSH 的会话文件是**多个 zstd frame 顺序
  拼接**的。用 `ZstdDecompressor().decompress()` 只会拿到第一帧然后**静默截断**。
* `test_document_title_is_searchable` —— 曾经只把正文写进 FTS 表，导致
  **搜文档自己的标题返回空**。夹具的标题刻意不出现在正文中，所以这个 bug 藏不住。

---

## 环境

* Python **3.10+**，无需任何第三方包（可选：`zstandard`、`pypdf`、`openpyxl`）
* SQLite **3.34+ 且带 FTS5**（中文检索用到 `trigram` 分词器，它从 3.34 开始才有；
  Python 3.10+ 自带的 SQLite 都满足。`doctor` 会实测并报出来）
* Windows / macOS / Linux

## 许可

[MIT](LICENSE)
