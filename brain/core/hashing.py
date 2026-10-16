"""File fingerprints and content hashes.

There are **three different questions** here, and conflating them was a defect
in the first draft of this layer:

| question | answered by | cost |
|---|---|---|
| might the file have changed? | `Fingerprint` (mtime_ns + size) | one `stat` |
| did the file's bytes change? | `hash_file()` | reads the file |
| did the *indexable content* change? | `content_hash()` in `normalize` | needs a parse |

`stat_is_trusted()` is the only place allowed to decide that the cheap answer is
good enough. It is deliberately conservative — a wrong "unchanged" silently
loses an edit forever, while a wrong "changed" merely costs one re-parse.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import Optional

__all__ = [
    "Fingerprint", "fingerprint", "hash_bytes", "hash_file", "hash_text",
    "stat_is_trusted", "GRACE_NS", "now_ns",
]

_READ_CHUNK = 1 << 20  # 1 MiB

# If a file's timestamp is newer than this, the timestamp itself cannot be
# trusted: a filesystem with coarse granularity (exFAT/SMB, or FAT at 2 s) can
# stamp two same-size edits inside one tick.
GRACE_NS = 2_000_000_000


def now_ns() -> int:
    import time

    return time.time_ns()


@dataclass(frozen=True)
class Fingerprint:
    path: str
    mtime_ns: int
    mtime: float
    size: int

    def as_dict(self) -> dict:
        return {"mtime_ns": self.mtime_ns, "mtime": self.mtime, "size": self.size}


def fingerprint(path: str) -> Optional[Fingerprint]:
    """Stat only. `None` when the path is gone, unreadable, or not a file."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    if not os.path.isfile(path):
        return None
    return Fingerprint(
        path=path,
        mtime_ns=st.st_mtime_ns,   # exact: NTFS is 100 ns, a float loses that
        mtime=st.st_mtime,
        size=st.st_size,
    )


def stat_is_trusted(
    *,
    recorded_mtime_ns: Optional[int],
    recorded_size: Optional[int],
    fp: Fingerprint,
    at_ns: Optional[int] = None,
    grace_ns: int = GRACE_NS,
) -> tuple[bool, str]:
    """May we skip re-reading the file?

    Returns `(trusted, reason)`. The reason is surfaced in run reports so a
    surprising re-index can be explained rather than guessed at.
    """
    if recorded_mtime_ns is None or not recorded_mtime_ns:
        return False, "no recorded timestamp"
    if recorded_size is None:
        return False, "no recorded size"
    if fp.size != recorded_size:
        return False, "size changed"
    if fp.mtime_ns != recorded_mtime_ns:
        return False, "mtime changed"
    now = at_ns if at_ns is not None else now_ns()
    if now - fp.mtime_ns < grace_ns:
        return False, "timestamp too recent to trust"
    return True, "stat fingerprint matches"


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_text(text: str) -> str:
    """Raw sha256 of a string. Not a content hash — see `normalize.content_hash`."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def hash_file(path: str) -> str:
    """Streaming sha256 of the file's bytes. Never loads it all into memory."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(_READ_CHUNK)
            if not block:
                break
            h.update(block)
    return h.hexdigest()
