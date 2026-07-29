"""Clipboard schema, limits and row serialization — the shape of the data.

Deliberately NOT part of cmx.sqlite3 and deliberately not reusing filebox_files:
Clipboard is Owner-facing browser state with a 24h burn cycle, while the MCP
database holds resident tokens and caches. Keeping them apart means Clipboard
can be wiped or rolled back on its own.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

TTL_SECONDS = 86_400
TEXT_LIMIT = 10_000                      # Unicode code points
FILE_LIMIT = 20
ENTRY_BYTE_LIMIT = 1024**3               # strict <
ACCOUNT_QUOTA_BYTES = 2 * 1024**3
QUOTA_WARN_BYTES = 1536 * 1024**2        # 1.5 GiB: below this the UI hides the meter
LIST_LIMIT = 100
DELETE_MANY_LIMIT = 100
TOPIC_MAX_CHARS = 24

VIEW_TEMPORARY = "temporary"
VIEW_FAVORITE = "favorite"
VIEWS = frozenset({VIEW_TEMPORARY, VIEW_FAVORITE})

SCHEMA = """
CREATE TABLE IF NOT EXISTS clipboard_entries (
  entry_id         TEXT PRIMARY KEY,
  owner_account_id TEXT NOT NULL,
  text             TEXT NOT NULL,
  created_at       INTEGER NOT NULL,
  expires_at       INTEGER,
  favorited_at     INTEGER,
  topic            TEXT,
  total_bytes      INTEGER NOT NULL,
  file_count       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS clipboard_entries_owner
  ON clipboard_entries(owner_account_id, created_at DESC);
CREATE INDEX IF NOT EXISTS clipboard_entries_expiry
  ON clipboard_entries(expires_at);

CREATE TABLE IF NOT EXISTS clipboard_files (
  file_id       TEXT PRIMARY KEY,
  entry_id      TEXT NOT NULL
                REFERENCES clipboard_entries(entry_id) ON DELETE CASCADE,
  original_name TEXT NOT NULL,
  safe_name     TEXT NOT NULL,
  content_type  TEXT NOT NULL,
  size_bytes    INTEGER NOT NULL,
  created_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS clipboard_files_entry
  ON clipboard_files(entry_id);
"""


class ClipboardError(RuntimeError):
    """Carries the wire code and HTTP status so routes stay free of policy."""

    def __init__(self, code: str, status: int = 400, **extra: Any) -> None:
        super().__init__(code)
        self.code = code
        self.status = status
        self.extra = extra

    def payload(self) -> dict[str, Any]:
        return {"error": self.code, **self.extra}


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def initialize(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = not path.exists()
    conn = connect(path)
    try:
        if fresh:
            # Only settable before the first table exists; afterwards it would
            # need a full VACUUM. Clipboard burns rows every day, so without
            # this the file grows monotonically and never gives space back.
            conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def entry_json(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    files = conn.execute(
        "SELECT * FROM clipboard_files WHERE entry_id = ? ORDER BY created_at, file_id",
        (row["entry_id"],),
    ).fetchall()
    return {
        "entry_id": row["entry_id"],
        "text": row["text"],
        "created_at": int(row["created_at"]),
        "expires_at": None if row["expires_at"] is None else int(row["expires_at"]),
        "favorited": row["favorited_at"] is not None,
        "topic": row["topic"],
        "total_bytes": int(row["total_bytes"]),
        "file_count": int(row["file_count"]),
        "files": [
            {
                "file_id": f["file_id"],
                "name": f["original_name"],
                "content_type": f["content_type"],
                "size_bytes": int(f["size_bytes"]),
                "url": f"/clipboard-api/entries/{row['entry_id']}/files/{f['file_id']}",
            }
            for f in files
        ],
    }


def matches_kind(entry: dict[str, Any], kind: str) -> bool:
    if kind == "text":
        return bool(entry["text"])
    if kind == "image":
        return any(str(f["content_type"]).startswith("image/") for f in entry["files"])
    return True
