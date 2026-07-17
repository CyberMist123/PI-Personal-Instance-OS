from __future__ import annotations

import logging
from typing import Literal

from mcp.server.fastmcp import FastMCP

from .compact import (
    compact_account,
    compact_media,
    compact_notification,
    compact_search,
    compact_status,
    compact_status_page,
)
from .config import Settings
from .mastodon_client import MastodonClient
from .security import resolve_safe_media_file


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("cmx_mcp")

mcp = FastMCP("CMX AI Resident")
_settings: Settings | None = None
_client: MastodonClient | None = None


def settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings.from_env()
    return _settings


def client() -> MastodonClient:
    global _client
    if _client is None:
        current = settings()
        _client = MastodonClient(
            base_url=current.base_url,
            host_header=current.host_header,
            access_token=current.access_token,
            timeout_seconds=current.timeout_seconds,
        )
    return _client


def audit(tool: str, action: str, *, ok: bool = True, target_id: str | None = None) -> None:
    # Prototype intentionally logs no token and no private body text.
    logger.info(
        "tool=%s action=%s ok=%s target_id=%s profile=%s",
        tool,
        action,
        ok,
        target_id or "-",
        settings().profile.value,
    )


@mcp.tool()
def cmx_identity(action: Literal["get"] = "get") -> dict:
    """Read the connected resident identity."""
    settings().require("identity.read")
    result = compact_account(client().verify_credentials())
    result["profile"] = settings().profile.value
    audit("identity", action)
    return result


@mcp.tool()
def cmx_timeline(
    action: Literal["home", "list"] = "home",
    limit: int = 10,
    cursor: str | None = None,
    list_id: str | None = None,
) -> dict:
    """Read a compact home or list timeline."""
    current = settings()
    current.require("timeline.read")
    limit = current.clamp_limit(limit)

    if action == "home":
        items = client().home_timeline(limit=limit, max_id=cursor)
    else:
        if not list_id:
            raise ValueError("list_id is required for action=list")
        items = client().list_timeline(list_id, limit=limit, max_id=cursor)

    next_cursor = str(items[-1].get("id")) if len(items) == limit and items else None
    audit("timeline", action, target_id=list_id)
    return compact_status_page(items, next_cursor=next_cursor)


@mcp.tool()
def cmx_status(
    action: Literal[
        "get",
        "context",
        "create",
        "reply",
        "delete",
        "favourite",
        "unfavourite",
        "bookmark",
        "unbookmark",
        "reblog",
        "unreblog",
    ],
    status_id: str | None = None,
    text: str | None = None,
    visibility: Literal["public", "unlisted", "private", "direct"] | None = None,
    media_ids: list[str] | None = None,
    spoiler_text: str | None = None,
    sensitive: bool = False,
    language: str | None = None,
    scheduled_at: str | None = None,
    idempotency_key: str | None = None,
) -> dict:
    """Read or act on a status; writes use the resident account only."""
    current = settings()

    if action == "get":
        current.require("status.read")
        target = _require_id(status_id)
        result = compact_status(client().get_status(target))
    elif action == "context":
        current.require("status.read")
        target = _require_id(status_id)
        raw = client().status_context(target)
        result = {
            "ancestors": [compact_status(item) for item in raw.get("ancestors", [])],
            "descendants": [compact_status(item) for item in raw.get("descendants", [])],
        }
    elif action in {"create", "reply"}:
        current.require("status.write")
        if not text or not text.strip():
            raise ValueError("text is required")
        reply_target = _require_id(status_id) if action == "reply" else None
        raw = client().create_status(
            text=text.strip(),
            visibility=visibility or current.default_visibility,
            in_reply_to_id=reply_target,
            media_ids=media_ids or [],
            spoiler_text=spoiler_text,
            sensitive=sensitive,
            language=language,
            scheduled_at=scheduled_at,
            idempotency_key=idempotency_key,
        )
        result = _status_write_confirmation(raw)
    elif action == "delete":
        current.require("status.write")
        target = _require_id(status_id)
        raw = client().delete_status(target)
        result = {"ok": True, "status_id": str(raw.get("id") or target), "deleted": True}
    else:
        current.require("status.interact")
        target = _require_id(status_id)
        raw = client().status_action(target, action)
        result = {
            "ok": True,
            "status_id": str(raw.get("id") or target),
            "action": action,
        }

    audit("status", action, target_id=status_id or result.get("status_id"))
    return result


@mcp.tool()
def cmx_media(
    action: Literal["upload"],
    file_path: str,
    description: str | None = None,
) -> dict:
    """Upload media only from this connection's configured inbox."""
    current = settings()
    current.require("media.write")
    safe = resolve_safe_media_file(
        media_root=current.media_root,
        requested_path=file_path,
        max_bytes=current.max_media_bytes,
    )
    raw = client().upload_media(
        path=safe.path,
        mime_type=safe.mime_type,
        description=description,
    )
    result = compact_media(raw)
    result.update({"ok": True, "size_bytes": safe.size_bytes})
    audit("media", action, target_id=result.get("id"))
    return result


@mcp.tool()
def cmx_notifications(
    action: Literal["list", "dismiss", "clear"] = "list",
    notification_id: str | None = None,
    limit: int = 10,
    cursor: str | None = None,
    types: list[str] | None = None,
    exclude_types: list[str] | None = None,
) -> dict:
    """Read or clear compact notifications."""
    current = settings()
    if action == "list":
        current.require("notifications.read")
        limit = current.clamp_limit(limit)
        items = client().notifications(
            limit=limit,
            max_id=cursor,
            types=types,
            exclude_types=exclude_types,
        )
        next_cursor = str(items[-1].get("id")) if len(items) == limit and items else None
        result = {
            "items": [compact_notification(item) for item in items],
            "next_cursor": next_cursor,
        }
    elif action == "dismiss":
        current.require("notifications.write")
        target = _require_id(notification_id)
        client().dismiss_notification(target)
        result = {"ok": True, "notification_id": target, "dismissed": True}
    else:
        current.require("notifications.write")
        client().clear_notifications()
        result = {"ok": True, "cleared": True}

    audit("notifications", action, target_id=notification_id)
    return result


@mcp.tool()
def cmx_search(
    query: str,
    result_type: Literal["accounts", "hashtags", "statuses"] | None = None,
    limit: int = 10,
    resolve: bool = False,
    following: bool = False,
) -> dict:
    """Search visible accounts, tags, or statuses with compact results."""
    current = settings()
    current.require("search.read")
    if not query.strip():
        raise ValueError("query is required")
    result = compact_search(
        client().search(
            query=query.strip(),
            result_type=result_type,
            limit=current.clamp_limit(limit),
            resolve=resolve,
            following=following,
        )
    )
    audit("search", "search")
    return result


@mcp.tool()
def cmx_relationships(
    action: Literal[
        "get",
        "follow",
        "unfollow",
        "mute",
        "unmute",
        "block",
        "unblock",
    ],
    account_ids: list[str] | None = None,
    account_id: str | None = None,
) -> dict:
    """Read relationships or perform Personal-profile relationship actions."""
    current = settings()
    if action == "get":
        current.require("relationships.read")
        ids = account_ids or ([account_id] if account_id else [])
        if not ids:
            raise ValueError("account_ids is required")
        raw = client().relationships(ids)
        result = {
            "items": [
                {
                    "id": str(item.get("id", "")),
                    "following": bool(item.get("following", False)),
                    "followed_by": bool(item.get("followed_by", False)),
                    "muting": bool(item.get("muting", False)),
                    "blocking": bool(item.get("blocking", False)),
                }
                for item in raw
            ]
        }
        target_id = None
    else:
        current.require("relationships.write")
        target_id = _require_id(account_id)
        raw = client().account_action(target_id, action)
        result = {
            "ok": True,
            "account_id": str(raw.get("id") or target_id),
            "action": action,
        }

    audit("relationships", action, target_id=target_id)
    return result


@mcp.tool()
def cmx_lists(
    action: Literal["list", "create", "delete", "accounts", "add", "remove", "timeline"],
    list_id: str | None = None,
    title: str | None = None,
    account_ids: list[str] | None = None,
    limit: int = 10,
    cursor: str | None = None,
) -> dict:
    """Read or manage the resident's own lists."""
    current = settings()
    if action in {"list", "accounts", "timeline"}:
        current.require("lists.read")
    else:
        current.require("lists.write")

    if action == "list":
        result = {
            "items": [
                {"id": str(item.get("id", "")), "title": item.get("title", "")}
                for item in client().lists()
            ]
        }
    elif action == "create":
        if not title or not title.strip():
            raise ValueError("title is required")
        raw = client().create_list(title=title.strip())
        result = {"ok": True, "id": str(raw.get("id", "")), "title": raw.get("title", title)}
    elif action == "delete":
        target = _require_id(list_id)
        client().delete_list(target)
        result = {"ok": True, "list_id": target, "deleted": True}
    elif action == "accounts":
        target = _require_id(list_id)
        raw = client().list_accounts(target, limit=current.clamp_limit(limit))
        result = {"items": [compact_account(item) for item in raw]}
    elif action in {"add", "remove"}:
        target = _require_id(list_id)
        if not account_ids:
            raise ValueError("account_ids is required")
        client().update_list_accounts(
            list_id=target,
            account_ids=account_ids,
            remove=action == "remove",
        )
        result = {"ok": True, "list_id": target, "action": action, "count": len(account_ids)}
    else:
        target = _require_id(list_id)
        page_limit = current.clamp_limit(limit)
        raw = client().list_timeline(
            target,
            limit=page_limit,
            max_id=cursor,
        )
        next_cursor = str(raw[-1].get("id")) if len(raw) == page_limit and raw else None
        result = compact_status_page(raw, next_cursor=next_cursor)

    audit("lists", action, target_id=list_id)
    return result


@mcp.tool()
def cmx_profile(
    action: Literal["get", "update"] = "get",
    display_name: str | None = None,
    note: str | None = None,
    bot: bool | None = None,
    locked: bool | None = None,
) -> dict:
    """Read or update the connected resident's own profile."""
    current = settings()
    if action == "get":
        current.require("profile.read")
        result = compact_account(client().verify_credentials())
    else:
        current.require("profile.write")
        result = compact_account(
            client().update_profile(
                display_name=display_name,
                note=note,
                bot=bot,
                locked=locked,
            )
        )
        result["ok"] = True

    audit("profile", action)
    return result


def _require_id(value: str | None) -> str:
    if not value or not value.strip():
        raise ValueError("A target id is required")
    return value.strip()


def _status_write_confirmation(raw: dict) -> dict:
    return {
        "ok": True,
        "status_id": str(raw.get("id", "")),
        "created_at": raw.get("created_at") or raw.get("scheduled_at"),
        "visibility": raw.get("visibility"),
        "media_count": len(raw.get("media_attachments") or []),
        "scheduled": "scheduled_at" in raw and "created_at" not in raw,
    }


def main() -> None:
    # Force early validation before the MCP handshake to fail fast without
    # printing secrets or exposing a half-configured tool server.
    current = settings()
    logger.info(
        "starting transport=stdio profile=%s base_url=%s host=%s",
        current.profile.value,
        current.base_url,
        current.host_header,
    )
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
