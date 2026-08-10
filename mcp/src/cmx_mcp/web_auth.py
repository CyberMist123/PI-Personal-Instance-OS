"""Verify a caller's own Mastodon web-session bearer against this instance.

The token belongs to the browser session of the person using the page, not to
CMX. It is used exactly once per verification, in memory, for a single upstream
call. It is never written to SQLite, to disk, or to any log line, and it is
never returned to the caller. Only the digest of a *successfully* verified token
is ever retained, and only for a minute.
"""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass

import httpx

VERIFY_TIMEOUT_SECONDS = 10.0
CACHE_TTL_SECONDS = 60.0
CACHE_MAX_ENTRIES = 64


@dataclass(frozen=True, slots=True)
class WebIdentity:
    """The minimum we need to scope Clipboard rows to one Mastodon account."""

    account_id: str
    acct: str


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class IdentityCache:
    """Short-TTL identity cache keyed by the SHA-256 of the bearer.

    Only successes are cached. Caching failures would lock a user out for the
    whole TTL right after they sign in, and a failed verification is exactly the
    case where we want to ask the instance again.
    """

    def __init__(self, ttl_seconds: float = CACHE_TTL_SECONDS, max_entries: int = CACHE_MAX_ENTRIES):
        self._ttl = ttl_seconds
        self._max = max_entries
        self._lock = threading.Lock()
        self._entries: dict[tuple[str, str], tuple[float, WebIdentity]] = {}

    def get(self, base_url: str, token: str) -> WebIdentity | None:
        key = (base_url, token_digest(token))
        now = time.monotonic()
        with self._lock:
            hit = self._entries.get(key)
            if hit is None:
                return None
            expires_at, identity = hit
            if expires_at <= now:
                self._entries.pop(key, None)
                return None
            return identity

    def put(self, base_url: str, token: str, identity: WebIdentity) -> None:
        key = (base_url, token_digest(token))
        now = time.monotonic()
        with self._lock:
            self._entries = {k: v for k, v in self._entries.items() if v[0] > now}
            if len(self._entries) >= self._max:
                oldest = min(self._entries, key=lambda k: self._entries[k][0])
                self._entries.pop(oldest, None)
            self._entries[key] = (now + self._ttl, identity)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


_CACHE = IdentityCache()


def _fetch_identity(base_url: str, token: str) -> WebIdentity | None:
    try:
        with httpx.Client(
            base_url=base_url,
            timeout=VERIFY_TIMEOUT_SECONDS,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = client.get(
                "/api/v1/accounts/verify_credentials",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    account_id = str(payload.get("id") or "")
    acct = str(payload.get("acct") or "")
    if not account_id:
        # Without a stable account id we cannot scope rows to an owner, so we
        # fail closed rather than fall back to acct, which is mutable.
        return None
    return WebIdentity(account_id=account_id, acct=acct)


def verify_web_identity(base_url: str, token: str, *, cache: IdentityCache | None = None) -> WebIdentity | None:
    """Return the caller's identity, or None. Blocking: call via threadpool."""
    if not token:
        return None
    store = _CACHE if cache is None else cache
    cached = store.get(base_url, token)
    if cached is not None:
        return cached
    identity = _fetch_identity(base_url, token)
    if identity is not None:
        store.put(base_url, token, identity)
    return identity


def verify_web_bearer(base_url: str, token: str) -> bool:
    """Boolean form kept for callers that only need "is this a real session"."""
    return verify_web_identity(base_url, token) is not None


def reset_cache() -> None:
    """Test hook: drop every cached identity."""
    _CACHE.clear()
