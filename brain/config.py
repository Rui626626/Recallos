"""Configuration: one JSON file, no hidden defaults in code paths.

Everything that could differ between machines lives in `brain.config.json`:
which directories are in scope, which extensions are indexed, and the retrieval
budgets. Nothing here reads a Windows-specific path unless the config says so.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Optional

__all__ = ["Config", "load_config", "project_root", "CONFIG_NAME"]

CONFIG_NAME = "brain.config.json"

DEFAULT_EXTENSIONS = (
    ".md", ".markdown", ".txt", ".text",
    ".docx", ".pptx", ".xlsx",
    ".pdf", ".html", ".htm",
)

DEFAULT_EXCLUDES = (
    "**/~$*", "**/.git/**", "**/.svn/**", "**/node_modules/**",
    "**/__pycache__/**", "**/.venv/**", "**/venv/**",
    "**/$RECYCLE.BIN/**", "**/System Volume Information/**",
    "**/.dsh-backup/**", "**/.trash/**",
)


def project_root() -> str:
    """The directory that contains `bin/` and `brain/`.

    `RECALLOS_ROOT` overrides it. That single hook is what lets the test suite
    drive a throwaway root, a scheduled task pin an explicit location, and a
    future WPS skill point at whatever storage it is given — without any code
    change or hard-coded path.
    """
    override = os.environ.get("RECALLOS_ROOT", "").strip()
    if override:
        return os.path.abspath(override)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@dataclass
class Root:
    path: str
    enabled: bool = True
    label: str = ""

    def resolved(self, base: str) -> str:
        p = self.path
        if not os.path.isabs(p):
            p = os.path.join(base, p)
        return os.path.normpath(p)


@dataclass
class Config:
    root_dir: str
    roots: list[Root] = field(default_factory=list)
    extensions: list[str] = field(default_factory=lambda: list(DEFAULT_EXTENSIONS))
    excludes: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDES))
    chunk_target_chars: int = 900
    chunk_max_chars: int = 1600
    search_limit: int = 20
    max_chunks_per_doc: int = 3
    token_budget: int = 2500
    snippet_chars: int = 240
    raw: dict = field(default_factory=dict)

    # ------------------------------------------------------------------ paths

    @property
    def data_dir(self) -> str:
        return self._path("data")

    @property
    def index_dir(self) -> str:
        return self._path("index")

    @property
    def state_dir(self) -> str:
        return self._path("state")

    @property
    def cache_dir(self) -> str:
        return self._path("cache")

    @property
    def db_path(self) -> str:
        return os.path.join(self.index_dir, "recall.db")

    @property
    def sources_path(self) -> str:
        return os.path.join(self.state_dir, "sources.json")

    @property
    def journal_path(self) -> str:
        return os.path.join(self.state_dir, "imports.jsonl")

    @property
    def aliases_path(self) -> str:
        """别名表：把「你平时怎么叫」映射到「文档里怎么写」。"""
        return os.path.join(self.root_dir, "aliases.json")

    @property
    def memory_dir(self) -> str:
        """长期记忆：一个主题一个文件，带修订历史。

        放在 `data/` 下是刻意的——记忆是**权威数据**，不是派生物。索引删掉、
        cache 清空，记忆都必须还在。
        """
        return os.path.join(self.data_dir, "memory")

    @property
    def expired_memory_dir(self) -> str:
        """已过期的记忆。放到子目录里，让检索可以按路径默认排除。"""
        return os.path.join(self.memory_dir, "_expired")

    @property
    def artifacts_dir(self) -> str:
        """由检索结果汇编出来的成品（复习材料、报告、大纲）。"""
        return self._path("artifacts")

    def docs_dir(self) -> str:
        return os.path.join(self.data_dir, "docs")

    def _path(self, key: str) -> str:
        paths = self.raw.get("paths") or {}
        value = paths.get(key, key)
        if not os.path.isabs(value):
            value = os.path.join(self.root_dir, value)
        return os.path.normpath(value)

    # ------------------------------------------------------------------ edits

    def enabled_roots(self) -> list[str]:
        base = self.root_dir
        return [r.resolved(base) for r in self.roots if r.enabled]

    def to_json(self) -> dict:
        return {
            "version": 1,
            "roots": [{"path": r.path, "enabled": r.enabled, "label": r.label}
                      for r in self.roots],
            "extensions": list(self.extensions),
            "excludes": list(self.excludes),
            "chunk": {
                "target_chars": self.chunk_target_chars,
                "max_chars": self.chunk_max_chars,
            },
            "search": {
                "default_limit": self.search_limit,
                "max_chunks_per_doc": self.max_chunks_per_doc,
                "token_budget": self.token_budget,
                "snippet_chars": self.snippet_chars,
            },
            "paths": self.raw.get("paths") or {},
        }


def _default_config_dict() -> dict:
    return {
        "version": 1,
        "roots": [],
        "extensions": list(DEFAULT_EXTENSIONS),
        "excludes": list(DEFAULT_EXCLUDES),
        "chunk": {"target_chars": 900, "max_chars": 1600},
        "search": {
            "default_limit": 20,
            "max_chunks_per_doc": 3,
            "token_budget": 2500,
            "snippet_chars": 240,
        },
        "paths": {},
    }


def default_config(root_dir: Optional[str] = None) -> Config:
    root = root_dir or project_root()
    return _from_dict(root, _default_config_dict())


def load_config(root_dir: Optional[str] = None,
                path: Optional[str] = None) -> Config:
    """Load `brain.config.json`. A missing file yields defaults — the tool must
    be usable immediately after cloning, before any configuration exists."""
    root = root_dir or project_root()
    cfg_path = path or os.path.join(root, CONFIG_NAME)
    if not os.path.exists(cfg_path):
        return default_config(root)
    with open(cfg_path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    return _from_dict(root, raw)


def _from_dict(root: str, raw: dict) -> Config:
    roots = [
        Root(path=str(r.get("path", "")),
             enabled=bool(r.get("enabled", True)),
             label=str(r.get("label", "")))
        for r in (raw.get("roots") or [])
        if str(r.get("path", "")).strip()
    ]
    chunk = raw.get("chunk") or {}
    search = raw.get("search") or {}
    return Config(
        root_dir=root,
        roots=roots,
        extensions=[e.lower() for e in (raw.get("extensions") or DEFAULT_EXTENSIONS)],
        excludes=list(raw.get("excludes") or DEFAULT_EXCLUDES),
        chunk_target_chars=int(chunk.get("target_chars", 900)),
        chunk_max_chars=int(chunk.get("max_chars", 1600)),
        search_limit=int(search.get("default_limit", 20)),
        max_chunks_per_doc=int(search.get("max_chunks_per_doc", 3)),
        token_budget=int(search.get("token_budget", 2500)),
        snippet_chars=int(search.get("snippet_chars", 240)),
        raw=raw,
    )


def save_config(cfg: Config, path: Optional[str] = None) -> str:
    target = path or os.path.join(cfg.root_dir, CONFIG_NAME)
    # 建父目录：`scope` 可能是用户在新机器上跑的**第一条**命令，那时还没有
    # `init` 过，甚至 `RECALLOS_ROOT` 指着的目录都还不存在。少这一行就会在
    # 「刚装好」这条最该顺畅的路径上抛 FileNotFoundError。
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(target, "w", encoding="utf-8") as fh:
        json.dump(cfg.to_json(), fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return target


def ensure_dirs(cfg: Config) -> None:
    for d in (cfg.data_dir, cfg.index_dir, cfg.state_dir, cfg.cache_dir,
              cfg.docs_dir(), cfg.memory_dir, cfg.artifacts_dir):
        os.makedirs(d, exist_ok=True)
    # 过期目录**不预建**：它出现本身就意味着「确实有东西被标记过期了」。


def dump(cfg: Config) -> dict[str, Any]:
    return cfg.to_json()
