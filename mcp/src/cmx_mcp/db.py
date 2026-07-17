from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True, slots=True)
class Bot:
    bot_id: str
    display_name: str
    profile: str
    media_root: Path
    token_ref: str
    default_audience: str
    allow_public: bool
    enabled: bool


class Database:
    def __init__(self, path: Path):
        self.path = path

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS bots (
                    bot_id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    profile TEXT NOT NULL CHECK(profile IN ('reader','resident','personal')),
                    media_root TEXT NOT NULL,
                    token_ref TEXT NOT NULL,
                    default_audience TEXT NOT NULL
                        CHECK(default_audience IN ('residents','direct','public_explicit')),
                    allow_public INTEGER NOT NULL DEFAULT 0,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS status_cache (
                    status_id TEXT PRIMARY KEY,
                    author_id TEXT NOT NULL,
                    author_acct TEXT NOT NULL,
                    text TEXT NOT NULL,
                    spoiler_text TEXT NOT NULL DEFAULT '',
                    created_at TEXT,
                    edited_at TEXT,
                    visibility TEXT,
                    reply_to_id TEXT,
                    payload_json TEXT NOT NULL,
                    indexed_at INTEGER NOT NULL
                );

                CREATE VIRTUAL TABLE IF NOT EXISTS status_fts USING fts5(
                    status_id UNINDEXED,
                    author_acct,
                    text,
                    spoiler_text,
                    tokenize='unicode61 remove_diacritics 2'
                );

                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at INTEGER NOT NULL,
                    bot_id TEXT NOT NULL,
                    tool TEXT NOT NULL,
                    action TEXT NOT NULL,
                    target_id TEXT,
                    ok INTEGER NOT NULL,
                    detail TEXT
                );

                CREATE TABLE IF NOT EXISTS publish_dedup (
                    request_key TEXT PRIMARY KEY,
                    bot_id TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    response_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_audit_bot_created
                    ON audit_events(bot_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_status_created
                    ON status_cache(created_at DESC);
                """
            )

    def upsert_bot(
        self,
        *,
        bot_id: str,
        display_name: str,
        profile: str,
        media_root: Path,
        token_ref: str,
        default_audience: str,
        allow_public: bool,
    ) -> None:
        now = int(time.time())
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO bots(
                    bot_id, display_name, profile, media_root, token_ref,
                    default_audience, allow_public, enabled, created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,1,?,?)
                ON CONFLICT(bot_id) DO UPDATE SET
                    display_name=excluded.display_name,
                    profile=excluded.profile,
                    media_root=excluded.media_root,
                    token_ref=excluded.token_ref,
                    default_audience=excluded.default_audience,
                    allow_public=excluded.allow_public,
                    enabled=1,
                    updated_at=excluded.updated_at
                """,
                (
                    bot_id,
                    display_name,
                    profile,
                    str(media_root),
                    token_ref,
                    default_audience,
                    int(allow_public),
                    now,
                    now,
                ),
            )

    def get_bot(self, bot_id: str) -> Bot:
        with self.connect() as db:
            row = db.execute("SELECT * FROM bots WHERE bot_id=?", (bot_id,)).fetchone()
        if row is None:
            raise RuntimeError(f"Unknown bot: {bot_id}")
        return Bot(
            bot_id=row["bot_id"],
            display_name=row["display_name"],
            profile=row["profile"],
            media_root=Path(row["media_root"]),
            token_ref=row["token_ref"],
            default_audience=row["default_audience"],
            allow_public=bool(row["allow_public"]),
            enabled=bool(row["enabled"]),
        )

    def list_bots(self) -> list[Bot]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM bots ORDER BY bot_id").fetchall()
        return [
            Bot(
                bot_id=row["bot_id"],
                display_name=row["display_name"],
                profile=row["profile"],
                media_root=Path(row["media_root"]),
                token_ref=row["token_ref"],
                default_audience=row["default_audience"],
                allow_public=bool(row["allow_public"]),
                enabled=bool(row["enabled"]),
            )
            for row in rows
        ]

    def set_enabled(self, bot_id: str, enabled: bool) -> None:
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE bots SET enabled=?, updated_at=? WHERE bot_id=?",
                (int(enabled), int(time.time()), bot_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"Unknown bot: {bot_id}")

    def cache_statuses(self, statuses: Iterable[dict[str, Any]]) -> None:
        with self.connect() as db:
            for status in statuses:
                status_id = str(status["id"])
                account = status.get("author") or status.get("account") or {}
                text = str(status.get("text") or "")
                spoiler = str(status.get("spoiler_text") or "")
                db.execute(
                    """
                    INSERT INTO status_cache(
                        status_id, author_id, author_acct, text, spoiler_text,
                        created_at, edited_at, visibility, reply_to_id,
                        payload_json, indexed_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(status_id) DO UPDATE SET
                        author_id=excluded.author_id,
                        author_acct=excluded.author_acct,
                        text=excluded.text,
                        spoiler_text=excluded.spoiler_text,
                        created_at=excluded.created_at,
                        edited_at=excluded.edited_at,
                        visibility=excluded.visibility,
                        reply_to_id=excluded.reply_to_id,
                        payload_json=excluded.payload_json,
                        indexed_at=excluded.indexed_at
                    """,
                    (
                        status_id,
                        str(account.get("id") or ""),
                        str(account.get("acct") or ""),
                        text,
                        spoiler,
                        status.get("created_at"),
                        status.get("edited_at"),
                        status.get("visibility"),
                        status.get("reply_to_id") or status.get("in_reply_to_id"),
                        json.dumps(status, ensure_ascii=False, separators=(",", ":")),
                        int(time.time()),
                    ),
                )
                db.execute("DELETE FROM status_fts WHERE status_id=?", (status_id,))
                db.execute(
                    "INSERT INTO status_fts(status_id,author_acct,text,spoiler_text) VALUES(?,?,?,?)",
                    (status_id, str(account.get("acct") or ""), text, spoiler),
                )

    def search_statuses(self, query: str, limit: int) -> list[dict[str, Any]]:
        with self.connect() as db:
            try:
                rows = db.execute(
                    """
                    SELECT c.payload_json
                    FROM status_fts f
                    JOIN status_cache c ON c.status_id=f.status_id
                    WHERE status_fts MATCH ?
                    ORDER BY bm25(status_fts), c.created_at DESC
                    LIMIT ?
                    """,
                    (query, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                pattern = f"%{query}%"
                rows = db.execute(
                    """
                    SELECT payload_json FROM status_cache
                    WHERE text LIKE ? OR spoiler_text LIKE ? OR author_acct LIKE ?
                    ORDER BY created_at DESC LIMIT ?
                    """,
                    (pattern, pattern, pattern, limit),
                ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def audit(
        self,
        *,
        bot_id: str,
        tool: str,
        action: str,
        ok: bool,
        target_id: str | None = None,
        detail: str | None = None,
    ) -> None:
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO audit_events(created_at,bot_id,tool,action,target_id,ok,detail)
                VALUES(?,?,?,?,?,?,?)
                """,
                (int(time.time()), bot_id, tool, action, target_id, int(ok), detail),
            )

    def get_dedup(self, request_key: str, max_age_seconds: int = 21600) -> dict[str, Any] | None:
        cutoff = int(time.time()) - max_age_seconds
        with self.connect() as db:
            row = db.execute(
                "SELECT response_json FROM publish_dedup WHERE request_key=? AND created_at>=?",
                (request_key, cutoff),
            ).fetchone()
        return json.loads(row["response_json"]) if row else None

    def put_dedup(self, request_key: str, bot_id: str, response: dict[str, Any]) -> None:
        with self.connect() as db:
            db.execute(
                """
                INSERT OR REPLACE INTO publish_dedup(request_key,bot_id,created_at,response_json)
                VALUES(?,?,?,?)
                """,
                (
                    request_key,
                    bot_id,
                    int(time.time()),
                    json.dumps(response, ensure_ascii=False, separators=(",", ":")),
                ),
            )
            db.execute(
                "DELETE FROM publish_dedup WHERE created_at<?",
                (int(time.time()) - 86400,),
            )
