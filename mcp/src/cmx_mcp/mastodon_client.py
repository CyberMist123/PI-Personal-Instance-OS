from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, BinaryIO, Iterable

import httpx

_LINK_RE = re.compile(r'<([^>]+)>;\s*rel="([^"]+)"')
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


@dataclass(frozen=True, slots=True)
class Page:
    items: list[dict[str, Any]]
    next_cursor: str | None


class MastodonApiError(RuntimeError):
    pass


class MastodonClient:
    def __init__(self, *, base_url: str, host_header: str, token: str, timeout: float):
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "cmx-mcp/0.2.0",
        }
        # A custom Host header is only needed for an explicitly configured
        # loopback reverse proxy. Public HTTPS uses the URL's native host.
        if httpx.URL(base_url).host in _LOOPBACK:
            headers["Host"] = host_header

        self._client = httpx.Client(
            base_url=base_url,
            headers=headers,
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        )

    def close(self) -> None:
        self._client.close()

    def verify_credentials(self) -> dict[str, Any]:
        return self._json("GET", "/api/v1/accounts/verify_credentials")

    def home_timeline(
        self,
        *,
        limit: int,
        max_id: str | None = None,
        since_id: str | None = None,
    ) -> Page:
        params = _drop_none({"limit": limit, "max_id": max_id, "since_id": since_id})
        response = self._request("GET", "/api/v1/timelines/home", params=params)
        return Page(response.json(), _next_cursor(response))

    def get_status(self, status_id: str) -> dict[str, Any]:
        return self._json("GET", f"/api/v1/statuses/{status_id}")

    def context(self, status_id: str) -> dict[str, Any]:
        return self._json("GET", f"/api/v1/statuses/{status_id}/context")

    def publish(
        self,
        *,
        text: str,
        visibility: str,
        reply_to_id: str | None,
        media_ids: Iterable[str],
        idempotency_key: str,
    ) -> dict[str, Any]:
        fields: list[tuple[str, str]] = [
            ("status", text),
            ("visibility", visibility),
        ]
        if reply_to_id:
            fields.append(("in_reply_to_id", reply_to_id))
        for media_id in media_ids:
            fields.append(("media_ids[]", str(media_id)))
        return self._json(
            "POST",
            "/api/v1/statuses",
            data=fields,
            headers={"Idempotency-Key": idempotency_key},
        )

    def react(self, status_id: str, action: str) -> dict[str, Any]:
        allowed = {
            "favourite",
            "unfavourite",
            "bookmark",
            "unbookmark",
            "reblog",
            "unreblog",
        }
        if action not in allowed:
            raise ValueError(f"Unsupported reaction: {action}")
        return self._json("POST", f"/api/v1/statuses/{status_id}/{action}")

    def upload_image(
        self,
        *,
        stream: BinaryIO,
        filename: str,
        mime_type: str,
        description: str | None,
    ) -> dict[str, Any]:
        files = {"file": (filename, stream, mime_type)}
        data = {"description": description} if description else None
        return self._json("POST", "/api/v2/media", files=files, data=data)

    def notifications(self, *, limit: int, max_id: str | None = None) -> Page:
        response = self._request(
            "GET",
            "/api/v1/notifications",
            params=_drop_none({"limit": limit, "max_id": max_id}),
        )
        return Page(response.json(), _next_cursor(response))

    def dismiss_notification(self, notification_id: str) -> None:
        self._request("POST", f"/api/v1/notifications/{notification_id}/dismiss")

    def _json(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self._request(method, path, **kwargs)
        if response.status_code == 204 or not response.content:
            return {"ok": True}
        try:
            return response.json()
        except json.JSONDecodeError as exc:
            raise MastodonApiError(
                f"Mastodon API {method} {path} returned non-JSON content"
            ) from exc

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise MastodonApiError(
                f"Mastodon request failed: {exc.__class__.__name__}"
            ) from exc
        if response.is_redirect:
            raise MastodonApiError(
                f"Mastodon API unexpectedly redirected ({response.status_code})"
            )
        if response.status_code >= 400:
            detail = _safe_error_detail(response)
            suffix = f": {detail}" if detail else ""
            raise MastodonApiError(
                f"Mastodon API {method} {path} returned "
                f"{response.status_code} {response.reason_phrase}{suffix}"
            )
        return response


def _safe_error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
        if isinstance(payload, dict):
            value = payload.get("error_description") or payload.get("error")
            if value:
                return str(value)[:300]
    except (ValueError, json.JSONDecodeError):
        pass
    text = response.text.strip().replace("\r", " ").replace("\n", " ")
    return text[:300]


def _next_cursor(response: httpx.Response) -> str | None:
    link = response.headers.get("Link", "")
    for url, relation in _LINK_RE.findall(link):
        if relation != "next":
            continue
        parsed = httpx.URL(url)
        value = parsed.params.get("max_id")
        if value:
            return value
    return None


def _drop_none(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}
