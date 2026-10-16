---
name: recallos
description: RecallOS 个人外接大脑——找回你保存过的资料、笔记、课件与过去的结论，并在新任务里复用它们。当用户说「我之前保存过一个…」「那个文件放哪了」「我们之前决定过什么」「帮我把这些资料整理成复习材料」「记得我说过的…」，或提到个人资料库／记忆／知识复用／外接大脑／RecallOS 时使用。
---

# RecallOS

个人 AI 外接大脑。核心命题：**你不必记得自己保存过什么，AI 会在需要时找到相关资料，并帮你用起来。**

## 当前阶段：P5（导入 + 检索 + 对话归档 + 成品汇编 + 长期记忆 + 零设置检索）

| 命令 | 作用 | 状态 |
|---|---|---|
| `init` | 创建目录骨架（幂等，绝不覆盖已有文件） | ✅ |
| `doctor` | 自检目录、Python、SQLite/FTS5、数据目录读写、索引、测试与配置、导入层格式 | ✅ |
| `add <路径...>` | 导入 Markdown / TXT / HTML / Word / PPT / Excel / PDF | ✅ |
| `archive <路径...>` | 对话记录（DSH 会话 `.jsonl.zstd`、通用 JSONL/JSON）转成可检索档案 | ✅ |
| `index --stats` | 查看统计与完整性 | ✅ |
| `index --rebuild [--from-source]` | 从档案（或原文件）原子重建索引 | ✅ |
| `search <查询>` | **检索**，返回带出处、受预算约束的上下文 | ✅ |
| `search <查询> --in <目录>` | **不建索引**直接搜目录（现场模式，零设置） | ✅ |
| `alias list/add/rm/learn` | 维护「你平时怎么叫 → 文档里怎么写」的别名表 | ✅ |
| `compose <查询>` | 把检索结果汇编成带出处的成品（review / outline / report / cards） | ✅ |
| `verify <文件...>` | 检查文件里每个出处还能不能回查到数据库行 | ✅ |
| `memory remember/list/show/expire/restore/export` | 长期记忆，一个主题一个文件 | ✅ |
| `install-skill [--user]` | 把随包技能装到 DSH 会扫的位置 | ✅ |

161 项黑盒测试（`tests/test_p0..p5.py`），`doctor` 全绿。

## ⭐ 库是空的时候，不要只会说「请先建库」

这是最容易被忽略、也最影响体验的一条。

`brain.config.json` 的 `roots` 默认为空，也就是说**刚装好的库搜不到任何东西**。
这时候不要说「索引里没有资料」，那等于把用户挡在门外。正确做法是：

1. **先问他一个目录**（不要自己扫硬盘——铁律：只处理明确给出的路径）：
   > 「你想让我先看哪个文件夹？比如 `Documents` 或 `Desktop` 里的某个目录。」
2. **拿到目录就立刻现场搜，不要先建库**：
   ```
   python bin/recall.py search "用户的查询" --in "<他给的目录>"
   ```
   这条命令不需要 `init`、不需要 `add`，直接出结果。
3. 如果他还会反复问，再建议建库（更快、每次都能回查出处）：
   `python bin/recall.py add "<那个目录>"`

**为什么强调这个**：用户下载一个「帮我找文件」的工具，第一句话得到的如果是
「请先运行 add」，他会直接放弃。先给结果，再谈优化。

## 用法

```powershell
# 先进入 RecallOS 的项目目录 —— 就是含 bin/ 与 brain/ 的那个目录。
# 不确定在哪？本文件所在仓库的根目录就是它；`doctor` 也会把它打印出来。
$env:PYTHONIOENCODING="utf-8"

python bin/recall.py doctor                       # 先自检
python bin/recall.py install-skill --user         # 让 DSH 认识它（只需一次）
python bin/recall.py search "查询" --in "某目录"    # 零设置：不建库直接搜
python bin/recall.py add "某个文件夹"               # 导入（只读，绝不改动原文件）
python bin/recall.py search "通货膨胀"              # 检索
python bin/recall.py search "通货膨胀" --explain     # 看每一段召回了多少
python bin/recall.py search "复习资料" --render      # 带 locator 的 markdown，可回查
python bin/recall.py compose "通货膨胀" --kind review  # 汇编成带出处的成品
python bin/recall.py verify artifacts/*.md         # 回查成品里每个出处
python bin\recall.py memory remember "用户偏好简洁回答" --topic "回答偏好"
python bin\recall.py memory expire "过时的结论"      # 默认搜不到，--include-expired 能搜到
python bin\recall.py archive "会话目录或文件"         # 对话记录入库
python bin\recall.py alias learn                  # 从正文里学「中文（English）」对照
```

**重要**：`add` / `archive` 只接受用户**明确给出**的路径。`brain.config.json` 的 `roots` 默认为空数组——在没有用户明确指示时，**不要**扫描任何目录、不要导入历史对话、不要创建定时任务。对话记录属于隐私内容，尤其要等用户点名。

## 检索能力（务必如实描述）

`search` 分四段，每段都能解释自己贡献了什么（`--explain` / `--json` 的 `stages`）：

1. **精确短语** —— 原样相邻出现，置信度最高
2. **别名扩展** —— 「菲利普斯」→「phillips」这类跨语言/跨叫法的等价
3. **模糊 OR** —— 拆成探针词，任一命中即召回，容**错别字**与**少字多字**
4. **相似度重排** —— 用覆盖率把第 3 段召回里的噪声丢掉（阈值动态）

**能做到**：错别字（`通或膨胀` → 通货膨胀）、少字（`基尼` → 基尼系数）、
中英互指（`菲利普斯` → 只有英文标题的 `the Phillips curve`，靠别名表）、
**拼音搜索**（`tonghuopengzhang` → 通货膨胀）、**同音容错**（`直接水` → 直接税）、
**搜文档标题与小标题**（索引文本 = 标题 + 标题路径 + 正文 + 拼音连写）。

**做不到（明确的取舍，不是 bug）**：
- **没有语义检索**。同义词、上下位关系（「通胀」↔「物价上涨」）只能写进 `aliases.json`。
  **同音**能容错，**同义**不能。
- 文档里一个汉字都没有的英文笔记，统计方法**无解**；拼音也帮不上——拼音表里没有
  那几个字，因为文档里根本没有它们。只能靠别名表。
- 不做分词、不做 OCR、不改动原文件。
- **`compose` 不写结论**。它是汇编器不是作者：正文全部来自原文，结论小节留空给
  Agent 或人补写。不要替它编造未出现在出处里的内容。

## 铁律（写在这里，因为每条都踩过）

1. **原始文件只读。** 永不移动、改名或删除用户的任何资料，只建立索引。
2. **`data/` 是权威，`index/` 是派生物。** 索引随时可删可重建；用户数据绝不能只存在于索引里。
   `aliases.json` 与 `data/memory/` 同理——都有测试守着「索引删掉后数据仍在」。
3. **失败必须留痕。** 任何失败的导入/转换都要有一行记录和具体原因，不允许静默跳过。
4. **不把没实现的功能报告为通过。** `doctor` 用独立状态区分通过 / 警告 / 未实现。
5. **一切断言落在真实文件与数据库行上**，不接受「模型说成功了」。
6. **检索结果必须带出处。** 每条命中都有 `locator`（`doc:<id>#h=<hash>@<序号>`），
   引用时不写 locator 等于不可核查；成品写完立刻用 `verify` 自查。
7. **返回内容有预算上限**（默认 2500 tokens），被裁掉的条数明确报出来。

## 改代码时必须知道的三件事

1. **改了「写进 FTS 表的内容」就要 bump `INDEX_CONTENT_VERSION`**（`brain/core/versions.py`）。
   漏掉这一步的后果是：老索引不重建，而且**不会报错**——它会安静地少返回东西。
   这是本项目最危险的一类 bug。已发生的实例：原来只索引正文，导致「搜文档自己的标题
   返回空」，版本戳从 `ch1` 提到 `ch2` 才自动修好；加拼音投影时提到 `ch3`。
2. **索引写入与阈值判定必须用同一个投影函数**（`pinyin.indexed_projection`）。
   曾经写入用拼音版、判定用原始正文版，结果拼音召回了候选却被阈值全部否掉，
   表现为「明明召回了却返回空」。
3. **`REQUIRED_DIRS` 只有一个来源**（`brain/cli/init.py`），`doctor` 从那里 import。
   曾经复制过一份，结果两边数字对不上（doctor 报 6 个、init 建 8 个）。
4. **覆写整个文件前先读它。** 这条不是客套：开发中因为覆盖 `SKILL.md` 时丢了
   frontmatter，`recallos` 直接从技能目录里消失过一次。

## 通用事实（对所有机器成立）

- **项目根 = 含 `bin/recall.py` 与 `brain/` 的那个目录。** `doctor` 会把它打印出来。
- **Skill 入口放哪**：DSH 用「向上找 `.git`，找不到就用 cwd」确定项目根，技能放在
  `<项目根>/.dsh/skills/<名字>/SKILL.md`。所以：仓库**自己有 `.git`** 时，入口放在
  仓库**内部**；仓库**没有 `.git`** 时，入口放在工作区里、仓库的**旁边**。
- 中文检索有两条通道：`trigram`（≥3 字）与逐字索引（2 字查询靠它）。模糊层在它们之上。
- 索引的可检索文本 = **标题 + 标题路径 + 正文 + 拼音连写**。改这段内容必须 bump
  `INDEX_CONTENT_VERSION`，否则老索引不会重建，而且**不会报错**。
- 拼音表随包发布（`brain/data/pinyin.tsv.gz`），不依赖任何第三方库。删掉它即降级。
- `brain.config.json` 的 `roots` 默认为空：**不扫描任何目录**，只处理明确给出的路径。

## 本机事实（⚠️ 换机器时请替换本节，不要照搬）

> 以下是**这一台机器**实测到的环境，仅供排查参考。换机器请用 `doctor` 重新确认。

- Windows；`pwsh` 是 Windows PowerShell **5.1**（不是 PowerShell 7）
- Python 3.13.9（Anaconda）；SQLite 3.51.0 带 FTS5
- **`.ps1` 脚本要带 UTF-8 BOM**：PowerShell 5.1 在无 BOM 时按 ANSI 解码，中文会乱码。
  `install.ps1` 因此正文用英文并带 BOM。
- **DSH 会话文件是多个 zstd frame 顺序拼接的**，必须用 `stream_reader` 读到底；
  用 `.decompress()` 只会拿到第一帧然后**静默截断**（`archive` 里有回归测试守着）
- Python 读的网络代理来自 Windows 注册表，清空环境变量无效；装包要用
  `curl --noproxy "*"` 直连下载后离线安装

## 测试

```powershell
python tests\test_p0.py   # 17 项  骨架 / 自检 / 索引基础
python tests\test_p1.py   # 16 项  导入 / 更新 / 改名 / 失败 / 重建
python tests\test_p2.py   # 56 项  检索 / 模糊 / 别名 / 拼音 / 预算 / 标题可搜
python tests\test_p3.py   # 19 项  对话归档 / 多帧 zstd / 失败留痕
python tests\test_p4.py   # 25 项  成品汇编 / 出处回查 / 长期记忆
python tests\test_p5.py   # 28 项  零设置检索 / 技能安装 / 打包不变量
```

共 161 项，全部是黑盒：驱动真实命令行、在临时项目根里跑、断言真实文件与数据库行、不 mock 被测代码。

**拼音表怎么来的**：`tools/build_pinyin_table.py`（从 pypinyin 0.55.0 一次性生成，
结果可复现）。它是随包数据，不是运行时依赖——`brain/data/pinyin.tsv.gz` 删掉就降级。

## 相关文档

- `recallos/README.md` —— 完整用法与**做不到什么**
- `recallos/CHANGELOG.md` —— 每轮改了什么、修了哪些实测发现的坑
- `recallos/CONTRIBUTING.md` —— 改索引内容时必须 bump 哪个版本戳
- `recallos/docs/` —— 设计与审查文档
