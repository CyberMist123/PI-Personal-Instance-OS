from __future__ import annotations

import asyncio
import sqlite3

import pytest
from mcp.server.auth.provider import AuthorizationParams, AuthorizeError
from mcp.shared.auth import OAuthClientInformationFull

from cmx_mcp.remote_auth import CmxOAuthProvider, OAuthStore, READ_SCOPE, SOCIAL_SCOPE


def _client() -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id="client-1",
        client_id_issued_at=1,
        redirect_uris=["http://127.0.0.1:9999/callback"],
        token_endpoint_auth_method="none",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope=READ_SCOPE,
        client_name="test client",
    )


def _provider(tmp_path, *, enabled=lambda bot_id: bot_id == "gpt"):
    store = OAuthStore(tmp_path / "oauth.sqlite3")
    store.initialize()
    provider = CmxOAuthProvider(
        store=store,
        approval_origin="http://127.0.0.1:8766",
        resource_to_bot=lambda resource: (
            "gpt" if resource.rstrip("/") == "https://pi.example/mcp/gpt" else None
        ),
        bot_is_enabled=enabled,
    )
    return store, provider


def _params(resource: str = "https://pi.example/mcp/gpt", scopes=None) -> AuthorizationParams:
    return AuthorizationParams(
        state="client-state",
        scopes=scopes or [READ_SCOPE],
        code_challenge="A" * 43,
        redirect_uri="http://127.0.0.1:9999/callback",
        redirect_uri_provided_explicitly=True,
        resource=resource,
    )


def test_refresh_scopes_are_a_subset_of_original_grant(tmp_path):
    async def scenario():
        _store, provider = _provider(tmp_path)
        client = _client()
        await provider.register_client(client)
        approval_url = await provider.authorize(client, _params(scopes=[READ_SCOPE, SOCIAL_SCOPE]))
        callback = provider.complete(approval_url.rsplit("=", 1)[1], approved=True)
        code = await provider.load_authorization_code(client, callback.split("code=", 1)[1].split("&", 1)[0])
        tokens = await provider.exchange_authorization_code(client, code)
        refresh = await provider.load_refresh_token(client, tokens.refresh_token)
        assert refresh is not None
        reduced = await provider.exchange_refresh_token(client, refresh, [SOCIAL_SCOPE, READ_SCOPE, READ_SCOPE])
        assert reduced.scope == "cmx:read cmx:social"

        refresh = await provider.load_refresh_token(client, reduced.refresh_token)
        assert refresh is not None
        with pytest.raises(Exception):
            await provider.exchange_refresh_token(client, refresh, ["cmx:read", "cmx:unknown"])

    asyncio.run(scenario())


def test_oauth_code_refresh_revoke_and_hashed_storage(tmp_path):
    async def scenario():
        store, provider = _provider(tmp_path)
        client = _client()
        await provider.register_client(client)
        assert (await provider.get_client("client-1")).client_name == "test client"

        approval_url = await provider.authorize(client, _params())
        request_id = approval_url.rsplit("=", 1)[1]
        callback = provider.complete(request_id, approved=True)
        raw_code = callback.split("code=", 1)[1].split("&", 1)[0]
        code = await provider.load_authorization_code(client, raw_code)
        assert code is not None
        assert code.subject == "gpt"
        assert code.resource == "https://pi.example/mcp/gpt"

        tokens = await provider.exchange_authorization_code(client, code)
        access = await provider.load_access_token(tokens.access_token)
        assert access is not None
        assert access.subject == "gpt"
        assert access.scopes == [READ_SCOPE]

        refresh = await provider.load_refresh_token(client, tokens.refresh_token)
        assert refresh is not None

        # Grants are SQLite-backed, so a service restart does not disconnect clients.
        _restart_store, restarted = _provider(tmp_path)
        assert await restarted.load_access_token(tokens.access_token) is not None
        refresh = await restarted.load_refresh_token(client, tokens.refresh_token)
        assert refresh is not None
        rotated = await restarted.exchange_refresh_token(client, refresh, [READ_SCOPE])
        assert await provider.load_refresh_token(client, tokens.refresh_token) is None
        assert await provider.load_access_token(tokens.access_token) is None
        assert await restarted.load_access_token(rotated.access_token) is not None

        rotated_access = await restarted.load_access_token(rotated.access_token)
        await restarted.revoke_token(rotated_access)
        assert await provider.load_access_token(rotated.access_token) is None
        assert await provider.load_refresh_token(client, rotated.refresh_token) is None

        with sqlite3.connect(store.path) as db:
            stored = " ".join(
                str(value)
                for row in db.execute("SELECT * FROM mcp_oauth_tokens").fetchall()
                for value in row
            )
        assert tokens.access_token not in stored
        assert tokens.refresh_token not in stored
        assert raw_code not in stored

    asyncio.run(scenario())


def test_disabled_resident_invalidates_existing_access_token(tmp_path):
    async def scenario():
        enabled = {"value": True}
        _store, provider = _provider(
            tmp_path,
            enabled=lambda bot_id: bot_id == "gpt" and enabled["value"],
        )
        client = _client()
        await provider.register_client(client)
        approval_url = await provider.authorize(client, _params())
        callback = provider.complete(approval_url.rsplit("=", 1)[1], approved=True)
        raw_code = callback.split("code=", 1)[1].split("&", 1)[0]
        code = await provider.load_authorization_code(client, raw_code)
        tokens = await provider.exchange_authorization_code(client, code)
        assert await provider.load_access_token(tokens.access_token) is not None

        enabled["value"] = False
        assert await provider.load_access_token(tokens.access_token) is None

    asyncio.run(scenario())


def test_resource_and_redirect_boundaries(tmp_path):
    async def scenario():
        _store, provider = _provider(tmp_path)
        client = _client()
        await provider.register_client(client)
        with pytest.raises(AuthorizeError):
            await provider.authorize(client, _params("https://pi.example/mcp/fable"))

        unsafe = _client().model_copy(
            update={"client_id": "bad", "redirect_uris": ["http://evil.example/callback"]}
        )
        with pytest.raises(Exception):
            await provider.register_client(unsafe)

    asyncio.run(scenario())
