from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


def _escape_like(value: str) -> str:
    """Neutralise LIKE wildcards so a query of `50%` looks for those three characters."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


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
            if version_row and version_row[0] is not None and int(version_row[0]) > 7:
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

                CREATE TABLE IF NOT EXISTS filebox_files (
                    bot_id TEXT NOT NULL, file_id TEXT NOT NULL, file_name TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL, created_at INTEGER NOT NULL,
                    PRIMARY KEY (bot_id, file_id)
                );

                CREATE TABLE IF NOT EXISTS cmx_settings (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS worker_done (
                    bot_id TEXT NOT NULL,
                    status_id TEXT NOT NULL,
                    done_at INTEGER NOT NULL,
                    PRIMARY KEY (bot_id, status_id)
                );

                -- Keyed by image content, not bot_id: unlike every cache table above,
                -- this one is deliberately shared across residents. Three bots looking
                -- at the same photo must reuse one recognition result computed once,
                -- rather than paying the local model (and any cloud pass) three times.
                CREATE TABLE IF NOT EXISTS image_recognition (
                    image_sha256 TEXT PRIMARY KEY,
                    local_ocr_text TEXT NOT NULL DEFAULT '',
                    local_line_count INTEGER NOT NULL DEFAULT 0,
                    local_mean_confidence REAL,
                    cloud_corrected_text TEXT,
                    cloud_description TEXT,
                    search_keywords TEXT,
                    uncertain_text TEXT,
                    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','done')),
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_image_recognition_state
                    ON image_recognition(state);

                -- image_recognition is keyed by content and knows nothing about
                -- Mastodon; this is the join that lets a search over recognised
                -- text come back with statuses. Many attachments legitimately map
                -- to one hash — the same photo posted twice is recognised once.
                CREATE TABLE IF NOT EXISTS status_media (
                    status_id TEXT NOT NULL,
                    media_id TEXT NOT NULL,
                    image_sha256 TEXT NOT NULL,
                    linked_at INTEGER NOT NULL,
                    PRIMARY KEY (status_id, media_id)
                );
                CREATE INDEX IF NOT EXISTS idx_status_media_sha
                    ON status_media(image_sha256);

                -- A local guardrail for the owner's metered Gemini free tier.
                -- Count attempts, not successes, because rejected/invalid replies
                -- still consumed an upstream request. UTC makes the rollover
                -- deterministic across Windows timezone or daylight changes.
                CREATE TABLE IF NOT EXISTS gemini_daily_usage (
                    day_utc TEXT PRIMARY KEY,
                    attempts INTEGER NOT NULL CHECK(attempts >= 0),
                    updated_at INTEGER NOT NULL
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
            db.execute("INSERT INTO schema_version(version) VALUES(7)")

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

    def filebox_add(self, *, bot_id: str, file_id: str, file_name: str, size_bytes: int) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO filebox_files(bot_id,file_id,file_name,size_bytes,created_at) VALUES(?,?,?,?,?)",
                (bot_id, file_id, file_name, int(size_bytes), int(time.time())),
            )

    def filebox_usage(self, bot_id: str) -> int:
        with self.connect() as db:
            row = db.execute(
                "SELECT COALESCE(SUM(size_bytes),0) FROM filebox_files WHERE bot_id=?", (bot_id,)
            ).fetchone()
        return int(row[0])

    def filebox_get(self, bot_id: str, file_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM filebox_files WHERE bot_id=? AND file_id=?", (bot_id, file_id)
            ).fetchone()
        return dict(row) if row else None

    def filebox_list(self, bot_id: str | None = None) -> list[dict[str, Any]]:
        with self.connect() as db:
            if bot_id:
                rows = db.execute(
                    "SELECT * FROM filebox_files WHERE bot_id=? ORDER BY created_at DESC", (bot_id,)
                ).fetchall()
            else:
                rows = db.execute("SELECT * FROM filebox_files ORDER BY created_at DESC").fetchall()
        return [dict(row) for row in rows]

    def filebox_remove(self, bot_id: str, file_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM filebox_files WHERE bot_id=? AND file_id=?", (bot_id, file_id)
            ).fetchone()
            if row is None:
                return None
            db.execute("DELETE FROM filebox_files WHERE bot_id=? AND file_id=?", (bot_id, file_id))
        return dict(row)

    def worker_is_done(self, bot_id: str, status_id: str) -> bool:
        with self.connect() as db:
            row = db.execute(
                "SELECT 1 FROM worker_done WHERE bot_id=? AND status_id=?", (bot_id, status_id)
            ).fetchone()
        return row is not None

    def worker_mark_done(self, bot_id: str, status_id: str) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO worker_done(bot_id,status_id,done_at) VALUES(?,?,?)",
                (bot_id, status_id, int(time.time())),
            )

    def get_setting(self, key: str) -> str | None:
        with self.connect() as db:
            row = db.execute("SELECT value FROM cmx_settings WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO cmx_settings(key,value) VALUES(?,?)", (key, value)
            )

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
        """Substring-match this bot's non-direct cache, newest first.

        Deliberately a scan rather than `status_fts MATCH`. FTS5's unicode61
        tokenizer does not segment CJK, so a run of Chinese becomes a single
        token: indexing 「学习烧菜的第一天」 makes it reachable only by typing that
        exact string back, and a query of 烧菜 matches nothing. MATCH returns zero
        rows rather than raising, so this was silent. Substring matching is the
        semantics this corpus needs, and the corpus is one small instance's cache.

        Text recognised inside a status's images is searched too, so a photo of a
        menu is findable by what it says. The image tables are global while this
        query is not, hence the join through status_media; the visibility filter
        stays on the outer table, so a direct status is excluded no matter what
        its attachments contain.

        `visibility IS NOT 'direct'` mirrors what cache_statuses indexes, and is
        null-safe: statuses cached without a visibility count as non-direct.
        `self` diaries ride on Mastodon's direct visibility, so they stay out too.
        """
        pattern = f"%{_escape_like(query)}%"
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT payload_json FROM status_cache c
                WHERE c.bot_id=? AND c.visibility IS NOT 'direct' AND (
                    c.text LIKE ?2 ESCAPE '\\'
                    OR c.spoiler_text LIKE ?2 ESCAPE '\\'
                    OR c.author_acct LIKE ?2 ESCAPE '\\'
                    OR EXISTS (
                        SELECT 1 FROM status_media m
                        JOIN image_recognition r ON r.image_sha256 = m.image_sha256
                        WHERE m.status_id = c.status_id AND (
                            r.local_ocr_text LIKE ?2 ESCAPE '\\'
                            OR r.cloud_corrected_text LIKE ?2 ESCAPE '\\'
                            OR r.cloud_description LIKE ?2 ESCAPE '\\'
                            OR r.search_keywords LIKE ?2 ESCAPE '\\'
                        )
                    )
                )
                ORDER BY c.created_at DESC LIMIT ?3
                """,
                (bot_id, pattern, limit),
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

    def record_local_ocr(
        self,
        image_sha256: str,
        *,
        text: str,
        line_count: int,
        mean_confidence: float | None,
    ) -> None:
        """Insert or refresh the local-model pass for one image's content hash.

        Re-recording the same hash (e.g. re-OCRing after a model upgrade) refreshes
        only the local columns; it never clears an existing cloud result or resets
        state back to 'pending', and it never creates a second row for the hash.
        """
        now = int(time.time())
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO image_recognition(
                    image_sha256, local_ocr_text, local_line_count, local_mean_confidence,
                    state, created_at, updated_at
                ) VALUES(?,?,?,?,'pending',?,?)
                ON CONFLICT(image_sha256) DO UPDATE SET
                    local_ocr_text=excluded.local_ocr_text,
                    local_line_count=excluded.local_line_count,
                    local_mean_confidence=excluded.local_mean_confidence,
                    updated_at=excluded.updated_at
                """,
                (image_sha256, text, int(line_count), mean_confidence, now, now),
            )

    def record_cloud_recognition(
        self,
        image_sha256: str,
        *,
        corrected_text: str | None = None,
        description: str | None = None,
        keywords: str | None = None,
        uncertain_text: str | None = None,
    ) -> None:
        """Attach a cloud (Gemini) pass onto a row that local OCR already created.

        Every argument is optional and an omitted one leaves the stored column
        alone, so attaching a description later cannot silently blank a
        correction written by an earlier call. Nothing needs to clear a column
        back to NULL, so COALESCE is the whole story.
        """
        now = int(time.time())
        with self.connect() as db:
            cursor = db.execute(
                """
                UPDATE image_recognition SET
                    cloud_corrected_text=COALESCE(?, cloud_corrected_text),
                    cloud_description=COALESCE(?, cloud_description),
                    search_keywords=COALESCE(?, search_keywords),
                    uncertain_text=COALESCE(?, uncertain_text),
                    state='done', updated_at=?
                WHERE image_sha256=?
                """,
                (corrected_text, description, keywords, uncertain_text, now, image_sha256),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"Unknown image_sha256: {image_sha256}")

    def link_status_media(self, status_id: str, media_id: str, image_sha256: str) -> None:
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO status_media(status_id, media_id, image_sha256, linked_at)
                VALUES(?,?,?,?)
                ON CONFLICT(status_id, media_id) DO UPDATE SET
                    image_sha256=excluded.image_sha256, linked_at=excluded.linked_at
                """,
                (status_id, media_id, image_sha256, int(time.time())),
            )

    def recognitions_for_status(self, status_id: str) -> dict[str, dict[str, Any]]:
        """Return each attachment's recognition row, keyed by Mastodon media id."""
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT m.media_id, r.* FROM status_media m
                JOIN image_recognition r ON r.image_sha256 = m.image_sha256
                WHERE m.status_id=?
                """,
                (status_id,),
            ).fetchall()
        return {row["media_id"]: dict(row) for row in rows}

    def get_image_recognition(self, image_sha256: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM image_recognition WHERE image_sha256=?", (image_sha256,)
            ).fetchone()
        return dict(row) if row else None

    def list_pending_image_recognition(self, limit: int = 100) -> list[dict[str, Any]]:
        """Rows still waiting on (or stuck without) a cloud pass. Posting never waits on these."""
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM image_recognition WHERE state='pending' ORDER BY created_at LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def claim_gemini_daily_attempt(self, limit: int, *, day_utc: str | None = None) -> bool:
        """Atomically reserve one Gemini call under the UTC daily ceiling.

        A zero limit explicitly disables cloud recognition. Old counters are
        discarded after 35 days; they are operational telemetry, not content.
        """
        if limit <= 0:
            return False
        today = day_utc or datetime.now(timezone.utc).date().isoformat()
        cutoff = (
            datetime.strptime(today, "%Y-%m-%d").date() - timedelta(days=35)
        ).isoformat()
        now = int(time.time())
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM gemini_daily_usage WHERE day_utc < ?", (cutoff,))
            cursor = db.execute(
                """
                INSERT INTO gemini_daily_usage(day_utc, attempts, updated_at)
                VALUES(?, 1, ?)
                ON CONFLICT(day_utc) DO UPDATE SET
                    attempts=gemini_daily_usage.attempts + 1,
                    updated_at=excluded.updated_at
                WHERE gemini_daily_usage.attempts < ?
                """,
                (today, now, limit),
            )
            return cursor.rowcount == 1

    def gemini_daily_attempts(self, *, day_utc: str | None = None) -> int:
        today = day_utc or datetime.now(timezone.utc).date().isoformat()
        with self.connect() as db:
            row = db.execute(
                "SELECT attempts FROM gemini_daily_usage WHERE day_utc=?", (today,)
            ).fetchone()
        return int(row[0]) if row else 0
