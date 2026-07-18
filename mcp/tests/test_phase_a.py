from types import SimpleNamespace

import httpx
import pytest

from cmx_mcp.compact import compact_v2_status
from cmx_mcp.mastodon_client import MastodonApiError, MastodonClient
from cmx_mcp.server import build_server
from cmx_mcp.scope import READ_SCOPE, SOCIAL_SCOPE, require_request_scope
from cmx_mcp.server import _remote_post
from cmx_mcp.server import _remote_interact
from cmx_mcp.server import _visibility_failure


def test_compact_v2_omits_empty_fields_and_preserves_reply_and_direct_mentions():
    result = compact_v2_status({
        "id": "1", "created_at": "2026-07-18T00:00:00Z", "visibility": "direct",
        "content": "<p>hello</p>", "in_reply_to_id": "0",
        "account": {"acct": "alice"}, "mentions": [{"acct": "bob"}],
        "spoiler_text": "cw", "media_attachments": [],
    })
    assert result == {
        "id": "1", "author": "alice", "at": "2026-07-18T00:00:00Z", "text": "hello",
        "reply_to": "0", "vis": "direct", "to": ["bob"], "cw": "cw",
    }


def _runtime(**overrides):
    class Runtime: pass
    runtime = Runtime()
    runtime.bot = SimpleNamespace(
        bot_id="gpt", profile="resident", allow_public=False,
        remote_polls=True, remote_boosts=overrides.get("boosts", False),
        remote_notifications=overrides.get("notifications", False),
    )
    runtime.settings = SimpleNamespace(max_items=30)
    runtime.client = None
    runtime.db = None
    return runtime


def test_remote_reader_surface_is_exactly_three_tools():
    server = build_server(_runtime(), remote_profile="reader", remote_capabilities=_runtime().bot)
    assert [tool.name for tool in server._tool_manager.list_tools()] == ["cmx_home", "cmx_status", "cmx_search"]


def test_remote_social_surface_hides_boost_and_notifications_when_disabled():
    server = build_server(_runtime(), remote_profile="social", remote_capabilities=_runtime().bot)
    tools = {tool.name: tool for tool in server._tool_manager.list_tools()}
    assert set(tools) == {"cmx_home", "cmx_status", "cmx_search", "cmx_post", "cmx_interact"}
    assert "boost" not in tools["cmx_interact"].parameters["properties"]["action"]["enum"]


def test_request_scope_is_checked_from_current_request_state():
    class State:
        cmx_scopes = [READ_SCOPE]
    class Request:
        state = State()
    class RequestContext:
        request = Request()
    class Context:
        request_context = RequestContext()
    require_request_scope(Context(), READ_SCOPE)
    try:
        require_request_scope(Context(), SOCIAL_SCOPE)
    except PermissionError as exc:
        assert str(exc) == "insufficient_scope"
    else:
        raise AssertionError("missing social scope was accepted")


class _ScopeContext:
    def __init__(self):
        self.request_context = SimpleNamespace(
            request=SimpleNamespace(state=SimpleNamespace(cmx_scopes=[READ_SCOPE]))
        )


def test_remote_search_revalidates_candidates_and_removes_forbidden_cache():
    runtime = _runtime()
    class DB:
        def __init__(self): self.removed = []
        def search_statuses(self, bot_id, query, limit): return [{"id": "gone"}, {"id": "forbidden"}, {"id": "ok"}]
        def invalidate_status(self, bot_id, status_id): self.removed.append((bot_id, status_id))
        def cache_statuses(self, bot_id, statuses): pass
    class Client:
        def get_status(self, status_id):
            if status_id == "gone": raise MastodonApiError("Mastodon API GET /api/v1/statuses/gone returned 404 Not Found")
            if status_id == "forbidden": raise MastodonApiError("Mastodon API GET /api/v1/statuses/forbidden returned 403 Forbidden", status_code=403)
            return {"id": "ok", "created_at": "2026-07-18T00:00:00Z", "content": "<p>fresh</p>", "account": {"acct": "alice"}}
    runtime.db, runtime.client = DB(), Client()
    server = build_server(runtime, remote_profile="reader", remote_capabilities=runtime.bot)
    result = server._tool_manager.get_tool("cmx_search").fn("fresh", 1, _ScopeContext())
    assert result["items"][0]["author"] == "alice"
    assert result["items"][0]["text"] == "fresh"
    assert runtime.db.removed == [("gpt", "gone"), ("gpt", "forbidden")]
    assert _visibility_failure(MastodonApiError("Mastodon API GET /x returned 403 Forbidden"))


@pytest.mark.parametrize("error", [
    MastodonApiError("resident token is invalid", status_code=401),
    MastodonApiError("Mastodon rate limit exceeded", status_code=429),
    MastodonApiError("Mastodon service unavailable", status_code=500),
    MastodonApiError("Mastodon connection failed"),
])
def test_remote_search_propagates_non_visibility_errors(error):
    runtime = _runtime()
    class DB:
        def search_statuses(self, bot_id, query, limit): return [{"id": "candidate"}]
        def invalidate_status(self, *args): raise AssertionError("non-visibility error was cleared")
    class Client:
        def get_status(self, status_id): raise error
    runtime.db, runtime.client = DB(), Client()
    server = build_server(runtime, remote_profile="reader", remote_capabilities=runtime.bot)
    with pytest.raises(MastodonApiError) as caught:
        server._tool_manager.get_tool("cmx_search").fn("query", 1, _ScopeContext())
    assert caught.value is error


def test_remote_post_does_not_reject_or_truncate_long_url_text():
    runtime = _runtime()
    text = "https://example.test/" + "x" * 450
    class DB:
        def claim_dedup(self, **_): return {"claimed": True, "state": "pending"}
        def finish_dedup(self, **_): pass
        def cache_statuses(self, *args): pass
    class Client:
        def publish(self, **kwargs): self.published = kwargs; return {"id": "new", "created_at": "now"}
    runtime.db, runtime.client = DB(), Client()
    result = _remote_post(runtime, lambda _ctx: None, "create", text, None, "residents", None, "req-long", "public")
    assert result["id"] == "new"
    assert runtime.client.published["text"] == text


def test_mastodon_422_becomes_compact_content_limit_error():
    client = object.__new__(MastodonClient)
    client._json = lambda *args, **kwargs: (_ for _ in ()).throw(
        MastodonApiError("Mastodon validation failed: too long", status_code=422)
    )
    try:
        with pytest.raises(MastodonApiError, match="^content exceeds instance limit$") as caught:
            client.publish(text="long", visibility="private", reply_to_id=None,
                           media_ids=[], idempotency_key="req")
        assert caught.value.status_code == 422
    finally:
        pass


def _mock_mastodon_422(detail: str) -> MastodonClient:
    client = object.__new__(MastodonClient)
    client._client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(422, json={"error": detail})),
        base_url="https://mastodon.example",
    )
    return client


def test_mastodon_422_validation_is_not_misreported_as_content_limit():
    client = object.__new__(MastodonClient)
    client._json = lambda *args, **kwargs: (_ for _ in ()).throw(
        MastodonApiError("Mastodon validation failed: visibility is invalid", status_code=422)
    )
    try:
        with pytest.raises(MastodonApiError, match="^Mastodon validation failed: visibility is invalid$"):
            client.publish(text="hello", visibility="private", reply_to_id=None,
                           media_ids=[], idempotency_key="req")
    finally:
        pass


def test_publish_urlencodes_form_fields_for_httpx():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["content_type"] = request.headers.get("Content-Type")
        seen["body"] = request.read().decode("utf-8")
        return httpx.Response(200, json={"id": "new"})

    client = object.__new__(MastodonClient)
    client._client = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://mastodon.example",
    )
    try:
        result = client.publish(
            text="hello",
            visibility="private",
            reply_to_id=None,
            media_ids=[],
            idempotency_key="req",
        )
        assert result == {"id": "new"}
        assert seen["content_type"] == "application/x-www-form-urlencoded"
        assert "status=hello" in seen["body"]
        assert "visibility=private" in seen["body"]
    finally:
        client.close()


def test_vote_poll_urlencodes_repeated_choice_fields_for_httpx():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["content_type"] = request.headers.get("Content-Type")
        seen["body"] = request.read().decode("utf-8")
        return httpx.Response(200, json={"voted": True})

    client = object.__new__(MastodonClient)
    client._client = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://mastodon.example",
    )
    try:
        result = client.vote_poll("poll-1", [0, 2])
        assert result == {"voted": True}
        assert seen["content_type"] == "application/x-www-form-urlencoded"
        assert seen["body"] == "choices%5B%5D=0&choices%5B%5D=2"
    finally:
        client.close()


@pytest.mark.parametrize("detail", ["already voted", "poll has expired", "choices are invalid"])
def test_poll_422_errors_are_not_content_limit(detail):
    client = object.__new__(MastodonClient)
    client._json = lambda *args, **kwargs: (_ for _ in ()).throw(
        MastodonApiError(f"Mastodon validation failed: {detail}", status_code=422)
    )
    try:
        with pytest.raises(MastodonApiError) as caught:
            client.vote_poll("poll-1", [0])
        assert str(caught.value).startswith("Mastodon validation failed")
        assert "content exceeds instance limit" not in str(caught.value)
    finally:
        pass


def test_edit_validation_422_is_not_content_limit_and_redacts_secrets():
    client = _mock_mastodon_422("invalid status; Authorization: Bearer secret-token")
    try:
        with pytest.raises(MastodonApiError) as caught:
            client.edit_status("status-1", text="hello")
        message = str(caught.value)
        assert message.startswith("Mastodon validation failed")
        assert "content exceeds instance limit" not in message
        assert "Authorization" not in message
        assert "secret-token" not in message
    finally:
        client.close()


def _interact_runtime(raw_status=None):
    runtime = _runtime(boosts=True)
    class Client:
        def get_status(self, _):
            return {"id": "status-1", "poll": {"id": "poll-1", "options": [{"title": "yes"}], "multiple": False}} if raw_status is None else raw_status
        def react(self, status_id, action):
            return raw_status or {"id": "status-1", "favourited": False, "bookmarked": False, "reblogged": False}
        def vote_poll(self, poll_id, choices):
            self.voted = (poll_id, choices)
            return {"options": [{"title": "yes", "votes_count": 1}], "multiple": False}
    runtime.client = Client()
    return runtime


@pytest.mark.parametrize("action", ["like", "unlike", "bookmark", "unbookmark", "boost", "unboost"])
def test_interact_rejects_choices_for_every_non_vote_action(action):
    with pytest.raises(ValueError, match="^choices is only accepted for vote$"):
        _remote_interact(_interact_runtime(), lambda _ctx: None, action, "status-1", [0], None)


def test_interact_vote_requires_choices_and_accepts_valid_choices():
    runtime = _interact_runtime()
    with pytest.raises(ValueError, match="^poll choices are required$"):
        _remote_interact(runtime, lambda _ctx: None, "vote", "status-1", None, None)
    result = _remote_interact(runtime, lambda _ctx: None, "vote", "status-1", [0], None)
    assert result["id"] == "status-1"


def test_interact_sparse_state_omits_empty_state():
    runtime = _interact_runtime()
    result = _remote_interact(runtime, lambda _ctx: None, "like", "status-1", None, None)
    assert result == {"id": "status-1"}
    assert all(value not in ({}, None, False) for value in result.values())


def test_interact_includes_non_empty_state():
    runtime = _interact_runtime({"id": "status-1", "favourited": True})
    result = _remote_interact(runtime, lambda _ctx: None, "like", "status-1", None, None)
    assert result == {"id": "status-1", "state": {"favourite": True}}


def test_scope_guard_fails_closed_without_context_or_state():
    for value in (None, SimpleNamespace(), SimpleNamespace(request_context=SimpleNamespace(request=None))):
        try:
            require_request_scope(value, READ_SCOPE)
        except PermissionError as exc:
            assert str(exc) == "insufficient_scope"
        else:
            raise AssertionError("scope guard opened without request state")


def test_direct_reply_keeps_target_author_mentions_and_inherits_cw():
    runtime = _runtime()
    class DB:
        def claim_dedup(self, **_): return {"claimed": True, "state": "pending"}
        def finish_dedup(self, **_): pass
        def cache_statuses(self, *_): pass
    class Client:
        published = None
        def get_status(self, _):
            return {"id": "target", "visibility": "direct", "spoiler_text": "private topic",
                    "account": {"acct": "author@example.test"},
                    "mentions": [{"acct": "helper@example.test"}, {"acct": "self@example.test"}]}
        def verify_credentials(self): return {"id": "me", "acct": "self@example.test"}
        def publish(self, **kwargs): self.published = kwargs; return {"id": "new", "created_at": "now"}
    runtime.db, runtime.client = DB(), Client()
    result = _remote_post(runtime, lambda _ctx: None, "reply", "hello", "target", "residents", None, "req-1", None)
    assert result["id"] == "new"
    assert runtime.client.published["visibility"] == "direct"
    assert runtime.client.published["spoiler_text"] == "private topic"
    assert runtime.client.published["text"] == "@author@example.test @helper@example.test hello"


def test_edit_rejects_complex_status_before_put():
    runtime = _runtime()
    class Client:
        called = False
        def get_status(self, _): return {"id": "1", "account": {"id": "me"}, "media_attachments": [{}]}
        def verify_credentials(self): return {"id": "me"}
        def edit_status(self, **_): self.called = True; return {"id": "1"}
    runtime.client = Client()
    runtime.db = SimpleNamespace()
    try:
        _remote_post(runtime, lambda _ctx: None, "edit", "new", "1", "residents", None, "r", None)
    except ValueError as exc:
        assert "complex" in str(exc)
    else:
        raise AssertionError("complex status was editable")
    assert runtime.client.called is False
