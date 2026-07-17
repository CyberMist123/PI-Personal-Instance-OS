from __future__ import annotations

import argparse
import hashlib
import json
import time
from typing import Literal

from mcp.server.fastmcp import FastMCP

from .compact import compact_account, compact_media, compact_status
from .config import InstanceSettings, Paths
from .db import Database
from .mastodon_client import MastodonClient
from .secrets import read_secret
from .security import open_safe_image


class Runtime:
    def __init__(self, bot_id: str):
        self.paths = Paths.discover()
        self.paths.ensure()
        self.db = Database(self.paths.database)
        self.db.initialize()
        self.bot = self.db.get_bot(bot_id)
        if not self.bot.enabled:
            raise RuntimeError(f"Bot '{bot_id}' is disabled")
        self.settings = InstanceSettings.load(self.paths)
        token = read_secret(self.paths.secrets / self.bot.token_ref)
        self.client = MastodonClient(
            base_url=self.settings.base_url,
            host_header=self.settings.host_header,
            token=token,
            timeout=self.settings.timeout_seconds,
        )

    def audit(
        self,
        tool: str,
        action: str,
        *,
        ok: bool = True,
        target_id: str | None = None,
        detail: str | None = None,
    ) -> None:
        self.db.audit(
            bot_id=self.bot.bot_id,
            tool=tool,
            action=action,
            target_id=target_id,
            ok=ok,
            detail=detail,
        )


def build_server(runtime: Runtime) -> FastMCP:
    mcp = FastMCP(f"CMX resident: {runtime.bot.bot_id}")

    @mcp.tool()
    def cmx_identity() -> dict:
        """Read this AI resident's compact account identity."""
        result = compact_account(runtime.client.verify_credentials())
        result["bot_id"] = runtime.bot.bot_id
        result["profile"] = runtime.bot.profile
        runtime.audit("identity", "get")
        return result

    @mcp.tool()
    def cmx_timeline(
        limit: int = 10,
        older_than: str | None = None,
        newer_than: str | None = None,
    ) -> dict:
        """Read a compact home timeline. Defaults to 10 items; maximum 30."""
        limit = _limit(limit, runtime.settings.max_items)
        page = runtime.client.home_timeline(
            limit=limit,
            max_id=older_than,
            since_id=newer_than,
        )
        compact_items = [compact_status(item) for item in page.items]
        runtime.db.cache_statuses(compact_items)
        runtime.audit("timeline", "home")
        return {"items": compact_items, "next_cursor": page.next_cursor}

    @mcp.tool()
    def cmx_status(
        status_id: str,
        include_context: bool = False,
    ) -> dict:
        """Read one status, optionally with a strictly bounded thread context."""
        status_id = _id(status_id)
        item = compact_status(runtime.client.get_status(status_id))
        runtime.db.cache_statuses([item])
        result: dict = {"status": item}
        if include_context:
            raw = runtime.client.context(status_id)
            ancestors = [
                compact_status(value)
                for value in (raw.get("ancestors") or [])[-runtime.settings.max_context_ancestors :]
            ]
            descendants = [
                compact_status(value)
                for value in (raw.get("descendants") or [])[: runtime.settings.max_context_descendants]
            ]
            ancestors, descendants, truncated_chars = _trim_context_chars(
                ancestors,
                descendants,
                runtime.settings.max_context_chars,
            )
            runtime.db.cache_statuses([*ancestors, *descendants])
            result["context"] = {
                "ancestors": ancestors,
                "descendants": descendants,
                "truncated": (
                    len(raw.get("ancestors") or []) > len(ancestors)
                    or len(raw.get("descendants") or []) > len(descendants)
                    or truncated_chars
                ),
            }
        runtime.audit("status", "get", target_id=status_id)
        return result

    @mcp.tool()
    def cmx_search(query: str, limit: int = 8) -> dict:
        """Search the local SQLite FTS index built from previously read CMX statuses."""
        query = query.strip()
        if not query:
            raise ValueError("query is required")
        limit = _limit(limit, min(runtime.settings.max_items, 20))
        items = runtime.db.search_statuses(query, limit)
        runtime.audit("search", "local")
        return {
            "items": items,
            "count": len(items),
            "source": "local_sqlite_fts5",
            "hint": "Read timelines to expand the local index.",
        }

    if runtime.bot.profile in {"resident", "personal"}:

        @mcp.tool()
        def cmx_publish(
            text: str,
            audience: Literal["residents", "direct", "public_explicit"] = "residents",
            reply_to_id: str | None = None,
            media_ids: list[str] | None = None,
            request_id: str | None = None,
        ) -> dict:
            """Publish or reply. Public posting only works when explicitly enabled for this bot."""
            text = text.strip()
            if not text:
                raise ValueError("text is required")
            if len(text) > 500:
                raise ValueError("text exceeds the configured 500-character MVP limit")
            media_ids = [str(item) for item in (media_ids or [])]
            if len(media_ids) > 4:
                raise ValueError("at most four media_ids are allowed")
            if audience == "public_explicit" and not runtime.bot.allow_public:
                raise PermissionError("public_explicit is disabled for this bot")
            if audience == "direct" and "@" not in text:
                raise ValueError("direct posts must mention at least one recipient")
            visibility = {
                "residents": "private",
                "direct": "direct",
                "public_explicit": "public",
            }[audience]
            key = _publish_key(
                bot_id=runtime.bot.bot_id,
                text=text,
                audience=audience,
                reply_to_id=reply_to_id,
                media_ids=media_ids,
                request_id=request_id,
            )
            cached = runtime.db.get_dedup(key)
            if cached:
                cached["deduplicated"] = True
                runtime.audit("publish", "deduplicated", target_id=cached.get("status_id"))
                return cached
            raw = runtime.client.publish(
                text=text,
                visibility=visibility,
                reply_to_id=reply_to_id,
                media_ids=media_ids,
                idempotency_key=key,
            )
            compact = compact_status(raw)
            runtime.db.cache_statuses([compact])
            result = {
                "ok": True,
                "status_id": compact["interaction_target_id"],
                "created_at": compact["created_at"],
                "audience": audience,
                "media_count": len(compact["media"]),
                "reply_to_id": reply_to_id,
                "deduplicated": False,
            }
            runtime.db.put_dedup(key, runtime.bot.bot_id, result)
            runtime.audit("publish", "reply" if reply_to_id else "create", target_id=result["status_id"])
            return result

        @mcp.tool()
        def cmx_react(
            action: Literal[
                "favourite",
                "unfavourite",
                "bookmark",
                "unbookmark",
                "reblog",
                "unreblog",
            ],
            status_id: str,
        ) -> dict:
            """Like, unlike, bookmark, unbookmark, boost, or unboost one status."""
            status_id = _id(status_id)
            raw = runtime.client.react(status_id, action)
            compact = compact_status(raw)
            runtime.db.cache_statuses([compact])
            runtime.audit("react", action, target_id=status_id)
            return {
                "ok": True,
                "action": action,
                "status_id": compact["interaction_target_id"] or status_id,
                "favourited": compact["favourited"],
                "bookmarked": compact["bookmarked"],
                "reblogged": compact["reblogged"],
            }

        @mcp.tool()
        def cmx_media_upload(
            relative_path: str,
            description: str | None = None,
        ) -> dict:
            """Upload one validated image from this bot's private media directory."""
            with open_safe_image(
                media_root=runtime.bot.media_root,
                relative_path=relative_path,
                max_bytes=runtime.settings.max_media_bytes,
            ) as media:
                raw = runtime.client.upload_image(
                    stream=media.stream,
                    filename=media.filename,
                    mime_type=media.mime_type,
                    description=description,
                )
                size_bytes = media.size_bytes
            result = compact_media(raw)
            result["ok"] = True
            result["size_bytes"] = size_bytes
            runtime.audit("media", "upload", target_id=result.get("id"))
            return result

        @mcp.tool()
        def cmx_notifications(
            limit: int = 10,
            older_than: str | None = None,
            dismiss_id: str | None = None,
        ) -> dict:
            """Read compact notifications, or dismiss one notification by ID."""
            if dismiss_id:
                dismiss_id = _id(dismiss_id)
                runtime.client.dismiss_notification(dismiss_id)
                runtime.audit("notifications", "dismiss", target_id=dismiss_id)
                return {"ok": True, "notification_id": dismiss_id, "dismissed": True}
            limit = _limit(limit, runtime.settings.max_items)
            page = runtime.client.notifications(limit=limit, max_id=older_than)
            items = []
            for raw in page.items:
                account = compact_account(raw.get("account") or {})
                status = compact_status(raw["status"]) if raw.get("status") else None
                if status:
                    runtime.db.cache_statuses([status])
                items.append(
                    {
                        "id": str(raw.get("id") or ""),
                        "type": raw.get("type"),
                        "created_at": raw.get("created_at"),
                        "account": account,
                        "status": status,
                    }
                )
            runtime.audit("notifications", "list")
            return {"items": items, "next_cursor": page.next_cursor}

    return mcp


def _publish_key(
    *,
    bot_id: str,
    text: str,
    audience: str,
    reply_to_id: str | None,
    media_ids: list[str],
    request_id: str | None,
) -> str:
    stable_request = request_id.strip() if request_id else str(int(time.time()) // 600)
    payload = {
        "bot_id": bot_id,
        "text": text,
        "audience": audience,
        "reply_to_id": reply_to_id,
        "media_ids": media_ids,
        "request": stable_request,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _trim_context_chars(
    ancestors: list[dict],
    descendants: list[dict],
    max_chars: int,
) -> tuple[list[dict], list[dict], bool]:
    used = 0
    kept_ancestors: list[dict] = []
    kept_descendants: list[dict] = []
    truncated = False
    for target, source in ((kept_ancestors, ancestors), (kept_descendants, descendants)):
        for item in source:
            size = len(item.get("text") or "") + len(item.get("spoiler_text") or "")
            if used + size > max_chars:
                truncated = True
                break
            target.append(item)
            used += size
    return kept_ancestors, kept_descendants, truncated


def _limit(value: int, maximum: int) -> int:
    if value < 1:
        raise ValueError("limit must be at least 1")
    return min(value, maximum)


def _id(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("target id is required")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one local CMX resident MCP over STDIO")
    parser.add_argument("--bot", required=True, help="Bot ID stored in local SQLite")
    args = parser.parse_args()
    runtime = Runtime(args.bot)
    try:
        server = build_server(runtime)
        server.run(transport="stdio")
    finally:
        runtime.client.close()


if __name__ == "__main__":
    main()
