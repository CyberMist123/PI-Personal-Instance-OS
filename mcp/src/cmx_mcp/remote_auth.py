from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken


READ_SCOPE = "cmx:read"
SOCIAL_SCOPE = "cmx:social"
KNOWN_SCOPES = frozenset({READ_SCOPE, SOCIAL_SCOPE})
AUTHORIZATION_CODE_TTL = 300
ACCESS_TOKEN_TTL = 3600
REFRESH_TOKEN_TTL = 30 * 86400
PENDING_LIMIT = 64
CLIENT_LIMIT = 100


class CmxRefreshToken(RefreshToken):
    resource: str
    family_id: str


@dataclass(frozen=True, slots=True)
class PendingAuthorization:
    request_id: str
    client_id: str
    client_name: str
    bot_id: str
    resource: str
    scopes: tuple[str, ...]
    state: str | None
    code_challenge: str
    redirect_uri: str
    redirect_uri_provided_explicitly: bool
    expires_at: float


class OAuthStore:
    """Private SQLite storage for MCP OAuth metadata and hashed grants."""

    def __init__(self, path: Path):
        self.path = path

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def initialize(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS mcp_oauth_clients (
                    client_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS mcp_oauth_codes (
                    code_hash TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    bot_id TEXT NOT NULL,
                    resource TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    created_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS mcp_oauth_tokens (
                    token_hash TEXT PRIMARY KEY,
                    token_kind TEXT NOT NULL CHECK(token_kind IN ('access','refresh')),
                    family_id TEXT NOT NULL,
                    client_id TEXT NOT NULL,
                    bot_id TEXT NOT NULL,
                    resource TEXT NOT NULL,
                    scopes_json TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    created_at INTEGER NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_mcp_oauth_family
                    ON mcp_oauth_tokens(family_id);
                CREATE INDEX IF NOT EXISTS idx_mcp_oauth_expiry
                    ON mcp_oauth_tokens(expires_at);
                """
            )
        self.cleanup()

    def cleanup(self) -> None:
        now = int(time.time())
        with self.connect() as db:
            db.execute("DELETE FROM mcp_oauth_codes WHERE expires_at < ?", (now,))
            db.execute("DELETE FROM mcp_oauth_tokens WHERE expires_at < ?", (now,))

    def client_count(self) -> int:
        with self.connect() as db:
            return int(db.execute("SELECT COUNT(*) FROM mcp_oauth_clients").fetchone()[0])

    def save_client(self, client: OAuthClientInformationFull) -> None:
        client_id = str(client.client_id or "")
        if not client_id:
            raise RegistrationError("invalid_client_metadata", "client_id is required")
        now = int(time.time())
        payload = client.model_dump_json(exclude_none=True)
        if len(payload.encode("utf-8")) > 16384:
            raise RegistrationError("invalid_client_metadata", "client metadata is too large")
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO mcp_oauth_clients(client_id,payload_json,created_at,updated_at)
                VALUES(?,?,?,?)
                ON CONFLICT(client_id) DO UPDATE SET
                    payload_json=excluded.payload_json,
                    updated_at=excluded.updated_at
                """,
                (client_id, payload, now, now),
            )

    def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT payload_json FROM mcp_oauth_clients WHERE client_id=?",
                (client_id,),
            ).fetchone()
        if row is None:
            return None
        return OAuthClientInformationFull.model_validate_json(row["payload_json"])

    def save_code(self, code: AuthorizationCode) -> None:
        payload = code.model_dump(mode="json", exclude={"code"})
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO mcp_oauth_codes(
                    code_hash,client_id,bot_id,resource,payload_json,expires_at,created_at
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (
                    _token_hash(code.code),
                    code.client_id,
                    str(code.subject or ""),
                    str(code.resource or ""),
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    int(code.expires_at),
                    int(time.time()),
                ),
            )

    def load_code(self, raw_code: str) -> AuthorizationCode | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT payload_json,expires_at FROM mcp_oauth_codes WHERE code_hash=?",
                (_token_hash(raw_code),),
            ).fetchone()
        if row is None or int(row["expires_at"]) < int(time.time()):
            return None
        payload = json.loads(row["payload_json"])
        payload["code"] = raw_code
        return AuthorizationCode.model_validate(payload)

    def consume_code(self, raw_code: str, client_id: str) -> bool:
        with self.connect() as db:
            cursor = db.execute(
                "DELETE FROM mcp_oauth_codes WHERE code_hash=? AND client_id=?",
                (_token_hash(raw_code), client_id),
            )
        return cursor.rowcount == 1

    def save_token_pair(
        self,
        *,
        access_token: str,
        refresh_token: str,
        family_id: str,
        client_id: str,
        bot_id: str,
        resource: str,
        scopes: list[str],
    ) -> None:
        now = int(time.time())
        scopes_json = json.dumps(scopes, separators=(",", ":"))
        with self.connect() as db:
            db.executemany(
                """
                INSERT INTO mcp_oauth_tokens(
                    token_hash,token_kind,family_id,client_id,bot_id,resource,
                    scopes_json,expires_at,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                [
                    (
                        _token_hash(access_token),
                        "access",
                        family_id,
                        client_id,
                        bot_id,
                        resource,
                        scopes_json,
                        now + ACCESS_TOKEN_TTL,
                        now,
                    ),
                    (
                        _token_hash(refresh_token),
                        "refresh",
                        family_id,
                        client_id,
                        bot_id,
                        resource,
                        scopes_json,
                        now + REFRESH_TOKEN_TTL,
                        now,
                    ),
                ],
            )

    def load_token(self, raw_token: str, kind: str) -> sqlite3.Row | None:
        with self.connect() as db:
            row = db.execute(
                """
                SELECT * FROM mcp_oauth_tokens
                WHERE token_hash=? AND token_kind=? AND expires_at>=?
                """,
                (_token_hash(raw_token), kind, int(time.time())),
            ).fetchone()
        return row

    def revoke_family_for_token(self, raw_token: str) -> None:
        token_hash = _token_hash(raw_token)
        with self.connect() as db:
            row = db.execute(
                "SELECT family_id FROM mcp_oauth_tokens WHERE token_hash=?",
                (token_hash,),
            ).fetchone()
            if row:
                db.execute(
                    "DELETE FROM mcp_oauth_tokens WHERE family_id=?",
                    (row["family_id"],),
                )

    def rotate_family(self, family_id: str) -> bool:
        with self.connect() as db:
            cursor = db.execute(
                "DELETE FROM mcp_oauth_tokens WHERE family_id=? AND token_kind='refresh'",
                (family_id,),
            )
            if cursor.rowcount != 1:
                return False
            db.execute("DELETE FROM mcp_oauth_tokens WHERE family_id=?", (family_id,))
        return True


class CmxOAuthProvider:
    """OAuth 2.1 provider with local-only owner approval and per-bot binding."""

    def __init__(
        self,
        *,
        store: OAuthStore,
        approval_origin: str,
        resource_to_bot: Callable[[str], str | None],
        bot_is_enabled: Callable[[str], bool],
    ) -> None:
        self.store = store
        self.approval_origin = approval_origin.rstrip("/")
        self.resource_to_bot = resource_to_bot
        self.bot_is_enabled = bot_is_enabled
        self._pending: OrderedDict[str, PendingAuthorization] = OrderedDict()
        self._pending_lock = threading.RLock()

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self.store.get_client(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        redirect_uris = list(client_info.redirect_uris or [])
        if not redirect_uris or len(redirect_uris) > 8:
            raise RegistrationError(
                "invalid_redirect_uri", "between one and eight redirect URIs are required"
            )
        for raw in redirect_uris:
            if not _safe_redirect_uri(str(raw)):
                raise RegistrationError(
                    "invalid_redirect_uri",
                    "redirect URIs must use HTTPS, or loopback HTTP for local clients",
                )
        if self.store.client_count() >= CLIENT_LIMIT:
            raise RegistrationError(
                "invalid_client_metadata", "the private MCP client registry is full"
            )
        self.store.save_client(client_info)

    async def authorize(
        self,
        client: OAuthClientInformationFull,
        params: AuthorizationParams,
    ) -> str:
        resource = str(params.resource or "").rstrip("/")
        bot_id = self.resource_to_bot(resource)
        if not bot_id:
            raise AuthorizeError("invalid_request", "resource must name one CMX resident MCP URL")
        if not self.bot_is_enabled(bot_id):
            raise AuthorizeError("access_denied", "the requested CMX resident is unavailable")
        try:
            scopes = normalize_scopes(params.scopes or [READ_SCOPE])
        except ValueError as exc:
            raise AuthorizeError("invalid_scope", str(exc)) from exc

        pending_id = secrets.token_urlsafe(32)
        pending = PendingAuthorization(
            request_id=pending_id,
            client_id=str(client.client_id or ""),
            client_name=str(client.client_name or "MCP client")[:100],
            bot_id=bot_id,
            resource=resource,
            scopes=scopes,
            state=params.state,
            code_challenge=params.code_challenge,
            redirect_uri=str(params.redirect_uri),
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            expires_at=time.time() + AUTHORIZATION_CODE_TTL,
        )
        with self._pending_lock:
            self._cleanup_pending_locked()
            while len(self._pending) >= PENDING_LIMIT:
                self._pending.popitem(last=False)
            self._pending[pending_id] = pending
        return f"{self.approval_origin}/oauth/approve?request={pending_id}"

    def pending(self, request_id: str) -> PendingAuthorization | None:
        with self._pending_lock:
            self._cleanup_pending_locked()
            return self._pending.get(request_id)

    def complete(self, request_id: str, *, approved: bool) -> str:
        with self._pending_lock:
            self._cleanup_pending_locked()
            pending = self._pending.pop(request_id, None)
        if pending is None:
            raise RuntimeError("This authorization request expired or was already used")
        if not approved:
            return construct_redirect_uri(
                pending.redirect_uri,
                error="access_denied",
                error_description="The CMX owner denied this connection",
                state=pending.state,
            )

        raw_code = secrets.token_urlsafe(32)
        code = AuthorizationCode(
            code=raw_code,
            scopes=list(pending.scopes),
            expires_at=time.time() + AUTHORIZATION_CODE_TTL,
            client_id=pending.client_id,
            code_challenge=pending.code_challenge,
            redirect_uri=pending.redirect_uri,
            redirect_uri_provided_explicitly=pending.redirect_uri_provided_explicitly,
            resource=pending.resource,
            subject=pending.bot_id,
        )
        self.store.save_code(code)
        return construct_redirect_uri(
            pending.redirect_uri,
            code=raw_code,
            state=pending.state,
        )

    async def load_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: str,
    ) -> AuthorizationCode | None:
        code = self.store.load_code(authorization_code)
        if code is None or code.client_id != str(client.client_id or ""):
            return None
        return code

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        client_id = str(client.client_id or "")
        if not self.store.consume_code(authorization_code.code, client_id):
            raise TokenError("invalid_grant", "authorization code was already used")
        return self._issue_pair(
            client_id=client_id,
            bot_id=str(authorization_code.subject or ""),
            resource=str(authorization_code.resource or ""),
            scopes=list(authorization_code.scopes),
        )

    async def load_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: str,
    ) -> CmxRefreshToken | None:
        row = self.store.load_token(refresh_token, "refresh")
        if row is None or row["client_id"] != str(client.client_id or ""):
            return None
        return CmxRefreshToken(
            token=refresh_token,
            client_id=row["client_id"],
            scopes=json.loads(row["scopes_json"]),
            expires_at=row["expires_at"],
            subject=row["bot_id"],
            resource=row["resource"],
            family_id=row["family_id"],
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: CmxRefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        requested = normalize_scopes(scopes)
        original = normalize_scopes(refresh_token.scopes)
        if not set(requested).issubset(original):
            raise TokenError("invalid_scope", "refresh scope cannot exceed the original grant")
        if not self.bot_is_enabled(str(refresh_token.subject or "")):
            raise TokenError("invalid_grant", "the CMX resident is unavailable")
        if not self.store.rotate_family(refresh_token.family_id):
            raise TokenError("invalid_grant", "refresh token was already used or revoked")
        return self._issue_pair(
            client_id=str(client.client_id or ""),
            bot_id=str(refresh_token.subject or ""),
            resource=refresh_token.resource,
            scopes=requested,
            family_id=refresh_token.family_id,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        row = self.store.load_token(token, "access")
        if row is None or not self.bot_is_enabled(row["bot_id"]):
            return None
        return AccessToken(
            token=token,
            client_id=row["client_id"],
            scopes=json.loads(row["scopes_json"]),
            expires_at=row["expires_at"],
            resource=row["resource"],
            subject=row["bot_id"],
            claims={"family_id": row["family_id"]},
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        self.store.revoke_family_for_token(token.token)

    def _issue_pair(
        self,
        *,
        client_id: str,
        bot_id: str,
        resource: str,
        scopes: list[str],
        family_id: str | None = None,
    ) -> OAuthToken:
        scopes = normalize_scopes(scopes)
        if not bot_id or not self.bot_is_enabled(bot_id):
            raise TokenError("invalid_grant", "the CMX resident is unavailable")
        if self.resource_to_bot(resource) != bot_id:
            raise TokenError("invalid_grant", "the grant resource no longer matches the resident")
        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(40)
        self.store.save_token_pair(
            access_token=access,
            refresh_token=refresh,
            family_id=family_id or secrets.token_urlsafe(18),
            client_id=client_id,
            bot_id=bot_id,
            resource=resource,
            scopes=scopes,
        )
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL,
            scope=" ".join(scopes),
            refresh_token=refresh,
        )

    def _cleanup_pending_locked(self) -> None:
        now = time.time()
        for key, value in list(self._pending.items()):
            if value.expires_at < now:
                self._pending.pop(key, None)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def normalize_scopes(scopes: list[str] | tuple[str, ...] | str) -> list[str]:
    values = scopes.split() if isinstance(scopes, str) else [str(value) for value in scopes]
    result = sorted(set(value for value in values if value))
    unknown = [value for value in result if value not in KNOWN_SCOPES]
    if unknown:
        raise ValueError("unknown scope")
    return result


def require_scope(granted_scopes: list[str] | tuple[str, ...], required_scope: str) -> None:
    if required_scope not in normalize_scopes(granted_scopes):
        raise PermissionError("insufficient_scope")


def _safe_redirect_uri(value: str) -> bool:
    if len(value) > 2048:
        return False
    parsed = urlparse(value)
    if parsed.fragment or parsed.username or parsed.password or not parsed.hostname:
        return False
    if parsed.scheme == "https":
        return True
    return parsed.scheme == "http" and parsed.hostname.lower() in {
        "127.0.0.1",
        "localhost",
        "::1",
    }
