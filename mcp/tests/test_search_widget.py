from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from starlette.testclient import TestClient

from cmx_mcp.config import Paths
from cmx_mcp.db import Database
from cmx_mcp.remote import create_remote_app
from cmx_mcp.search_widget import SEARCH_WIDGET_JS, SEARCH_WIDGET_VERSION
from cmx_mcp.web_auth import WebIdentity


def _paths(tmp_path) -> Paths:
    return Paths(
        home=tmp_path / "mcp",
        runtime=tmp_path / "mcp" / "runtime",
        database=tmp_path / "mcp" / "runtime" / "cmx.sqlite3",
        secrets=tmp_path / "mcp" / "runtime" / "secrets",
        logs=tmp_path / "mcp" / "runtime" / "logs",
    )


def _app(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
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
    return create_remote_app(paths)


def test_search_js_is_served_as_a_plain_static_script(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    with TestClient(app, base_url="https://pi.example") as client:
        response = client.get("/files/search.js")

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/javascript")
    # no-cache, not max-age: a version bump has to reach the browser on the
    # next load, same rationale as voice.js.
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["etag"] == '"search-' + SEARCH_WIDGET_VERSION + '"'

    body = response.text
    assert SEARCH_WIDGET_VERSION in body
    assert "initial-state" in body
    assert "access_token" in body
    assert "__piSearchWidget" in body
    assert "/api/v2/search" in body
    assert "/files/search" in body
    assert "/api/v1/statuses" in body


def test_search_js_route_is_not_shadowed_by_the_filebox_download_route(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    with TestClient(app, base_url="https://pi.example") as client:
        assert client.get("/files/search.js").status_code == 200
        assert client.get("/files/gpt/nope/x.txt").status_code == 404
        # The literal /files/search route (server-side whole-instance search)
        # must keep answering independently of the new /files/search.js script
        # route; unauthenticated, it is a 401, not a 404 or a JS payload.
        assert client.get("/files/search").status_code == 401


def test_legacy_site_search_fails_closed_without_an_explicit_owner(tmp_path, monkeypatch):
    monkeypatch.delenv("CMX_SITE_SEARCH_OWNER_USERNAME", raising=False)
    monkeypatch.setattr(
        "cmx_mcp.remote.verify_web_identity",
        lambda *_args: WebIdentity(account_id="owner-id", acct="owner"),
    )
    app = _app(tmp_path, monkeypatch)

    with TestClient(app, base_url="https://pi.example") as client:
        response = client.get("/files/search?q=test", headers={"Authorization": "Bearer token"})

    assert response.status_code == 403
    assert response.json() == {"error": "owner_only"}


def test_legacy_site_search_rejects_other_real_accounts(tmp_path, monkeypatch):
    monkeypatch.setenv("CMX_SITE_SEARCH_OWNER_USERNAME", "owner")
    monkeypatch.setattr(
        "cmx_mcp.remote.verify_web_identity",
        lambda *_args: WebIdentity(account_id="other-id", acct="other"),
    )
    app = _app(tmp_path, monkeypatch)

    with TestClient(app, base_url="https://pi.example") as client:
        response = client.get("/files/search?q=test", headers={"Authorization": "Bearer token"})

    assert response.status_code == 403
    assert response.json() == {"error": "owner_only"}


def test_legacy_site_search_allows_only_the_configured_owner(tmp_path, monkeypatch):
    monkeypatch.setenv("CMX_SITE_SEARCH_OWNER_USERNAME", "owner")
    monkeypatch.setattr(
        "cmx_mcp.remote.verify_web_identity",
        lambda *_args: WebIdentity(account_id="owner-id", acct="owner"),
    )
    monkeypatch.setattr(
        "cmx_mcp.remote.search_site",
        lambda query, *, limit: [{"id": "1", "text": query, "limit": limit}],
    )
    app = _app(tmp_path, monkeypatch)

    with TestClient(app, base_url="https://pi.example") as client:
        response = client.get("/files/search?q=test&limit=7", headers={"Authorization": "Bearer token"})

    assert response.status_code == 200
    assert response.json()["items"] == [{"id": "1", "text": "test", "limit": 7}]


def test_widget_source_stays_backtick_free_and_bails_out_without_a_token() -> None:
    # Embedded into nginx config discussions, HTML and shell docs, same as the
    # voice widget: plain string concatenation only, never template literals.
    assert "`" not in SEARCH_WIDGET_JS
    assert "${" not in SEARCH_WIDGET_JS

    # Logged-out pages have no meta.access_token; the widget must never patch
    # window.fetch in that case.
    assert "access_token" in SEARCH_WIDGET_JS
    assert "if (!token) {" in SEARCH_WIDGET_JS
    assert "window.fetch = function" in SEARCH_WIDGET_JS

    assert SEARCH_WIDGET_VERSION == "1" and "search widget v1" in SEARCH_WIDGET_JS
    assert SEARCH_WIDGET_JS.count("{") == SEARCH_WIDGET_JS.count("}")
    assert SEARCH_WIDGET_JS.count("(") == SEARCH_WIDGET_JS.count(")")


def test_no_absolute_origin_is_hardcoded_anywhere_in_the_script() -> None:
    # This repository is public; WEB_DOMAIN must never appear in tracked files,
    # and neither should any other hardcoded absolute origin.
    assert "http://" not in SEARCH_WIDGET_JS
    assert "https://" not in SEARCH_WIDGET_JS


def test_only_same_origin_get_search_calls_are_intercepted() -> None:
    assert 'var SEARCH_PATH = "/api/v2/search";' in SEARCH_WIDGET_JS
    assert "parsed.origin !== window.location.origin" in SEARCH_WIDGET_JS
    assert "parsed.pathname !== SEARCH_PATH" in SEARCH_WIDGET_JS
    # Non-GET calls (and calls with no q) must fall straight through untouched.
    assert 'if (described.method !== "GET") {\n        return originalFetch(input, init);' in (
        SEARCH_WIDGET_JS
    )
    assert "if (!query) {\n        return originalFetch(input, init);" in SEARCH_WIDGET_JS


def test_input_can_be_a_string_url_or_request() -> None:
    assert "typeof input === \"string\"" in SEARCH_WIDGET_JS
    assert "input instanceof URL" in SEARCH_WIDGET_JS
    assert 'typeof input.url === "string"' in SEARCH_WIDGET_JS


def test_the_native_response_is_always_awaited_and_cloned_before_reading() -> None:
    # The original Response body must stay untouched so it can still be
    # returned, and read once by Mastodon's own code, if anything downstream
    # of it fails.
    assert "var nativePromise = originalFetch(input, init);" in SEARCH_WIDGET_JS
    assert "native.clone().json()" in SEARCH_WIDGET_JS
    assert "if (!native.ok) {\n            return native;" in SEARCH_WIDGET_JS
    assert "if (!cmx.ok) {" in SEARCH_WIDGET_JS


def test_statuses_are_fetched_in_chunks_of_20_and_keep_search_order() -> None:
    assert "var STATUS_CHUNK_SIZE = 20;" in SEARCH_WIDGET_JS
    assert "function fetchStatusChunk(ids, token, signal, rawFetch)" in SEARCH_WIDGET_JS
    assert '"id[]=" + encodeURIComponent(id)' in SEARCH_WIDGET_JS
    assert "STATUSES_PATH + \"?\" + params" in SEARCH_WIDGET_JS
    assert "function fetchStatusesInOrder(ids, token, signal, rawFetch)" in SEARCH_WIDGET_JS
    # Order is rebuilt from the site-search id list, not trusted from the batch.
    assert "ids.forEach(function (id) {\n        var status = byId[String(id)];" in (
        SEARCH_WIDGET_JS
    )


def test_site_search_is_called_with_the_page_bearer_and_a_limit() -> None:
    assert 'var SITE_SEARCH_PATH = "/files/search";' in SEARCH_WIDGET_JS
    assert "var SITE_SEARCH_LIMIT = 30;" in SEARCH_WIDGET_JS
    assert 'Authorization: "Bearer " + token' in SEARCH_WIDGET_JS
    assert "encodeURIComponent(query) + \"&limit=\" + SITE_SEARCH_LIMIT" in SEARCH_WIDGET_JS


def test_merged_response_keeps_native_accounts_and_hashtags() -> None:
    assert "function mergedResponse(nativeJson, statuses)" in SEARCH_WIDGET_JS
    assert "nativeJson.accounts" in SEARCH_WIDGET_JS
    assert "nativeJson.hashtags" in SEARCH_WIDGET_JS
    assert "new window.Response(JSON.stringify(payload)" in SEARCH_WIDGET_JS


def test_a_timeout_aborts_the_cmx_lookup_without_touching_native_search() -> None:
    assert "var TIMEOUT_MS = 8000;" in SEARCH_WIDGET_JS
    assert "new window.AbortController()" in SEARCH_WIDGET_JS
    assert "controller.abort();" in SEARCH_WIDGET_JS
