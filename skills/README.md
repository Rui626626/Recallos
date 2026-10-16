# Skill 安装

`deepseek-harness/SKILL.md` 是给 DSH（DeepSeek Harness）用的技能入口，让 Agent 知道
这个项目存在、能用它做什么、以及哪些事**不能**做（比如未经允许就扫描你的目录）。

**装不装的区别**：装了之后你用大白话就行（「我之前存过一个讲通货膨胀的笔记」）；
不装就只能自己在终端敲命令。

---

## 推荐做法：一条命令

```bash
python bin/recall.py install-skill --user
```

`--user` 装到 `$DSH_HOME/skills/`，**所有工作区都能用**——不管你在哪个目录里开 DSH。

不带 `--user` 则是项目级，装到 DSH 为当前项目算出来的那个位置（只在该工作区生效）。

命令会告诉你**装在哪、怎么撤销**，并且：

* 已经装好且内容一致 → 不动它
* 已存在但**内容不同** → **拒绝覆盖**（你可能自己改过），要覆盖得显式加 `--force`

预设的安装脚本已经包含这一步：

```bash
bash install.sh                                   # macOS / Linux
powershell -ExecutionPolicy Bypass -File install.ps1   # Windows
```

不想要这一步就加 `--skip-skill` / `-SkipSkill`。

---

## 手工安装（不想用命令时）

DSH 用「向上找 `.git`，找不到就用当前工作目录」确定项目根，技能放在
`<项目根>/.dsh/skills/<名字>/SKILL.md`。所以：

| 你的情况 | 技能该放哪 |
|---|---|
| 正常 `git clone` 下来的仓库 | **仓库内部**：`<repo>/.dsh/skills/recallos/SKILL.md` |
| 仓库被放在某个工作区里当子目录（没有 `.git`） | **仓库旁边**：`<那个工作区>/.dsh/skills/recallos/SKILL.md` |

```bash
mkdir -p <目标>/.dsh/skills/recallos
cp skills/deepseek-harness/SKILL.md <目标>/.dsh/skills/recallos/SKILL.md
```

（`install-skill` 算的就是这件事，它还会把其他候选位置列出来给你看。）

---

## 验证装好了

重启会话后，技能列表里应该出现 `recallos`。也可以直接问：

```bash
python bin/recall.py doctor          # #7 会报技能是否被 DSH 发现
```

如果没出现，最常见的原因是 **frontmatter 丢了或写坏**——`SKILL.md` 必须以这三行开头：

```
---
name: recallos
description: ...
---
```

（这条不是理论：开发过程中真的因为覆盖文件时丢了 frontmatter，导致技能从列表里
消失过一次。所以 `tests/test_p5.py` 里有一条测试专门守着 frontmatter。）

## 不装会怎样

命令行本身**完全不受影响**——`search` / `add` / `compose` / `memory` 都能正常用。
只是 Agent 不知道它存在，你得自己敲命令，享受不到「说人话就行」。
