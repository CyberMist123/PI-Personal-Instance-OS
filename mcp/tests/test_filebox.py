from __future__ import annotations

import io
from types import SimpleNamespace

from starlette.testclient import TestClient

from cmx_mcp.config import Paths
from cmx_mcp.db import Database
from cmx_mcp.remote import create_remote_app, hash_passphrase
from cmx_mcp.remote_auth import OAuthStore, READ_SCOPE, SOCIAL_SCOPE


def _paths(tmp_path) -> Paths:
    return Paths(
        home=tmp_path / "mcp",
        runtime=tmp_path / "mcp" / "runtime",
        database=tmp_path / "mcp" / "runtime" / "cmx.sqlite3",
        secrets=tmp_path / "mcp" / "runtime" / "secrets",
        logs=tmp_path / "mcp" / "runtime" / "logs",
    )


def _app(tmp_path, monkeypatch, max_bytes: int | None = None):
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
    if max_bytes is not None:
        monkeypatch.setenv("CMX_FILEBOX_MAX_BYTES", str(max_bytes))
    monkeypatch.delenv("CMX_FILEBOX_QUOTA_BYTES", raising=False)
    monkeypatch.setattr("cmx_mcp.remote.Runtime", FakeRuntime)
    return create_remote_app(paths), paths, database


def _token(paths, scopes) -> str:
    store = OAuthStore(paths.database)
    store.initialize()
    import secrets as pysecrets

    access = pysecrets.token_urlsafe(32)
    store.save_token_pair(
        access_token=access,
        refresh_token=pysecrets.token_urlsafe(40),
        family_id="fam-filebox",
        client_id="client-filebox",
        bot_id="gpt",
        resource="https://pi.example/mcp/gpt",
        scopes=list(scopes),
    )
    return access


def test_bearer_upload_and_capability_download(tmp_path, monkeypatch):
    app, paths, database = _app(tmp_path, monkeypatch)
    token = _token(paths, [READ_SCOPE, SOCIAL_SCOPE])
    with TestClient(app, base_url="https://pi.example") as client:
        uploaded = client.post(
            "/files/upload",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": ("论文 final(1).tar.zst", io.BytesIO(b"binary-body"), "application/octet-stream")},
        )
        assert uploaded.status_code == 200, uploaded.text
        payload = uploaded.json()
        assert payload["bot"] == "gpt" and payload["size_bytes"] == 11
        assert payload["url"].startswith("https://pi.example/files/gpt/")

        path_part = payload["url"].split("https://pi.example", 1)[1]
        downloaded = client.get(path_part)
        assert downloaded.status_code == 200
        assert downloaded.content == b"binary-body"

        # Capability URL is required: a wrong file_id 404s.
        assert client.get(f"/files/gpt/wrong-id/{payload['name']}").status_code == 404
        assert database.filebox_usage("gpt") == 11


def test_upload_requires_social_scope_and_respects_size_limit(tmp_path, monkeypatch):
    app, paths, _database = _app(tmp_path, monkeypatch, max_bytes=1024 * 1024)
    read_only = _token(paths, [READ_SCOPE])
    with TestClient(app, base_url="https://pi.example") as client:
        rejected = client.post(
            "/files/upload",
            headers={"Authorization": f"Bearer {read_only}"},
            files={"file": ("x.bin", io.BytesIO(b"x"), "application/octet-stream")},
        )
        assert rejected.status_code == 401

        social = _token(paths, [READ_SCOPE, SOCIAL_SCOPE])
        big = client.post(
            "/files/upload",
            headers={"Authorization": f"Bearer {social}"},
            files={"file": ("big.bin", io.BytesIO(b"z" * (1024 * 1024 + 1)), "application/octet-stream")},
        )
        assert big.status_code == 413
        assert big.json()["error"] == "file_too_large"


def test_owner_page_passphrase_flow(tmp_path, monkeypatch):
    app, paths, database = _app(tmp_path, monkeypatch)
    with TestClient(app, base_url="https://pi.example") as client:
        unset = client.get("/files/up")
        assert unset.status_code == 200 and "filebox-pass" in unset.text

        database.set_setting("filebox_pass", hash_passphrase("correct horse battery"))
        wrong = client.post(
            "/files/up",
            data={"passphrase": "nope"},
            files={"file": ("d.txt", io.BytesIO(b"data"), "text/plain")},
        )
        assert "口令不正确" in wrong.text

        ok = client.post(
            "/files/up",
            data={"passphrase": "correct horse battery"},
            files={"file": ("旅行照片.raw", io.BytesIO(b"raw-bytes"), "application/octet-stream")},
        )
        assert ok.status_code == 200 and "上传成功" in ok.text
        assert "/files/_owner/" in ok.text
        stored = database.filebox_list("_owner")
        assert stored and stored[0]["file_name"] == "旅行照片.raw"


def test_filenames_are_sanitized_against_traversal(tmp_path, monkeypatch):
    app, paths, database = _app(tmp_path, monkeypatch)
    token = _token(paths, [READ_SCOPE, SOCIAL_SCOPE])
    with TestClient(app, base_url="https://pi.example") as client:
        uploaded = client.post(
            "/files/upload",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": ("..\\..\\evil.exe", io.BytesIO(b"payload"), "application/octet-stream")},
        )
        assert uploaded.status_code == 200
        name = uploaded.json()["name"]
        assert "/" not in name and "\\" not in name and not name.startswith(".")
        stored = database.filebox_list("gpt")[0]
        assert stored["file_name"] == name
        real = paths.filebox / "gpt" / uploaded.json()["file_id"] / name
        assert real.is_file() and real.resolve().is_relative_to(paths.filebox.resolve())
