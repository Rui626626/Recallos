"""`recall.py scope` — 一次性授权「你可以搜哪些目录」。

为什么需要这个命令（这是一次产品层面的修正）
--------------------------------------------
原来的铁律是「只处理明确给出的路径，不静默扫盘」。这条**意图**是对的：不该有工具
偷偷把你整个硬盘读一遍。但它被实现成了「每次检索前都问用户目录」，于是产生了一个
自相矛盾的体验：

    用户：「帮我找简历」
    AI ：「请告诉我简历在哪个文件夹」
    用户：……我知道在哪，我还要你干什么？

**用户说得清目录，就说明他知道文件在哪 —— 那这个工具就没有存在价值了。**
工具的用途恰恰是「我不记得，或者不想记得」。

所以把「每次问」换成「问一次、记住」：

    recall.py scope default      # 把 Documents / Desktop / Downloads 记进范围（一次）
    recall.py search "简历"       # 之后一句话就搜，不再问

铁律本身没有松动，只是被放到了正确的位置：**范围之外永远不碰**。
`enabled_roots()` 是唯一的事实来源 —— 没有记录在案的目录，代码路径不会去读它。

为什么要落盘到 `brain.config.json`
----------------------------------
同一条事实只存一处（这是本项目反复踩过的坑）。范围是配置，不是缓存：
用户可以在文件里改，`scope show` 读的就是它。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from ..config import Config, Root, load_config, save_config

__all__ = ["register", "run", "default_candidates", "normalize_targets"]

CONFIG_HINT = "run: recall.py scope --help"


def _key(path: str) -> str:
    """去重键。Windows 上大小写不敏感，不能只比字符串。"""
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def normalize_targets(paths: list[str]) -> tuple[list[str], list[str]]:
    """把用户给的路径校验成可以入配置的绝对路径。

    返回 `(可用, 问题描述)`。**问题必须留痕**，不能静默丢掉 —— 用户以为授权了
    三个目录，实际只生效两个，那比不生效更糟。
    """
    ok: list[str] = []
    problems: list[str] = []
    seen: set[str] = set()
    for raw in paths:
        expanded = os.path.expanduser(os.path.expandvars(raw.strip()))
        if not expanded:
            continue
        absolute = os.path.abspath(expanded)
        if not os.path.exists(absolute):
            problems.append(f"不存在：{absolute}")
            continue
        if not os.path.isdir(absolute):
            problems.append(f"不是目录：{absolute}")
            continue
        key = _key(absolute)
        if key in seen:
            continue
        seen.add(key)
        ok.append(absolute)
    return ok, problems


def default_candidates(home: str | None = None) -> list[str]:
    """常见的资料目录。**只返回真实存在的**，不替用户想象一个目录。"""
    base = home or os.path.expanduser("~")
    names = [
        "Documents", "Desktop", "Downloads",
        os.path.join("OneDrive", "Documents"),
        os.path.join("OneDrive", "Desktop"),
        "文档", "桌面", "下载",
    ]
    out: list[str] = []
    seen: set[str] = set()
    for name in names:
        candidate = os.path.abspath(os.path.join(base, name))
        if not os.path.isdir(candidate):
            continue
        key = _key(candidate)
        if key in seen:
            continue
        seen.add(key)
        out.append(candidate)
    return out


def _current(cfg: Config) -> list[str]:
    return [r.resolved(cfg.root_dir) for r in cfg.roots]


def _write_scope(cfg: Config, paths: list[str]) -> str:
    cfg.roots = [Root(path=p, enabled=True, label="") for p in paths]
    return save_config(cfg)


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "scope",
        help="authorise the directories search may look in (ask once, remember)",
        description="一次性授权可以被检索的目录。授权之后 search 直接在这些目录里找，"
                    "不再问你要路径；范围之外的目录永远不会被读到。")
    p.add_argument("action", nargs="?", default="show",
                   choices=["show", "set", "add", "rm", "clear", "default"],
                   help="show (default) | set | add | rm | clear | default")
    p.add_argument("paths", nargs="*", help="directories for set/add/rm")
    p.add_argument("--home", default=None,
                   help="base for `default` (defaults to your home directory)")
    p.add_argument("--dry-run", action="store_true",
                   help="for `default`: only print what would be authorised")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    cfg = load_config()
    action = args.action

    if action == "show":
        return _show(cfg, args)
    if action == "clear":
        return _clear(cfg, args)
    if action == "default":
        return _default(cfg, args)
    if action in ("set", "add", "rm"):
        return _edit(cfg, args, action)
    print(f"未知动作：{action}", file=sys.stderr)
    return 2


def _show(cfg: Config, args) -> int:
    current = _current(cfg)
    payload = {"scope": current, "configured": bool(current),
               "config": os.path.join(cfg.root_dir, "brain.config.json")}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if not current:
        print("还没有授权任何目录。\n")
        print("  这意味着 `search` 不会自己扫盘 —— 它不知道该去哪里找。")
        print("  一次性授权（之后一句话就能搜，不再问你）：\n")
        print("    recall.py scope default        # 用 Documents / Desktop / Downloads")
        print('    recall.py scope set "某个目录"  # 或者自己指定')
        print("\n  也可以只搜一次、不授权：search \"查询\" --in \"某个目录\"")
        return 0

    print(f"已授权 {len(current)} 个目录（`search` 会直接在这些目录里找）：")
    for path in current:
        marker = "" if os.path.isdir(path) else "   ← 已不存在"
        print(f"  - {path}{marker}")
    print("\n  范围之外的目录永远不会被读到。调整：scope set / scope add / scope rm")
    return 0


def _edit(cfg: Config, args, action: str) -> int:
    if not args.paths:
        print(f"scope {action} 需要一个或多个目录", file=sys.stderr)
        return 2

    if action == "rm":
        # 删除时允许「已经不存在」的路径 —— 否则一个被删掉的目录永远删不掉。
        wanted = {_key(p) for p in args.paths}
        before = _current(cfg)
        after = [p for p in before if _key(p) not in wanted]
        removed = [p for p in before if _key(p) in wanted]
        _write_scope(cfg, after)
        payload = {"action": "rm", "removed": removed, "scope": after}
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        for path in removed:
            print(f"  移除  {path}")
        if not removed:
            print("  没有匹配的已授权目录。")
        print(f"  现在共 {len(after)} 个目录")
        return 0

    ok, problems = normalize_targets(args.paths)
    for problem in problems:
        print(f"  ! {problem}", file=sys.stderr)
    if not ok:
        print("没有可用的目录，未做改动。", file=sys.stderr)
        return 2

    if action == "set":
        after = ok
    else:
        existing = _current(cfg)
        seen = {_key(p) for p in existing}
        after = existing + [p for p in ok if _key(p) not in seen]

    config_path = _write_scope(cfg, after)
    payload = {"action": action, "added": ok, "problems": problems,
               "scope": after, "config": config_path}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if not problems else 0

    for path in ok:
        print(f"  授权  {path}")
    print(f"\n  现在共 {len(after)} 个目录；之后 `search \"查询\"` 直接在里面找，"
          "不会再问你。")
    print(f"  记录在 {config_path}（范围之外的目录永远不会被读到）")
    return 0


def _default(cfg: Config, args) -> int:
    candidates = default_candidates(args.home)
    if not candidates:
        print("没找到常见的资料目录（Documents / Desktop / Downloads）。\n"
              '  用 `recall.py scope set "<目录>"` 自己指定。', file=sys.stderr)
        return 2

    if args.dry_run:
        payload = {"action": "default", "dry_run": True, "would_authorise": candidates}
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        print("将会授权这些目录（--dry-run 没有写入）：")
        for path in candidates:
            print(f"  - {path}")
        return 0

    config_path = _write_scope(cfg, candidates)
    payload = {"action": "default", "added": candidates, "scope": candidates,
               "config": config_path}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print("已授权这些目录：")
    for path in candidates:
        print(f"  - {path}")
    print("\n  之后 `search \"查询\"` 直接在这些目录里找，不会再问你。")
    print(f"  记录在 {config_path}；想缩小范围用 scope set / scope rm。")
    return 0
