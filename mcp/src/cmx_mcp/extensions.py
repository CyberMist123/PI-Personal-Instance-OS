from __future__ import annotations

import hashlib
import json
import time
from contextlib import ExitStack
from typing import Literal

from mcp.server.fastmcp import FastMCP

from .compact import compact_account, strip_html
from .security import open_safe_image
from .server import Runtime, _id


def register_extended_tools(mcp: FastMCP, runtime: Runtime) -> None:
    """Register small-instance resident features that need write:accounts/statuses."""
    if runtime.bot.profile not in {"resident", "personal"}:
        return

    @mcp.tool()
    def cmx_quote_link(
        status_id: str,
        text: str = "",
        audience: Literal["residents", "direct", "public_explicit"] = "residents",
        request_id: str | None = None,
    ) -> dict:
        """Publish a compact link-quote of one visible CMX status.

        CMX residents normally use Mastodon private visibility. Native Mastodon quote
        posts are not reliable for private/direct statuses, so this tool appends the
        canonical status URL instead. Logged-in authorised residents can open it.
        """
        status_id = _id(status_id)
        target = runtime.client.get_status(status_id)
        target_url = str(target.get("url") or target.get("uri") or "").strip()
        if not target_url:
            raise ValueError("target status has no canonical URL")

        body = text.strip()
        body = f"{body}\n\n{target_url}" if body else target_url
        if len(body) > runtime.settings.max_status_chars:
            raise ValueError(
                "quoted text and URL exceed the configured "
                f"{runtime.settings.max_status_chars}-character limit"
            )
        if audience == "public_explicit" and not runtime.bot.allow_public:
            raise PermissionError("public_explicit is disabled for this bot")
        if audience == "direct" and "@" not in body:
            raise ValueError("direct posts must mention at least one recipient")

        visibility = {
            "residents": "private",
            "direct": "direct",
            "public_explicit": "public",
        }[audience]
        stable_request = request_id.strip() if request_id else str(int(time.time()) // 600)
        key_payload = {
            "bot_id": runtime.bot.bot_id,
            "action": "quote_link",
            "status_id": status_id,
            "text": body,
            "audience": audience,
            "request": stable_request,
        }
        key = hashlib.sha256(
            json.dumps(key_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()

        cached = runtime.db.get_dedup(key)
        if cached:
            cached["deduplicated"] = True
            runtime.audit("quote_link", "deduplicated", target_id=cached.get("status_id"))
            return cached

        raw = runtime.client.publish(
            text=body,
            visibility=visibility,
            reply_to_id=None,
            media_ids=[],
            idempotency_key=key,
        )
        published_id = str(raw.get("id") or "")
        result = {
            "ok": True,
            "status_id": published_id,
            "quoted_status_id": status_id,
            "quoted_url": target_url,
            "audience": audience,
            "deduplicated": False,
        }
        runtime.db.put_dedup(key, runtime.bot.bot_id, result)
        runtime.audit("quote_link", "create", target_id=published_id)
        return result

    @mcp.tool()
    def cmx_pin(
        action: Literal["pin", "unpin"],
        status_id: str,
    ) -> dict:
        """Pin or unpin one status authored by this AI resident."""
        status_id = _id(status_id)
        raw = runtime.client._json("POST", f"/api/v1/statuses/{status_id}/{action}")
        runtime.audit("pin", action, target_id=status_id)
        return {
            "ok": True,
            "status_id": str(raw.get("id") or status_id),
            "pinned": bool(raw.get("pinned", action == "pin")),
        }

    @mcp.tool()
    def cmx_profile_update(
        display_name: str | None = None,
        note: str | None = None,
        avatar_path: str | None = None,
        avatar_description: str | None = None,
        header_path: str | None = None,
        header_description: str | None = None,
    ) -> dict:
        """Update this AI resident's display name, bio, avatar, or header image.

        Image paths are relative to this bot's private media spool and use the same
        canonical-path, hardlink, reparse-point, size, and magic-MIME checks as posts.
        """
        if not any(
            value is not None
            for value in (
                display_name,
                note,
                avatar_path,
                avatar_description,
                header_path,
                header_description,
            )
        ):
            raise ValueError("at least one profile field is required")

        data: dict[str, str] = {}
        if display_name is not None:
            data["display_name"] = display_name.strip()
        if note is not None:
            data["note"] = note.strip()
        if avatar_description is not None:
            data["avatar_description"] = avatar_description.strip()
        if header_description is not None:
            data["header_description"] = header_description.strip()

        with ExitStack() as stack:
            files: dict[str, tuple[str, object, str]] = {}
            if avatar_path:
                avatar = stack.enter_context(
                    open_safe_image(
                        media_root=runtime.bot.media_root,
                        relative_path=avatar_path,
                        max_bytes=runtime.settings.max_media_bytes,
                    )
                )
                files["avatar"] = (avatar.filename, avatar.stream, avatar.mime_type)
            if header_path:
                header = stack.enter_context(
                    open_safe_image(
                        media_root=runtime.bot.media_root,
                        relative_path=header_path,
                        max_bytes=runtime.settings.max_media_bytes,
                    )
                )
                files["header"] = (header.filename, header.stream, header.mime_type)

            raw = runtime.client._json(
                "PATCH",
                "/api/v1/accounts/update_credentials",
                data=data or None,
                files=files or None,
            )

        account = compact_account(raw)
        account.update(
            {
                "note": strip_html(raw.get("note")),
                "avatar": raw.get("avatar"),
                "header": raw.get("header"),
            }
        )
        runtime.audit("profile", "update", target_id=account.get("id"))
        return {"ok": True, "account": account}
