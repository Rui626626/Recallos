"""Durable bookkeeping that lives outside the database.

Two files, both human-readable:

* `state/sources.json` — **the authoritative list of what is tracked.** This is
  what makes a rebuild possible after `index/` is deleted, and it is the only
  record that a previously-seen path ever existed.

* `state/imports.jsonl` — one JSON object per *state change*. Skips are
  deliberately not journaled: a daily run over a large tree would otherwise grow
  the log without adding information. Skip counts live in the run summary.

Two properties this module is responsible for:

**The authoritative layer must not lag the derived layer.** The pipeline calls
`save()` after every document, immediately *before* committing that document's
database transaction. A crash must never leave a document that is indexed but
not registered — such a document could never be rebuilt.

**Writing must be idempotent.** `save()` compares the serialised payload with
what was last written and skips the write when nothing changed, so re-running an
import does not churn `updated_at` and does not make `data/` undiffable.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from typing import Iterator, Optional

__all__ = ["SourceRegistry", "Journal", "now_iso"]


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class SourceRegistry:
    """Small JSON file mapping tracked sources to their document ids."""

    VERSION = 1

    def __init__(self, path: str) -> None:
        self.path = path
        self._items: dict[str, dict] = {}
        self._last_written: Optional[str] = None
        self.load()

    # ------------------------------------------------------------------ load

    def load(self) -> None:
        self._items = {}
        self._last_written = None
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw_text = fh.read()
            raw = json.loads(raw_text)
        except (OSError, json.JSONDecodeError):
            # A corrupt registry must not block work; entries are restored by
            # the next successful import of each source.
            return
        for item in raw.get("sources") or []:
            key = item.get("key")
            if key:
                self._items[key] = item
        self._last_written = raw_text

    # ------------------------------------------------------------------ save

    def _payload_text(self) -> str:
        payload = {
            "version": self.VERSION,
            "sources": sorted(self._items.values(), key=lambda i: i.get("key", "")),
        }
        return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"

    def save(self, force: bool = False) -> str:
        """Write only when the content actually changed.

        Returns the path. A no-op write returns immediately, which is what keeps
        `sources.json` byte-identical across repeated imports.
        """
        text = self._payload_text()
        if not force and text == self._last_written:
            return self.path
        os.makedirs(os.path.dirname(os.path.abspath(self.path)) or ".", exist_ok=True)
        _atomic_write_text(self.path, text)
        self._last_written = text
        return self.path

    # ------------------------------------------------------------------ edit

    @staticmethod
    def key(source_kind: str, source_ref: str) -> str:
        from .core.ids import source_key

        return source_key(source_kind, source_ref)

    def add(self, source_kind: str, source_ref: str, doc_id: str,
            extra: Optional[dict] = None) -> str:
        """Register a source. Returns `added` | `changed` | `unchanged`.

        `updated_at` only moves when something actually differs, so a repeated
        import leaves the file untouched.
        """
        key = self.key(source_kind, source_ref)
        existing = self._items.get(key)
        entry = {
            "key": key,
            "source_kind": source_kind,
            "source_ref": source_ref,
            "doc_id": doc_id,
            "added_at": (existing or {}).get("added_at") or now_iso(),
            "updated_at": (existing or {}).get("updated_at") or now_iso(),
        }
        if extra:
            entry.update(extra)
        entry.pop("missing_at", None)

        if existing is None:
            entry["updated_at"] = now_iso()
            self._items[key] = entry
            return "added"

        comparable = {k: v for k, v in existing.items() if k != "updated_at"}
        if comparable == {k: v for k, v in entry.items() if k != "updated_at"}:
            self._items[key] = existing
            return "unchanged"

        entry["updated_at"] = now_iso()
        self._items[key] = entry
        return "changed"

    def mark_missing(self, source_kind: str, source_ref: str,
                     note: str = "") -> bool:
        """Flag a source as absent **without forgetting it**.

        Forgetting is what would destroy the only evidence that a later
        re-appearance at a different path is the same material.
        """
        key = self.key(source_kind, source_ref)
        entry = self._items.get(key)
        if entry is None or entry.get("missing_at"):
            return False
        entry["missing_at"] = now_iso()
        if note:
            entry["missing_note"] = note
        return True

    def clear_missing(self, source_kind: str, source_ref: str) -> bool:
        key = self.key(source_kind, source_ref)
        entry = self._items.get(key)
        if entry is None or not entry.get("missing_at"):
            return False
        entry.pop("missing_at", None)
        entry.pop("missing_note", None)
        entry["updated_at"] = now_iso()
        return True

    def remove(self, source_kind: str, source_ref: str) -> bool:
        """Forget a source entirely. Only used by an explicit `--prune`."""
        return self._items.pop(self.key(source_kind, source_ref), None) is not None

    def forget_doc(self, doc_id: str) -> int:
        """Drop every entry pointing at `doc_id` (used when a doc is pruned)."""
        victims = [k for k, v in self._items.items() if v.get("doc_id") == doc_id]
        for key in victims:
            del self._items[key]
        return len(victims)

    # ------------------------------------------------------------------ read

    def all(self) -> list[dict]:
        return sorted(self._items.values(), key=lambda i: i.get("source_ref", ""))

    def present(self) -> list[dict]:
        return [i for i in self.all() if not i.get("missing_at")]

    def missing(self) -> list[dict]:
        return [i for i in self.all() if i.get("missing_at")]

    def get(self, source_kind: str, source_ref: str) -> Optional[dict]:
        return self._items.get(self.key(source_kind, source_ref))

    def doc_ids(self) -> dict[str, str]:
        return {i["source_ref"]: i["doc_id"] for i in self.all() if "doc_id" in i}

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self) -> Iterator[dict]:
        return iter(self.all())


class Journal:
    """Append-only log of state changes. One JSON object per line."""

    def __init__(self, path: str) -> None:
        self.path = path

    def append(self, record: dict) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.path)) or ".", exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": now_iso(), **record}, ensure_ascii=False) + "\n")

    def tail(self, n: int = 20) -> list[dict]:
        if not os.path.exists(self.path):
            return []
        with open(self.path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
        out: list[dict] = []
        for line in lines[-n:]:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # a torn final line is tolerated, never fatal
        return out

    def counts(self) -> dict:
        counts: dict[str, int] = {}
        if not os.path.exists(self.path):
            return counts
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                state = rec.get("status", "?")
                counts[state] = counts.get(state, 0) + 1
        return counts

    def find_doc(self, doc_id: str, limit: int = 5) -> list[dict]:
        """Most recent journal entries for one document, newest first."""
        if not os.path.exists(self.path):
            return []
        found: list[dict] = []
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("doc_id") == doc_id:
                    found.append(rec)
        return list(reversed(found))[:limit]


def _atomic_write_text(path: str, text: str) -> None:
    """Write-then-rename so a crash cannot leave a half-written registry."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise
