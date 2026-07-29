"""Clipboard operations — what you can do to the data.

Every read and write is bound to owner_account_id. A row belonging to someone
else and a row past its TTL are both reported as "not found": the caller must
not be able to tell them apart.
"""

from __future__ import annotations

import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from . import clipboard_query as queries
from .clipboard_db import (
    ACCOUNT_QUOTA_BYTES,
    DELETE_MANY_LIMIT,
    ENTRY_BYTE_LIMIT,
    FILE_LIMIT,
    QUOTA_WARN_BYTES,
    TEXT_LIMIT,
    TOPIC_MAX_CHARS,
    TTL_SECONDS,
    VIEW_TEMPORARY,
    ClipboardError,
    connect,
    entry_json,
    initialize,
)


class ClipboardStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def initialize(self) -> None:
        initialize(self.path)

    def _connect(self) -> sqlite3.Connection:
        return connect(self.path)

    # ---------- reads (query logic lives in clipboard_query) ----------

    def usage(self, owner: str) -> dict[str, int]:
        conn = self._connect()
        try:
            return {
                "used_bytes": queries.usage_bytes(conn, owner),
                "quota_bytes": ACCOUNT_QUOTA_BYTES,
                "warn_bytes": QUOTA_WARN_BYTES,
            }
        finally:
            conn.close()

    def list_entries(self, owner: str, **kwargs: Any) -> tuple[list[dict[str, Any]], bool]:
        conn = self._connect()
        try:
            kwargs.setdefault("view", VIEW_TEMPORARY)
            return queries.list_entries(conn, owner, **kwargs)
        finally:
            conn.close()

    def get_entry(self, owner: str, entry_id: str, *, now: int | None = None) -> dict[str, Any]:
        now = int(time.time()) if now is None else now
        conn = self._connect()
        try:
            return entry_json(conn, self._require_entry(conn, owner, entry_id, now))
        finally:
            conn.close()

    def get_file(
        self, owner: str, entry_id: str, file_id: str, *, now: int | None = None
    ) -> dict[str, Any]:
        now = int(time.time()) if now is None else now
        conn = self._connect()
        try:
            self._require_entry(conn, owner, entry_id, now)
            row = conn.execute(
                "SELECT * FROM clipboard_files WHERE file_id = ? AND entry_id = ?",
                (file_id, entry_id),
            ).fetchone()
            if row is None:
                raise ClipboardError("not_found", status=404)
            return dict(row)
        finally:
            conn.close()

    def search_rows(self, owner: str) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            return queries.search_rows(conn, owner)
        finally:
            conn.close()

    # ---------- writes ----------

    def create_entry(
        self,
        owner: str,
        *,
        text: str,
        staged: Sequence[dict[str, Any]],
        promote: Callable[[str], None],
        now: int | None = None,
    ) -> dict[str, Any]:
        """Insert one entry, calling promote() to move files INSIDE the txn.

        If promote() raises, the rows roll back with it, so we never leave
        metadata pointing at files that are not on disk.
        """
        now = int(time.time()) if now is None else now
        if len(text) > TEXT_LIMIT:
            raise ClipboardError("text_too_long", max_chars=TEXT_LIMIT)
        if len(staged) > FILE_LIMIT:
            raise ClipboardError("too_many_files", max_files=FILE_LIMIT)
        if not text and not staged:
            raise ClipboardError("empty_entry")

        total_bytes = len(text.encode("utf-8")) + sum(int(i["size_bytes"]) for i in staged)
        if total_bytes >= ENTRY_BYTE_LIMIT:
            raise ClipboardError("entry_too_large", status=413, max_bytes=ENTRY_BYTE_LIMIT)

        entry_id = secrets.token_urlsafe(16)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            used = queries.usage_bytes(conn, owner)
            if used + total_bytes > ACCOUNT_QUOTA_BYTES:
                conn.execute("ROLLBACK")
                raise ClipboardError(
                    "quota_exceeded", status=413,
                    used_bytes=used, quota_bytes=ACCOUNT_QUOTA_BYTES,
                )
            conn.execute(
                "INSERT INTO clipboard_entries (entry_id, owner_account_id, text, created_at,"
                " expires_at, favorited_at, topic, total_bytes, file_count)"
                " VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, ?)",
                (entry_id, owner, text, now, now + TTL_SECONDS, total_bytes, len(staged)),
            )
            for item in staged:
                conn.execute(
                    "INSERT INTO clipboard_files (file_id, entry_id, original_name, safe_name,"
                    " content_type, size_bytes, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (item["file_id"], entry_id, item["original_name"], item["safe_name"],
                     item["content_type"], int(item["size_bytes"]), now),
                )
            promote(entry_id)
            conn.execute("COMMIT")
        except ClipboardError:
            raise
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()
        return self.get_entry(owner, entry_id, now=now)

    def set_favorite(
        self, owner: str, entry_id: str, favorite: bool, *, now: int | None = None
    ) -> None:
        now = int(time.time()) if now is None else now
        conn = self._connect()
        try:
            self._require_entry(conn, owner, entry_id, now)
            if favorite:
                conn.execute(
                    "UPDATE clipboard_entries SET favorited_at = ?, expires_at = NULL"
                    " WHERE entry_id = ? AND owner_account_id = ?",
                    (now, entry_id, owner),
                )
            else:
                # Restart the clock from NOW, not from created_at: an entry that
                # sat favourited for a week would otherwise vanish the instant
                # it is un-favourited.
                conn.execute(
                    "UPDATE clipboard_entries SET favorited_at = NULL, expires_at = ?"
                    " WHERE entry_id = ? AND owner_account_id = ?",
                    (now + TTL_SECONDS, entry_id, owner),
                )
        finally:
            conn.close()

    def set_topic(
        self, owner: str, entry_id: str, topic: str | None, *, now: int | None = None
    ) -> None:
        if topic is not None and len(topic) > TOPIC_MAX_CHARS:
            raise ClipboardError("topic_too_long", max_chars=TOPIC_MAX_CHARS)
        now = int(time.time()) if now is None else now
        conn = self._connect()
        try:
            self._require_entry(conn, owner, entry_id, now)
            conn.execute(
                "UPDATE clipboard_entries SET topic = ? WHERE entry_id = ? AND owner_account_id = ?",
                (topic or None, entry_id, owner),
            )
        finally:
            conn.close()

    def delete_entries(
        self, owner: str, entry_ids: Iterable[str], *, now: int | None = None
    ) -> list[str]:
        """Delete entries owned by `owner`; return the ids actually removed."""
        ids = [i for i in dict.fromkeys(entry_ids) if i]
        if not ids:
            return []
        if len(ids) > DELETE_MANY_LIMIT:
            raise ClipboardError("too_many_ids", max_ids=DELETE_MANY_LIMIT)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            marks = ",".join("?" * len(ids))
            rows = conn.execute(
                f"SELECT entry_id FROM clipboard_entries"
                f" WHERE owner_account_id = ? AND entry_id IN ({marks})",
                (owner, *ids),
            ).fetchall()
            removed = [str(row["entry_id"]) for row in rows]
            if removed:
                conn.execute(
                    f"DELETE FROM clipboard_entries WHERE owner_account_id = ?"
                    f" AND entry_id IN ({','.join('?' * len(removed))})",
                    (owner, *removed),
                )
            conn.execute("COMMIT")
            return removed
        finally:
            conn.close()

    def delete_file(
        self, owner: str, entry_id: str, file_id: str, *, now: int | None = None
    ) -> dict[str, Any]:
        """Remove one file. Returns {'file': row, 'entry_removed': bool}."""
        now = int(time.time()) if now is None else now
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            entry = self._require_entry(conn, owner, entry_id, now)
            row = conn.execute(
                "SELECT * FROM clipboard_files WHERE file_id = ? AND entry_id = ?",
                (file_id, entry_id),
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                raise ClipboardError("not_found", status=404)
            conn.execute("DELETE FROM clipboard_files WHERE file_id = ?", (file_id,))
            remaining = int(entry["file_count"]) - 1
            entry_removed = remaining == 0 and not str(entry["text"])
            if entry_removed:
                conn.execute("DELETE FROM clipboard_entries WHERE entry_id = ?", (entry_id,))
            else:
                conn.execute(
                    "UPDATE clipboard_entries SET file_count = ?, total_bytes = ? WHERE entry_id = ?",
                    (remaining, int(entry["total_bytes"]) - int(row["size_bytes"]), entry_id),
                )
            conn.execute("COMMIT")
            return {"file": dict(row), "entry_removed": entry_removed}
        finally:
            conn.close()

    def purge_expired(self, *, now: int | None = None) -> list[str]:
        """Delete entries past their TTL. Favourites (expires_at IS NULL) survive."""
        now = int(time.time()) if now is None else now
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT entry_id FROM clipboard_entries"
                " WHERE expires_at IS NOT NULL AND expires_at <= ?",
                (now,),
            ).fetchall()
            removed = [str(row["entry_id"]) for row in rows]
            if removed:
                conn.execute(
                    f"DELETE FROM clipboard_entries WHERE entry_id IN"
                    f" ({','.join('?' * len(removed))})",
                    removed,
                )
            conn.execute("COMMIT")
            if removed:
                conn.execute("PRAGMA incremental_vacuum")
            return removed
        finally:
            conn.close()

    def _require_entry(
        self, conn: sqlite3.Connection, owner: str, entry_id: str, now: int
    ) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM clipboard_entries WHERE entry_id = ? AND owner_account_id = ?",
            (entry_id, owner),
        ).fetchone()
        if row is None or (row["expires_at"] is not None and int(row["expires_at"]) <= now):
            raise ClipboardError("not_found", status=404)
        return row
