"""`recall.py install-skill` — 把随包技能装到 DSH 的技能根。

为什么要有这个命令
------------------
技能必须放在 DSH 会去扫的目录里才会生效，而那个位置由宿主决定（项目级还是用户级、
取决于有没有 `.git`）。让用户照着文档手工复制，等于让他去猜一个宿主内部规则。

这个命令把「猜」变成「算」：复用 `brain/adapters/dsh` 里已经验证过的探测逻辑，
算出确切路径，复制过去，然后告诉他**装在哪、怎么撤销**。

为什么用 Python 而不是写在 install.ps1 / install.sh 里
--------------------------------------------------------
两个 shell 各写一份路径探测逻辑，就是在制造我已经修过三次的那类 bug——
同一份事实存两处，迟早对不上。这里只写一次，两个安装脚本都调它。

安装位置（默认按 DSH 自己的规则算）
-----------------------------------
* **项目级**（默认）：`<项目根>/.dsh/skills/recallos/SKILL.md`
  项目根 = 从当前目录向上找 `.git`，找不到就用当前目录。
* **用户级**（`--user`）：`$DSH_HOME/skills/recallos/SKILL.md`
  —— 装一次，**所有项目都能用**。想让技能「到哪都能用」就选它。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional

from ..adapters import dsh

__all__ = ["register", "run", "SKILL_NAME", "bundled_skill_path",
           "default_anchor", "existing_install", "entry_path", "render_skill",
           "read_installed_entry", "SkillTemplateError", "ENTRY_MARK",
           "WHERE_BEGIN", "WHERE_END"]

SKILL_NAME = "recallos"

#: 本机路径块的界标。技能被复制到 `<技能根>/recallos/SKILL.md` 之后，文件已经
#: **不在仓库里**了 —— 「本文件所在仓库的根目录」这类自述会指向技能目录本身，
#: 于是 AI 永远找不到 `bin/recall.py`。这就是真实发生过的故障：
#: 用户下载后 AI 说「只有技能说明，recall.py 工具还没装」，然后把用户挡在门外。
#:
#: 修法不是改文案，而是**把绝对路径写进安装出来的那一份**：界标之间的内容在
#: `install-skill` 时按本机重新渲染，随包的模板里只放一句兜底说明。
WHERE_BEGIN = "<!-- recallos:where:begin -->"
WHERE_END = "<!-- recallos:where:end -->"

#: 机器可读的一行，`doctor` 靠它判断「装好的技能指向的路径还在不在」。
ENTRY_MARK = "<!-- recallos:entry:"


class SkillTemplateError(RuntimeError):
    """随包技能缺少本机路径块的界标 —— 发行包坏了，不能静默照抄。"""


def entry_path(repo: str | None = None) -> str:
    """绝对路径形式的入口脚本。所有提示文案都用它，不再写相对路径。"""
    return os.path.join(os.path.abspath(repo or repo_dir()), "bin", "recall.py")


def _python_cmd() -> str:
    """按平台给一个能跑的命令名：Windows 上 `python`，POSIX 上通常是 `python3`。"""
    return "python" if os.name == "nt" else "python3"


def render_local_block(repo: str) -> str:
    """把本机事实渲染成一块 markdown（含界标本身）。

    除了常规的 `python "…"` 写法，还写一条**解释器绝对路径**的兜底：
    `python` 不在 PATH 上（`py` 启动器、conda、只装了 `python3`）时，
    前者跑不起来，而跑不起来的后果就是「AI 说工具没装」。
    """
    entry = entry_path(repo)
    py = _python_cmd()
    lines = [
        WHERE_BEGIN,
        f"{ENTRY_MARK} {entry} -->",
        "## 本机位置（由 `install-skill` 写入，请勿手工编辑）",
        "",
        f"- 仓库：`{os.path.abspath(repo)}`",
        f"- 入口：`{entry}`",
        "",
        "从**任何目录**直接这样调用，不要 `cd`、不要猜路径：",
        "",
        "```",
        f'{py} "{entry}" <子命令> "参数"',
        "```",
        "",
        "确认工具真的在（输出 `recallos <版本号>` 就对了）：",
        "",
        "```",
        f'{py} "{entry}" --version',
        "```",
        "",
        "上面那条报「python 不是内部或外部命令 / command not found」时，"
        "用这个解释器的完整路径（安装它的那个）：",
        "",
        "```",
        f'"{sys.executable}" "{entry}" --version',
        "```",
        WHERE_END,
    ]
    return "\n".join(lines)


def _split_block(text: str) -> Optional[tuple[str, str]]:
    """返回本机块之外的 `(之前, 之后)`；没有界标则返回 None。"""
    begin = text.find(WHERE_BEGIN)
    end = text.find(WHERE_END)
    if begin < 0 or end < 0 or end < begin:
        return None
    return text[:begin], text[end + len(WHERE_END):]


def render_skill(text: str, repo: str) -> str:
    """把模板里的本机块换成这台机器的事实。

    幂等：对已经渲染过的文本、同一个 repo 再渲染一次，结果逐字节相同。
    """
    parts = _split_block(text)
    if parts is None:
        raise SkillTemplateError(
            "SKILL.md 里找不到本机路径块的界标 "
            f"({WHERE_BEGIN} / {WHERE_END})；发行包不完整")
    before, after = parts
    return before + render_local_block(repo) + after


def read_installed_entry(skill_md: str) -> Optional[str]:
    """从装好的技能里读回它声称的入口路径。"""
    end = skill_md.find("-->", skill_md.find(ENTRY_MARK))
    if end < 0:
        return None
    value = skill_md[skill_md.find(ENTRY_MARK) + len(ENTRY_MARK):end].strip()
    return value or None


def _decide(existing: str, rendered: str) -> tuple[str, str]:
    """判断该不该写，以及怎么写。返回 `(status, action)`。

    `action` 表达的是「**如果允许**，应该写还是跳过」；「不许写」由调用方根据
    `--force` 把 `write` 降级成 `refused`。曾经这里直接返回 `refused`，结果
    `--force` 永远不起作用 —— 用户被卡在一个自己无法越过的拒绝里。

    * 逐字节相同 → 不动。
    * 正文相同、只有本机块不同（仓库搬家了，或者副本是本机块还没填的模板）
      → **静默更新**，这不算用户改动。
    * 没有本机块 → 老版本装出来的副本 / 从别处拷来的旧文件，要 `--force`。
    * 有本机块但正文不同 → **装过的是旧版本**（`git pull` 之后就会遇到），
      也可能用户改过。两种都拒绝自动覆盖，但要**把话说对**：用户需要知道
      加 `--force` 是升级，不是"毁掉自己的改动"。
    """
    if existing == rendered:
        return "up-to-date", "skip"
    existing_parts = _split_block(existing)
    rendered_parts = _split_block(rendered)
    if existing_parts is None:
        return "needs-local-block", "write"
    if existing_parts == rendered_parts:
        return "refreshed", "write"
    if ENTRY_MARK in existing:
        return "outdated", "write"
    return "differs", "write"


def bundled_skill_path() -> str:
    """随包发布的技能文件。路径相对**代码位置**，不相对运行时数据根。"""
    return os.path.join(repo_dir(), "skills", "deepseek-harness", "SKILL.md")


def repo_dir() -> str:
    return os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))


def default_anchor(repo: str | None = None) -> tuple[str, str]:
    """推断「技能该装在哪」的锚点，并说明理由。

    DSH 的规则是「从会话目录向上找 `.git`，找不到就用会话目录」。难点在于
    **我们不知道用户会从哪个目录启动 DSH**，所以只能按仓库位置推断：

    * 仓库**自己有 `.git`**（正常 `git clone` 下来的）→ 锚点取仓库本身，
      技能装进仓库内部，在该目录里启动的会话能认出来。
    * 否则（仓库被放在某个工作区里当子目录）→ 锚点取仓库的**父目录**。

    用「命令在哪个目录跑」当锚点是错的：从 `recallos/` 里跑会把技能装进
    `recallos/.dsh/`，而 DSH 根本不会去那里找。

    「已经装过一份怎么办」不在这里处理 —— 见 `existing_install()`。
    """
    repo = os.path.abspath(repo or repo_dir())
    if os.path.isdir(os.path.join(repo, ".git")):
        return repo, "repo (has .git)"
    return os.path.dirname(repo), "repo parent (no .git)"


def existing_install(repo: str | None = None) -> Optional[tuple[str, str, str]]:
    """如果技能**已经装在某处**，返回 `(SKILL.md 路径, 技能根, 范围)`。

    为什么必须先查这个：不查就会在现有那份旁边**再放一份**。两份必然漂移，而且
    漂移是静默的——用户以为改动生效了，DSH 读的却是另一份。这是本项目里反复出现
    的一类 bug（同一份事实存两处），所以在源头挡掉。

    查找顺序：仓库级 → 父目录级 → 用户级。先项目后用户，因为项目级更具体。
    """
    repo = os.path.abspath(repo or repo_dir())
    roots: list[tuple[str, str]] = []
    for anchor in (repo, os.path.dirname(repo)):
        roots += [(b, s) for b, s in dsh.skill_roots(anchor) if s == "project"]
    roots += [(b, s) for b, s in dsh.skill_roots(repo) if s == "user"]

    seen: set[str] = set()
    for base, source in roots:
        if base in seen:            # 仓库与父目录可能算出同一个根
            continue
        seen.add(base)
        for candidate in (os.path.join(base, SKILL_NAME, "SKILL.md"),
                          os.path.join(base, f"{SKILL_NAME}.md")):
            if os.path.isfile(candidate):
                return candidate, base, source
    return None


def _read(path: str) -> str:
    # `utf-8-sig`：Windows 上的编辑器（含 PowerShell 5.1 的 `Set-Content`）默认会
    # 写一个 UTF-8 BOM。带着 BOM 读进来，正文第一个字符就变成 `\ufeff`，
    # 于是「逐字节相同」永远不成立 —— 用户什么都没改却被告知「内容不同」。
    with open(path, "r", encoding="utf-8-sig") as fh:
        return fh.read()


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("install-skill",
                       help="install the bundled Skill where DSH will find it")
    p.add_argument("--to", default=None,
                   help="explicit skill root (a directory); overrides detection")
    p.add_argument("--user", action="store_true",
                   help="install into the user-level root so every project sees it")
    p.add_argument("--name", default=SKILL_NAME, help="skill directory name")
    p.add_argument("--force", action="store_true",
                   help="overwrite an existing, different SKILL.md")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    source = bundled_skill_path()
    if not os.path.isfile(source):
        print(f"随包的技能文件不存在：{source}\n"
              "（发行包里应当有 skills/deepseek-harness/SKILL.md）", file=sys.stderr)
        return 2
    try:
        rendered = render_skill(_read(source), repo_dir())
    except SkillTemplateError as exc:
        print(f"技能模板不可用：{exc}", file=sys.stderr)
        return 2

    anchor, anchor_reason = default_anchor()
    candidates = dsh.skill_roots(anchor)
    project_root_path = dsh.find_project_root(anchor)

    if args.to:
        root_dir = os.path.abspath(args.to)
        target = os.path.join(root_dir, args.name, "SKILL.md")
        chosen_source = "explicit"
    elif args.user:
        root_dir = os.path.join(dsh.dsh_home(), "skills")
        target = os.path.join(root_dir, args.name, "SKILL.md")
        chosen_source = "user"
    else:
        # 已经装过一份就更新那一份，**绝不在旁边再放一份**（两份必然漂移）。
        already = existing_install()
        if already:
            target, root_dir, chosen_source = already
            anchor_reason += f" · 沿用已装位置 {already[0]}"
        else:
            target = dsh.skill_entry_path(anchor, args.name)
            root_dir = os.path.dirname(target)
            chosen_source = "project"

    existed = os.path.isfile(target)
    existing_text = _read(target) if existed else ""
    if existed:
        status, action = _decide(existing_text, rendered)
        if action == "write" and status in ("differs", "needs-local-block",
                                           "outdated") and not args.force:
            action = "refused"
    else:
        status, action = "installed", "write"

    if action == "write":
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(rendered)

    payload = {
        "source": source,
        "target": target,
        "skill_root": root_dir,
        "scope": chosen_source,
        "anchor": anchor,
        "anchor_reason": anchor_reason,
        "project_root": project_root_path,
        "status": status,
        "action": action,
        "existed": existed,
        "identical": bool(existed and existing_text == rendered),
        "entry": entry_path(),
        "python": _python_cmd(),
        "other_roots": [{"path": p, "source": s} for p, s in candidates],
    }

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if action != "refused" else 1

    if action == "skip":
        print(f"技能已经装好了，内容一致：\n  {target}")
        print("  （没有做任何改动）")
        return 0

    if action == "refused":
        if status == "needs-local-block":
            print(f"目标位置那份 SKILL.md 没有「本机位置」块：\n  {target}\n")
            print("说明它不是由 install-skill 写的（老版本副本或手工复制）——")
            print("这类副本里没有绝对路径，AI 会找不到 recall.py。")
            print("加 --force 重新生成一份带本机路径的技能。")
        elif status == "outdated":
            print(f"装过的技能和随包的版本不一致：\n  {target}\n")
            print("最常见的原因是**技能更新过了**（git pull 之后就会这样）；")
            print("也可能是你自己改过。两种情况都先不覆盖。")
            print("加 --force 升级到随包版本——它只重写那份 SKILL.md，"
                  "不碰你的资料库、索引和记忆。")
        else:
            print(f"目标位置已经有一个**内容不同**的 SKILL.md：\n  {target}\n")
            print("这可能是你自己改过的版本。为免覆盖你的改动，这里不写入。")
            print("确认要覆盖就加 --force；想换个位置用 --to <目录>。")
        return 1

    headline = {
        "installed": "技能已安装。",
        "refreshed": "已就地更新技能里的本机路径（正文没有改动）。",
    }.get(status, "技能已安装。")
    print(headline)
    print(f"  来源    {source}")
    print(f"  目标    {target}")
    scope_label = {"project": "项目级（只在这个工作区生效）",
                   "user": "用户级（所有项目都能用）",
                   "explicit": "你指定的位置"}[chosen_source]
    print(f"  范围    {scope_label}")
    print(f"  入口    {payload['python']} \"{payload['entry']}\"")
    print(f"  撤销    删除 {target}")

    other = [(p, s) for p, s in candidates
             if os.path.abspath(p) != os.path.abspath(root_dir)]
    if other:
        print("\n  DSH 还会去这些地方找技能（当前没装）：")
        for path, src in other[:3]:
            print(f"    [{src}] {path}")
        print(f'  想让所有项目都能用：{payload["python"]} "{entry_path()}" '
              "install-skill --user")

    print("\n  下一步：重启会话（或等宿主重新扫描技能目录），"
          "之后技能列表里应当出现 recallos。")
    return 0
