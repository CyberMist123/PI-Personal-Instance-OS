from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace

import pytest
from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull
from starlette.testclient import TestClient

from cmx_mcp.config import Paths
from cmx_mcp.db import Database
from cmx_mcp.remote import create_remote_app
from cmx_mcp.remote_auth import (
    INVITE_ATTEMPT_LIMIT,
    CmxOAuthProvider,
    OAuthStore,
    READ_SCOPE,
    SOCIAL_SCOPE,
)


def _client() -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id="client-1",
        client_id_issued_at=1,
        redirect_uris=["http://127.0.0.1:9999/callback"],
        token_endpoint_auth_method="none",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope=READ_SCOPE,
        client_name="invite test client",
    )


def _provider(tmp_path, remote_profile: str = "social_plus"):
    store = OAuthStore(tmp_path / "oauth.sqlite3")
    store.initialize()
    provider = CmxOAuthProvider(
        store=store,
        approval_origin="http://127.0.0.1:8766",
        resource_to_bot=lambda resource: (
            "gpt" if resource.rstrip("/") == "https://pi.example/mcp/gpt" else None
        ),
        bot_is_enabled=lambda bot_id: bot_id == "gpt",
        bot_remote_profile=lambda _bot_id: remote_profile,
        redeem_origin="https://pi.example",
    )
    return store, provider


def _params(scopes=None) -> AuthorizationParams:
    return AuthorizationParams(
        state="client-state",
        scopes=scopes or [READ_SCOPE],
        code_challenge="A" * 43,
        redirect_uri="http://127.0.0.1:9999/callback",
        redirect_uri_provided_explicitly=True,
        resource="https://pi.example/mcp/gpt",
    )


async def _pending_request(provider, client, scopes=None) -> str:
    url = await provider.authorize(client, _params(scopes=scopes))
    assert "/oauth/invite?request=" in url
    return url.rsplit("=", 1)[1]


def test_invite_redeem_approves_pending_and_is_single_use(tmp_path):
    async def scenario():
        store, provider = _provider(tmp_path)
        client = _client()
        await provider.register_client(client)

        request_id = await _pending_request(provider, client)
        code = store.create_invite(bot_id="gpt", scopes=[READ_SCOPE])
        target = provider.redeem(request_id, code)
        assert target.startswith("http://127.0.0.1:9999/callback")
        assert "code=" in target and "state=client-state" in target

        raw_code = target.split("code=", 1)[1].split("&", 1)[0]
        auth_code = await provider.load_authorization_code(client, raw_code)
        assert auth_code is not None and auth_code.subject == "gpt"
        tokens = await provider.exchange_authorization_code(client, auth_code)
        access = await provider.load_access_token(tokens.access_token)
        assert access is not None and access.subject == "gpt"

        second = await _pending_request(provider, client)
        with pytest.raises(ValueError, match="邀请码"):
            provider.redeem(second, code)

    asyncio.run(scenario())


def test_the_invite_defines_the_grant_not_the_request(tmp_path):
    """Owner-minted invite wins over whatever scope the client asked for."""

    async def scenario():
        store, provider = _provider(tmp_path)
        client = _client()
        await provider.register_client(client)

        # Asking for more than the invite covers still lands on the invite.
        request_id = await _pending_request(provider, client, scopes=[READ_SCOPE, SOCIAL_SCOPE])
        read_only = store.create_invite(bot_id="gpt", scopes=[READ_SCOPE])
        target = provider.redeem(request_id, read_only)
        raw_code = target.split("code=", 1)[1].split("&", 1)[0]
        auth_code = await provider.load_authorization_code(client, raw_code)
        assert list(auth_code.scopes) == [READ_SCOPE]

        # And asking for less does too: this is the ChatGPT case. It sends
        # scope=cmx:read whatever we advertise, so honouring the request would
        # make a social connector impossible to create.
        request_id = await _pending_request(provider, client, scopes=[READ_SCOPE])
        social = store.create_invite(bot_id="gpt", scopes=[READ_SCOPE, SOCIAL_SCOPE])
        target = provider.redeem(request_id, social)
        raw_code = target.split("code=", 1)[1].split("&", 1)[0]
        auth_code = await provider.load_authorization_code(client, raw_code)
        assert sorted(auth_code.scopes) == [READ_SCOPE, SOCIAL_SCOPE]

    asyncio.run(scenario())


def test_reader_resident_narrows_an_explicit_social_request(tmp_path):
    """Clients replay our registration default, so cmx:social must not 400."""

    async def scenario():
        store, provider = _provider(tmp_path, remote_profile="reader")
        client = _client()
        await provider.register_client(client)

        request_id = await _pending_request(provider, client, scopes=[READ_SCOPE, SOCIAL_SCOPE])
        assert list(provider.pending(request_id).scopes) == [READ_SCOPE]
        invite = store.create_invite(bot_id="gpt", scopes=[READ_SCOPE])
        target = provider.redeem(request_id, invite)
        raw_code = target.split("code=", 1)[1].split("&", 1)[0]
        auth_code = await provider.load_authorization_code(client, raw_code)
        assert list(auth_code.scopes) == [READ_SCOPE]

    asyncio.run(scenario())


def test_expired_and_wrong_bot_invites_are_invalid(tmp_path):
    async def scenario():
        store, provider = _provider(tmp_path)
        client = _client()
        await provider.register_client(client)

        request_id = await _pending_request(provider, client)
        expired = store.create_invite(bot_id="gpt", scopes=[READ_SCOPE])
        with sqlite3.connect(store.path) as db:
            db.execute("UPDATE mcp_oauth_invites SET expires_at=1")
        with pytest.raises(ValueError, match="邀请码"):
            provider.redeem(request_id, expired)

        other = store.create_invite(bot_id="other", scopes=[READ_SCOPE])
        with pytest.raises(ValueError, match="邀请码"):
            provider.redeem(request_id, other)

    asyncio.run(scenario())


def test_scopeless_client_scopes_follow_the_invite(tmp_path):
    """ChatGPT-style clients send no scope: the minted invite defines the grant."""

    def scopeless_params(state: str) -> AuthorizationParams:
        return AuthorizationParams(
            state=state,
            scopes=None,
            code_challenge="A" * 43,
            redirect_uri="http://127.0.0.1:9999/callback",
            redirect_uri_provided_explicitly=True,
            resource="https://pi.example/mcp/gpt",
        )

    async def scenario():
        store, provider = _provider(tmp_path)
        client = _client()
        await provider.register_client(client)

        url = await provider.authorize(client, scopeless_params("s1"))
        request_id = url.rsplit("=", 1)[1]
        social_invite = store.create_invite(bot_id="gpt", scopes=[READ_SCOPE, SOCIAL_SCOPE])
        target = provider.redeem(request_id, social_invite)
        raw_code = target.split("code=", 1)[1].split("&", 1)[0]
        auth_code = await provider.load_authorization_code(client, raw_code)
        assert sorted(auth_code.scopes) == [READ_SCOPE, SOCIAL_SCOPE]
        tokens = await provider.exchange_authorization_code(client, auth_code)
        access = await provider.load_access_token(tokens.access_token)
        assert sorted(access.scopes) == [READ_SCOPE, SOCIAL_SCOPE]

        # A read-only invite for a scope-less client grants read; it is not a
        # ceiling violation.
        url = await provider.authorize(client, scopeless_params("s2"))
        request_id = url.rsplit("=", 1)[1]
        read_invite = store.create_invite(bot_id="gpt", scopes=[READ_SCOPE])
        target = provider.redeem(request_id, read_invite)
        raw_code = target.split("code=", 1)[1].split("&", 1)[0]
        auth_code = await provider.load_authorization_code(client, raw_code)
        assert list(auth_code.scopes) == [READ_SCOPE]

    asyncio.run(scenario())


def test_attempt_limit_drops_the_pending_request(tmp_path):
    async def scenario():
        _store, provider = _provider(tmp_path)
        client = _client()
        await provider.register_client(client)

        request_id = await _pending_request(provider, client)
        for _ in range(INVITE_ATTEMPT_LIMIT):
            with pytest.raises(ValueError):
                provider.redeem(request_id, "cmx-wrong")
        with pytest.raises(RuntimeError, match="Too many"):
            provider.redeem(request_id, "cmx-wrong")
        assert provider.pending(request_id) is None

    asyncio.run(scenario())


def test_invites_store_only_hashes(tmp_path):
    store, _provider_unused = _provider(tmp_path)
    code = store.create_invite(bot_id="gpt", scopes=[READ_SCOPE])
    with sqlite3.connect(store.path) as db:
        dump = " ".join(
            str(value)
            for row in db.execute("SELECT * FROM mcp_oauth_invites").fetchall()
            for value in row
        )
    assert code not in dump
    listed = store.list_invites("gpt")
    assert listed and listed[0]["status"] == "active"
    assert store.revoke_invites("gpt") == 1


def test_confidential_client_full_flow_like_chatgpt(tmp_path, monkeypatch):
    """ChatGPT-shaped client: secret-based auth + PKCE must reach a token."""
    import base64
    import hashlib

    paths = Paths(
        home=tmp_path / "mcp",
        runtime=tmp_path / "mcp" / "runtime",
        database=tmp_path / "mcp" / "runtime" / "cmx.sqlite3",
        secrets=tmp_path / "mcp" / "runtime" / "secrets",
        logs=tmp_path / "mcp" / "runtime" / "logs",
    )
    database = Database(paths.database)
    database.initialize()
    database.upsert_bot(
        bot_id="gpt",
        display_name="GPT",
        profile="resident",
        media_root=tmp_path / "media",
        token_ref="gpt.token.dpapi",
        default_audience="residents",
        allow_public=False,
        remote_profile="social",
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

    verifier = "chatgpt-style-verifier-" + "v" * 24
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )

    with TestClient(app, base_url="https://pi.example") as client:
        registered = client.post(
            "/register",
            json={
                "redirect_uris": ["https://chatgpt.com/connector/oauth/x1"],
                "token_endpoint_auth_method": "client_secret_post",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "client_name": "ChatGPT",
            },
        )
        assert registered.status_code in {200, 201}
        payload = registered.json()
        assert payload.get("client_secret")
        # A zero/instant expiry here is exactly the bug that broke ChatGPT.
        assert not payload.get("client_secret_expires_at")
        # ChatGPT registers without asking for a scope and then replays this
        # value on every /authorize. Handing back read-only pinned its
        # connector to a read-only token no invite could widen.
        assert sorted(payload["scope"].split()) == [READ_SCOPE, SOCIAL_SCOPE]

        authorize = client.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": payload["client_id"],
                "redirect_uri": "https://chatgpt.com/connector/oauth/x1",
                "state": "gpt-state",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                # Verbatim from the nginx log of a real connector setup on
                # 2026-07-31: ChatGPT asks for cmx:read alone even though it
                # just registered with cmx:read cmx:social and both discovery
                # documents list the social scope.
                "scope": READ_SCOPE,
                "resource": "https://pi.example/mcp/gpt",
            },
            follow_redirects=False,
        )
        assert authorize.status_code in {302, 307}
        request_id = authorize.headers["location"].rsplit("=", 1)[1]

        invite_code = OAuthStore(paths.database).create_invite(
            bot_id="gpt", scopes=[READ_SCOPE, SOCIAL_SCOPE]
        )
        redeemed = client.post(
            "/oauth/invite",
            data={"request": request_id, "code": invite_code},
            follow_redirects=False,
        )
        assert redeemed.status_code == 303
        auth_code = redeemed.headers["location"].split("code=", 1)[1].split("&", 1)[0]

        tokens = client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": auth_code,
                "redirect_uri": "https://chatgpt.com/connector/oauth/x1",
                "client_id": payload["client_id"],
                "client_secret": payload["client_secret"],
                "code_verifier": verifier,
            },
        )
        assert tokens.status_code == 200, tokens.text
        body = tokens.json()
        assert body.get("access_token") and body.get("refresh_token")
        # The social invite must actually reach the token: this is the whole
        # reason the ChatGPT connector could only read.
        assert sorted(body["scope"].split()) == [READ_SCOPE, SOCIAL_SCOPE]


def test_public_invite_page_end_to_end(tmp_path, monkeypatch):
    paths = Paths(
        home=tmp_path / "mcp",
        runtime=tmp_path / "mcp" / "runtime",
        database=tmp_path / "mcp" / "runtime" / "cmx.sqlite3",
        secrets=tmp_path / "mcp" / "runtime" / "secrets",
        logs=tmp_path / "mcp" / "runtime" / "logs",
    )
    database = Database(paths.database)
    database.initialize()
    database.upsert_bot(
        bot_id="gpt",
        display_name="GPT",
        profile="resident",
        media_root=tmp_path / "media",
        token_ref="gpt.token.dpapi",
        default_audience="residents",
        allow_public=False,
        remote_profile="reader",
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
        registered = client.post(
            "/register",
            json={
                "redirect_uris": ["http://127.0.0.1:9999/callback"],
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "scope": READ_SCOPE,
                "client_name": "invite e2e",
            },
        )
        assert registered.status_code in {200, 201}
        client_id = registered.json()["client_id"]

        authorize = client.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": "http://127.0.0.1:9999/callback",
                "state": "st4te",
                "code_challenge": "A" * 43,
                "code_challenge_method": "S256",
                "scope": READ_SCOPE,
                "resource": "https://pi.example/mcp/gpt",
            },
            follow_redirects=False,
        )
        assert authorize.status_code in {302, 307}
        location = authorize.headers["location"]
        assert location.startswith("https://pi.example/oauth/invite?request=")
        request_id = location.rsplit("=", 1)[1]

        page = client.get(f"/oauth/invite?request={request_id}")
        assert page.status_code == 200
        assert "邀请码" in page.text
        assert page.headers["cache-control"] == "no-store"

        wrong = client.post(
            "/oauth/invite",
            data={"request": request_id, "code": "cmx-wrong"},
            follow_redirects=False,
        )
        assert wrong.status_code == 200
        assert "无效" in wrong.text

        invite_code = OAuthStore(paths.database).create_invite(
            bot_id="gpt", scopes=[READ_SCOPE]
        )
        redeemed = client.post(
            "/oauth/invite",
            data={"request": request_id, "code": invite_code},
            follow_redirects=False,
        )
        assert redeemed.status_code == 303
        target = redeemed.headers["location"]
        assert target.startswith("http://127.0.0.1:9999/callback")
        assert "code=" in target and "state=st4te" in target
