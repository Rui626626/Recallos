"""DeepSeek Harness adapter — only verified host facts live here.

Every claim below was confirmed by reading the installed DSH sources or by
running a command in this environment. Anything unverified is marked as such
and must not be relied on.

**Skill discovery** (`@deepseek-ai/dsh-skill-filesystem`, `roots()`):

    project root   = walk up from the session cwd looking for `.git`;
                     if no `.git` is found anywhere, the cwd itself is used
    project roots  = <projectRoot>/.dsh/skills  then  <projectRoot>/.agents/skills
    custom roots   = config.customSkillDirs
    user roots     = $DSH_HOME/skills           then  ~/.agents/skills
    bundled        = $DSH_BUNDLED_SKILL_DIR

Consequence for this project: the session cwd is
`…/deepseek-harness/default-workspace` and there is **no `.git` above it**, so
the project root is the workspace itself. The Skill entry therefore belongs at

    <workspace>/.dsh/skills/recallos/SKILL.md

i.e. *beside* the `recallos/` code directory, not inside it. A `SKILL.md` placed
inside `recallos/` would never be discovered.

Two accepted shapes: a directory bundle (`<name>/SKILL.md` with YAML
frontmatter) or a flat `<name>.md`. The filesystem provider also watches the
roots with chokidar, so edits are picked up without restarting the host.
"""

from __future__ import annotations

import os
from typing import Iterator, Optional

HOST = "dsh"
VERIFIED_ON = "2026-10-15"

PROJECT_SKILL_DIRS = ((".dsh", "skills"), (".agents", "skills"))
USER_SKILL_DIRS = ((".dsh", "skills"), (".agents", "skills"))


def dsh_home() -> str:
    """`$DSH_HOME`, falling back to `~/.dsh`."""
    override = os.environ.get("DSH_HOME", "").strip()
    if override:
        return os.path.abspath(override)
    return os.path.join(os.path.expanduser("~"), ".dsh")


def find_project_root(cwd: str) -> str:
    """Mirror of DSH's `findProjectRoot`: nearest `.git` above, else `cwd`."""
    current = os.path.abspath(cwd)
    while True:
        if os.path.isdir(os.path.join(current, ".git")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return os.path.abspath(cwd)
        current = parent


def skill_roots(project_dir: str) -> list[tuple[str, str]]:
    """Every root DSH scans, in rank order, as (path, source)."""
    root = find_project_root(project_dir)
    roots: list[tuple[str, str]] = []
    for parts in PROJECT_SKILL_DIRS:
        roots.append((os.path.join(root, *parts), "project"))
    home = os.path.expanduser("~")
    # 用户级：`.dsh` 那一支直接在 `$DSH_HOME` 下面，**不再拼一层 `.dsh`**。
    #
    # 曾经写成 `os.path.join(dsh_home(), *parts)`，而 parts 是 `(".dsh","skills")`，
    # 于是得到 `~/.dsh/.dsh/skills` —— 一个 DSH 永远不会扫描的路径。后果很隐蔽：
    # 项目级装上时一切正常，只有 `--user` 安装会「报告成功但永远不生效」。
    roots.append((os.path.join(dsh_home(), "skills"), "user"))
    roots.append((os.path.join(home, ".agents", "skills"), "user"))
    return roots


def skill_entry_path(project_dir: str, name: str = "recallos") -> str:
    """The location the Skill entry should occupy for this project."""
    root = find_project_root(project_dir)
    return os.path.join(root, ".dsh", "skills", name, "SKILL.md")


def find_skill_entry(project_dir: str, name: str = "recallos") -> Optional[str]:
    for base, _ in skill_roots(project_dir):
        candidate = os.path.join(base, name, "SKILL.md")
        if os.path.isfile(candidate):
            return candidate
        flat = os.path.join(base, f"{name}.md")
        if os.path.isfile(flat):
            return flat
    return None


def session_root() -> str:
    """Where DSH writes session transcripts, one directory per encoded cwd."""
    return os.path.join(dsh_home(), "sessions")


def session_transcripts(cwd: Optional[str] = None) -> Iterator[str]:
    """Yield `session.v4.jsonl.zstd` paths.

    Note for P3: these files are **multiple zstd frames concatenated**. Reading
    only the first frame silently truncates them, so a streaming decompressor
    (`zstandard.ZstdDecompressor().stream_reader(...)`) is mandatory.

    Not used in P0 — this function exists so the adapter's shape is settled
    before the session-archive work starts.
    """
    root = session_root()
    if not os.path.isdir(root):
        return
    encoded = None
    if cwd:
        encoded = "--" + os.path.abspath(cwd).replace("\\", "-").replace(":", "-") + "--"
    for entry in sorted(os.listdir(root)):
        directory = os.path.join(root, entry)
        if not os.path.isdir(directory):
            continue
        if encoded and entry != encoded:
            continue
        for session in sorted(os.listdir(directory)):
            path = os.path.join(directory, session, "session.v4.jsonl.zstd")
            if os.path.isfile(path):
                yield path


def probe(project_dir: str) -> dict:
    """Report the host facts this adapter depends on, as data.

    Used by `doctor` and by the tests, so a change in the host's layout shows
    up as a failing check rather than as a silently misplaced skill.
    """
    entry = skill_entry_path(project_dir)
    return {
        "host": HOST,
        "verified_on": VERIFIED_ON,
        "dsh_home": dsh_home(),
        "project_root": find_project_root(project_dir),
        "skill_roots": [{"path": p, "source": s} for p, s in skill_roots(project_dir)],
        "expected_skill_entry": entry,
        "skill_entry_present": os.path.isfile(entry),
        "session_root": session_root(),
        "session_root_exists": os.path.isdir(session_root()),
    }
