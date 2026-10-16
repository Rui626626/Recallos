"""Schema definition for the derived index.

`index/recall.db` is a *derived artefact*: it is rebuilt from `data/docs/*.json`
plus `cache/`, so there is no data migration here — an unknown or older version
is dropped and recreated. That is also why bumping `SCHEMA_VERSION` is cheap and
should be done whenever the DDL below changes.

Version history
---------------
1   P0: doc / chunk / two FTS tables / meta.
2   P1: split byte-identity from content-identity (`file_hash` vs
    `content_hash`), exact timestamps (`mtime_ns`), normalised source keys,
    per-document version stamps, durable error and missing markers, source
    history for rename/move, and content-addressed chunk identity.
"""

from __future__ import annotations

import sqlite3
from typing import Final

__all__ = ["SCHEMA_VERSION", "DDL", "TABLES", "create_schema", "drop_all",
           "read_version", "write_version"]

SCHEMA_VERSION: Final[int] = 2

# Contentful FTS is deliberate: snippet() and highlight() return NULL on a
# contentless table (verified locally), and cheap previews need both.
DDL: Final[tuple[str, ...]] = (
    """
    CREATE TABLE IF NOT EXISTS doc (
      doc_id            TEXT PRIMARY KEY,
      source_kind       TEXT NOT NULL,
      source_key        TEXT NOT NULL,
      source_ref        TEXT NOT NULL,
      title             TEXT,

      mtime             REAL,
      mtime_ns          INTEGER,
      size              INTEGER,

      file_hash         TEXT,
      content_hash      TEXT,
      doc_hash          TEXT,

      status            TEXT NOT NULL DEFAULT 'indexed',
      duplicate_of      TEXT,
      missing_at        TEXT,
      error             TEXT,
      error_at          TEXT,

      norm_version      TEXT NOT NULL DEFAULT '',
      chunker_version   TEXT NOT NULL DEFAULT '',
      extractor         TEXT,
      extractor_version TEXT,

      char_count        INTEGER DEFAULT 0,
      chunk_count       INTEGER DEFAULT 0,
      imported_at       TEXT NOT NULL,
      updated_at        TEXT NOT NULL
    )
    """,
    # Identity is the *normalised* reference, so `C:\\A.md` and `c:\\a.md` are
    # one row rather than two rows fighting over one primary key.
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_doc_src ON doc(source_kind, source_key)",
    "CREATE INDEX IF NOT EXISTS ix_doc_hash ON doc(doc_hash)",
    "CREATE INDEX IF NOT EXISTS ix_doc_filehash ON doc(file_hash)",
    "CREATE INDEX IF NOT EXISTS ix_doc_status ON doc(status)",
    "CREATE INDEX IF NOT EXISTS ix_doc_dup ON doc(duplicate_of)",
    "CREATE INDEX IF NOT EXISTS ix_doc_missing ON doc(missing_at)",

    """
    CREATE TABLE IF NOT EXISTS chunk (
      chunk_id     TEXT PRIMARY KEY,
      doc_id       TEXT NOT NULL REFERENCES doc(doc_id) ON DELETE CASCADE,
      ordinal      INTEGER NOT NULL,
      heading_path TEXT,
      text         TEXT NOT NULL,
      page         INTEGER,
      ts_start     REAL,
      ts_end       REAL,
      char_start   INTEGER,
      char_end     INTEGER,
      chunk_hash   TEXT NOT NULL,
      occurrence   INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_chunk_doc ON chunk(doc_id, ordinal)",
    "CREATE INDEX IF NOT EXISTS ix_chunk_hash ON chunk(chunk_hash)",

    """
    CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(
      text,
      chunk_id UNINDEXED,
      doc_id   UNINDEXED,
      tokenize = 'trigram'
    )
    """,
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts_cjk USING fts5(
      text,
      chunk_id UNINDEXED,
      doc_id   UNINDEXED,
      tokenize = 'unicode61'
    )
    """,

    # Every path a document has ever been seen at. This is the only thing that
    # makes "the file was renamed, not deleted and re-added" decidable.
    """
    CREATE TABLE IF NOT EXISTS source_history (
      doc_id        TEXT NOT NULL,
      source_kind   TEXT NOT NULL,
      source_ref    TEXT NOT NULL,
      source_key    TEXT NOT NULL,
      reason        TEXT NOT NULL,
      first_seen_at TEXT NOT NULL,
      last_seen_at  TEXT NOT NULL,
      note          TEXT,
      PRIMARY KEY (doc_id, source_key)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_history_key ON source_history(source_kind, source_key)",

    """
    CREATE TABLE IF NOT EXISTS doc_revision (
      doc_id       TEXT NOT NULL,
      content_hash TEXT NOT NULL,
      file_hash    TEXT,
      char_count   INTEGER,
      chunk_count  INTEGER,
      recorded_at  TEXT NOT NULL,
      PRIMARY KEY (doc_id, content_hash)
    )
    """,

    """
    CREATE TABLE IF NOT EXISTS meta (
      key   TEXT PRIMARY KEY,
      value TEXT NOT NULL
    )
    """,
)

TABLES: Final[tuple[str, ...]] = (
    "chunk_fts_cjk", "chunk_fts", "chunk", "doc_revision", "source_history",
    "doc", "meta",
)

# Keys the pipeline owner writes into `meta`.
META_SCHEMA_VERSION = "schema_version"
META_LAST_RUN_STATE = "last_run_state"     # running | complete | failed
META_LAST_RUN_AT = "last_run_at"
META_LAST_RUN_MODE = "last_run_mode"


def create_schema(conn: sqlite3.Connection) -> None:
    for stmt in DDL:
        conn.execute(stmt)
    write_version(conn)


def drop_all(conn: sqlite3.Connection) -> None:
    """Wipe every object we own. Used for a rebuild and for a version change."""
    for table in TABLES:
        try:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        except sqlite3.OperationalError:
            pass  # already gone


def read_version(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key=?", (META_SCHEMA_VERSION,)
        ).fetchone()
    except sqlite3.OperationalError:
        return 0
    if not row:
        return 0
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return 0


def write_version(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (META_SCHEMA_VERSION, str(SCHEMA_VERSION)),
    )
