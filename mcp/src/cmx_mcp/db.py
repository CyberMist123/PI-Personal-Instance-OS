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
    remote_profile: str = "reader"
    remote_polls: bool = True
    remote_boosts: bool = False
    remote_notifications: bool = False


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
            db.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
            version_row = db.execute("SELECT MAX(version) FROM schema_version").fetchone()
            if version_row and version_row[0] is not None and int(version_row[0]) > 3:
                raise RuntimeError(f"Unsupported future database schema version: {version_row[0]}")
            self._migrate_legacy_cache(db)
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
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
                    bot_id TEXT NOT NULL,
                    status_id TEXT NOT NULL,
                    author_id TEXT NOT NULL,
                    author_acct TEXT NOT NULL,
                    text TEXT NOT NULL,
                    spoiler_text TEXT NOT NULL DEFAULT '',
                    created_at TEXT,
                    edited_at TEXT,
                    visibility TEXT,
                    reply_to_id TEXT,
                    payload_json TEXT NOT NULL,
                    indexed_at INTEGER NOT NULL,
                    PRIMARY KEY (bot_id, status_id)
                );

                CREATE VIRTUAL TABLE IF NOT EXISTS status_fts USING fts5(
                    bot_id UNINDEXED,
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
                    ON status_cache(bot_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS browse_state (
                    bot_id TEXT NOT NULL, feed TEXT NOT NULL, timeline_watermark TEXT,
                    updated_at INTEGER NOT NULL, PRIMARY KEY(bot_id, feed)
                );
                CREATE TABLE IF NOT EXISTS browse_seen (
                    bot_id TEXT NOT NULL, source_status_id TEXT NOT NULL, seen_at INTEGER NOT NULL,
                    PRIMARY KEY(bot_id, source_status_id)
                );
                CREATE TABLE IF NOT EXISTS browse_visits (
                    visit_id TEXT PRIMARY KEY, bot_id TEXT NOT NULL, allowed_ids_json TEXT NOT NULL,
                    opened_ids_json TEXT NOT NULL DEFAULT '[]', max_open INTEGER NOT NULL,
                    char_budget_limit INTEGER NOT NULL, char_budget_used INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL
                );
                """
            )
            for name, definition in (
                ("remote_profile", "TEXT NOT NULL DEFAULT 'reader'"),
                ("remote_polls", "INTEGER NOT NULL DEFAULT 1"),
                ("remote_boosts", "INTEGER NOT NULL DEFAULT 0"),
                ("remote_notifications", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if name not in {r[1] for r in db.execute("PRAGMA table_info(bots)")}:
                    db.execute(f"ALTER TABLE bots ADD COLUMN {name} {definition}")
            self._migrate_dedup(db)
            db.execute("DELETE FROM schema_version")
            db.execute("INSERT INTO schema_version(version) VALUES(3)")

    def get_browse_watermark(self, bot_id: str, feed: str = "timeline") -> str | None:
        with self.connect() as db:
            row = db.execute("SELECT timeline_watermark FROM browse_state WHERE bot_id=? AND feed=?", (bot_id, feed)).fetchone()
        return str(row[0]) if row and row[0] is not None else None

    def commit_browse(self, *, bot_id: str, feed: str, expected_watermark: str | None,
                      watermark: str | None, seen_ids: list[str], visit_id: str,
                      allowed_ids: list[str], max_open: int, char_budget_limit: int,
                      char_budget_used: int, expires_at: int) -> bool:
        now = int(time.time())
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT timeline_watermark FROM browse_state WHERE bot_id=? AND feed=?", (bot_id, feed)).fetchone()
            actual = str(row[0]) if row and row[0] is not None else None
            if actual != expected_watermark:
                return False
            db.execute("INSERT INTO browse_state(bot_id,feed,timeline_watermark,updated_at) VALUES(?,?,?,?) ON CONFLICT(bot_id,feed) DO UPDATE SET timeline_watermark=excluded.timeline_watermark,updated_at=excluded.updated_at", (bot_id, feed, watermark, now))
            db.executemany("INSERT OR IGNORE INTO browse_seen(bot_id,source_status_id,seen_at) VALUES(?,?,?)", [(bot_id, value, now) for value in seen_ids])
            db.execute("DELETE FROM browse_visits WHERE expires_at<=?", (now,))
            db.execute("INSERT INTO browse_visits(visit_id,bot_id,allowed_ids_json,max_open,char_budget_limit,char_budget_used,expires_at) VALUES(?,?,?,?,?,?,?)", (visit_id, bot_id, json.dumps(allowed_ids), max_open, char_budget_limit, char_budget_used, expires_at))
            return True

    def seen_status_ids(self, bot_id: str, ids: list[str]) -> set[str]:
        if not ids: return set()
        marks = ",".join("?" for _ in ids)
        with self.connect() as db:
            rows = db.execute(f"SELECT source_status_id FROM browse_seen WHERE bot_id=? AND source_status_id IN ({marks})", (bot_id, *ids)).fetchall()
        return {str(row[0]) for row in rows}

    def get_visit(self, bot_id: str, visit_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM browse_visits WHERE bot_id=? AND visit_id=? AND expires_at>?", (bot_id, visit_id, int(time.time()))).fetchone()
        return dict(row) if row else None

    def use_visit(self, *, bot_id: str, visit_id: str, opened_ids: list[str], added_chars: int) -> bool:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM browse_visits WHERE bot_id=? AND visit_id=? AND expires_at>?", (bot_id, visit_id, int(time.time()))).fetchone()
            if not row: raise ValueError("visit_id is invalid or expired")
            old = set(json.loads(row["opened_ids_json"]))
            if old.intersection(opened_ids): raise ValueError("a status cannot be reopened in the same visit")
            merged = [*old, *opened_ids]
            if len(merged) > int(row["max_open"]):
                raise ValueError(f"visit may open at most {row['max_open']} distinct statuses")
            if row["char_budget_used"] + added_chars > row["char_budget_limit"]:
                return False
            db.execute("UPDATE browse_visits SET opened_ids_json=?,char_budget_used=char_budget_used+? WHERE visit_id=?", (json.dumps(merged), added_chars, visit_id))
            return True

    def _migrate_dedup(self, db: sqlite3.Connection) -> None:
        columns = {r[1] for r in db.execute("PRAGMA table_info(publish_dedup)")}
        if not columns or "state" in columns:
            return
        db.execute("ALTER TABLE publish_dedup RENAME TO publish_dedup_legacy")
        db.execute("""CREATE TABLE publish_dedup (
            bot_id TEXT NOT NULL, operation TEXT NOT NULL, request_id TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('pending','succeeded','failed')),
            status_id TEXT, error_code TEXT, lease_expires_at INTEGER,
            created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, response_json TEXT,
            PRIMARY KEY(bot_id,operation,request_id))""")
        db.execute("""INSERT INTO publish_dedup
            (bot_id,operation,request_id,state,created_at,updated_at,response_json)
            SELECT bot_id,'publish',request_key,'succeeded',created_at,created_at,response_json
            FROM publish_dedup_legacy""")
        db.execute("DROP TABLE publish_dedup_legacy")

    def _migrate_legacy_cache(self, db: sqlite3.Connection) -> None:
        """Migrate the pre-Phase-0 global cache without dropping its rows."""
        db.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        row = db.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
        if row and int(row[0]) >= 2:
            return
        columns = {r[1] for r in db.execute("PRAGMA table_info(status_cache)")}
        if not columns:
            return
        if columns and "bot_id" not in columns:
            db.execute("ALTER TABLE status_cache RENAME TO status_cache_legacy")
            db.execute("DROP TABLE IF EXISTS status_fts")
            bots = [r[0] for r in db.execute("SELECT bot_id FROM bots ORDER BY bot_id")]
            legacy_bot = bots[0] if len(bots) == 1 else "__legacy__"
            db.execute("""CREATE TABLE status_cache (
                bot_id TEXT NOT NULL, status_id TEXT NOT NULL, author_id TEXT NOT NULL,
                author_acct TEXT NOT NULL, text TEXT NOT NULL, spoiler_text TEXT NOT NULL DEFAULT '',
                created_at TEXT, edited_at TEXT, visibility TEXT, reply_to_id TEXT,
                payload_json TEXT NOT NULL, indexed_at INTEGER NOT NULL,
                PRIMARY KEY (bot_id, status_id))""")
            db.execute("""INSERT INTO status_cache
                (bot_id,status_id,author_id,author_acct,text,spoiler_text,created_at,edited_at,
                 visibility,reply_to_id,payload_json,indexed_at)
                SELECT ?,status_id,author_id,author_acct,text,spoiler_text,created_at,edited_at,
                 visibility,reply_to_id,payload_json,indexed_at FROM status_cache_legacy""", (legacy_bot,))
            db.execute("DROP TABLE status_cache_legacy")
        db.execute("DROP TABLE IF EXISTS status_fts")
        db.execute("""CREATE VIRTUAL TABLE status_fts USING fts5(
            bot_id UNINDEXED, status_id UNINDEXED, author_acct, text, spoiler_text,
            tokenize='unicode61 remove_diacritics 2')""")
        db.execute("""INSERT INTO status_fts(bot_id,status_id,author_acct,text,spoiler_text)
            SELECT bot_id,status_id,author_acct,text,spoiler_text FROM status_cache
            WHERE visibility IS NULL OR visibility != 'direct'""")
        db.execute("DELETE FROM schema_version")
        db.execute("INSERT INTO schema_version(version) VALUES(2)")

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
        remote_profile: str = "reader",
        remote_polls: bool = True,
        remote_boosts: bool = False,
        remote_notifications: bool = False,
    ) -> None:
        if remote_profile not in {"disabled", "reader", "social", "social_plus"}:
            raise ValueError("invalid remote_profile")
        if not all(isinstance(value, bool) for value in (remote_polls, remote_boosts, remote_notifications)):
            raise ValueError("remote capabilities must be boolean")
        now = int(time.time())
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO bots(
                    bot_id, display_name, profile, media_root, token_ref,
                    default_audience, allow_public, enabled, created_at, updated_at,
                    remote_profile, remote_polls, remote_boosts, remote_notifications
                ) VALUES(?,?,?,?,?,?,?,1,?,?,?,?,?,?)
                ON CONFLICT(bot_id) DO UPDATE SET
                    display_name=excluded.display_name,
                    profile=excluded.profile,
                    media_root=excluded.media_root,
                    token_ref=excluded.token_ref,
                    default_audience=excluded.default_audience,
                    allow_public=excluded.allow_public,
                    enabled=1,
                    updated_at=excluded.updated_at,
                    remote_profile=excluded.remote_profile,
                    remote_polls=excluded.remote_polls,
                    remote_boosts=excluded.remote_boosts,
                    remote_notifications=excluded.remote_notifications
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
                    remote_profile,
                    int(remote_polls),
                    int(remote_boosts),
                    int(remote_notifications),
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
            remote_profile=row["remote_profile"] if "remote_profile" in row.keys() else "reader",
            remote_polls=bool(row["remote_polls"]) if "remote_polls" in row.keys() else True,
            remote_boosts=bool(row["remote_boosts"]) if "remote_boosts" in row.keys() else False,
            remote_notifications=bool(row["remote_notifications"]) if "remote_notifications" in row.keys() else False,
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
                remote_profile=row["remote_profile"] if "remote_profile" in row.keys() else "reader",
                remote_polls=bool(row["remote_polls"]) if "remote_polls" in row.keys() else True,
                remote_boosts=bool(row["remote_boosts"]) if "remote_boosts" in row.keys() else False,
                remote_notifications=bool(row["remote_notifications"]) if "remote_notifications" in row.keys() else False,
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

    def cache_statuses(self, bot_id: str, statuses: Iterable[dict[str, Any]]) -> None:
        with self.connect() as db:
            for status in statuses:
                status_id = str(status["id"])
                account = status.get("author") or status.get("account") or {}
                text = str(status.get("text") or "")
                spoiler = str(status.get("spoiler_text") or "")
                db.execute(
                    """
                    INSERT INTO status_cache(
                        bot_id, status_id, author_id, author_acct, text, spoiler_text,
                        created_at, edited_at, visibility, reply_to_id,
                        payload_json, indexed_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(bot_id,status_id) DO UPDATE SET
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
                        bot_id, status_id,
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
                db.execute("DELETE FROM status_fts WHERE bot_id=? AND status_id=?", (bot_id, status_id))
                if status.get("visibility") != "direct":
                    db.execute(
                        "INSERT INTO status_fts(bot_id,status_id,author_acct,text,spoiler_text) VALUES(?,?,?,?,?)",
                        (bot_id, status_id, str(account.get("acct") or ""), text, spoiler),
                    )

    def search_statuses(self, bot_id: str, query: str, limit: int) -> list[dict[str, Any]]:
        with self.connect() as db:
            try:
                rows = db.execute(
                    """
                    SELECT c.payload_json
                    FROM status_fts f
                    JOIN status_cache c ON c.status_id=f.status_id AND c.bot_id=f.bot_id
                    WHERE status_fts MATCH ? AND f.bot_id=? AND c.bot_id=?
                    ORDER BY bm25(status_fts), c.created_at DESC
                    LIMIT ?
                    """,
                    (query, bot_id, bot_id, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                pattern = f"%{query}%"
                rows = db.execute(
                    """
                    SELECT payload_json FROM status_cache
                    WHERE bot_id=? AND (text LIKE ? OR spoiler_text LIKE ? OR author_acct LIKE ?)
                    ORDER BY created_at DESC LIMIT ?
                    """,
                    (bot_id, pattern, pattern, pattern, limit),
                ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def invalidate_status(self, bot_id: str, status_id: str) -> None:
        """Remove a status and its search row after current-token revalidation fails."""
        with self.connect() as db:
            db.execute("DELETE FROM status_fts WHERE bot_id=? AND status_id=?", (bot_id, status_id))
            db.execute("DELETE FROM status_cache WHERE bot_id=? AND status_id=?", (bot_id, status_id))

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
                "SELECT response_json FROM publish_dedup WHERE operation='publish' AND request_id=? AND state='succeeded' AND created_at>=?",
                (request_key, cutoff),
            ).fetchone()
        return json.loads(row["response_json"]) if row else None

    def put_dedup(self, request_key: str, bot_id: str, response: dict[str, Any]) -> None:
        with self.connect() as db:
            db.execute(
                """
                INSERT OR REPLACE INTO publish_dedup(
                    bot_id,operation,request_id,state,created_at,updated_at,response_json
                ) VALUES(?,?,?,'succeeded',?,?,?)
                """,
                (
                    bot_id,
                    "publish",
                    request_key,
                    int(time.time()),
                    int(time.time()),
                    json.dumps(response, ensure_ascii=False, separators=(",", ":")),
                ),
            )
            db.execute(
                "DELETE FROM publish_dedup WHERE updated_at<?",
                (int(time.time()) - 86400,),
            )

    def claim_dedup(
        self, *, bot_id: str, operation: str, request_id: str, lease_seconds: int = 300
    ) -> dict[str, Any]:
        """Atomically claim one external operation, or return its durable state."""
        now = int(time.time())
        lease = now + max(1, lease_seconds)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM publish_dedup WHERE bot_id=? AND operation=? AND request_id=?",
                (bot_id, operation, request_id),
            ).fetchone()
            if row is None:
                db.execute("""INSERT INTO publish_dedup
                    (bot_id,operation,request_id,state,lease_expires_at,created_at,updated_at)
                    VALUES(?,?,?,'pending',?,?,?)""", (bot_id, operation, request_id, lease, now, now))
                return {"state": "pending", "claimed": True, "lease_expires_at": lease}
            state = str(row["state"])
            if state == "succeeded":
                return {"state": state, "claimed": False, "response": json.loads(row["response_json"])}
            if state == "pending" and row["lease_expires_at"] and int(row["lease_expires_at"]) > now:
                return {"state": state, "claimed": False, "lease_expires_at": row["lease_expires_at"]}
            db.execute("""UPDATE publish_dedup SET state='pending',lease_expires_at=?,
                error_code=NULL,updated_at=? WHERE bot_id=? AND operation=? AND request_id=?""",
                       (lease, now, bot_id, operation, request_id))
            return {"state": "pending", "claimed": True, "lease_expires_at": lease}

    def finish_dedup(
        self, *, bot_id: str, operation: str, request_id: str,
        response: dict[str, Any] | None = None, error_code: str | None = None,
    ) -> None:
        state = "succeeded" if response is not None and error_code is None else "failed"
        with self.connect() as db:
            db.execute("""UPDATE publish_dedup SET state=?,response_json=?,error_code=?,
                lease_expires_at=NULL,updated_at=? WHERE bot_id=? AND operation=? AND request_id=?
                AND state='pending'""", (state, json.dumps(response, separators=(",", ":")) if response is not None else None,
                error_code, int(time.time()), bot_id, operation, request_id))

    def cleanup_dedup(self, *, ttl_seconds: int = 86400) -> int:
        with self.connect() as db:
            cur = db.execute("DELETE FROM publish_dedup WHERE updated_at<?", (int(time.time()) - ttl_seconds,))
            return cur.rowcount
