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
VOICE_SCRIPT_TAG = '<script src="/files/voice.js" defer></script>'


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
    # v3 ordering: the status goes out with an empty body, then a PUT carrying
    # media_attributes fills in both the body and the audio alt text.
    assert 'body: JSON.stringify({ status: "", media_ids: [mediaId], visibility: visibility })' in (
        VOICE_WIDGET_JS
    )
    assert 'fetch("/api/v1/statuses/" + encodeURIComponent(statusId), {' in VOICE_WIDGET_JS
    assert 'method: "PUT"' in VOICE_WIDGET_JS
    assert "media_attributes: [{ id: mediaId, description: clip(text, ALT_MAX_CHARS) }]" in (
        VOICE_WIDGET_JS
    )
    assert "status: clip(text, STATUS_MAX_CHARS)" in VOICE_WIDGET_JS
    # publish() must resolve before backfill() is even called.
    assert VOICE_WIDGET_JS.index(".then(publish)") < VOICE_WIDGET_JS.index(
        "backfill(statusId, clipMediaId, clipBlob, clipName)"
    )
    # The background edit reads only locals captured at ✓ time, so a second
    # recording started mid-transcription cannot redirect it.
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
    assert VOICE_WIDGET_VERSION == "8" and "voice widget v8" in VOICE_WIDGET_JS
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
        return {"text": " 今天天气不错 ", "elapsed_ms": 7}

    monkeypatch.setattr(remote_module, "transcribe_file", fake_transcribe_file)
    with TestClient(app, base_url="https://pi.example") as client:
        ok = client.post(
            "/files/transcribe",
            headers={"Authorization": "Bearer web"},
            files={"file": ("voice.m4a", io.BytesIO(b"fake-audio"), "audio/mp4")},
        )

    assert ok.status_code == 200, ok.text
    assert ok.json() == {"text": "今天天气不错"}
    assert ok.headers["cache-control"] == "no-store"
    assert len(calls) == 1
    assert calls[0]["bytes"] == b"fake-audio"
    assert calls[0]["model_dir"] == str(model_dir) and calls[0]["language"] == "zh"
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
    assert "form-action 'none'" in csp_line
    assert "object-src" not in csp_line


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
    remux_at = VOICE_WIDGET_JS.index("return toOgg(recorded")
    upload_at = VOICE_WIDGET_JS.index("return upload(clipBlob")
    assert remux_at < upload_at, "the remux must happen before /api/v2/media"
    assert 'clipName = OGG_NAME' in VOICE_WIDGET_JS
    assert 'var OGG_NAME = "voice.ogg"' in VOICE_WIDGET_JS


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


def test_remux_rejects_a_file_that_is_not_audio(monkeypatch, tmp_path) -> None:
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
    assert response.json()["error"] == "remux_failed"


def test_player_only_touches_the_owners_own_statuses() -> None:
    from cmx_mcp.voice_player import VOICE_PLAYER_JS

    # The gate: a status is only restyled when it links to the logged-in acct.
    assert "function isOwn(status, acct)" in VOICE_PLAYER_JS
    assert "if (!isOwn(status, acct)) {" in VOICE_PLAYER_JS
    assert "return;" in VOICE_PLAYER_JS
    assert VOICE_PLAYER_JS.strip() in VOICE_WIDGET_JS


def test_player_hides_mastodons_controls_without_removing_them() -> None:
    from cmx_mcp.voice_player import VOICE_PLAYER_JS

    # Mastodon keeps owning the <audio>; we only hide its chrome and drive it.
    assert 'original.style.display = "none"' in VOICE_PLAYER_JS
    assert ".removeChild(original" not in VOICE_PLAYER_JS
    assert "audio.play()" in VOICE_PLAYER_JS and "audio.pause()" in VOICE_PLAYER_JS
    assert "audio.currentTime = ratio * audio.duration" in VOICE_PLAYER_JS


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
    assert "/files/fonts/lxgw-wenkai-gb2312.woff2" in VOICE_WIDGET_JS
    assert 'display: "swap"' in VOICE_WIDGET_JS
    # System Kai must still win where it exists: no reason to fetch 1.6 MB.
    assert VOICE_WIDGET_JS.index('"Kaiti SC"') < VOICE_WIDGET_JS.index("LXGW WenKai GB")

    font = REPOSITORY_ROOT / "mcp" / "assets" / "fonts" / "lxgw-wenkai-gb2312.woff2"
    assert font.is_file()
    assert font.stat().st_size < 3 * 1024**2, "subset regressed towards the 25 MB full face"
    # SIL OFL requires the licence to travel with the font.
    assert (font.parent / "LXGW-WenKai-OFL.txt").is_file()

    app = _app(tmp_path, monkeypatch)
    with TestClient(app, base_url="https://pi.example") as client:
        ok = client.get("/files/fonts/lxgw-wenkai-gb2312.woff2")
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
    released = VOICE_WIDGET_JS.index('flash("\\u5df2\\u53d1\\u9001')   # "已发送"
    remux = VOICE_WIDGET_JS.index("return toOgg(recorded")
    upload = VOICE_WIDGET_JS.index("return upload(clipBlob")
    assert released < remux < upload


def test_player_sits_where_mastodons_player_was() -> None:
    """Appending to the status pushed the player below the reply/boost row."""
    assert "original.parentElement.insertBefore(host, original.nextSibling)" in VOICE_WIDGET_JS
    assert "anchor.appendChild(host)" not in VOICE_WIDGET_JS


def test_kai_beats_mastodons_own_font_rule() -> None:
    """Mastodon styles the <p> inside .status__content, so a font-family on the
    container alone never shows."""
    assert 'setProperty("font-family", KAI, "important")' in VOICE_WIDGET_JS
    assert 'querySelectorAll("p, span, a")' in VOICE_WIDGET_JS
    assert "applyKai(content)" in VOICE_WIDGET_JS
