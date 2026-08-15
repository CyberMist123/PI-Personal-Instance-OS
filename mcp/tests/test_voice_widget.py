from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

from starlette.testclient import TestClient

from cmx_mcp import remote as remote_module
from cmx_mcp.config import Paths
from cmx_mcp.db import Database
from cmx_mcp.remote import create_remote_app
from cmx_mcp.voice_widget import VOICE_WIDGET_JS, VOICE_WIDGET_VERSION


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
NGINX_CONF = REPOSITORY_ROOT / "nginx" / "default.conf"
# Asserted as fragments rather than one exact directive: the same sub_filter
# now injects the Clip Brain site-switch script alongside the voice widget, and
# pinning the whole line makes this test break every time a widget is added.
SUB_FILTER_START = "sub_filter '</body>' "
VOICE_SCRIPT_TAG = '<script src="/files/voice.js?cmx-v=20" defer></script>'


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
    for name in (
        "CMX_WHISPER_MODEL_DIR",
        "CMX_WHISPER_DEVICE",
        "CMX_WHISPER_COMPUTE",
        "CMX_WHISPER_LANGUAGE",
        "CMX_WHISPER_INITIAL_PROMPT",
        "CMX_WHISPER_HOTWORDS",
        "CMX_WHISPER_BEAM_SIZE",
        "CMX_WORKER_POLL_SECONDS",
        "CMX_WHISPER_MAX_SECONDS",
        "CMX_WORKER_MAX_AUDIO_BYTES",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("cmx_mcp.remote.Runtime", FakeRuntime)
    return create_remote_app(paths)


def test_voice_js_is_served_as_a_plain_static_script(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    with TestClient(app, base_url="https://pi.example") as client:
        response = client.get("/files/voice.js")

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/javascript")
    # no-cache, not max-age: a version bump has to reach the browser on the next
# load. Two fixes already sat invisible behind a stale copy of this script.
    assert response.headers["cache-control"] == "no-cache"
    assert VOICE_WIDGET_VERSION in response.headers["etag"]

    body = response.text
    assert "initial-state" in body
    assert "MediaRecorder" in body
    assert "/api/v2/media" in body
    assert "/api/v1/statuses" in body
    assert "__piVoiceWidget" in body
    # v3: publish immediately, then transcribe and edit the transcript in.
    assert "/files/transcribe" in body
    assert "PUT" in body
    assert "media_attributes" in body
    assert "description" in body
    # v4+: styles are inline (no injected stylesheet), so a nonce-locked CSP
    # can no longer hide the button.
    assert "style." in body
    assert 'createElement("style")' not in body


def test_voice_js_route_is_not_shadowed_by_the_filebox_download_route(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    with TestClient(app, base_url="https://pi.example") as client:
        # The templated /files/{bot}/{file}/{name} route must not win: a 200 with
        # JavaScript proves the literal voice.js route is registered ahead of it.
        assert client.get("/files/voice.js").status_code == 200
        assert client.get("/files/gpt/nope/x.txt").status_code == 404


def test_widget_source_stays_backtick_free_and_bails_out_without_a_token() -> None:
    # The script is embedded into nginx config discussions, HTML and shell docs;
    # keeping it free of backticks means no template-literal/shell interpolation
    # ever applies to it, so plain string concatenation is used throughout.
    assert "`" not in VOICE_WIDGET_JS
    assert "${" not in VOICE_WIDGET_JS

    # Logged-out pages have no meta.access_token and must be silently skipped.
    assert "access_token" in VOICE_WIDGET_JS
    assert "media_ids" in VOICE_WIDGET_JS
    # The status goes out with an empty body, then a PUT carrying media_attributes
    # fills in both the body and the audio alt text.
    assert 'status: ""' in VOICE_WIDGET_JS
    assert "media_ids: [mediaId]" in VOICE_WIDGET_JS
    assert "visibility: entryVisibility || visibility" in VOICE_WIDGET_JS
    assert 'fetch("/api/v1/statuses/" + encodeURIComponent(statusId), {' in VOICE_WIDGET_JS
    assert 'method: "PUT"' in VOICE_WIDGET_JS
    assert "media_attributes: [{ id: mediaId, description: clip(text, ALT_MAX_CHARS) }]" in (
        VOICE_WIDGET_JS
    )
    assert "status: clip(text, STATUS_MAX_CHARS)" in VOICE_WIDGET_JS
    # v6 persists the Blob and a stable idempotency key before the first upload,
    # then keeps that entry until its transcript edit succeeds.
    assert 'var OUTBOX_DB = "cmx-voice-outbox";' in VOICE_WIDGET_JS
    assert "window.indexedDB.open(OUTBOX_DB, OUTBOX_VERSION)" in VOICE_WIDGET_JS
    assert VOICE_WIDGET_JS.index("return persistEntry(entry);") < VOICE_WIDGET_JS.index(
        "return publishEntry(entry);"
    )
    assert "idempotencyKey: stableKey" in VOICE_WIDGET_JS
    assert "return outboxDelete(entry.id).catch" in VOICE_WIDGET_JS
    assert 'window.addEventListener("online", retryOutbox);' in VOICE_WIDGET_JS
    # The large mic itself is the finish-and-upload target; ✓ remains equivalent.
    assert 'if (recording) {\n        finishAndPublish();' in VOICE_WIDGET_JS
    assert 'okButton.addEventListener("click", function () {\n      finishAndPublish();' in VOICE_WIDGET_JS
    assert "var clipMime = mimeType;" in VOICE_WIDGET_JS
    assert "function blobName(blob, mime)" in VOICE_WIDGET_JS
    assert "TRANSCRIBE_TIMEOUT_MS = 90000" in VOICE_WIDGET_JS
    assert "STATUS_MAX_CHARS = 4900" in VOICE_WIDGET_JS
    assert "ALT_MAX_CHARS = 1500" in VOICE_WIDGET_JS
    # v4+: never inject a <style> element (Mastodon's CSP style-src is nonce
    # locked); every rule is an inline element.style.* property and the pulse
    # is a setInterval, so no stylesheet and no CSS keyframes exist.
    assert 'createElement("style")' not in VOICE_WIDGET_JS
    assert "@keyframes" not in VOICE_WIDGET_JS
    assert "textContent = [" not in VOICE_WIDGET_JS
    assert "function setStyle(element, styles)" in VOICE_WIDGET_JS
    assert "element.style[keys[i]] = styles[keys[i]]" in VOICE_WIDGET_JS
    assert "function startPulse()" in VOICE_WIDGET_JS and "window.setInterval" in VOICE_WIDGET_JS
    assert VOICE_WIDGET_VERSION == "20" and "voice widget v20" in VOICE_WIDGET_JS
    # v5: the mic is deliberately prominent on this private single-user instance.
    assert 'width: "64px"' in VOICE_WIDGET_JS and 'height: "64px"' in VOICE_WIDGET_JS
    assert 'var MIC_RESTING = "0.5";' in VOICE_WIDGET_JS
    assert 'width: "44px"' in VOICE_WIDGET_JS
    assert "default_privacy" in VOICE_WIDGET_JS
    assert "audio/mp4" in VOICE_WIDGET_JS
    assert "Idempotency-Key" in VOICE_WIDGET_JS
    assert VOICE_WIDGET_JS.count("(function () {") >= 1
    assert VOICE_WIDGET_JS.count("{") == VOICE_WIDGET_JS.count("}")
    assert VOICE_WIDGET_JS.count("(") == VOICE_WIDGET_JS.count(")")


def test_v20_observes_native_image_posts_without_blocking_or_replacing_xhr() -> None:
    assert 'xhr.__cmxPath === "/api/v2/media"' in VOICE_WIDGET_JS
    assert 'xhr.__cmxPath === "/api/v1/statuses"' in VOICE_WIDGET_JS
    assert 'form.append("status_id", record.statusId)' in VOICE_WIDGET_JS
    assert 'form.append("media_id", record.mediaId)' in VOICE_WIDGET_JS
    assert 'fetch("/files/recognize", {' in VOICE_WIDGET_JS
    assert 'if (result && result.alt_error)' in VOICE_WIDGET_JS
    assert 'var DB_NAME = "cmx-image-recognition-outbox";' in VOICE_WIDGET_JS
    assert "memoryRecords[record.mediaId] = record;" in VOICE_WIDGET_JS
    assert 'setAttribute("data-cmx-image-recognition", "20")' in VOICE_WIDGET_JS
    assert "return originalSend.apply(this, arguments);" in VOICE_WIDGET_JS
    assert "responseText =" not in VOICE_WIDGET_JS


def _audio(size: int = 64):
    return {"file": ("voice.webm", io.BytesIO(b"a" * size), "audio/webm")}


def test_transcribe_rejects_a_missing_or_invalid_page_bearer(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    seen: list[tuple[str, str]] = []

    def fake_verify(base_url, token):
        seen.append((base_url, token))
        return False

    monkeypatch.setattr(remote_module, "_verify_mastodon_bearer", fake_verify)
    with TestClient(app, base_url="https://pi.example") as client:
        anonymous = client.post("/files/transcribe", files=_audio())
        assert anonymous.status_code == 401 and anonymous.json() == {"error": "unauthorized"}
        assert seen == []  # no bearer header at all: never touch the instance

        bad = client.post(
            "/files/transcribe",
            headers={"Authorization": "Bearer not-a-real-web-token"},
            files=_audio(),
        )
        assert bad.status_code == 401 and bad.json() == {"error": "unauthorized"}
        assert bad.headers["cache-control"] == "no-store"
        assert seen == [("https://pi.example", "not-a-real-web-token")]


def test_transcribe_local_trusted_media_skips_the_bearer_on_loopback_host(tmp_path, monkeypatch):
    monkeypatch.setenv("CMX_LOCAL_TRUSTED_MEDIA", "1")
    app = _app(tmp_path, monkeypatch)
    model_dir = tmp_path / "model"
    model_dir.mkdir(parents=True, exist_ok=True)
    (model_dir / "model.bin").write_bytes(b"weights")
    monkeypatch.setenv("CMX_WHISPER_MODEL_DIR", str(model_dir))
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(
        remote_module, "_verify_mastodon_bearer", lambda base, token: seen.append((base, token)) or True
    )
    monkeypatch.setattr(
        remote_module, "transcribe_file", lambda path, **kwargs: {"text": "ok"}
    )
    with TestClient(app, base_url="https://pi.example") as client:
        response = client.post(
            "/files/transcribe", headers={"Host": "127.0.0.1:8766"}, files=_audio()
        )
        assert response.status_code == 200, response.text
        assert seen == []  # local trust short-circuits before the instance is ever asked

        # A dummy bearer must not be treated any differently from no bearer at all.
        response = client.post(
            "/files/transcribe",
            headers={"Host": "127.0.0.1:8766", "Authorization": "Bearer dummy"},
            files=_audio(),
        )
        assert response.status_code == 200, response.text
        assert seen == []


def test_transcribe_reports_which_engine_served_the_request(tmp_path, monkeypatch):
    monkeypatch.setenv("CMX_LOCAL_TRUSTED_MEDIA", "1")
    app = _app(tmp_path, monkeypatch)
    model_dir = tmp_path / "model"
    model_dir.mkdir(parents=True, exist_ok=True)
    (model_dir / "model.bin").write_bytes(b"weights")
    monkeypatch.setenv("CMX_WHISPER_MODEL_DIR", str(model_dir))
    seen: list[str] = []

    def fake_transcribe(path, **kwargs):
        seen.append(kwargs.get("engine", "<unset>"))
        return {"text": "转写", "engine": "faster-whisper", "duration": 12.345}

    monkeypatch.setattr(remote_module, "transcribe_file", fake_transcribe)
    with TestClient(app, base_url="https://pi.example") as client:
        response = client.post(
            "/files/transcribe", headers={"Host": "127.0.0.1:8766"}, files=_audio()
        )
    assert response.status_code == 200, response.text
    # Without this the caller cannot tell a good local transcript from a silent
    # degradation to the small Whisper fallback.
    assert response.json() == {"text": "转写", "engine": "faster-whisper", "duration": 12.35}
    assert seen == ["local"]


def test_transcribe_passes_the_cloud_engine_through_and_surfaces_its_extras(tmp_path, monkeypatch):
    monkeypatch.setenv("CMX_LOCAL_TRUSTED_MEDIA", "1")
    app = _app(tmp_path, monkeypatch)
    model_dir = tmp_path / "model"
    model_dir.mkdir(parents=True, exist_ok=True)
    (model_dir / "model.bin").write_bytes(b"weights")
    monkeypatch.setenv("CMX_WHISPER_MODEL_DIR", str(model_dir))
    seen: list[str] = []

    def fake_transcribe(path, **kwargs):
        seen.append(kwargs.get("engine", "<unset>"))
        return {
            "text": "云端转写",
            "engine": "qwen3-asr-flash",
            "detected_language": "zh",
            "emotion": "neutral",
        }

    monkeypatch.setattr(remote_module, "transcribe_file", fake_transcribe)
    with TestClient(app, base_url="https://pi.example") as client:
        response = client.post(
            "/files/transcribe",
            headers={"Host": "127.0.0.1:8766"},
            files=_audio(),
            data={"engine": "cloud"},
        )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "text": "云端转写",
        "engine": "qwen3-asr-flash",
        "detected_language": "zh",
        "emotion": "neutral",
    }
    assert seen == ["cloud"]


def test_transcribe_rejects_an_engine_it_does_not_know(tmp_path, monkeypatch):
    monkeypatch.setenv("CMX_LOCAL_TRUSTED_MEDIA", "1")
    app = _app(tmp_path, monkeypatch)
    called: list[str] = []
    monkeypatch.setattr(
        remote_module, "transcribe_file", lambda path, **kwargs: called.append("ran") or {}
    )
    with TestClient(app, base_url="https://pi.example") as client:
        response = client.post(
            "/files/transcribe",
            headers={"Host": "127.0.0.1:8766"},
            files=_audio(),
            data={"engine": "gpt-4o-transcribe"},
        )
    assert response.status_code == 400
    assert response.json() == {"error": "unknown_engine", "supported": ["local", "cloud"]}
    # An unrecognised engine must not quietly fall through to a local run.
    assert called == []


def test_transcribe_local_trusted_media_does_not_weaken_the_public_host(tmp_path, monkeypatch):
    monkeypatch.setenv("CMX_LOCAL_TRUSTED_MEDIA", "1")
    app = _app(tmp_path, monkeypatch)
    monkeypatch.setattr(remote_module, "_verify_mastodon_bearer", lambda base, token: False)
    with TestClient(app, base_url="https://pi.example") as client:
        response = client.post("/files/transcribe", files=_audio())
    assert response.status_code == 401 and response.json() == {"error": "unauthorized"}


def test_transcribe_requires_a_bearer_on_loopback_host_when_the_flag_is_off(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    monkeypatch.setattr(remote_module, "_verify_mastodon_bearer", lambda base, token: False)
    with TestClient(app, base_url="https://pi.example") as client:
        response = client.post(
            "/files/transcribe", headers={"Host": "127.0.0.1:8766"}, files=_audio()
        )
    assert response.status_code == 401 and response.json() == {"error": "unauthorized"}


def test_transcribe_is_unavailable_without_a_local_model_directory(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    monkeypatch.setattr(remote_module, "_verify_mastodon_bearer", lambda base, token: True)
    with TestClient(app, base_url="https://pi.example") as client:
        unset = client.post(
            "/files/transcribe", headers={"Authorization": "Bearer web"}, files=_audio()
        )
        assert unset.status_code == 503
        assert unset.json() == {"error": "transcriber_unavailable"}

        monkeypatch.setenv("CMX_WHISPER_MODEL_DIR", str(tmp_path / "no-such-model"))
        missing = client.post(
            "/files/transcribe", headers={"Authorization": "Bearer web"}, files=_audio()
        )
        assert missing.status_code == 503
        assert missing.json() == {"error": "transcriber_unavailable"}


def test_transcribe_returns_the_transcript_off_the_event_loop(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    paths = _paths(tmp_path)
    model_dir = tmp_path / "model"
    model_dir.mkdir(parents=True, exist_ok=True)
    (model_dir / "model.bin").write_bytes(b"weights")
    monkeypatch.setenv("CMX_WHISPER_MODEL_DIR", str(model_dir))
    monkeypatch.setenv("CMX_WHISPER_LANGUAGE", "zh")
    monkeypatch.setattr(remote_module, "_verify_mastodon_bearer", lambda base, token: True)

    calls: list[dict] = []

    def fake_transcribe_file(path, **kwargs):
        # Proof the CPU-bound call really ran in a worker thread, with the audio
        # bytes already on disk.
        import threading

        calls.append(
            {
                "path": str(path),
                "bytes": Path(path).read_bytes(),
                "thread": threading.current_thread().name,
                **kwargs,
            }
        )
        return {"text": " 今天天气不错 ", "elapsed_ms": 7, "engine": "qwen3-asr"}

    monkeypatch.setattr(remote_module, "transcribe_file", fake_transcribe_file)
    with TestClient(app, base_url="https://pi.example") as client:
        ok = client.post(
            "/files/transcribe",
            headers={"Authorization": "Bearer web"},
            files={"file": ("voice.m4a", io.BytesIO(b"fake-audio"), "audio/mp4")},
        )

    assert ok.status_code == 200, ok.text
    assert ok.json() == {"text": "今天天气不错", "engine": "qwen3-asr"}
    assert ok.headers["cache-control"] == "no-store"
    assert len(calls) == 1
    assert calls[0]["bytes"] == b"fake-audio"
    assert calls[0]["model_dir"] == str(model_dir) and calls[0]["language"] == "zh"
    assert calls[0]["initial_prompt"]
    assert calls[0]["hotwords"] == "CMX, PI OS"
    assert calls[0]["beam_size"] == 5
    assert calls[0]["path"].endswith(".m4a")
    assert "MainThread" not in calls[0]["thread"]
    # The temporary upload is always removed, success or failure.
    assert list((paths.runtime / "voice-tmp").glob("*")) == []


def test_transcriber_error_becomes_502_and_oversize_audio_413(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    model_dir = tmp_path / "model"
    model_dir.mkdir(parents=True, exist_ok=True)
    (model_dir / "model.bin").write_bytes(b"weights")
    monkeypatch.setenv("CMX_WHISPER_MODEL_DIR", str(model_dir))
    monkeypatch.setenv("CMX_WORKER_MAX_AUDIO_BYTES", str(1024 * 1024))
    monkeypatch.setattr(remote_module, "_verify_mastodon_bearer", lambda base, token: True)
    monkeypatch.setattr(
        remote_module,
        "transcribe_file",
        lambda path, **kwargs: {"error": "transcription_failed", "detail": "boom"},
    )
    with TestClient(app, base_url="https://pi.example") as client:
        failed = client.post(
            "/files/transcribe", headers={"Authorization": "Bearer web"}, files=_audio()
        )
        assert failed.status_code == 502
        assert failed.json() == {"error": "transcription_failed"}

        big = client.post(
            "/files/transcribe",
            headers={"Authorization": "Bearer web"},
            files=_audio(1024 * 1024 + 1),
        )
        assert big.status_code == 413 and big.json()["error"] == "file_too_large"

        empty = client.post(
            "/files/transcribe", headers={"Authorization": "Bearer web"}, files=_audio(0)
        )
        assert empty.status_code == 400 and empty.json()["error"] == "empty_file"

        no_field = client.post(
            "/files/transcribe", headers={"Authorization": "Bearer web"}, data={"x": "y"}
        )
        assert no_field.status_code == 400 and "file" in no_field.json()["error"]
    assert list((tmp_path / "mcp" / "runtime" / "voice-tmp").glob("*")) == []


def test_nginx_injects_the_widget_into_mastodon_html() -> None:
    conf = NGINX_CONF.read_text(encoding="utf-8")

    assert SUB_FILTER_START in conf
    assert VOICE_SCRIPT_TAG in conf
    assert 'proxy_set_header Accept-Encoding "";' in conf
    assert "sub_filter_once on;" in conf
    # Exactly one injection point, in exactly one location block.
    assert conf.count("/files/voice.js") == 1
    assert conf.count("sub_filter '</body>'") == 1
    # The injected page must drop Mastodon's nonce-locked CSP and re-issue one
    # that lets our same-origin script + inline styles run, while staying strict
    # everywhere else.
    assert "proxy_hide_header Content-Security-Policy;" in conf
    # Take the policy from the Mastodon `location /` block specifically. The
    # /clipboard/ block ships its own, much stricter CSP earlier in the file,
    # and matching the first header in the file would silently test that one.
    mastodon_block = conf[conf.rindex("location / {") :]
    csp_line = next(
        line
        for line in mastodon_block.splitlines()
        if "add_header Content-Security-Policy" in line
    )
    assert "script-src" in csp_line and "'unsafe-inline'" in csp_line
    assert "style-src 'self' 'unsafe-inline'" in csp_line
    assert "default-src 'none'" in csp_line
    assert "base-uri 'none'" in csp_line
    assert "frame-ancestors 'none'" in csp_line
    # object-src is intentionally omitted so it inherits default-src 'none'.
    # Same-origin forms must keep working: form-action 'none' silently blocked
    # Mastodon's password login POST during the two-account browser smoke.
    assert "form-action 'self'" in csp_line
    assert "object-src" not in csp_line


def test_nginx_never_caches_the_service_worker_across_mastodon_upgrades() -> None:
    conf = NGINX_CONF.read_text(encoding="utf-8")
    start = conf.index("location = /sw.js")
    block = conf[start : conf.index("\n  }", start)]
    assert 'proxy_hide_header Cache-Control;' in block
    assert 'Cache-Control "no-cache, no-store, must-revalidate"' in block
    assert 'url="/sw.js?cmx-sw=4.6.4-1"' in conf
    assert conf.index("sub_filter '<head>'") < conf.index("sub_filter '</body>'")


def test_webm_is_preferred_over_mp4_for_recording() -> None:
    """Regression: an .m4a shares its container with MP4 video, so Mastodon
    detects video/quicktime, the extension stops matching, and Paperclip's
    spoof check rejects the upload with 422. Desktop Chrome supports audio/mp4,
    so listing it first broke every desktop recording."""
    line = next(
        line for line in VOICE_WIDGET_JS.splitlines() if "MIME_CANDIDATES" in line and "=" in line
    )
    candidates = [part.strip().strip('"') for part in line.split("[", 1)[1].split("]")[0].split(",")]
    assert candidates[0].startswith("audio/webm")
    assert candidates[-1] == "audio/mp4", "MP4 must stay last: it is the iOS Safari fallback only"
    assert "audio/mp4" in candidates, "iOS Safari records nothing else"


def test_widget_rewraps_the_recording_before_uploading_to_mastodon() -> None:
    """Regression for the 422: MediaRecorder only emits WebM or MP4, whose magic
    bytes read as video, so Mastodon either trips Paperclip's spoof check or
    types the upload as a video with no video stream."""
    assert '/files/voice-remux' in VOICE_WIDGET_JS
    assert "return toMp3(entry.blob" in VOICE_WIDGET_JS
    assert "return prepareEntry(entry)\n        .then(ensureMedia)" in VOICE_WIDGET_JS
    assert "return upload(entry.blob" in VOICE_WIDGET_JS
    assert 'entry.filename = MP3_NAME' in VOICE_WIDGET_JS
    assert 'var MP3_NAME = "voice.mp3"' in VOICE_WIDGET_JS


def test_remux_route_is_registered_and_needs_a_session(monkeypatch, tmp_path) -> None:
    app = _app(tmp_path, monkeypatch)
    monkeypatch.setattr(remote_module, "_verify_mastodon_bearer", lambda base, token: False)
    with TestClient(app, base_url="https://pi.example") as client:
        response = client.post(
            "/files/voice-remux",
            files={"file": ("voice.webm", b"not really audio", "audio/webm")},
        )
    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"


def test_convert_rejects_a_file_that_is_not_audio(monkeypatch, tmp_path) -> None:
    app = _app(tmp_path, monkeypatch)
    monkeypatch.setattr(remote_module, "_verify_mastodon_bearer", lambda base, token: True)
    with TestClient(app, base_url="https://pi.example") as client:
        response = client.post(
            "/files/voice-remux",
            files={"file": ("voice.webm", b"not really audio", "audio/webm")},
            headers={"Authorization": "Bearer page-token"},
        )
    # 422, not 500: the upload was understood and refused, not a server fault.
    assert response.status_code == 422
    assert response.json()["error"] == "convert_failed"


def test_player_only_touches_the_owners_own_statuses() -> None:
    from cmx_mcp.voice_owner import VOICE_OWNER_JS
    from cmx_mcp.voice_player import VOICE_PLAYER_JS

    # The gate: a status is only restyled when it links to the logged-in acct.
    # Asserted against the composed script — the check is declared in the owner
    # half and consumed by the wiring half.
    assert "function isOwn(status, acct)" in VOICE_WIDGET_JS
    assert "if (!isOwn(status, acct)) {" in VOICE_WIDGET_JS
    assert VOICE_OWNER_JS.strip() in VOICE_WIDGET_JS
    assert VOICE_PLAYER_JS.strip() in VOICE_WIDGET_JS


def test_player_hides_mastodons_controls_without_removing_them() -> None:
    from cmx_mcp.voice_player import VOICE_PLAYER_JS

    # Mastodon's node is only hidden, never removed.
    assert "function hideNativeChrome(audio)" in VOICE_PLAYER_JS
    assert ".removeChild(original" not in VOICE_PLAYER_JS
    assert "ours.play()" in VOICE_PLAYER_JS and "ours.pause()" in VOICE_PLAYER_JS
    assert "ours.currentTime = ratio * ours.duration" in VOICE_PLAYER_JS


def test_playback_uses_our_own_element_so_mastodon_cannot_pop_it_out() -> None:
    """Mastodon deploys picture-in-picture when its own audio element is
    unmounted while playing. Driving that element meant every timeline
    re-render handed the sound to a popped-out player and left a "restore"
    placeholder: the bar went silent while audio played in the corner. Ours
    stays paused for ever, so that branch never fires."""
    from cmx_mcp.voice_player import VOICE_PLAYER_JS

    assert 'var ours = document.createElement("audio")' in VOICE_PLAYER_JS
    assert "ours.setAttribute(OWN_MARK" in VOICE_PLAYER_JS
    # Mastodon's element is never started, seeked or listened to for playback.
    for forbidden in (
        "audio.play()",
        "audio.pause()",
        "audio.currentTime =",
        'audio.addEventListener("timeupdate"',
        'audio.addEventListener("play"',
    ):
        assert forbidden not in VOICE_PLAYER_JS, forbidden
    # And our element must not be mistaken for a status attachment: every sweep
    # selects on the tag name, so both entry points check the mark.
    assert VOICE_PLAYER_JS.count('getAttribute(OWN_MARK) === "1"') >= 2


def test_only_one_clip_can_be_audible() -> None:
    """Our element lives inside a host React can drop at any moment, and a
    detached media element keeps playing. Without this a remount left a voice
    nobody could pause."""
    from cmx_mcp.voice_player import VOICE_PLAYER_JS

    assert "function playOnly(element)" in VOICE_PLAYER_JS
    assert "playOnly(ours)" in VOICE_PLAYER_JS
    # A dropped host silences its own element before the replacement is built.
    assert "audio._piOwn.pause()" in VOICE_PLAYER_JS


def test_the_native_chrome_is_clipped_rather_than_undisplayed() -> None:
    """display:none takes the element out of the render tree, and iOS will not
    play media that is not rendered — the phone showed a player that did
    nothing. Clipping hides it just as completely and keeps it playable."""
    from cmx_mcp.voice_player import VOICE_PLAYER_JS

    assert 'display: "none"' not in VOICE_PLAYER_JS
    assert 'style.display = "none"' not in VOICE_PLAYER_JS
    assert 'clip: "rect(0 0 0 0)"' in VOICE_PLAYER_JS
    assert 'opacity: "0"' in VOICE_PLAYER_JS
    assert 'pointerEvents: "none"' in VOICE_PLAYER_JS
    # Invisible controls that still take focus are a trap for a screen reader.
    assert 'setAttribute("aria-hidden", "true")' in VOICE_PLAYER_JS


def test_waveform_sampling_survives_a_source_that_arrives_late() -> None:
    """decorate runs once per element, and at that moment the element usually
    has no chosen source, so a single `if (audio.currentSrc)` attempt meant the
    real waveform never appeared and every bar stayed at placeholder height."""
    from cmx_mcp.voice_player import VOICE_PLAYER_JS

    assert "function mediaSource(audio)" in VOICE_PLAYER_JS
    assert 'audio.currentSrc || audio.getAttribute("src")' in VOICE_PLAYER_JS
    assert 'querySelector("source[src]")' in VOICE_PLAYER_JS
    # The one-shot gate is gone: sampling is retried on the events that only
    # fire once a resource exists.
    assert "if (audio.currentSrc && window.AudioContext)" not in VOICE_PLAYER_JS
    assert "function sampleWaveform()" in VOICE_PLAYER_JS
    assert 'ours.addEventListener("canplay", sampleWaveform)' in VOICE_PLAYER_JS
    assert VOICE_PLAYER_JS.count("sampleWaveform()") >= 3
    # ...but not without bound: a failing fetch must not retry forever.
    assert "sampleAttempts >= 3" in VOICE_PLAYER_JS


def test_refused_playback_is_reported_instead_of_silent() -> None:
    """play() rejects rather than throws. An unhandled rejection is exactly how
    "the button does nothing" stays unexplained."""
    from cmx_mcp.voice_player import VOICE_PLAYER_JS

    assert "var started = ours.play();" in VOICE_PLAYER_JS
    assert 'warn("playback was refused by the browser", error)' in VOICE_PLAYER_JS


def test_player_is_idempotent_under_react_rerenders() -> None:
    # Asserted against the composed script: the mark is declared in the drawing
    # half and consumed by the wiring half, so only the whole thing is coherent.
    assert 'PLAYER_MARK = "data-pi-voice-player"' in VOICE_WIDGET_JS
    assert 'audio.getAttribute(PLAYER_MARK) === "1"' in VOICE_WIDGET_JS
    assert "new MutationObserver" in VOICE_WIDGET_JS


def test_player_uses_one_ink_that_flips_with_the_theme() -> None:
    assert "#eef1f5" in VOICE_WIDGET_JS      # dark theme ink
    assert "#4d535f" in VOICE_WIDGET_JS      # light theme ink: slate, not black
    assert "#6364ff" not in VOICE_WIDGET_JS  # no accent hue anywhere
    assert "KaiTi" in VOICE_WIDGET_JS


def test_kai_is_self_hosted_for_phones(tmp_path, monkeypatch) -> None:
    """iOS and Android ship no Kai, so the transcript would quietly fall back to
    a serif on exactly the devices the recordings are made on."""
    # Registered via the Font Loading API: the widget stays style-element-free,
    # so it does not depend on style-src being relaxed for it.
    assert "new window.FontFace(" in VOICE_WIDGET_JS
    assert "document.fonts.add(loaded)" in VOICE_WIDGET_JS
    assert "/files/fonts/lxgw-wenkai-screen-gb2312.woff2" in VOICE_WIDGET_JS
    assert 'display: "swap"' in VOICE_WIDGET_JS
    # System Kai must still win where it exists: no reason to fetch 1.9 MB.
    # Sliced from the declaration — the tier comment above it names the families too.
    stack = VOICE_WIDGET_JS[VOICE_WIDGET_JS.index("var KAI ="):]
    assert stack.index('"Kaiti SC"') < stack.index("LXGW WenKai GB")

    font = REPOSITORY_ROOT / "mcp" / "assets" / "fonts" / "lxgw-wenkai-screen-gb2312.woff2"
    assert font.is_file()
    assert font.stat().st_size < 3 * 1024**2, "subset regressed towards the 25 MB full face"
    # SIL OFL requires the licence to travel with the font.
    assert (font.parent / "LXGW-WenKai-OFL.txt").is_file()

    app = _app(tmp_path, monkeypatch)
    with TestClient(app, base_url="https://pi.example") as client:
        ok = client.get("/files/fonts/lxgw-wenkai-screen-gb2312.woff2")
        traversal = client.get("/files/fonts/..%2F..%2Fsecrets.woff2")
        wrong_type = client.get("/files/fonts/evil.js")
    assert ok.status_code == 200
    assert ok.headers["content-type"] == "font/woff2"
    assert "immutable" in ok.headers["cache-control"]
    assert traversal.status_code == 404
    assert wrong_type.status_code == 404


def test_the_mic_is_released_before_the_upload_finishes() -> None:
    """The point of the recorder is to record and walk away. Waiting on remux,
    upload and publish put an hourglass in front of that."""
    released = VOICE_WIDGET_JS.index('flash("\\u5df2\\u4fdd\\u5b58')   # "已保存"
    publish = VOICE_WIDGET_JS.index("return publishEntry(entry);", released)
    assert released < publish
    assert "return prepareEntry(entry)\n        .then(ensureMedia)" in VOICE_WIDGET_JS


def test_player_sits_above_the_transcript_not_at_the_end() -> None:
    """Appending to the status pushed the player below the reply/boost row; the
    first fix moved the text instead, which fought React. Inserting the player
    before the text gets the same reading order and moves nothing."""
    assert "anchor.parentElement.insertBefore(host, anchor)" in VOICE_WIDGET_JS
    assert "anchor.appendChild(host)" not in VOICE_WIDGET_JS
    assert "original.nextSibling" not in VOICE_WIDGET_JS


def test_kai_beats_mastodons_own_font_rule() -> None:
    """Mastodon styles the <p> inside .status__content, so a font-family on the
    container alone never shows."""
    assert 'setProperty("font-family", KAI, "important")' in VOICE_WIDGET_JS
    assert 'querySelectorAll("p, span, a")' in VOICE_WIDGET_JS
    assert "applyKai(content)" in VOICE_WIDGET_JS


def test_the_upload_format_plays_on_ios() -> None:
    """Ogg cleared Mastodon's magic-byte check but WebKit will not decode it, so
    every browser on iPhone went silent. MP3 is the one format both ends take."""
    from cmx_mcp.voice_media import MP3_MIME, MP3_SUFFIX

    assert MP3_MIME == "audio/mpeg" and MP3_SUFFIX == ".mp3"
    assert 'var MP3_NAME = "voice.mp3"' in VOICE_WIDGET_JS
    # The container must be gone from the code path, not merely unmentioned:
    # "Logged-out" contains the substring, so match the real references.
    assert ".ogg" not in VOICE_WIDGET_JS
    assert "audio/ogg" not in VOICE_WIDGET_JS
    assert "toOgg" not in VOICE_WIDGET_JS


def test_a_missing_account_object_does_not_disable_the_player() -> None:
    """#initial-state carries the account on some views and not others, which is
    why the player worked on the phone and not on the desktop timeline."""
    assert "function resolveAcct(state)" in VOICE_WIDGET_JS
    assert "/api/v1/accounts/verify_credentials" in VOICE_WIDGET_JS
    assert "startWatching(acct)" in VOICE_WIDGET_JS


def test_transcripts_are_biased_to_simplified_chinese() -> None:
    """Whisper renders Mandarin in Traditional about as often as Simplified and
    offers no flag; the decoder conditions on the prompt instead."""
    from cmx_mcp.transcribe import SIMPLIFIED_PROMPT

    import inspect

    from cmx_mcp import transcribe as module

    signature = inspect.signature(module.transcribe_file)
    source = inspect.getsource(module.transcribe_file)
    assert signature.parameters["initial_prompt"].default == SIMPLIFIED_PROMPT
    assert "initial_prompt=initial_prompt or None" in source
    assert "简体" in SIMPLIFIED_PROMPT


def test_the_licensed_kai_never_reaches_the_repository() -> None:
    """Every commercial Kai grant excludes webfont embedding, so the licensed
    face is generated on the machine and must stay off GitHub. The open-source
    face ships so a fresh clone still renders Kai on phones."""
    import subprocess

    ignore = (REPOSITORY_ROOT / "mcp" / ".gitignore").read_text(encoding="utf-8")
    assert "assets/fonts/*-private.woff2" in ignore

    tracked = subprocess.run(
        ["git", "ls-files", "mcp/assets/fonts"],
        cwd=REPOSITORY_ROOT, capture_output=True, text=True, encoding="utf-8",
    ).stdout
    assert "lxgw-wenkai-screen-gb2312.woff2" in tracked
    assert "-private.woff2" not in tracked, "a licensed font is staged for commit"

    # Preference order: system Kai, then the licensed local face, then open source.
    stack = VOICE_WIDGET_JS[VOICE_WIDGET_JS.index("var KAI ="):]
    assert stack.index("KaiTi") < stack.index("PI Kai Local") < stack.index("LXGW WenKai GB")
    # A machine without the private face must degrade silently, not warn.
    assert "kai-private.woff2" in VOICE_WIDGET_JS


def test_player_also_claims_attachments_mastodon_typed_as_video() -> None:
    """An ambiguous container gets filed as video — every pre-MP3 recording was.
    The element still exposes the same play and seek surface."""
    assert 'querySelectorAll("audio, video")' in VOICE_WIDGET_JS


def test_the_clock_shows_one_value_so_the_bars_keep_their_width() -> None:
    """It grew from 0:00 to 00:02 / 00:02 after the bars had been sized to the
    old width, and they ran on underneath the text."""
    assert "clock.textContent = mmssClock(showing)" in VOICE_WIDGET_JS
    assert '" / "' not in VOICE_WIDGET_JS
    assert 'minWidth: "42px"' in VOICE_WIDGET_JS


def test_no_react_owned_node_is_ever_relocated() -> None:
    """Moving .status__content under the player made React put it back, which
    tripped the observer, which moved it again: the timeline strobed."""
    assert "host.appendChild(content)" not in VOICE_WIDGET_JS
    assert "anchor.parentElement.insertBefore(host, anchor)" in VOICE_WIDGET_JS
    # Attributes must stay unobserved, or our own restyling re-enters the loop.
    assert "{ childList: true, subtree: true }" in VOICE_WIDGET_JS
    assert "attributes: true" not in VOICE_WIDGET_JS


def test_the_swap_happens_on_the_next_frame() -> None:
    """A 120 ms coalescing window was long enough to watch Mastodon's own player
    appear and then be replaced."""
    assert "window.requestAnimationFrame" in VOICE_WIDGET_JS
    assert "}, 120);" not in VOICE_WIDGET_JS


def test_a_dropped_host_is_put_back() -> None:
    """React can discard our node mid-render; the status would then show a
    hidden player and nothing else."""
    assert "audio._piHost && audio._piHost.isConnected" in VOICE_WIDGET_JS
    assert "audio.removeAttribute(PLAYER_MARK)" in VOICE_WIDGET_JS


def test_only_the_hiding_happens_before_the_frame_is_painted() -> None:
    """Mastodon's player must not be painted, so hiding is synchronous inside
    the observer callback. Building the replacement there as well is what v14
    did, and it broke both the waveform and the sound: React has not finished
    with the element that early. Only the hiding has to beat the paint."""
    assert "function claimEarly(records)" in VOICE_WIDGET_JS
    assert "claimQuietly(element, acct)" in VOICE_WIDGET_JS
    assert "function claimQuietly(audio, acct)" in VOICE_WIDGET_JS
    assert "decorate(element, acct)" not in VOICE_WIDGET_JS
    # claimQuietly hides and stops; the build belongs to the coalesced sweep.
    body = VOICE_WIDGET_JS[VOICE_WIDGET_JS.index("function claimQuietly(audio, acct)"):]
    body = body[: body.index("function decorate(")]
    assert "hideNativeChrome(audio)" in body
    assert "createElement" not in body
    claim = VOICE_WIDGET_JS.index("claimEarly(records)")
    coalesce = VOICE_WIDGET_JS.index("window.requestAnimationFrame(flush)")
    assert claim < coalesce, "the synchronous claim must precede the coalesced sweep"


def test_a_console_probe_reports_where_the_chain_breaks() -> None:
    """The desktop timeline has never been claimed and the offline harness
    cannot say why: its markup is an imitation, so it proves there is no loop
    and no leak but never that a selector matches the real thing."""
    assert "window.__piVoiceDebug = function ()" in VOICE_WIDGET_JS
    assert "function installDebug(acct)" in VOICE_WIDGET_JS
    for field in ("media:", "inStatus:", "own:", "claimed:", "acctLinks:"):
        assert field in VOICE_WIDGET_JS


def test_the_sweep_cannot_deadlock_in_a_hidden_tab() -> None:
    """requestAnimationFrame never fires while the page is not compositing. With
    rAF alone the in-flight flag stayed set and the observer was dead for the
    rest of the session — the harness caught this as hosts stuck at zero."""
    assert "window.requestAnimationFrame(flush)" in VOICE_WIDGET_JS
    assert "window.setTimeout(flush, 100)" in VOICE_WIDGET_JS


def test_one_resize_listener_for_the_whole_page() -> None:
    """One per player leaked without bound: a virtualised timeline remounts rows
    every time you scroll, and each handler pinned a detached subtree."""
    assert "var relayoutBound = false" in VOICE_WIDGET_JS
    assert VOICE_WIDGET_JS.count('addEventListener("resize"') == 1
