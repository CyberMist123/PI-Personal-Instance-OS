from __future__ import annotations

import io
from types import SimpleNamespace

from starlette.testclient import TestClient

from cmx_mcp import remote as remote_module
from cmx_mcp.config import Paths
from cmx_mcp.db import Database
from cmx_mcp.remote import create_remote_app


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
    # Every test in here supplies its own local-model + Gemini doubles; the
    # real RapidOCR weights and the owner's live, metered Gemini key must
    # never be touched from the test suite.
    monkeypatch.setattr(remote_module, "ocr_model_dir_ready", lambda model_dir, tier: True)
    monkeypatch.setattr(remote_module, "gemini_key_configured", lambda paths: False)
    monkeypatch.setattr(remote_module, "_write_recognition_alt", lambda *args: None)
    return create_remote_app(paths), paths, database


def _image(size: int = 32, name: str = "photo.jpg", content_type: str = "image/jpeg"):
    return {"file": (name, io.BytesIO(b"\xff" * size), content_type)}


def _fake_ocr(**fields):
    base = {"text": "hello world", "line_count": 1, "mean_confidence": 0.9}
    base.update(fields)

    def fake(path, **kwargs):
        return base

    return fake


def test_recognize_rejects_a_missing_or_invalid_page_bearer(tmp_path, monkeypatch):
    app, _paths, _database = _app(tmp_path, monkeypatch)
    seen: list[tuple[str, str]] = []

    def fake_verify(base_url, token):
        seen.append((base_url, token))
        return False

    monkeypatch.setattr(remote_module, "_verify_mastodon_bearer", fake_verify)
    with TestClient(app, base_url="https://pi.example") as client:
        anonymous = client.post("/files/recognize", files=_image())
        assert anonymous.status_code == 401 and anonymous.json() == {"error": "unauthorized"}
        assert anonymous.headers["cache-control"] == "no-store"
        assert seen == []  # no bearer header at all: never touch the instance

        bad = client.post(
            "/files/recognize",
            headers={"Authorization": "Bearer not-a-real-web-token"},
            files=_image(),
        )
        assert bad.status_code == 401 and bad.json() == {"error": "unauthorized"}
        assert seen == [("https://pi.example", "not-a-real-web-token")]


def test_recognize_local_trusted_media_skips_the_bearer_on_loopback_host(tmp_path, monkeypatch):
    monkeypatch.setenv("CMX_LOCAL_TRUSTED_MEDIA", "1")
    app, _paths, _database = _app(tmp_path, monkeypatch)
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(
        remote_module, "_verify_mastodon_bearer", lambda base, token: seen.append((base, token)) or True
    )
    monkeypatch.setattr(remote_module, "ocr_image", _fake_ocr())
    with TestClient(app, base_url="https://pi.example") as client:
        response = client.post(
            "/files/recognize",
            headers={"Host": "127.0.0.1:8766"},
            files=_image(),
        )
        assert response.status_code == 200, response.text
        assert seen == []  # local trust short-circuits before the instance is ever asked

        # A dummy bearer must not be treated any differently from no bearer at all.
        response = client.post(
            "/files/recognize",
            headers={"Host": "127.0.0.1:8766", "Authorization": "Bearer dummy"},
            files=_image(name="b.jpg"),
        )
        assert response.status_code == 200, response.text
        assert seen == []


def test_recognize_local_trusted_media_does_not_weaken_the_public_host(tmp_path, monkeypatch):
    monkeypatch.setenv("CMX_LOCAL_TRUSTED_MEDIA", "1")
    app, _paths, _database = _app(tmp_path, monkeypatch)
    monkeypatch.setattr(remote_module, "_verify_mastodon_bearer", lambda base, token: False)
    with TestClient(app, base_url="https://pi.example") as client:
        response = client.post("/files/recognize", files=_image())
    assert response.status_code == 401 and response.json() == {"error": "unauthorized"}


def test_recognize_requires_a_bearer_on_loopback_host_when_the_flag_is_off(tmp_path, monkeypatch):
    app, _paths, _database = _app(tmp_path, monkeypatch)
    monkeypatch.setattr(remote_module, "_verify_mastodon_bearer", lambda base, token: False)
    with TestClient(app, base_url="https://pi.example") as client:
        response = client.post(
            "/files/recognize", headers={"Host": "127.0.0.1:8766"}, files=_image()
        )
    assert response.status_code == 401 and response.json() == {"error": "unauthorized"}


def test_recognize_requires_the_file_field(tmp_path, monkeypatch):
    app, _paths, _database = _app(tmp_path, monkeypatch)
    monkeypatch.setattr(remote_module, "_verify_mastodon_bearer", lambda base, token: True)
    with TestClient(app, base_url="https://pi.example") as client:
        response = client.post(
            "/files/recognize", headers={"Authorization": "Bearer web"}, data={"x": "y"}
        )
    assert response.status_code == 400
    assert "file" in response.json()["error"]
    assert response.headers["cache-control"] == "no-store"


def test_recognize_rejects_an_oversized_upload(tmp_path, monkeypatch):
    app, _paths, _database = _app(tmp_path, monkeypatch)
    monkeypatch.setattr(remote_module, "_verify_mastodon_bearer", lambda base, token: True)
    monkeypatch.setattr(remote_module, "MAX_IMAGE_BYTES", 1024)
    with TestClient(app, base_url="https://pi.example") as client:
        big = client.post(
            "/files/recognize",
            headers={"Authorization": "Bearer web"},
            files=_image(1024 + 1),
        )
        assert big.status_code == 413
        assert big.json()["error"] == "file_too_large"

        empty = client.post(
            "/files/recognize", headers={"Authorization": "Bearer web"}, files=_image(0)
        )
        assert empty.status_code == 400 and empty.json()["error"] == "empty_file"


def test_recognize_is_unavailable_without_a_local_model_directory(tmp_path, monkeypatch):
    app, _paths, _database = _app(tmp_path, monkeypatch)
    monkeypatch.setattr(remote_module, "_verify_mastodon_bearer", lambda base, token: True)
    monkeypatch.setattr(remote_module, "ocr_model_dir_ready", lambda model_dir, tier: False)
    calls: list[int] = []
    monkeypatch.setattr(remote_module, "ocr_image", lambda path, **kwargs: calls.append(1) or {})
    with TestClient(app, base_url="https://pi.example") as client:
        response = client.post(
            "/files/recognize", headers={"Authorization": "Bearer web"}, files=_image()
        )
    assert response.status_code == 503
    assert response.json() == {"error": "recognizer_unavailable"}
    assert calls == []  # the model is never invoked once the directory check fails


def test_recognize_runs_local_ocr_off_the_event_loop_and_returns_it(tmp_path, monkeypatch):
    app, paths, database = _app(tmp_path, monkeypatch)
    monkeypatch.setattr(remote_module, "_verify_mastodon_bearer", lambda base, token: True)

    calls: list[dict] = []

    def fake_ocr_image(path, **kwargs):
        import threading

        calls.append({"path": str(path), "thread": threading.current_thread().name, **kwargs})
        return {"text": "hello world", "line_count": 2, "mean_confidence": 0.87}

    monkeypatch.setattr(remote_module, "ocr_image", fake_ocr_image)
    with TestClient(app, base_url="https://pi.example") as client:
        response = client.post(
            "/files/recognize", headers={"Authorization": "Bearer web"}, files=_image()
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["cache_hit"] is False
    assert body["state"] == "pending"  # no cloud pass configured in this test
    assert body["local"] == {"text": "hello world", "line_count": 2, "mean_confidence": 0.87}
    assert "cloud" not in body and "cloud_error" not in body
    assert len(body["sha256"]) == 64

    assert len(calls) == 1
    assert "MainThread" not in calls[0]["thread"]
    assert list((paths.runtime / "recognize-tmp").glob("*")) == []  # temp file always cleaned up

    stored = database.get_image_recognition(body["sha256"])
    assert stored is not None and stored["local_ocr_text"] == "hello world"


def test_a_cache_hit_skips_both_the_local_model_and_gemini(tmp_path, monkeypatch):
    app, paths, database = _app(tmp_path, monkeypatch)
    monkeypatch.setattr(remote_module, "_verify_mastodon_bearer", lambda base, token: True)
    monkeypatch.setattr(remote_module, "gemini_key_configured", lambda paths: True)

    ocr_calls: list[int] = []
    cloud_calls: list[int] = []

    def fake_ocr_image(path, **kwargs):
        ocr_calls.append(1)
        return {"text": "same photo", "line_count": 1, "mean_confidence": 0.9}

    def fake_recognize_image(image_bytes, **kwargs):
        cloud_calls.append(1)
        return {
            "corrected_text": "same photo",
            "description": "a cat",
            "keywords": "cat, pet",
            "uncertain_text": "",
        }

    monkeypatch.setattr(remote_module, "ocr_image", fake_ocr_image)
    monkeypatch.setattr(remote_module, "recognize_image", fake_recognize_image)

    with TestClient(app, base_url="https://pi.example") as client:
        first = client.post(
            "/files/recognize", headers={"Authorization": "Bearer web"}, files=_image(name="a.jpg")
        )
        assert first.status_code == 200, first.text
        assert first.json()["cache_hit"] is False
        assert first.json()["state"] == "done"
        assert len(ocr_calls) == 1 and len(cloud_calls) == 1

        # Same bytes, different filename: the hash is over content, not name.
        second = client.post(
            "/files/recognize", headers={"Authorization": "Bearer web"}, files=_image(name="b.jpg")
        )
        assert second.status_code == 200, second.text
        body = second.json()
        assert body["cache_hit"] is True
        assert body["state"] == "done"
        assert body["sha256"] == first.json()["sha256"]
        assert body["cloud"]["description"] == "a cat"

    # Neither the local model nor Gemini ran a second time for the same hash.
    assert len(ocr_calls) == 1
    assert len(cloud_calls) == 1


def test_a_gemini_failure_still_returns_200_with_the_local_result_pending(tmp_path, monkeypatch):
    app, paths, database = _app(tmp_path, monkeypatch)
    monkeypatch.setattr(remote_module, "_verify_mastodon_bearer", lambda base, token: True)
    monkeypatch.setattr(remote_module, "gemini_key_configured", lambda paths: True)
    monkeypatch.setattr(remote_module, "ocr_image", _fake_ocr())
    monkeypatch.setattr(
        remote_module, "recognize_image", lambda image_bytes, **kwargs: {"error": "quota_exhausted"}
    )

    with TestClient(app, base_url="https://pi.example") as client:
        response = client.post(
            "/files/recognize", headers={"Authorization": "Bearer web"}, files=_image()
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "pending"
    assert body["local"]["text"] == "hello world"
    assert body["cloud_error"] == "quota_exhausted"
    assert "cloud" not in body

    stored = database.get_image_recognition(body["sha256"])
    assert stored is not None and stored["state"] == "pending"


def test_daily_limit_keeps_local_ocr_and_skips_gemini(tmp_path, monkeypatch):
    monkeypatch.setenv("CMX_GEMINI_DAILY_LIMIT", "0")
    app, _paths, database = _app(tmp_path, monkeypatch)
    monkeypatch.setattr(remote_module, "_verify_mastodon_bearer", lambda base, token: True)
    monkeypatch.setattr(remote_module, "gemini_key_configured", lambda paths: True)
    monkeypatch.setattr(remote_module, "ocr_image", _fake_ocr(text="本机文字"))
    cloud_calls: list[int] = []
    monkeypatch.setattr(
        remote_module,
        "recognize_image",
        lambda *args, **kwargs: cloud_calls.append(1) or {"description": "must not run"},
    )

    with TestClient(app, base_url="https://pi.example") as client:
        response = client.post(
            "/files/recognize", headers={"Authorization": "Bearer web"}, files=_image()
        )

    assert response.status_code == 200
    assert response.json()["local"]["text"] == "本机文字"
    assert response.json()["cloud_error"] == "daily_limit_reached"
    assert cloud_calls == []
    assert database.gemini_daily_attempts() == 0


def test_status_media_linking_when_ids_are_supplied(tmp_path, monkeypatch):
    app, paths, database = _app(tmp_path, monkeypatch)
    monkeypatch.setattr(remote_module, "_verify_mastodon_bearer", lambda base, token: True)
    monkeypatch.setattr(remote_module, "ocr_image", _fake_ocr())

    with TestClient(app, base_url="https://pi.example") as client:
        response = client.post(
            "/files/recognize",
            headers={"Authorization": "Bearer web"},
            data={"status_id": "status-1", "media_id": "media-1"},
            files=_image(),
        )
        assert response.status_code == 200, response.text
        sha256 = response.json()["sha256"]

        linked = database.recognitions_for_status("status-1")
        assert "media-1" in linked
        assert linked["media-1"]["image_sha256"] == sha256

        # Supplying only one of the two ids must not create a link.
        only_status = client.post(
            "/files/recognize",
            headers={"Authorization": "Bearer web"},
            data={"status_id": "status-2"},
            files=_image(name="other.jpg", size=48),
        )
        assert only_status.status_code == 200, only_status.text
    assert database.recognitions_for_status("status-2") == {}


def test_recognition_alt_uses_status_edit_and_preserves_source_and_existing_alt(monkeypatch):
    calls: dict[str, object] = {}

    class FakeResponse:
        def __init__(self, body):
            self.status_code = 200
            self._body = body

        def json(self):
            return self._body

    class FakeClient:
        def __init__(self, **kwargs):
            calls["client"] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, path):
            if path.endswith("/source"):
                return FakeResponse({"text": "原帖正文", "spoiler_text": "提醒"})
            return FakeResponse(
                {
                    "sensitive": True,
                    "language": "zh",
                    "media_attachments": [
                        {"id": "m1", "description": "居民原有 ALT"},
                        {"id": "m2", "description": None},
                    ],
                }
            )

        def put(self, path, json):
            calls["put"] = (path, json)
            return FakeResponse({})

    monkeypatch.setattr(remote_module.httpx, "Client", FakeClient)
    error = remote_module._write_recognition_alt(
        "https://pi.example",
        "page-token",
        "s1",
        "m1",
        {
            "cloud_description": "一杯青柠汽水",
            "cloud_corrected_text": "青柠汽水",
            "search_keywords": "饮料 绿色",
            "local_ocr_text": "",
        },
    )

    assert error is None
    path, payload = calls["put"]
    assert path == "/api/v1/statuses/s1"
    assert payload["status"] == "原帖正文"
    assert payload["spoiler_text"] == "提醒"
    assert payload["media_ids"] == ["m1", "m2"]
    description = payload["media_attributes"][0]["description"]
    assert description.startswith("居民原有 ALT\n\nAI识图：")
    assert "青柠汽水" in description and "饮料 绿色" in description
    assert calls["client"]["headers"]["Authorization"] == "Bearer page-token"
