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
import shutil
import sys
from typing import Optional

from ..adapters import dsh

__all__ = ["register", "run", "SKILL_NAME", "bundled_skill_path",
           "default_anchor", "existing_install"]

SKILL_NAME = "recallos"


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
    with open(path, "r", encoding="utf-8") as fh:
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
    same = existed and _read(target) == _read(source)

    if existed and same:
        status = "up-to-date"
        action = "skip"
    elif existed and not args.force:
        status = "differs"
        action = "refused"
    else:
        status = "installed"
        action = "write"
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copyfile(source, target)

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
        "identical": bool(same),
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
        print(f"目标位置已经有一个**内容不同**的 SKILL.md：\n  {target}\n")
        print("这可能是你自己改过的版本。为免覆盖你的改动，这里不写入。")
        print("确认要覆盖就加 --force；想换个位置用 --to <目录>。")
        return 1

    print("技能已安装。")
    print(f"  来源    {source}")
    print(f"  目标    {target}")
    scope_label = {"project": "项目级（只在这个工作区生效）",
                   "user": "用户级（所有项目都能用）",
                   "explicit": "你指定的位置"}[chosen_source]
    print(f"  范围    {scope_label}")
    print(f"  撤销    删除 {target}")

    other = [(p, s) for p, s in candidates
             if os.path.abspath(p) != os.path.abspath(root_dir)]
    if other:
        print("\n  DSH 还会去这些地方找技能（当前没装）：")
        for path, src in other[:3]:
            print(f"    [{src}] {path}")
        print("  想让所有项目都能用：recall.py install-skill --user")

    print("\n  下一步：重启会话（或等宿主重新扫描技能目录），"
          "之后技能列表里应当出现 recallos。")
    return 0
