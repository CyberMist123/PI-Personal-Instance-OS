from __future__ import annotations

import io
import time

import pytest

from cmx_mcp import clipboard_store
from cmx_mcp.clipboard_db import ClipboardError
from cmx_mcp.clipboard_files import ClipboardFiles, safe_filename
from cmx_mcp.clipboard_search import matching_entry_ids
from cmx_mcp.clipboard_store import ClipboardStore

from conftest_clipboard import entry_ids, headers, make_client, post_entry


@pytest.fixture
def client(tmp_path, monkeypatch):
    return make_client(tmp_path, monkeypatch)


# ---------- boundary ----------


def test_unauthenticated_requests_are_rejected(client):
    api_client, _ = client
    assert api_client.get("/clipboard-api/entries").status_code == 401
    assert api_client.get("/clipboard-api/entries", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert post_entry(api_client, token="nope", text="hi").status_code == 401


def test_mutations_require_a_known_origin(client):
    api_client, _ = client
    assert post_entry(api_client, text="hi", origin=None).status_code == 403
    assert post_entry(api_client, text="hi", origin="https://evil.test").status_code == 403
    assert post_entry(api_client, text="hi").status_code == 201


def test_reads_do_not_require_an_origin(client):
    api_client, _ = client
    post_entry(api_client, text="hi")
    response = api_client.get("/clipboard-api/entries", headers={"Authorization": "Bearer tok-a"})
    assert response.status_code == 200


def test_accounts_cannot_see_each_other(client):
    api_client, _ = client
    mine = post_entry(api_client, text="mine").json()["entry_id"]
    post_entry(api_client, token="tok-b", text="theirs")
    assert entry_ids(api_client, "tok-a") == [mine]
    assert mine not in entry_ids(api_client, "tok-b")
    assert api_client.get(
        f"/clipboard-api/entries/{mine}", headers=headers("tok-b")
    ).status_code in (404, 405)
    assert api_client.patch(
        f"/clipboard-api/entries/{mine}", json={"favorite": True}, headers=headers("tok-b")
    ).status_code == 404


def test_responses_are_no_store_and_downloads_are_attachments(client):
    api_client, _ = client
    entry = post_entry(api_client, text="t", files=[("a.txt", b"abc", "text/plain")]).json()
    listing = api_client.get("/clipboard-api/entries", headers=headers())
    assert listing.headers["cache-control"] == "no-store"
    file_id = entry["files"][0]["file_id"]
    download = api_client.get(
        f"/clipboard-api/entries/{entry['entry_id']}/files/{file_id}", headers=headers()
    )
    assert download.status_code == 200
    assert download.content == b"abc"
    assert download.headers["x-content-type-options"] == "nosniff"
    assert "attachment" in download.headers["content-disposition"]
    assert download.headers["cache-control"] == "no-store"


# ---------- limits ----------


def test_text_limit_boundary(client, monkeypatch):
    api_client, _ = client
    monkeypatch.setattr(clipboard_store, "TEXT_LIMIT", 10)
    assert post_entry(api_client, text="x" * 10).status_code == 201
    rejected = post_entry(api_client, text="x" * 11)
    assert rejected.status_code == 400
    assert rejected.json()["error"] == "text_too_long"


def test_file_count_boundary(client, monkeypatch):
    api_client, _ = client
    monkeypatch.setattr(clipboard_store, "FILE_LIMIT", 3)
    ok = [(f"f{i}.txt", b"x", "text/plain") for i in range(3)]
    assert post_entry(api_client, files=ok).status_code == 201
    too_many = [(f"f{i}.txt", b"x", "text/plain") for i in range(4)]
    assert post_entry(api_client, files=too_many).json()["error"] == "too_many_files"


def test_entry_byte_limit_is_strict(client, monkeypatch):
    api_client, _ = client
    monkeypatch.setattr(clipboard_store, "ENTRY_BYTE_LIMIT", 64)
    assert post_entry(api_client, files=[("a", b"x" * 63, "text/plain")]).status_code == 201
    over = post_entry(api_client, files=[("a", b"x" * 64, "text/plain")])
    assert over.status_code == 413
    assert over.json()["error"] == "entry_too_large"


def test_account_quota_is_separate_from_the_entry_limit(client, monkeypatch):
    api_client, _ = client
    monkeypatch.setattr(clipboard_store, "ACCOUNT_QUOTA_BYTES", 100)
    assert post_entry(api_client, files=[("a", b"x" * 60, "text/plain")]).status_code == 201
    over = post_entry(api_client, files=[("b", b"x" * 60, "text/plain")])
    assert over.status_code == 413
    assert over.json()["error"] == "quota_exceeded"
    # The other account still has its own full quota.
    assert post_entry(api_client, token="tok-b", files=[("c", b"x" * 60, "text/plain")]).status_code == 201


def test_empty_entry_is_rejected(client):
    api_client, _ = client
    assert post_entry(api_client, text="").json()["error"] == "empty_entry"


def test_usage_reports_quota_and_warn_thresholds(client):
    api_client, _ = client
    post_entry(api_client, files=[("a", b"x" * 10, "text/plain")])
    body = api_client.get("/clipboard-api/usage", headers=headers()).json()
    assert body["used_bytes"] == 10
    assert body["warn_bytes"] < body["quota_bytes"]


# ---------- files on disk ----------


def test_traversal_filenames_are_neutralised():
    for hostile in ["../../etc/passwd", r"..\..\win.ini", "a\r\nb.txt", "\x00evil"]:
        cleaned = safe_filename(hostile)
        assert "/" not in cleaned and "\\" not in cleaned
        assert "\r" not in cleaned and "\n" not in cleaned and "\x00" not in cleaned


def test_uploaded_bytes_land_under_the_entry_id(client, tmp_path):
    api_client, _ = client
    entry = post_entry(api_client, files=[("../../evil.txt", b"data", "text/plain")]).json()
    assert entry["files"][0]["name"] == "evil.txt"
    stored = tmp_path / "clipboard" / "objects" / entry["entry_id"] / entry["files"][0]["file_id"]
    assert stored.read_bytes() == b"data"


def test_failed_promote_rolls_back_rows_and_staging(tmp_path):
    store = ClipboardStore(tmp_path / "c.sqlite3")
    store.initialize()
    files = ClipboardFiles(tmp_path / "clipboard")
    files.ensure()
    batch = files.new_batch()
    staged = [files.stage(batch, io.BytesIO(b"x"), filename="a", content_type="text/plain", budget=99)]

    def explode(_entry_id: str) -> None:
        raise OSError("disk went away")

    with pytest.raises(OSError):
        store.create_entry("acct-a", text="t", staged=staged, promote=explode)
    files.discard(batch)
    assert store.list_entries("acct-a")[0] == []
    assert not any((tmp_path / "clipboard" / "staging").iterdir())
    assert not any((tmp_path / "clipboard" / "objects").iterdir())


def test_startup_clears_orphan_staging(tmp_path):
    files = ClipboardFiles(tmp_path / "clipboard")
    orphan = files.staging / "left-behind"
    orphan.mkdir(parents=True)
    (orphan / "chunk").write_bytes(b"x")
    assert files.cleanup_staging() == 1
    assert not orphan.exists()


def test_streaming_stops_at_the_budget(tmp_path):
    files = ClipboardFiles(tmp_path / "clipboard")
    files.ensure()
    batch = files.new_batch()
    with pytest.raises(ClipboardError) as excinfo:
        files.stage(batch, io.BytesIO(b"x" * 100), filename="a", content_type="text/plain", budget=10)
    assert excinfo.value.code == "entry_too_large"
    assert not any((files.staging / batch).iterdir())


# ---------- deletion ----------


def test_deleting_one_file_keeps_the_rest(client, tmp_path):
    api_client, _ = client
    entry = post_entry(
        api_client, text="keep me",
        files=[("a.txt", b"aaa", "text/plain"), ("b.txt", b"bbb", "text/plain")],
    ).json()
    victim, survivor = entry["files"][0], entry["files"][1]
    response = api_client.delete(
        f"/clipboard-api/entries/{entry['entry_id']}/files/{victim['file_id']}", headers=headers()
    )
    assert response.status_code == 200
    assert response.json()["entry_removed"] is False
    after = api_client.get(f"/clipboard-api/entries", headers=headers()).json()["entries"][0]
    assert after["text"] == "keep me"
    assert [f["file_id"] for f in after["files"]] == [survivor["file_id"]]
    objects = tmp_path / "clipboard" / "objects" / entry["entry_id"]
    assert not (objects / victim["file_id"]).exists()
    assert (objects / survivor["file_id"]).exists()


def test_delete_many_only_removes_named_ids(client):
    api_client, _ = client
    keep = post_entry(api_client, text="keep").json()["entry_id"]
    drop = post_entry(api_client, text="drop").json()["entry_id"]
    other = post_entry(api_client, token="tok-b", text="theirs").json()["entry_id"]
    response = api_client.post(
        "/clipboard-api/entries/delete-many",
        json={"entry_ids": [drop, other]},
        headers=headers(),
    )
    assert response.status_code == 200
    assert response.json()["removed"] == 1          # `other` belongs to acct-b
    assert entry_ids(api_client, "tok-a") == [keep]
    assert entry_ids(api_client, "tok-b") == [other]


def test_delete_many_refuses_an_unnamed_selection(client):
    api_client, _ = client
    post_entry(api_client, text="keep")
    response = api_client.post(
        "/clipboard-api/entries/delete-many", json={}, headers=headers()
    )
    assert response.status_code == 400
    assert response.json()["error"] == "entry_ids_required"
    assert len(entry_ids(api_client)) == 1


# ---------- expiry and favourites ----------


def test_expired_entries_become_invisible_and_lose_their_bytes(client, tmp_path):
    api_client, api = client
    entry = post_entry(api_client, files=[("a.txt", b"abc", "text/plain")]).json()
    objects = tmp_path / "clipboard" / "objects" / entry["entry_id"]
    assert objects.exists()
    api.store.purge_expired(now=int(time.time()) + 86_401)
    for entry_id in [entry["entry_id"]]:
        api.files.delete_entry(entry_id)
    assert entry_ids(api_client) == []
    assert not objects.exists()


def test_favourites_survive_the_sweep_and_reschedule_when_cleared(client):
    api_client, api = client
    entry_id = post_entry(api_client, text="remember").json()["entry_id"]
    api_client.patch(
        f"/clipboard-api/entries/{entry_id}", json={"favorite": True}, headers=headers()
    )
    assert entry_ids(api_client) == []
    assert entry_ids(api_client, view="favorite") == [entry_id]

    later = int(time.time()) + 86_400 * 7
    assert api.store.purge_expired(now=later) == []

    api.store.set_favorite("acct-a", entry_id, False, now=later)
    refreshed = api.store.get_entry("acct-a", entry_id, now=later)
    # Restarted from `later`, not from created_at: it must not expire instantly.
    assert refreshed["expires_at"] == later + 86_400


def test_patch_rejects_unknown_fields(client):
    api_client, _ = client
    entry_id = post_entry(api_client, text="x").json()["entry_id"]
    response = api_client.patch(
        f"/clipboard-api/entries/{entry_id}",
        json={"expires_at": 0, "owner_account_id": "acct-b"},
        headers=headers(),
    )
    assert response.status_code == 400
    assert response.json()["error"] == "unsupported_field"


def test_topic_round_trips_and_filters(client):
    api_client, _ = client
    cooking = post_entry(api_client, text="egg sop").json()["entry_id"]
    post_entry(api_client, text="unfiled")
    api_client.patch(
        f"/clipboard-api/entries/{cooking}", json={"topic": "烧菜"}, headers=headers()
    )
    assert entry_ids(api_client, topic="烧菜") == [cooking]


# ---------- search ----------


def test_search_matches_text_and_filenames_within_one_account(client):
    api_client, _ = client
    by_text = post_entry(api_client, text="溏心蛋 SOP").json()["entry_id"]
    by_name = post_entry(api_client, files=[("溏心蛋.pdf", b"x", "application/pdf")]).json()["entry_id"]
    post_entry(api_client, text="unrelated")
    post_entry(api_client, token="tok-b", text="溏心蛋 theirs")

    found = set(entry_ids(api_client, q="溏心"))
    assert found == {by_text, by_name}
    assert entry_ids(api_client, "tok-b", q="溏心") != []
    assert by_text not in entry_ids(api_client, "tok-b", q="溏心")


def test_two_character_cjk_queries_work(client):
    """The reason this is a scan and not FTS5: trigram needs three chars."""
    api_client, _ = client
    hit = post_entry(api_client, text="今天烧菜的记录").json()["entry_id"]
    assert entry_ids(api_client, q="烧菜") == [hit]


def test_search_helper_is_account_blind_by_design():
    rows = [{"entry_id": "1", "text": "Alpha", "names": ""}]
    assert matching_entry_ids(rows, "alpha") == {"1"}
    assert matching_entry_ids(rows, "beta") == set()


# ---------- secrets ----------


def test_bearer_never_reaches_sqlite(client, tmp_path):
    api_client, _ = client
    post_entry(api_client, text="hello", files=[("a.txt", b"abc", "text/plain")])
    blob = (tmp_path / "clipboard.sqlite3").read_bytes()
    assert b"tok-a" not in blob
    assert b"Bearer" not in blob


# ---------- infrastructure contract ----------


def test_nginx_proxies_clipboard_api_above_the_entry_limit():
    from pathlib import Path

    conf = (Path(__file__).resolve().parents[2] / "nginx" / "default.conf").read_text("utf-8")
    block = conf[conf.index("location ^~ /clipboard-api/") :]
    block = block[: block.index("\n  }")]
    assert "proxy_pass http://cmx_mcp;" in block
    assert "proxy_request_buffering off;" in block
    # Must exceed 1 GiB so the app returns a JSON 413, not Nginx's HTML one.
    size = block.split("client_max_body_size", 1)[1].split(";", 1)[0].strip()
    assert size.endswith("m") and int(size[:-1]) * 1024**2 > 1024**3


def test_request_size_middleware_exempts_clipboard_api():
    from cmx_mcp.remote import MAX_REQUEST_BYTES, RequestSizeLimitMiddleware

    seen: list[str] = []

    async def app(scope, receive, send):
        seen.append(scope["path"])

    middleware = RequestSizeLimitMiddleware(app, max_bytes=MAX_REQUEST_BYTES)
    scope = {
        "type": "http",
        "path": "/clipboard-api/entries",
        "headers": [(b"content-length", str(MAX_REQUEST_BYTES * 4).encode())],
    }

    import asyncio

    asyncio.run(middleware(scope, None, None))
    assert seen == ["/clipboard-api/entries"]
