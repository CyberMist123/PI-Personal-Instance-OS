from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Iterable

import httpx


class MastodonApiError(RuntimeError):
    pass


class MastodonClient:
    def __init__(self, *, base_url: str, access_token: str, timeout_seconds: float) -> None:
        self._client = httpx.Client(
            base_url=base_url,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
                "User-Agent": "cmx-mcp/0.0.1",
            },
            timeout=timeout_seconds,
            follow_redirects=False,
        )

    def close(self) -> None:
        self._client.close()

    def verify_credentials(self) -> dict[str, Any]:
        return self._request_json("GET", "/api/v1/accounts/verify_credentials")

    def home_timeline(
        self,
        *,
        limit: int,
        max_id: str | None = None,
        since_id: str | None = None,
    ) -> list[dict[str, Any]]:
        params = _drop_none({"limit": limit, "max_id": max_id, "since_id": since_id})
        return self._request_json("GET", "/api/v1/timelines/home", params=params)

    def list_timeline(
        self, list_id: str, *, limit: int, max_id: str | None = None
    ) -> list[dict[str, Any]]:
        params = _drop_none({"limit": limit, "max_id": max_id})
        return self._request_json("GET", f"/api/v1/timelines/list/{list_id}", params=params)

    def get_status(self, status_id: str) -> dict[str, Any]:
        return self._request_json("GET", f"/api/v1/statuses/{status_id}")

    def status_context(self, status_id: str) -> dict[str, Any]:
        return self._request_json("GET", f"/api/v1/statuses/{status_id}/context")

    def create_status(
        self,
        *,
        text: str,
        visibility: str,
        in_reply_to_id: str | None = None,
        media_ids: Iterable[str] = (),
        spoiler_text: str | None = None,
        sensitive: bool = False,
        language: str | None = None,
        scheduled_at: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        fields: list[tuple[str, str]] = [
            ("status", text),
            ("visibility", visibility),
            ("sensitive", "true" if sensitive else "false"),
        ]
        for key, value in {
            "in_reply_to_id": in_reply_to_id,
            "spoiler_text": spoiler_text,
            "language": language,
            "scheduled_at": scheduled_at,
        }.items():
            if value is not None:
                fields.append((key, value))
        for media_id in media_ids:
            fields.append(("media_ids[]", str(media_id)))

        headers = {"Idempotency-Key": idempotency_key or str(uuid.uuid4())}
        return self._request_json(
            "POST", "/api/v1/statuses", data=fields, headers=headers
        )

    def delete_status(self, status_id: str) -> dict[str, Any]:
        return self._request_json("DELETE", f"/api/v1/statuses/{status_id}")

    def status_action(self, status_id: str, action: str) -> dict[str, Any]:
        allowed = {
            "favourite",
            "unfavourite",
            "bookmark",
            "unbookmark",
            "reblog",
            "unreblog",
        }
        if action not in allowed:
            raise ValueError(f"Unsupported status action: {action}")
        return self._request_json("POST", f"/api/v1/statuses/{status_id}/{action}")

    def upload_media(
        self, *, path: Path, mime_type: str, description: str | None
    ) -> dict[str, Any]:
        with path.open("rb") as stream:
            files = {"file": (path.name, stream, mime_type)}
            data = {"description": description} if description else None
            return self._request_json("POST", "/api/v2/media", files=files, data=data)

    def notifications(
        self,
        *,
        limit: int,
        max_id: str | None = None,
        types: list[str] | None = None,
        exclude_types: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        params: list[tuple[str, str]] = [("limit", str(limit))]
        if max_id:
            params.append(("max_id", max_id))
        for item in types or []:
            params.append(("types[]", item))
        for item in exclude_types or []:
            params.append(("exclude_types[]", item))
        return self._request_json("GET", "/api/v1/notifications", params=params)

    def dismiss_notification(self, notification_id: str) -> dict[str, Any]:
        return self._request_json(
            "POST", f"/api/v1/notifications/{notification_id}/dismiss"
        )

    def clear_notifications(self) -> dict[str, Any]:
        return self._request_json("POST", "/api/v1/notifications/clear")

    def search(
        self,
        *,
        query: str,
        result_type: str | None,
        limit: int,
        resolve: bool,
        following: bool,
    ) -> dict[str, Any]:
        params = _drop_none(
            {
                "q": query,
                "type": result_type,
                "limit": limit,
                "resolve": str(resolve).lower(),
                "following": str(following).lower(),
            }
        )
        return self._request_json("GET", "/api/v2/search", params=params)

    def relationships(self, account_ids: Iterable[str]) -> list[dict[str, Any]]:
        params = [("id[]", str(account_id)) for account_id in account_ids]
        return self._request_json("GET", "/api/v1/accounts/relationships", params=params)

    def account_action(self, account_id: str, action: str) -> dict[str, Any]:
        allowed = {
            "follow",
            "unfollow",
            "mute",
            "unmute",
            "block",
            "unblock",
        }
        if action not in allowed:
            raise ValueError(f"Unsupported relationship action: {action}")
        return self._request_json("POST", f"/api/v1/accounts/{account_id}/{action}")

    def lists(self) -> list[dict[str, Any]]:
        return self._request_json("GET", "/api/v1/lists")

    def create_list(self, *, title: str) -> dict[str, Any]:
        return self._request_json("POST", "/api/v1/lists", data={"title": title})

    def delete_list(self, list_id: str) -> dict[str, Any]:
        return self._request_json("DELETE", f"/api/v1/lists/{list_id}")

    def list_accounts(self, list_id: str, *, limit: int) -> list[dict[str, Any]]:
        return self._request_json(
            "GET", f"/api/v1/lists/{list_id}/accounts", params={"limit": limit}
        )

    def update_list_accounts(
        self, *, list_id: str, account_ids: Iterable[str], remove: bool
    ) -> dict[str, Any]:
        data = [("account_ids[]", str(account_id)) for account_id in account_ids]
        method = "DELETE" if remove else "POST"
        return self._request_json(method, f"/api/v1/lists/{list_id}/accounts", data=data)

    def update_profile(
        self,
        *,
        display_name: str | None = None,
        note: str | None = None,
        bot: bool | None = None,
        locked: bool | None = None,
    ) -> dict[str, Any]:
        data: dict[str, str] = {}
        if display_name is not None:
            data["display_name"] = display_name
        if note is not None:
            data["note"] = note
        if bot is not None:
            data["bot"] = str(bot).lower()
        if locked is not None:
            data["locked"] = str(locked).lower()
        if not data:
            raise ValueError("At least one profile field is required")
        return self._request_json("PATCH", "/api/v1/accounts/update_credentials", data=data)

    def _request_json(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise MastodonApiError(f"Mastodon request failed: {exc.__class__.__name__}") from exc

        if response.status_code >= 400:
            message = _safe_error_message(response)
            raise MastodonApiError(
                f"Mastodon API {method} {path} returned {response.status_code}: {message}"
            )

        if response.status_code == 204 or not response.content:
            return {"ok": True}

        try:
            return response.json()
        except json.JSONDecodeError as exc:
            raise MastodonApiError(
                f"Mastodon API {method} {path} returned non-JSON content"
            ) from exc


def _safe_error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except json.JSONDecodeError:
        return response.text[:300].replace("\n", " ")
    if isinstance(payload, dict):
        return str(payload.get("error") or payload.get("error_description") or payload)[:300]
    return str(payload)[:300]


def _drop_none(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}
