from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock

import httpx
import pytest

from cmx_mcp.compact import compact_v2_status
from cmx_mcp.mastodon_client import MastodonApiError, MastodonClient
from cmx_mcp.server import build_server
from cmx_mcp.scope import READ_SCOPE, SOCIAL_SCOPE, require_request_scope
from cmx_mcp.server import _remote_post
from cmx_mcp.server import _remote_interact
from cmx_mcp.server import _visibility_failure
from cmx_mcp.server import _remote_timeline_funnel, _budget_statuses
from cmx_mcp.db import Database


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


def _browse_raw(value, *, source=None):
    item = {"id": value, "content": f"post {value}", "account": {"acct": "alice"}, "media_attachments": []}
    if source is not None:
        item["reblog"] = {"id": source, "content": f"post {source}", "account": {"acct": "alice"}, "media_attachments": []}
    return item


def _browse_settings(**overrides):
    return SimpleNamespace(browse_max_items=30, browse_preview_chars=50, browse_char_budget=5000,
                           browse_max_open=3, browse_visit_ttl_seconds=1800, **overrides)


def test_timeline_funnel_is_incremental_and_uses_single_min_id_page(tmp_path):
    runtime = _runtime(); runtime.settings = _browse_settings()
    runtime.db = Database(tmp_path / "browse.sqlite3"); runtime.db.initialize(); runtime.audit = lambda *args, **kwargs: None
    class Client:
        calls = []; round = 0
        def home_timeline(self, **kwargs):
            self.calls.append(kwargs)
            if self.round == 0:
                self.round = 1
                return SimpleNamespace(items=[_browse_raw("3"), _browse_raw("2"), _browse_raw("1")], next_cursor=None)
            return SimpleNamespace(items=[], next_cursor=None)
    runtime.client = Client()
    first = _remote_timeline_funnel(runtime); second = _remote_timeline_funnel(runtime)
    assert [x["id"] for x in first["items"]] == ["1", "2", "3"]
    assert second["items"] == []
    assert runtime.client.calls[-1]["min_id"] == "3"
    assert "max_id" not in runtime.client.calls[-1]


def test_remote_home_timeline_honors_requested_limit(tmp_path):
    runtime = _runtime(); runtime.settings = _browse_settings()
    runtime.settings.max_items = 30
    runtime.db = Database(tmp_path / "limit.sqlite3"); runtime.db.initialize()
    runtime.audit = lambda *args, **kwargs: None
    class Client:
        requested = None
        def home_timeline(self, **kwargs):
            self.requested = kwargs["limit"]
            return SimpleNamespace(items=[_browse_raw(str(value)) for value in range(30, 0, -1)], next_cursor=None)
    runtime.client = Client()
    tool = build_server(runtime, remote_profile="reader", remote_capabilities=runtime.bot)._tool_manager.get_tool("cmx_home")
    result = tool.fn("timeline", 10, None, True, _ScopeContext())
    assert runtime.client.requested == 10
    assert len(result["items"]) == 10


@pytest.mark.parametrize("new_count", [31, 65, 100])
def test_min_id_adjacent_pages_eventually_read_31_to_100_without_gaps(tmp_path, new_count):
    runtime = _runtime(); runtime.settings = _browse_settings()
    runtime.db = Database(tmp_path / "browse.sqlite3"); runtime.db.initialize(); runtime.audit=lambda *a, **k: None
    runtime.db.commit_browse(bot_id="gpt", feed="timeline", expected_watermark=None, watermark="100", seen_ids=[], visit_id="oldvisit", allowed_ids=[], max_open=3, char_budget_limit=5000, char_budget_used=0, expires_at=9999999999)
    all_items = [_browse_raw(str(value)) for value in range(101, 101 + new_count)]
    class Client:
        calls=[]
        def home_timeline(self, **kwargs):
            self.calls.append(kwargs)
            newer = [item for item in all_items if int(item["id"]) > int(kwargs["min_id"])]
            # Real min_id semantics: only the immediately newer page. Response order may be newest first.
            immediate = newer[:kwargs["limit"]]
            return SimpleNamespace(items=list(reversed(immediate)), next_cursor="ignored-rel-next")
    runtime.client=Client(); observed=[]
    while True:
        result = _remote_timeline_funnel(runtime)
        ids = [item["id"] for item in result["items"]]
        if not ids: break
        observed.extend(ids)
    expected = [str(value) for value in range(101, 101 + new_count)]
    assert observed == expected
    assert len(observed) == len(set(observed))
    assert all("max_id" not in call for call in runtime.client.calls)
    assert [call["min_id"] for call in runtime.client.calls[:2]] == ["100", "130"]


def test_seen_boost_advances_outer_watermark_without_duplicate(tmp_path):
    runtime = _runtime(); runtime.settings = _browse_settings()
    runtime.db = Database(tmp_path / "browse.sqlite3"); runtime.db.initialize(); runtime.audit=lambda *a, **k: None
    runtime.db.commit_browse(bot_id="gpt", feed="timeline", expected_watermark=None, watermark="100", seen_ids=["old"], visit_id="old", allowed_ids=[], max_open=3, char_budget_limit=5000, char_budget_used=0, expires_at=9999999999)
    class Client:
        def home_timeline(self, **kwargs):
            return SimpleNamespace(items=[_browse_raw("102"), _browse_raw("101", source="old")], next_cursor=None)
    runtime.client = Client(); result = _remote_timeline_funnel(runtime)
    assert [item["id"] for item in result["items"]] == ["102"]
    assert runtime.db.get_browse_watermark("gpt") == "102"


def test_two_concurrent_scans_same_bot_return_no_duplicate_catalog(tmp_path):
    path = tmp_path / "concurrent.sqlite3"
    seed = Database(path); seed.initialize()
    seed.commit_browse(bot_id="gpt", feed="timeline", expected_watermark=None, watermark="100", seen_ids=[], visit_id="seed", allowed_ids=[], max_open=3, char_budget_limit=5000, char_budget_used=0, expires_at=9999999999)
    barrier = Barrier(2); lock = Lock(); first_calls = 0
    statuses = [_browse_raw(str(value)) for value in range(101, 111)]
    class Client:
        def home_timeline(self, **kwargs):
            nonlocal first_calls
            with lock:
                first_calls += 1; call_number = first_calls
            if call_number <= 2:
                barrier.wait(timeout=5)
            newer = [item for item in statuses if int(item["id"]) > int(kwargs["min_id"])]
            return SimpleNamespace(items=list(reversed(newer[:kwargs["limit"]])), next_cursor=None)
    def scan():
        runtime = _runtime(); runtime.settings = _browse_settings(); runtime.db = Database(path)
        runtime.client = Client(); runtime.audit = lambda *a, **k: None
        return _remote_timeline_funnel(runtime)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: scan(), range(2)))
    catalogs = [[item["id"] for item in result["items"]] for result in results]
    flattened = [item for catalog in catalogs for item in catalog]
    assert sorted(flattened, key=int) == [str(value) for value in range(101, 111)]
    assert len(flattened) == len(set(flattened))
    assert sorted(map(len, catalogs)) == [0, 10]


def test_cache_or_audit_failure_cannot_advance_watermark(tmp_path):
    runtime = _runtime(); runtime.settings = _browse_settings()
    runtime.db = Database(tmp_path / "ordering.sqlite3"); runtime.db.initialize()
    runtime.db.commit_browse(bot_id="gpt", feed="timeline", expected_watermark=None, watermark="100", seen_ids=[], visit_id="seed", allowed_ids=[], max_open=3, char_budget_limit=5000, char_budget_used=0, expires_at=9999999999)
    runtime.client = SimpleNamespace(home_timeline=lambda **kwargs: SimpleNamespace(items=[_browse_raw("101")], next_cursor=None))
    runtime.audit = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("audit failed"))
    with pytest.raises(RuntimeError, match="audit failed"):
        _remote_timeline_funnel(runtime)
    assert runtime.db.get_browse_watermark("gpt") == "100"
    assert runtime.db.seen_status_ids("gpt", ["101"]) == set()


@pytest.mark.parametrize("text", ["😀" * 12, "中文边界" * 8])
def test_char_budget_counts_final_json_and_stops_at_whole_status(text):
    runtime=_runtime(); runtime.settings=SimpleNamespace(browse_char_budget=5000)
    items = [{"id":"1","text":text}, {"id":"2","text":"末条"}]
    exact_limit = next(limit for limit in range(501, 1000)
                       if len(_budget_statuses(runtime, ["1", "2", "missing"], items, ["missing"], {"char_budget_limit":limit,"char_budget_used":500})["items"]) == 2)
    exact = _budget_statuses(runtime, ["1", "2", "missing"], items, ["missing"], {"char_budget_limit":exact_limit,"char_budget_used":500})
    over = _budget_statuses(runtime, ["1", "2", "missing"], items, ["missing"], {"char_budget_limit":exact_limit - 1,"char_budget_used":500})
    assert [item["id"] for item in exact["items"]] == ["1", "2"]
    assert "truncated" not in exact and exact["budget_chars_remaining"] >= 0
    assert over["truncated"] is True and over["remaining_ids"] == ["1", "2"]
    assert over["items"] == []
    assert over["missing_ids"] == ["missing"]


def test_remote_status_batches_in_request_order_and_lists_missing():
    runtime = _runtime()
    class DB:
        def get_visit(self, *args): return None
        def cache_statuses(self, *args): pass
    class Client:
        def get_statuses(self, ids): return [_browse_raw("2"), _browse_raw("1")]
    runtime.db, runtime.client = DB(), Client()
    tool = build_server(runtime, remote_profile="reader", remote_capabilities=runtime.bot)._tool_manager.get_tool("cmx_status")
    result = tool.fn(["1", "missing", "2"], "compact", None, _ScopeContext())
    assert [item["id"] for item in result["items"]] == ["1", "2"]
    assert result["missing_ids"] == ["missing"]
    with pytest.raises(ValueError, match="between 1 and 3"):
        tool.fn(["1", "2", "3", "4"], "compact", None, _ScopeContext())


def test_remote_status_visit_allowlist_and_repeat_are_enforced(tmp_path):
    runtime = _runtime(); runtime.db = Database(tmp_path / "visit.sqlite3"); runtime.db.initialize()
    runtime.db.commit_browse(bot_id="gpt", feed="timeline", expected_watermark=None, watermark="1", seen_ids=[], visit_id="v", allowed_ids=["1"], max_open=3, char_budget_limit=5000, char_budget_used=500, expires_at=9999999999)
    class Client:
        def get_statuses(self, ids): return [_browse_raw(value) for value in ids]
    runtime.client = Client()
    tool = build_server(runtime, remote_profile="reader", remote_capabilities=runtime.bot)._tool_manager.get_tool("cmx_status")
    with pytest.raises(ValueError, match="not offered"):
        tool.fn(["2"], "compact", "v", _ScopeContext())
    assert tool.fn(["1"], "compact", "v", _ScopeContext())["items"][0]["id"] == "1"
    with pytest.raises(ValueError, match="reopened"):
        tool.fn(["1"], "compact", "v", _ScopeContext())


def test_mastodon_batch_statuses_uses_native_repeated_query():
    seen = {}
    def handler(request: httpx.Request) -> httpx.Response:
        seen["query"] = request.url.query.decode(); return httpx.Response(200, json=[])
    client = object.__new__(MastodonClient)
    client._client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://mastodon.example")
    try:
        assert client.get_statuses(["1", "2", "3"]) == []
        assert seen["query"] == "id%5B%5D=1&id%5B%5D=2&id%5B%5D=3"
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


def test_private_reply_to_self_does_not_require_direct_recipients():
    runtime = _runtime()

    class DB:
        def claim_dedup(self, **_): return {"claimed": True, "state": "pending"}
        def finish_dedup(self, **_): pass
        def cache_statuses(self, *_): pass

    class Client:
        published = None

        def get_status(self, _):
            return {
                "id": "target",
                "visibility": "private",
                "spoiler_text": "",
                "account": {"acct": "self@example.test"},
                "mentions": [],
            }

        def verify_credentials(self):
            return {"id": "me", "acct": "self@example.test"}

        def publish(self, **kwargs):
            self.published = kwargs
            return {"id": "new", "created_at": "now"}

    runtime.db, runtime.client = DB(), Client()
    result = _remote_post(runtime, lambda _ctx: None, "reply", "hello", "target", "residents", None, "req-2", None)
    assert result["id"] == "new"
    assert runtime.client.published["visibility"] == "private"
    assert runtime.client.published["text"] == "hello"


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
