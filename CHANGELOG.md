# 变更记录

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

## [0.7.0] — 零设置：不建索引也能搜

### 新增

- **`search --in <目录>`：不建索引，直接搜目录。** 全新下载、一次 `add` 都没跑过，
  给一个目录加一句查询就出结果。共用同一套抽取器、切块器和打分规则，所以同一个查询
  在两种模式下的排序一致——不会出现「试的时候找得到、建库之后反而找不到」。
  代价如实报出：每次都重新解析（PDF 慢），有 `--max-files`（默认 400）与
  `--time-limit`（默认 120 秒）双上限，被截断时明确说明。
- **`install-skill`：一条命令把随包技能装到 DSH 会扫的位置。** 路径由适配层算出来，
  不靠用户猜；会报告装在哪、怎么撤销；已经装好且内容一致时不改动；内容不同且没给
  `--force` 时**拒绝覆盖**（用户可能自己改过）。
- `install.ps1` / `install.sh` 现在会调用 `install-skill --user`，装一次所有工作区都能用；
  不想要就 `-SkipSkill` / `--skip-skill`。
- 测试套件 `tests/test_p5.py`（28 项）。

### 修复

- **用户级技能根算错了。** `brain/adapters/dsh` 把 `$DSH_HOME/skills` 拼成了
  `$DSH_HOME/.dsh/skills`（即 `~/.dsh/.dsh/skills`）——一个 DSH 永远不会扫描的路径。
  后果非常隐蔽：项目级安装一切正常，只有 `--user` 会「报告成功但永远不生效」。
- **空库的提示给不出可执行的下一步。** 原来只说「请先 add」；现在同时给出
  `search --in <目录>`（立刻能搜）与 `add`（长期好用）两条可以照抄的命令。
- 现场模式的路径改为相对**被搜的那个目录**；原来相对项目根会给出
  `..\..\..\Desktop\...` 这种对读者没有意义的路径。
- PDF 的页码不再重复显示（标题路径里已经有了「第 N 页」）。

### 说明

- `search` 的两种模式共用打分，但 live 模式的出处形如 `live:<路径>@<序号>`，
  **刻意**与索引模式的 `doc:<id>#h=<hash>@<n>` 长得不一样——它没法用 `verify` 回查，
  长得像「可以回查」是一种误导。

## [0.6.0] — 拼音搜索与同音容错

### 新增

- **拼音搜索**：输入 `tonghuopengzhang` 能找到「通货膨胀」。
- **同音字容错**：搜索「直接水」也能找到「直接税」（`shui` 相同），
  字面只重合一半的字也能命中。
- `brain/data/pinyin.tsv.gz` —— 内置拼音表（26,703 字，98 KB gzip），
  **零运行时依赖**。由 `tools/build_pinyin_table.py` 从 pypinyin 0.55.0 一次性生成，
  脚本随仓库发布，结果可复现。
- `brain/core/pinyin.py` —— 表加载、拼音投影、音节切分。
- 测试 `TestPinyin`（7 项），含「拼音表丢失时优雅降级而不是崩溃」。

### 修复

- **阈值判定与索引写入用了不同的文本。** 召回靠索引里的拼音投影，相似度阈值却
  只看原始正文，于是拼音召回上来的候选会被**全部否掉**——表现为「明明召回了却
  返回空」。现在两处共用 `pinyin.indexed_projection()`。
- `doctor` 会报出拼音表状态；`REQUIRED_DIRS` 现在由 `init` 单点定义、`doctor` 从中
  import（曾经复制过一份，导致两处数字对不上：doctor 报 6 个而 init 建 8 个）。
- 全新 clone 的 `doctor` 不再因为「还没把 Skill 装进 DSH」而报 FAIL——那是可选的
  宿主步骤，现在是 WARN。

### 变更

- `INDEX_CONTENT_VERSION` → `ch3`。已有索引会在下次 `add` 时**自动重建**，
  不需要手工干预。

## [0.5.0] — P4：成品汇编、出处回查、长期记忆

### 新增

- `compose` —— 把检索结果汇编成带出处的成品（`review` / `outline` / `report` / `cards`）。
  它是**汇编器不是作者**：不会替你写结论，正文全部来自原文，结论小节留空给
  人或 Agent 补写；每份成品顶部有出处清单，文末有机器可读的 locator 块。
- `verify` —— 检查一份 markdown 里的每个出处还能不能回查到数据库行。
- `memory` —— 长期记忆：`remember` / `list` / `show` / `expire` / `restore` / `export`。
  一个主题一个文件，旧结论留在同文件的修订历史里；过期靠移动到 `_expired/` 实现，
  检索默认排除；`export --dsh` 导出成现有记忆插件能直接读的格式。
- `brain/core/locator.py` —— 定位符解析与回查。
- `brain/core/memory.py` —— 记忆文件模型。
- 测试套件 `tests/test_p4.py`（25 项）。

### 修复

- **标题与小标题没有被索引。** `replace_chunks` 只把切片正文写进 FTS 表，而文档标题
  和 `heading_path` 都不在正文里，导致「搜文档自己的标题」和「搜某一节的名字」都
  返回空。索引文本改为 `标题 + 标题路径 + 正文`，并新增 `INDEX_CONTENT_VERSION`
  版本戳，让已有索引自动触发重建。
- 检索评分把精确匹配压到 0.76：原来用文档全文算 Jaccard，分母被文档长度主导，
  精确匹配和「字散落各处」几乎同分。改为相对查询的覆盖率后精确 = 1.0、错字 ≈ 0.5。
- `search` 的退出码把「预算裁光了」误报成「没找到」；现在只看检索有没有命中。
- `search --json` 缺少 `hits` 字段，调用方看不见有多少条被预算丢掉。
- 单行 JSON 聊天导出被 JSONL 分支当成「一条记录」，把 `messages` 整个吞掉。
- 记忆的修订历史会重复插入旧结论，导致版本号多涨一版。

## [0.4.0] — P3：历史对话入库

### 新增

- `archive` —— 把对话记录转成可检索档案。支持 DSH 会话（`.jsonl.zstd`）与通用
  JSONL / JSON 导出；按轮次切成标题，检索结果能说出「第几轮」。
- 测试套件 `tests/test_p3.py`（19 项），含多帧 zstd 的回归测试。

### 说明

- DSH 的会话文件是**多个 zstd frame 顺序拼接**的。用
  `ZstdDecompressor().decompress()` 只会拿到第一帧然后静默截断，必须用
  `stream_reader` 读到底。这条坑有专门的回归测试守着。

## [0.3.0] — P2：检索与模糊匹配

### 新增

- `search` —— 四段检索流水线：精确短语 → 别名扩展 → 模糊 OR → 相似度重排。
  返回受 token 预算约束、每条都带 `locator` 的上下文。
- `alias` —— 中英互指映射表。支持手工维护与从正文的「中文（English）」写法自动学习。
- `brain/core/fuzzy.py` —— 字符 / 三元组重叠度、动态覆盖率阈值、别名表。
- `brain/core/retrieve.py`、`brain/core/context.py` —— 检索编排与结果预算。
- 测试套件 `tests/test_p2.py`。

### 说明

- **没有 embedding，也没有语义检索。** 同义词与上下位关系只能写进别名表；文档里
  一个汉字都没有的英文笔记，统计方法无解。这是明确的取舍，不是缺陷。
- 模糊搜索**绝对可用**的含义是：错别字、少字多字、中英互指都能命中；
  乱码查询返回空而不是硬凑。

## [0.2.0] — P1：导入与索引

### 新增

- `add` —— 导入 Markdown / TXT / HTML / Word / PPT / Excel / PDF，只读原文件。
- `index --stats` / `index --rebuild [--from-source]` —— 原子重建，失败不动原索引。
- 按字节与按内容双重身份、改名/移动识别、重复检测、失败留痕、来源历史。

## [0.1.0] — P0：骨架

### 新增

- `init` / `doctor`，SQLite FTS5 索引（trigram + 逐字双通道）。
