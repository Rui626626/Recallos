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

## 手工安装（不推荐，但说得清楚）

DSH 用「向上找 `.git`，找不到就用当前工作目录」确定项目根，技能放在
`<项目根>/.dsh/skills/<名字>/SKILL.md`。所以：

| 你的情况 | 技能该放哪 |
|---|---|
| 正常 `git clone` 下来的仓库 | **仓库内部**：`<repo>/.dsh/skills/recallos/SKILL.md` |
| 仓库被放在某个工作区里当子目录（没有 `.git`） | **仓库旁边**：`<那个工作区>/.dsh/skills/recallos/SKILL.md` |

**但不要真的用手复制。** 随包的 `skills/deepseek-harness/SKILL.md` 是**模板**：
里面那段「本机位置」只有一句兜底说明，没有任何绝对路径。`install-skill` 会在
安装时把它换成目标机器的真实路径——手工复制出来的副本没有这一段，AI 就不知道
`recall.py` 在哪，会回答「工具还没装」，然后一直要你给目录。

真发生过，所以别赌。手工复制之后跑一次：

```bash
python bin/recall.py install-skill --force
```

它会把本机路径补进那份副本（正文不动，只刷新那一段）。

---

## 验证装好了

重启会话后，技能列表里应该出现 `recallos`。也可以直接问：

```bash
python bin/recall.py doctor
#   #7 tests+config  报技能是否被 DSH 发现
#   #8 skill-path    报技能里那条路径还指不指得到 recall.py
```

`skill-path` 报 `fail` 就说明技能指向了一个不存在的入口（仓库搬过家，或副本是从
别的机器拷来的），修复命令是 `install-skill --force`。

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
