from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace

import pytest
from mcp.server.auth.provider import AuthorizationParams, AuthorizeError
from mcp.shared.auth import OAuthClientInformationFull
from starlette.testclient import TestClient

from cmx_mcp.config import Paths
from cmx_mcp.db import Database
from cmx_mcp.remote_auth import CmxOAuthProvider, OAuthStore, READ_SCOPE, SOCIAL_SCOPE
from cmx_mcp.remote import _consent_copy, create_remote_app


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


@pytest.mark.parametrize("profile", ["reader", "social"])
def test_discovery_uses_one_canonical_issuer_for_every_bot(tmp_path, monkeypatch, profile):
    paths = Paths(
        home=tmp_path / "mcp",
        runtime=tmp_path / "mcp" / "runtime",
        database=tmp_path / "mcp" / "runtime" / "cmx.sqlite3",
        secrets=tmp_path / "mcp" / "runtime" / "secrets",
        logs=tmp_path / "mcp" / "runtime" / "logs",
    )
    database = Database(paths.database)
    database.initialize()
    bot_ids = ["gpt", "second-bot"]
    for bot_id in bot_ids:
        database.upsert_bot(
            bot_id=bot_id,
            display_name=bot_id,
            profile="resident",
            media_root=tmp_path / "media" / bot_id,
            token_ref=f"{bot_id}.token.dpapi",
            default_audience="residents",
            allow_public=False,
            remote_profile=profile,
        )

    class FakeRuntime:
        def __init__(self, bot_id):
            self.bot = database.get_bot(bot_id)
            self.settings = SimpleNamespace(max_items=30)
            self.client = SimpleNamespace(close=lambda: None)
            self.db = database

        def close(self):
            self.client.close()

    monkeypatch.setenv("WEB_DOMAIN", "pi.example")
    monkeypatch.setattr("cmx_mcp.remote.Runtime", FakeRuntime)
    app = create_remote_app(paths)

    with TestClient(app, base_url="https://pi.example") as client:
        authorization_metadata = client.get("/.well-known/oauth-authorization-server")
        assert authorization_metadata.status_code == 200
        assert authorization_metadata.headers["cache-control"] == "no-store"
        issuer = authorization_metadata.json()["issuer"]
        assert issuer == "https://pi.example/"

        for bot_id in bot_ids:
            protected_metadata = client.get(
                f"/.well-known/oauth-protected-resource/mcp/{bot_id}"
            )
            assert protected_metadata.status_code == 200
            document = protected_metadata.json()
            assert document["resource"] == f"https://pi.example/mcp/{bot_id}"
            assert document["authorization_servers"][0] == issuer
            expected_scopes = [READ_SCOPE] if profile == "reader" else [READ_SCOPE, SOCIAL_SCOPE]
            assert document["scopes_supported"] == expected_scopes
            assert protected_metadata.headers["cache-control"] == "no-store"


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
        # Store-level check: the rotated-away refresh token is no longer an
        # active refresh row. (Presenting it through the provider is reuse and
        # revokes the family; that behavior has its own test below.)
        assert store.load_token(tokens.refresh_token, "refresh") is None
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


def test_refresh_token_reuse_revokes_entire_family(tmp_path):
    async def scenario():
        _store, provider = _provider(tmp_path)
        client = _client()
        await provider.register_client(client)
        approval_url = await provider.authorize(client, _params())
        callback = provider.complete(approval_url.rsplit("=", 1)[1], approved=True)
        raw_code = callback.split("code=", 1)[1].split("&", 1)[0]
        code = await provider.load_authorization_code(client, raw_code)
        tokens = await provider.exchange_authorization_code(client, code)
        refresh = await provider.load_refresh_token(client, tokens.refresh_token)
        rotated = await provider.exchange_refresh_token(client, refresh, [READ_SCOPE])
        assert await provider.load_access_token(rotated.access_token) is not None

        # Replaying the rotated-away refresh token is reuse: the whole family
        # dies, including the freshly rotated pair.
        assert await provider.load_refresh_token(client, tokens.refresh_token) is None
        assert await provider.load_access_token(rotated.access_token) is None
        assert await provider.load_refresh_token(client, rotated.refresh_token) is None

    asyncio.run(scenario())


def test_authorize_rejects_scopes_without_read(tmp_path):
    async def scenario():
        _store, provider = _provider(tmp_path)
        client = _client()
        await provider.register_client(client)
        with pytest.raises(AuthorizeError):
            await provider.authorize(client, _params(scopes=[SOCIAL_SCOPE]))

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


def test_consent_copy_matches_requested_scope_and_profile():
    reader = type("Bot", (), {"remote_profile": "reader", "remote_notifications": False,
                               "remote_polls": False, "remote_boosts": False})()
    title, body = _consent_copy([READ_SCOPE], reader)
    assert "\u53ea\u8bfb" in title
    assert "\u4e0d\u80fd\u6267\u884c\u793e\u4ea4\u5199\u64cd\u4f5c" in body
    social = type("Bot", (), {"remote_profile": "social", "remote_notifications": False,
                               "remote_polls": False, "remote_boosts": False})()
    title, body = _consent_copy([READ_SCOPE, SOCIAL_SCOPE], social)
    assert "\u793e\u4ea4" in title
    assert "\u53d1\u5e16" in body
    assert "\u6295\u7968" not in body
    assert "\u8f6c\u53d1" not in body
    assert "Phase 0" not in body

    polls = type("Bot", (), {"remote_profile": "social", "remote_notifications": False,
                              "remote_polls": True, "remote_boosts": False})()
    _, body = _consent_copy([READ_SCOPE, SOCIAL_SCOPE], polls)
    assert "\u521b\u5efa\u6295\u7968\u548c\u53c2\u4e0e\u6295\u7968" in body
    assert "\u8f6c\u53d1\u548c\u53d6\u6d88\u8f6c\u53d1" not in body

    boosts = type("Bot", (), {"remote_profile": "social", "remote_notifications": False,
                               "remote_polls": False, "remote_boosts": True})()
    _, body = _consent_copy([READ_SCOPE, SOCIAL_SCOPE], boosts)
    assert "\u8f6c\u53d1\u548c\u53d6\u6d88\u8f6c\u53d1" in body
    assert "\u521b\u5efa\u6295\u7968\u548c\u53c2\u4e0e\u6295\u7968" not in body

    plus = type("Bot", (), {"remote_profile": "social_plus", "remote_notifications": True,
                             "remote_polls": True, "remote_boosts": True})()
    _, body = _consent_copy([READ_SCOPE, SOCIAL_SCOPE], plus)
    assert "\u521b\u5efa\u6295\u7968\u548c\u53c2\u4e0e\u6295\u7968" in body
    assert "\u8f6c\u53d1\u548c\u53d6\u6d88\u8f6c\u53d1" in body
    assert "\u53ea\u8bfb\u67e5\u770b\u901a\u77e5" in body
    assert "\u4e0d\u4f1a\u6267\u884c\u6e05\u9664\u3001\u6807\u8bb0\u5df2\u8bfb\u6216\u5176\u4ed6\u901a\u77e5\u5199\u64cd\u4f5c" in body

    for polls in (False, True):
        for boosts in (False, True):
            for notifications in (False, True):
                profile = "social_plus" if notifications else "social"
                bot = type("Bot", (), {"remote_profile": profile,
                                       "remote_notifications": notifications,
                                       "remote_polls": polls,
                                       "remote_boosts": boosts})()
                _, combination = _consent_copy([READ_SCOPE, SOCIAL_SCOPE], bot)
                assert ("\u521b\u5efa\u6295\u7968\u548c\u53c2\u4e0e\u6295\u7968" in combination) is polls
                assert ("\u8f6c\u53d1\u548c\u53d6\u6d88\u8f6c\u53d1" in combination) is boosts
                assert ("\u53ea\u8bfb\u67e5\u770b\u901a\u77e5" in combination) is notifications
