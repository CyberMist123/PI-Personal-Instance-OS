"""Read-side queries: usage, listing and the flat rows search scans.

Every function here takes an already-open connection and an owner id, and every
WHERE clause carries owner_account_id. None of them can be called in a way that
crosses accounts.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from .clipboard_db import (
    LIST_LIMIT,
    VIEW_FAVORITE,
    VIEWS,
    ClipboardError,
    entry_json,
    matches_kind,
)


def usage_bytes(conn: sqlite3.Connection, owner: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(total_bytes), 0) AS used FROM clipboard_entries"
        " WHERE owner_account_id = ?",
        (owner,),
    ).fetchone()
    return int(row["used"])


def list_entries(
    conn: sqlite3.Connection,
    owner: str,
    *,
    view: str,
    topic: str | None = None,
    kind: str | None = None,
    allowed_ids: set[str] | None = None,
    now: int | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    if view not in VIEWS:
        raise ClipboardError("invalid_view")
    now = int(time.time()) if now is None else now
    where = ["owner_account_id = ?"]
    args: list[Any] = [owner]
    if view == VIEW_FAVORITE:
        where.append("favorited_at IS NOT NULL")
    else:
        where.append("favorited_at IS NULL")
        where.append("expires_at > ?")
        args.append(now)
    if topic:
        where.append("topic = ?")
        args.append(topic)
    rows = conn.execute(
        f"SELECT * FROM clipboard_entries WHERE {' AND '.join(where)}"
        f" ORDER BY created_at DESC, entry_id DESC LIMIT {LIST_LIMIT + 1}",
        args,
    ).fetchall()
    entries = [entry_json(conn, row) for row in rows]
    if allowed_ids is not None:
        entries = [e for e in entries if e["entry_id"] in allowed_ids]
    if kind:
        entries = [e for e in entries if matches_kind(e, kind)]
    return entries[:LIST_LIMIT], len(entries) > LIST_LIMIT


def search_rows(conn: sqlite3.Connection, owner: str) -> list[dict[str, Any]]:
    """Flat (entry_id, text, names) rows for clipboard_search to scan."""
    rows = conn.execute(
        "SELECT e.entry_id AS entry_id, e.text AS text,"
        " COALESCE(GROUP_CONCAT(f.original_name, char(10)), '') AS names"
        " FROM clipboard_entries e"
        " LEFT JOIN clipboard_files f ON f.entry_id = e.entry_id"
        " WHERE e.owner_account_id = ? GROUP BY e.entry_id",
        (owner,),
    ).fetchall()
    return [dict(row) for row in rows]
