from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, BinaryIO, Iterable
from urllib.parse import urlencode

import httpx

_LINK_RE = re.compile(r'<([^>]+)>;\s*rel="([^"]+)"')
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


@dataclass(frozen=True, slots=True)
class Page:
    items: list[dict[str, Any]]
    next_cursor: str | None


class MastodonApiError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


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

    def account_statuses(self, account_id: str, *, limit: int, max_id: str | None = None) -> Page:
        response = self._request(
            "GET", f"/api/v1/accounts/{account_id}/statuses",
            params=_drop_none({"limit": limit, "max_id": max_id}),
        )
        return Page(response.json(), _next_cursor(response))

    def favourites(self, *, limit: int, max_id: str | None = None) -> Page:
        response = self._request("GET", "/api/v1/favourites", params=_drop_none({"limit": limit, "max_id": max_id}))
        return Page(response.json(), _next_cursor(response))

    def bookmarks(self, *, limit: int, max_id: str | None = None) -> Page:
        response = self._request("GET", "/api/v1/bookmarks", params=_drop_none({"limit": limit, "max_id": max_id}))
        return Page(response.json(), _next_cursor(response))

    def pinned_statuses(self, account_id: str, *, limit: int = 3) -> list[dict[str, Any]]:
        response = self._request(
            "GET", f"/api/v1/accounts/{account_id}/statuses",
            params={"pinned": "true", "limit": limit},
        )
        return response.json()

    def publish(
        self,
        *,
        text: str,
        visibility: str,
        reply_to_id: str | None,
        media_ids: Iterable[str],
        poll: dict[str, Any] | None = None,
        spoiler_text: str | None = None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        fields: list[tuple[str, str]] = [
            ("status", text),
            ("visibility", visibility),
        ]
        if spoiler_text:
            fields.append(("spoiler_text", spoiler_text))
        if reply_to_id:
            fields.append(("in_reply_to_id", reply_to_id))
        for media_id in media_ids:
            fields.append(("media_ids[]", str(media_id)))
        if poll:
            fields.append(("poll[expires_in]", str(poll["expires_in"])))
            fields.append(("poll[multiple]", "true" if poll.get("multiple") else "false"))
            fields.append(("poll[hide_totals]", "true" if poll.get("hide_totals") else "false"))
            for option in poll["options"]:
                fields.append(("poll[options][]", str(option)))
        try:
            return self._json(
                "POST",
                "/api/v1/statuses",
                content=_urlencoded_form(fields),
                headers={
                    "Idempotency-Key": idempotency_key,
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
        except MastodonApiError as exc:
            raise _content_limit_if_confirmed(exc) from exc

    def edit_status(self, status_id: str, *, text: str) -> dict[str, Any]:
        try:
            return self._json(
                "PUT", f"/api/v1/statuses/{status_id}",
                data={"status": text},
            )
        except MastodonApiError as exc:
            raise _content_limit_if_confirmed(exc) from exc

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

    def vote_poll(self, poll_id: str, choices: list[int]) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/api/v1/polls/{poll_id}/votes",
            content=_urlencoded_form([("choices[]", str(choice)) for choice in choices]),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

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
            raise MastodonApiError("Mastodon connection failed") from exc
        if response.is_redirect:
            raise MastodonApiError(
                f"Mastodon API unexpectedly redirected ({response.status_code})"
            )
        if response.status_code >= 400:
            if response.status_code == 401:
                raise MastodonApiError("resident token is invalid", status_code=401)
            if response.status_code == 422:
                detail = _safe_error_detail(response)
                suffix = f": {detail}" if detail else ""
                raise MastodonApiError(
                    f"Mastodon validation failed{suffix}", status_code=422
                )
            if response.status_code == 429:
                raise MastodonApiError("Mastodon rate limit exceeded", status_code=429)
            if response.status_code >= 500:
                raise MastodonApiError("Mastodon service unavailable", status_code=response.status_code)
            detail = _safe_error_detail(response)
            suffix = f": {detail}" if detail else ""
            raise MastodonApiError(
                f"Mastodon API {method} {path} returned "
                f"{response.status_code} {response.reason_phrase}{suffix}",
                status_code=response.status_code,
            )
        return response


def _safe_error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
        if isinstance(payload, dict):
            value = payload.get("error_description") or payload.get("error")
            if value:
                return _sanitize_error_detail(str(value))
    except (ValueError, json.JSONDecodeError):
        pass
    text = response.text.strip().replace("\r", " ").replace("\n", " ")
    return _sanitize_error_detail(text)


def _sanitize_error_detail(value: str) -> str:
    value = re.sub(r"(?i)\b(?:authorization|bearer|token|access_token)\b\s*[:=]?\s*(?:(?:bearer)\s+)?\S+", "[redacted]", value)
    return value[:200]


def _content_limit_if_confirmed(error: MastodonApiError) -> MastodonApiError:
    if error.status_code == 422 and _looks_like_content_limit(str(error)):
        return MastodonApiError("content exceeds instance limit", status_code=422)
    return error


def _looks_like_content_limit(message: str) -> bool:
    return bool(re.search(
        r"(?i)^too long$|\btoo long\b|"
        r"(character|characters|content|status|text).{0,40}(limit|maximum|max|too long|longer)|"
        r"(limit|maximum|max|too long).{0,40}(character|content|status|text)",
        message,
    ))


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


def _urlencoded_form(fields: list[tuple[str, str]]) -> bytes:
    return urlencode(fields).encode("utf-8")
