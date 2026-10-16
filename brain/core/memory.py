"""长期记忆：一个主题一个文件，带修订历史。

三条设计决定，每条都对应计划里的一个验收标准
----------------------------------------------

**1. 一个主题只产出一个文件。** 同一件事记两次 → 文件数不变，只是「当前结论」被
更新、修订历史多一行。靠 `topic` 作为键实现；不传 `--topic` 时用文本本身推一个
slug，所以同一句话重复记也落在同一个文件上。

**2. 旧版本留痕不删。** 历史写在同一个文件的 `## 修订历史` 里——**不删旧文件、
不建 v2 文件**。理由是这个项目的铁律：「用户数据绝不能只存在于索引里」。修订史
留在正文里，任何编辑器都能看，索引全删也不影响。

**3. 过期不是删除。** `expire` 把文件**移动**到 `_expired/` 子目录并把 frontmatter
的 `status` 改成 `expired`。检索默认按路径排除它，`--include-expired` 放回来。
用移动而不是打标记，是为了不碰索引 schema——而且 P1 已经有的「改名/移动识别」会
正确地把 doc_id 保留下来，历史不会断。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Optional

__all__ = [
    "MemoryEntry", "topic_slug", "entry_path", "load_entry", "render_entry",
    "find_entry", "remember", "set_status", "list_entries", "export_entry",
    "memory_id_for",
]

STATUS_ACTIVE = "active"
STATUS_EXPIRED = "expired"


# ------------------------------------------------------------------ 基础工具


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def topic_slug(text: str, fallback: str = "note") -> str:
    """主题键：小写、空格转连字符、去掉文件系统不友好的字符。"""
    s = (text or "").strip()
    s = re.sub(r"[\\/:*?\"<>|\n\r]+", " ", s)
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"[^\w\u4e00-\u9fff\-]", "", s).strip("-")
    return (s[:60].strip("-") or fallback).lower()


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """极简 frontmatter 解析：只认 `key: value` 与逗号分隔的 tags。"""
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}, text
    meta: dict = {}
    for raw in lines[1:end]:
        if ":" in raw and not raw.startswith((" ", "\t")):
            k, v = raw.split(":", 1)
            meta[k.strip()] = v.strip()
    return meta, "\n".join(lines[end + 1:]).lstrip("\n")


# ------------------------------------------------------------------ 数据结构


@dataclass
class MemoryEntry:
    topic: str
    title: str
    created: str = ""
    updated: str = ""
    revisions: int = 0
    status: str = STATUS_ACTIVE
    tags: list[str] = field(default_factory=list)
    conclusion: str = ""
    history: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "topic": self.topic, "title": self.title, "status": self.status,
            "created": self.created, "updated": self.updated,
            "revisions": self.revisions, "tags": list(self.tags),
            "conclusion": self.conclusion, "history": list(self.history),
        }


SECTION_NOW = "## 当前结论"
SECTION_HISTORY = "## 修订历史"


def render_entry(e: MemoryEntry) -> str:
    fm = [
        "---",
        f"topic: {e.topic}",
        f"title: {e.title}",
        f"created: {e.created}",
        f"updated: {e.updated}",
        f"revisions: {e.revisions}",
        f"status: {e.status}",
        f"tags: {', '.join(e.tags)}".rstrip(),
        "---",
        "",
    ]
    body = [
        f"# {e.title}",
        "",
        SECTION_NOW,
        "",
        e.conclusion.strip(),
        "",
        SECTION_HISTORY,
        "",
    ]
    body.extend(e.history or [f"- {e.updated} — {e.conclusion.strip()}"])
    body.append("")
    body.append("<!-- 本文件是权威数据：索引删掉、cache 清空，它都必须还在。 -->")
    body.append("")
    return "\n".join(fm) + "\n".join(body)


def parse_entry(text: str, fallback_topic: str = "") -> MemoryEntry:
    meta, body = _parse_frontmatter(text)
    tags = [t.strip() for t in str(meta.get("tags", "")).split(",") if t.strip()]
    conclusion = ""
    history: list[str] = []
    if SECTION_NOW in body:
        tail = body.split(SECTION_NOW, 1)[1]
        conclusion = tail.split(SECTION_HISTORY, 1)[0].strip() if \
            SECTION_HISTORY in tail else tail.strip()
    if SECTION_HISTORY in body:
        hist = body.split(SECTION_HISTORY, 1)[1]
        history = [ln.strip() for ln in hist.splitlines()
                   if ln.strip().startswith("- ")]
    title = next((ln[2:].strip() for ln in body.splitlines()
                  if ln.startswith("# ")), str(meta.get("topic") or fallback_topic))
    try:
        revisions = int(str(meta.get("revisions", "0")) or 0)
    except ValueError:
        revisions = len(history)
    return MemoryEntry(
        topic=str(meta.get("topic") or fallback_topic),
        title=title,
        created=str(meta.get("created") or ""),
        updated=str(meta.get("updated") or ""),
        revisions=revisions,
        status=str(meta.get("status") or STATUS_ACTIVE),
        tags=tags,
        conclusion=conclusion,
        history=history,
    )


# ------------------------------------------------------------------ 磁盘操作


def entry_path(cfg, topic: str, expired: bool = False) -> str:
    base = cfg.expired_memory_dir if expired else cfg.memory_dir
    return os.path.join(base, f"{topic}.md")


def find_entry(cfg, topic: str) -> Optional[tuple[str, MemoryEntry]]:
    """按主题找记忆。活跃目录优先，其次过期目录。"""
    key = topic_slug(topic)
    for expired in (False, True):
        path = entry_path(cfg, key, expired=expired)
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as fh:
                return path, parse_entry(fh.read(), fallback_topic=key)
    return None


def _write(path: str, entry: MemoryEntry) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(render_entry(entry))


def remember(cfg, text: str, topic: Optional[str] = None,
             tags: Iterable[str] = (), title: Optional[str] = None) -> dict:
    """记一件事。已存在的主题就地更新（文件数不变，修订 +1）。"""
    text = (text or "").strip()
    if not text:
        raise ValueError("要记的内容不能为空")
    key = topic_slug(topic or text)
    tags = [t.strip() for t in tags if str(t).strip()]

    found = find_entry(cfg, key)
    now = _now()
    moved_from = None

    if found:
        old_path, entry = found
        existed = True
        was_expired = entry.status == STATUS_EXPIRED or \
            os.path.dirname(old_path) == cfg.expired_memory_dir

        # 修订历史是**完整版本日志，最新在最前**：记第二次就多一行。
        # 内容没变时不动历史，也不涨版本号——重复粘贴同一句话不该产生噪声。
        if entry.conclusion != text:
            entry.history.insert(0, f"- {now} — {text}")
            entry.conclusion = text
            entry.revisions = max(entry.revisions + 1, len(entry.history))
        entry.updated = now
        entry.revisions = max(entry.revisions, len(entry.history), 1)
        if title:
            entry.title = title
        for t in tags:
            if t not in entry.tags:
                entry.tags.append(t)
        # 重新记录一条过期主题 = 它又有效了
        if was_expired:
            entry.status = STATUS_ACTIVE
            target = entry_path(cfg, key, expired=False)
            _write(target, entry)
            try:
                os.unlink(old_path)
            except OSError:
                pass
            moved_from = old_path
            path = target
        else:
            path = old_path
            _write(path, entry)
    else:
        existed = False
        entry = MemoryEntry(
            topic=key,
            title=title or (text.splitlines()[0][:60] if text else key),
            created=now, updated=now, revisions=1, status=STATUS_ACTIVE,
            tags=tags, conclusion=text,
            history=[f"- {now} — {text}"],
        )
        path = entry_path(cfg, key, expired=False)
        _write(path, entry)

    return {"path": path, "entry": entry, "existed": existed,
            "reactivated_from": moved_from, "topic": key}


def set_status(cfg, topic: str, status: str) -> dict:
    """active ↔ expired。跨目录**移动**文件，不是打标记。"""
    found = find_entry(cfg, topic)
    if not found:
        raise FileNotFoundError(f"没有这个主题：{topic}")
    old_path, entry = found
    entry.status = status
    entry.updated = _now()
    target = entry_path(cfg, entry.topic, expired=(status == STATUS_EXPIRED))
    _write(target, entry)
    if os.path.abspath(target) != os.path.abspath(old_path):
        try:
            os.unlink(old_path)
        except OSError:
            pass
    return {"path": target, "from": old_path, "entry": entry}


def list_entries(cfg, include_expired: bool = True) -> list[dict]:
    out: list[dict] = []
    dirs = [(cfg.memory_dir, STATUS_ACTIVE)]
    if include_expired:
        dirs.append((cfg.expired_memory_dir, STATUS_EXPIRED))
    for directory, default_status in dirs:
        if not os.path.isdir(directory):
            continue
        for name in sorted(os.listdir(directory)):
            if not name.lower().endswith(".md"):
                continue
            path = os.path.join(directory, name)
            if not os.path.isfile(path):
                continue
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    entry = parse_entry(fh.read(), fallback_topic=name[:-3])
            except OSError:
                continue
            if not entry.status:
                entry.status = default_status
            out.append({"path": path, **entry.to_dict()})
    out.sort(key=lambda d: (d["status"], d["topic"]))
    return out


# ------------------------------------------------------------------ 与 DSH 记忆互通


def memory_id_for(entry: MemoryEntry, when: Optional[str] = None) -> str:
    """DSH 记忆插件的 id 形态：`YYYYMMDD-HHMMSS-<slug>`。"""
    stamp = (when or entry.updated or _now())
    digits = re.sub(r"[^0-9]", "", stamp)[:14]
    if len(digits) < 14:
        digits = datetime.now().strftime("%Y%m%d%H%M%S")
    return f"{digits[:8]}-{digits[8:14]}-{entry.topic}"


def export_entry(entry: MemoryEntry, target_dir: str, scope: str = "user",
                 path_tag: str = "") -> str:
    """写成现有记忆插件能直接读的文件。**已存在的文件绝不覆盖**。"""
    os.makedirs(target_dir, exist_ok=True)
    base = memory_id_for(entry)
    candidate = os.path.join(target_dir, base + ".md")
    n = 2
    while os.path.exists(candidate):
        candidate = os.path.join(target_dir, f"{base}-{n}.md")
        n += 1
    saved_at = entry.updated or _now()
    if not saved_at.endswith("Z"):
        saved_at = saved_at.replace(" ", "T") + "Z"
    fm = [
        "---",
        f"id: {os.path.basename(candidate)[:-3]}",
        f"scope: {scope}",
        f"path: {path_tag}",
        f"saved_at: {saved_at}",
        f"tags: {', '.join(entry.tags)}".rstrip(),
        "---",
        "",
        f"# {entry.title}",
        "",
        entry.conclusion.strip(),
        "",
    ]
    with open(candidate, "w", encoding="utf-8") as fh:
        fh.write("\n".join(fm))
    return candidate
