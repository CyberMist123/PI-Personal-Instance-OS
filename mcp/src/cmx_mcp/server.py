from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets
import time
from typing import Any, Literal

from mcp.server.fastmcp import Context, FastMCP

from .compact import compact_account, compact_media, compact_status, compact_v2_status
from .config import InstanceSettings, Paths
from .db import Database
from .mastodon_client import MastodonApiError, MastodonClient
from .secrets import read_secret
from .security import open_safe_image
from .scope import READ_SCOPE, SOCIAL_SCOPE, require_request_scope

BasicInteractAction = Literal["like", "unlike", "bookmark", "unbookmark", "vote"]
BoostInteractAction = Literal["like", "unlike", "bookmark", "unbookmark", "vote", "boost", "unboost"]


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

    def close(self) -> None:
        self.client.close()

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


def build_server(
    runtime: Runtime,
    *,
    read_only: bool = False,
    remote_profile: str | None = None,
    remote_capabilities: Any | None = None,
    **fastmcp_options: Any,
) -> FastMCP:
    if remote_profile is not None:
        return _build_remote_server(
            runtime, profile=remote_profile, capabilities=remote_capabilities, **fastmcp_options
        )
    mcp = FastMCP(f"CMX resident: {runtime.bot.bot_id}", **fastmcp_options)

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
        runtime.db.cache_statuses(runtime.bot.bot_id, compact_items)
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
        runtime.db.cache_statuses(runtime.bot.bot_id, [item])
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
            runtime.db.cache_statuses(runtime.bot.bot_id, [*ancestors, *descendants])
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
        items = runtime.db.search_statuses(runtime.bot.bot_id, query, limit)
        runtime.audit("search", "local")
        return {
            "items": items,
            "count": len(items),
            "source": "local_sqlite_fts5",
            "hint": "Read timelines to expand the local index.",
        }

    if not read_only and runtime.bot.profile in {"resident", "personal"}:

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
            claim = runtime.db.claim_dedup(
                bot_id=runtime.bot.bot_id, operation="publish", request_id=key
            )
            if not claim["claimed"]:
                if claim["state"] == "succeeded":
                    cached = dict(claim["response"])
                    cached["deduplicated"] = True
                    runtime.audit("publish", "deduplicated", target_id=cached.get("status_id"))
                    return cached
                raise RuntimeError("publish request is already in progress")
            try:
                raw = runtime.client.publish(
                    text=text,
                    visibility=visibility,
                    reply_to_id=reply_to_id,
                    media_ids=media_ids,
                    idempotency_key=key,
                )
                compact = compact_status(raw)
                runtime.db.cache_statuses(runtime.bot.bot_id, [compact])
                result = {
                    "ok": True,
                    "status_id": compact["interaction_target_id"],
                    "created_at": compact["created_at"],
                    "audience": audience,
                    "media_count": len(compact["media"]),
                    "reply_to_id": reply_to_id,
                    "deduplicated": False,
                }
                runtime.db.finish_dedup(
                    bot_id=runtime.bot.bot_id, operation="publish", request_id=key, response=result
                )
            except Exception:
                runtime.db.finish_dedup(
                    bot_id=runtime.bot.bot_id, operation="publish", request_id=key, error_code="external_error"
                )
                raise
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
            runtime.db.cache_statuses(runtime.bot.bot_id, [compact])
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
                    runtime.db.cache_statuses(runtime.bot.bot_id, [status])
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
    stable_request = request_id.strip() if request_id and request_id.strip() else f"best-effort:{secrets.token_urlsafe(16)}"
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


def _build_remote_server(
    runtime: Runtime,
    *,
    profile: str,
    capabilities: Any | None,
    **fastmcp_options: Any,
) -> FastMCP:
    """Build only the Phase A/A+ remote surface; never reuse local full tools."""
    if profile not in {"reader", "social", "social_plus"}:
        raise ValueError("remote profile is not ready or is disabled")
    caps = capabilities
    polls = bool(getattr(caps, "polls", getattr(caps, "remote_polls", True)))
    boosts = bool(getattr(caps, "boosts", getattr(caps, "remote_boosts", False))) and profile in {"social", "social_plus"}
    notifications = bool(getattr(caps, "notifications", getattr(caps, "remote_notifications", False))) and profile == "social_plus"
    mcp = FastMCP(f"CMX remote resident: {runtime.bot.bot_id}", **fastmcp_options)

    def read_scope(ctx: Context) -> None:
        require_request_scope(ctx, READ_SCOPE)

    def social_scope(ctx: Context) -> None:
        require_request_scope(ctx, SOCIAL_SCOPE)

    @mcp.tool()
    def cmx_home(
        view: Literal["timeline", "bookmarks", "likes", "mine"] = "timeline",
        limit: int = 10,
        cursor: str | None = None,
        include_pinned: bool = True,
        ctx: Context = None,
    ) -> dict:
        """Read timeline, bookmarks, likes, or this resident's own posts."""
        read_scope(ctx)
        limit = _limit(limit, min(runtime.settings.max_items, 30))
        if view == "timeline":
            page = runtime.client.home_timeline(limit=limit, max_id=cursor)
        elif view == "bookmarks":
            page = runtime.client.bookmarks(limit=limit, max_id=cursor)
        elif view == "likes":
            page = runtime.client.favourites(limit=limit, max_id=cursor)
        else:
            account = runtime.client.verify_credentials()
            page = runtime.client.account_statuses(str(account.get("id") or ""), limit=limit, max_id=cursor)
            page = type(page)(
                [item for item in page.items if item.get("visibility") != "direct"], page.next_cursor
            )
        raw_items = page.items
        if view == "timeline" and include_pinned and not cursor:
            account = runtime.client.verify_credentials()
            raw_items = [*runtime.client.pinned_statuses(str(account.get("id") or ""), limit=3), *raw_items]
        items = [compact_v2_status(item) for item in raw_items[:limit + (3 if view == "timeline" and not cursor else 0)]]
        runtime.db.cache_statuses(runtime.bot.bot_id, [compact_status(item) for item in raw_items])
        runtime.audit("home", view)
        result = {"view": view, "items": items, "scope": "resident"}
        if page.next_cursor:
            result["cursor"] = page.next_cursor
        return result

    @mcp.tool()
    def cmx_status(
        status_id: str,
        view: Literal["compact", "thread", "media"] = "compact",
        ctx: Context = None,
    ) -> dict:
        """Read one compact status, bounded thread, or safe media metadata."""
        read_scope(ctx)
        status_id = _id(status_id)
        raw = runtime.client.get_status(status_id)
        compact = compact_v2_status(raw)
        runtime.db.cache_statuses(runtime.bot.bot_id, [compact_status(raw)])
        if view == "compact":
            return compact
        if view == "media":
            return {"id": compact["id"], "media": compact.get("media", [])}
        context = runtime.client.context(status_id)
        ancestors = [compact_v2_status(item) for item in context.get("ancestors") or []]
        descendants = [compact_v2_status(item) for item in context.get("descendants") or []]
        max_items = min(runtime.settings.max_items * 4, 120)
        all_items = ancestors + descendants
        truncated = len(all_items) > max_items
        all_items = all_items[:max_items]
        max_chars = getattr(runtime.settings, "max_context_chars", 16000)
        used = 0
        bounded: list[dict] = []
        for item in all_items:
            size = len(item.get("text") or "") + len(item.get("cw") or "")
            if used + size > max_chars:
                truncated = True
                break
            bounded.append(item)
            used += size
        all_items = bounded
        ancestor_count = min(len(ancestors), len(all_items))
        result = {"status": compact, "ancestors": all_items[:ancestor_count], "descendants": all_items[ancestor_count:]}
        if truncated:
            result["truncated"] = True
            result["reason"] = "thread_safety_limit"
        return result

    @mcp.tool()
    def cmx_search(query: str, limit: int = 5, ctx: Context = None) -> dict:
        """Search only this resident's previously read, non-direct cache."""
        read_scope(ctx)
        query = query.strip()
        if not query:
            raise ValueError("query is required")
        requested = _limit(limit, 20)
        candidates = runtime.db.search_statuses(runtime.bot.bot_id, query, min(requested * 3, 60))
        items: list[dict] = []
        for cached in candidates:
            status_id = str(cached.get("id") or "")
            if not status_id:
                continue
            try:
                raw = runtime.client.get_status(status_id)
            except MastodonApiError as exc:
                if _visibility_failure(exc):
                    runtime.db.invalidate_status(runtime.bot.bot_id, status_id)
                    continue
                raise
            refreshed = compact_status(raw)
            runtime.db.cache_statuses(runtime.bot.bot_id, [refreshed])
            items.append(compact_v2_status(raw))
            if len(items) >= requested:
                break
        return {"items": items, "scope": "cache",
                "coverage": "statuses previously read by this resident MCP"}

    if profile in {"social", "social_plus"}:
        if polls:
            @mcp.tool()
            def cmx_post(
                action: Literal["create", "reply", "edit"], text: str,
                status_id: str | None = None,
                audience: Literal["residents", "direct", "public_explicit"] = "residents",
                poll: dict | None = None, request_id: str | None = None,
                ctx: Context = None,
            ) -> dict:
                return _remote_post(runtime, social_scope, action, text, status_id, audience, poll, request_id, ctx)
        else:
            @mcp.tool()
            def cmx_post(
                action: Literal["create", "reply", "edit"], text: str,
                status_id: str | None = None,
                audience: Literal["residents", "direct", "public_explicit"] = "residents",
                request_id: str | None = None, ctx: Context = None,
            ) -> dict:
                return _remote_post(runtime, social_scope, action, text, status_id, audience, None, request_id, ctx)

        if boosts:
            @mcp.tool()
            def cmx_interact(action: BoostInteractAction, status_id: str, choices: list[int] | None = None, ctx: Context = None) -> dict:
                return _remote_interact(runtime, social_scope, action, status_id, choices, ctx)
        else:
            @mcp.tool()
            def cmx_interact(action: BasicInteractAction, status_id: str, choices: list[int] | None = None, ctx: Context = None) -> dict:
                return _remote_interact(runtime, social_scope, action, status_id, choices, ctx)

    if notifications:
        @mcp.tool()
        def cmx_notifications(limit: int = 10, cursor: str | None = None, ctx: Context = None) -> dict:
            read_scope(ctx)
            page = runtime.client.notifications(limit=_limit(limit, 30), max_id=cursor)
            items = []
            for item in page.items:
                entry = {"id": str(item.get("id") or ""), "type": item.get("type"), "at": item.get("created_at"),
                         "author": (item.get("account") or {}).get("acct") or ""}
                if item.get("status"):
                    entry["status"] = compact_v2_status(item["status"])
                items.append({key: value for key, value in entry.items() if value not in (None, "", [])})
            result = {"items": items}
            if page.next_cursor:
                result["cursor"] = page.next_cursor
            return result

    return mcp


def _remote_interact(runtime: Runtime, check_scope: Any, action: str, status_id: str,
                     choices: list[int] | None, ctx: Context) -> dict:
    check_scope(ctx)
    status_id = _id(status_id)
    if action != "vote" and choices:
        raise ValueError("choices is only accepted for vote")
    target = runtime.client.get_status(status_id)
    if action == "vote":
        if choices is None or not choices:
            raise ValueError("poll choices are required")
        poll = target.get("poll") or {}
        if not poll:
            raise ValueError("status has no poll")
        indexes = _validate_poll_choices(choices, poll)
        raw = runtime.client.vote_poll(str(poll["id"]), indexes)
        return {"id": str(target.get("id") or status_id), "poll": compact_v2_status({**target, "poll": raw}).get("poll")}
    api_action = {"like": "favourite", "unlike": "unfavourite", "boost": "reblog", "unboost": "unreblog"}.get(action, action)
    raw = runtime.client.react(status_id, api_action)
    compact = compact_v2_status(raw)
    result = {"id": compact.get("id", status_id)}
    if compact.get("state"):
        result["state"] = compact["state"]
    return result


def _remote_post(runtime: Runtime, check_scope: Any, action: str, text: str,
                 status_id: str | None, audience: str, poll: dict | None,
                 request_id: str | None, ctx: Context) -> dict:
    check_scope(ctx)
    text = text.strip()
    if not text:
        raise ValueError("text is required")
    if action == "create" and status_id is not None:
        raise ValueError("status_id is not accepted for create")
    if action in {"reply", "edit"} and not status_id:
        raise ValueError("status_id is required for reply and edit")
    if action == "edit":
        if audience != "residents" or poll is not None:
            raise ValueError("edit only accepts text and status_id")
        target = runtime.client.get_status(_id(status_id))
        me = runtime.client.verify_credentials()
        if str((target.get("account") or {}).get("id") or "") != str(me.get("id") or ""):
            raise PermissionError("only the current resident may edit its own status")
        if target.get("media_attachments") or target.get("poll") or target.get("spoiler_text") or target.get("sensitive"):
            raise ValueError("complex statuses must be edited in the web or local client")
        key = _operation_key(
            runtime.bot.bot_id, "edit", request_id, status_id,
            str(target.get("edited_at") or target.get("created_at") or ""),
            str(target.get("content") or ""),
        )
        claim = runtime.db.claim_dedup(bot_id=runtime.bot.bot_id, operation="edit", request_id=key)
        if not claim["claimed"]:
            if claim["state"] == "succeeded":
                return {"id": claim["response"]["id"], "deduplicated": True}
            raise RuntimeError("edit request is already in progress")
        try:
            # Mastodon PUT edit is guarded by the freshly-read target version;
            # its endpoint is not treated as supporting POST-style idempotency headers.
            raw = runtime.client.edit_status(_id(status_id), text=text)
            result = {"id": str(raw.get("id") or status_id)}
            runtime.db.finish_dedup(bot_id=runtime.bot.bot_id, operation="edit", request_id=key, response=result)
            return result
        except Exception:
            runtime.db.finish_dedup(bot_id=runtime.bot.bot_id, operation="edit", request_id=key, error_code="external_error")
            raise
    target = runtime.client.get_status(_id(status_id)) if action == "reply" else None
    if action == "reply":
        target_visibility = (target or {}).get("visibility")
        visibility = "direct" if target_visibility == "direct" else "private"
        target_author = str(((target or {}).get("account") or {}).get("acct") or "")
        mentions = [target_author, *[str(item.get("acct") or "") for item in (target or {}).get("mentions") or []]]
        me_acct = str((runtime.client.verify_credentials()).get("acct") or "")
        mentions = [item for item in dict.fromkeys(_normalize_acct(item) for item in mentions) if item and item != _normalize_acct(me_acct)]
        if not mentions:
            raise ValueError("direct reply has no valid recipient")
        prefix = " ".join(f"@{item}" for item in mentions if f"@{item}" not in text)
        text = f"{prefix} {text}".strip()
    else:
        visibility = {"residents": "private", "direct": "direct", "public_explicit": "public"}.get(audience)
        if visibility is None or (audience == "public_explicit" and not runtime.bot.allow_public):
            raise PermissionError("public_explicit is disabled for this bot")
    if visibility == "direct" and "@" not in text:
        raise ValueError("direct posts must mention at least one recipient")
    validated_poll = _validate_poll(poll) if poll is not None else None
    key = _operation_key(runtime.bot.bot_id, action, request_id, status_id, text)
    claim = runtime.db.claim_dedup(bot_id=runtime.bot.bot_id, operation=action, request_id=key)
    if not claim["claimed"]:
        if claim["state"] == "succeeded":
            return {"id": claim["response"]["id"], "deduplicated": True}
        raise RuntimeError("request is already in progress")
    try:
        inherited_cw = str((target or {}).get("spoiler_text") or "") if action == "reply" else ""
        raw = runtime.client.publish(text=text, visibility=visibility, reply_to_id=status_id,
                                     media_ids=[], poll=validated_poll, spoiler_text=inherited_cw or None,
                                     idempotency_key=key)
        result = {"id": str(raw.get("id") or "")}
        runtime.db.finish_dedup(bot_id=runtime.bot.bot_id, operation=action, request_id=key, response=result)
        runtime.db.cache_statuses(runtime.bot.bot_id, [compact_status(raw)])
        return result
    except Exception:
        runtime.db.finish_dedup(bot_id=runtime.bot.bot_id, operation=action, request_id=key, error_code="external_error")
        raise


def _operation_key(bot_id: str, operation: str, request_id: str | None, *parts: str | None) -> str:
    # Without an explicit request ID this is deliberately best-effort: a user
    # may publish identical text repeatedly and must not be blocked by dedup.
    request = request_id.strip() if request_id and request_id.strip() else f"best-effort:{secrets.token_urlsafe(16)}"
    payload = {"bot_id": bot_id, "operation": operation, "request_id": request, "parts": parts}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _normalize_acct(value: str) -> str:
    return value.strip().lstrip("@").lower()


def _validate_poll(poll: dict) -> dict:
    if not isinstance(poll, dict) or set(poll) - {"options", "expires_in", "multiple", "hide_totals"}:
        raise ValueError("poll contains unsupported fields")
    options = poll.get("options")
    expires = poll.get("expires_in")
    if not isinstance(options, list) or not 2 <= len(options) <= 4 or any(not isinstance(item, str) or not item.strip() for item in options):
        raise ValueError("poll requires two to four non-empty options")
    if not isinstance(expires, int) or not 300 <= expires <= 604800:
        raise ValueError("poll expires_in must be between 300 and 604800 seconds")
    if not isinstance(poll.get("multiple", False), bool) or not isinstance(poll.get("hide_totals", False), bool):
        raise ValueError("poll boolean fields are invalid")
    return {"options": [item.strip() for item in options], "expires_in": expires,
            "multiple": poll.get("multiple", False), "hide_totals": poll.get("hide_totals", False)}


def _validate_poll_choices(choices: list[int] | None, poll: dict) -> list[int]:
    values = choices or []
    if not values or any(not isinstance(item, int) or item < 0 for item in values) or len(set(values)) != len(values):
        raise ValueError("poll choices must be unique non-negative integers")
    options = poll.get("options") or []
    if any(item >= len(options) for item in values):
        raise ValueError("poll choice is out of range")
    if not poll.get("multiple") and len(values) != 1:
        raise ValueError("single-choice polls require exactly one choice")
    return values


def _visibility_failure(error: MastodonApiError) -> bool:
    status_code = getattr(error, "status_code", None)
    if status_code is not None:
        return status_code in {403, 404}
    return bool(re.search(r"returned (?:403|404)\b", str(error)))


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
        runtime.close()


if __name__ == "__main__":
    main()
