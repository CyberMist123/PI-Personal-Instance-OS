"""The voice observer: closed vocabulary, fail-open wiring, baseline storage.

The vocabulary tests here are deliberately golden-copy strict. The reader's
whole use of a voice_note is word-for-word comparison across weeks, so a token
rewording that would be a harmless cleanup anywhere else is a breaking change
here — these tests exist to make that breakage loud.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
from starlette.testclient import TestClient

from cmx_mcp import remote as remote_module
from cmx_mcp import voice_observer as observer_module
from cmx_mcp.config import Paths
from cmx_mcp.db import Database
from cmx_mcp.remote import create_remote_app
from cmx_mcp.voice_observer import (
    BOOL_FIELDS,
    ENUM_FIELDS,
    R18_EVENT_NAMES,
    observe_voice,
    render_r18_note,
    render_voice_note,
    validate_r18_observation,
    validate_observation,
)


def _observation(**overrides):
    """A fully-default (nothing notable) form, with per-test overrides."""
    fields = {name: allowed[0] for name, allowed in ENUM_FIELDS.items()}
    fields.update({name: False for name in BOOL_FIELDS})
    fields.update(overrides)
    return fields


# --- the closed vocabulary ---------------------------------------------------


def test_a_quiet_ordinary_clip_still_renders_pace_and_background():
    # "Nothing notable" is still information: her usual pace, somewhere quiet.
    assert render_voice_note(validate_observation(_observation())) == "[声音: 语速中等 · 背景安静]"


def test_the_questionnaire_sample_renders_word_for_word():
    observed = validate_observation(
        _observation(speed="slow", pause="many", volume="soft", breathy=True)
    )
    assert render_voice_note(observed) == "[声音: 语速偏慢 · 停顿多 · 音量轻 · 气声 · 背景安静]"


def test_every_token_the_vocabulary_can_ever_emit_is_frozen():
    # The complete render surface. If a change to the module alters any line
    # here, it is changing the vocabulary itself and must be a deliberate,
    # owner-visible decision — stored baselines are written in these words.
    everything = validate_observation(
        _observation(
            speed="fast",
            speed_change="speeding_up",
            pause="many",
            volume="loud",
            pitch_range="varied",
            laugh="clear",
            voice_quality="trembling",
            background="wind",
            breathy=True,
            sigh=True,
            breath_audible=True,
            restart=True,
            self_correction=True,
        )
    )
    assert render_voice_note(everything) == (
        "[声音: 语速偏快 · 越说越快 · 停顿多 · 音量大 · 起伏大 · 气声 · 声音发抖"
        " · 笑声 · 叹气 · 吸气明显 · 重说 · 改口 · 背景风声]"
    )
    other_arm = validate_observation(
        _observation(
            speed="slow",
            speed_change="slowing_down",
            volume="soft",
            pitch_range="flat",
            laugh="light",
            voice_quality="tense",
            background="voices",
        )
    )
    assert render_voice_note(other_arm) == (
        "[声音: 语速偏慢 · 越说越慢 · 音量轻 · 起伏小 · 声音发紧 · 轻笑 · 背景人声]"
    )
    backgrounds = {
        "quiet": "背景安静",
        "voices": "背景人声",
        "noisy": "背景嘈杂",
        "outdoor": "背景户外",
        "wind": "背景风声",
        "music": "背景音乐",
    }
    for value, token in backgrounds.items():
        note = render_voice_note(validate_observation(_observation(background=value)))
        assert note == f"[声音: 语速中等 · {token}]"


def test_no_emotion_word_can_reach_the_reader():
    # The defense is structural: there is no free-text field at all, and every
    # off-vocabulary value is rejected wholesale rather than rendered.
    assert validate_observation(_observation(speed="tired")) is None
    assert validate_observation(_observation(background="sad-room")) is None
    assert validate_observation(_observation(breathy="yes")) is None
    missing = _observation()
    missing.pop("restart")
    assert validate_observation(missing) is None
    assert validate_observation("语速偏慢，听起来有些疲惫") is None


# --- the Gemini call ---------------------------------------------------------


def _fake_paths(tmp_path) -> Paths:
    return Paths(
        home=tmp_path / "mcp",
        runtime=tmp_path / "mcp" / "runtime",
        database=tmp_path / "mcp" / "runtime" / "cmx.sqlite3",
        secrets=tmp_path / "mcp" / "runtime" / "secrets",
        logs=tmp_path / "mcp" / "runtime" / "logs",
    )


def _gemini_reply(fields) -> SimpleNamespace:
    body = {
        "candidates": [{"content": {"parts": [{"text": json.dumps(fields)}]}}]
    }
    return SimpleNamespace(status_code=200, text=json.dumps(body), json=lambda: body)


def test_observe_voice_returns_the_validated_form_and_its_rendering(tmp_path, monkeypatch):
    monkeypatch.setattr(observer_module, "load_gemini_key", lambda paths: "test-key")
    sent = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        sent["url"] = url
        sent["body"] = json
        return _gemini_reply(_observation(speed="slow", pause="many"))

    monkeypatch.setattr(
        observer_module, "httpx", SimpleNamespace(post=fake_post, HTTPError=httpx.HTTPError)
    )
    outcome = observe_voice(b"mp3-bytes", paths=_fake_paths(tmp_path))

    assert outcome["voice_note"] == "[声音: 语速偏慢 · 停顿多 · 背景安静]"
    assert outcome["observed"]["speed"] == "slow"
    # The schema is the vocabulary lock: every enum field travels as an enum.
    schema = sent["body"]["generationConfig"]["responseSchema"]
    assert set(schema["required"]) == set(ENUM_FIELDS) | set(BOOL_FIELDS)
    for name, allowed in ENUM_FIELDS.items():
        assert schema["properties"][name]["enum"] == list(allowed)
    # And the audio itself rode along inline.
    assert sent["body"]["contents"][0]["parts"][1]["inlineData"]["mimeType"] == "audio/mp3"


def test_an_off_vocabulary_reply_is_rejected_not_rendered(tmp_path, monkeypatch):
    monkeypatch.setattr(observer_module, "load_gemini_key", lambda paths: "test-key")
    monkeypatch.setattr(
        observer_module,
        "httpx",
        SimpleNamespace(
            post=lambda *a, **k: _gemini_reply(_observation(speed="soft-and-tired")),
            HTTPError=httpx.HTTPError,
        ),
    )
    outcome = observe_voice(b"mp3-bytes", paths=_fake_paths(tmp_path))
    assert outcome["error"] == "invalid_response"


def test_missing_key_and_quota_use_the_shared_error_buckets(tmp_path, monkeypatch):
    monkeypatch.setattr(observer_module, "load_gemini_key", lambda paths: None)
    assert observe_voice(b"x", paths=_fake_paths(tmp_path)) == {"error": "not_configured"}

    monkeypatch.setattr(observer_module, "load_gemini_key", lambda paths: "test-key")
    quota = SimpleNamespace(status_code=429, text="{}", json=lambda: {})
    monkeypatch.setattr(
        observer_module,
        "httpx",
        SimpleNamespace(post=lambda *a, **k: quota, HTTPError=httpx.HTTPError),
    )
    assert observe_voice(b"x", paths=_fake_paths(tmp_path)) == {"error": "quota_exhausted"}


def _r18_observation():
    candidates = {name: 0.0 for name in R18_EVENT_NAMES}
    candidates.update({"moan": 0.68, "sigh": 0.21, "nonlexical_vowel": 0.11})
    return {
        "events": [{
            "start_ms": 1400, "end_ms": 2900, "candidates": candidates,
            "perceptual": ["breathy", "soft", "wavering", "drawn_out", "fading"],
            "pitch_relative": "higher", "intensity": "soft", "attack": "gradual", "release": "fading",
        }],
        "trajectory": ["moan", "sigh", "moan"],
    }


def test_tg_r18_returns_multicandidate_events_and_compact_voice_note(tmp_path, monkeypatch):
    monkeypatch.setattr(observer_module, "load_gemini_key", lambda paths: "test-key")
    sent = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        sent["schema"] = json["generationConfig"]["responseSchema"]
        return _gemini_reply(_r18_observation())

    monkeypatch.setattr(observer_module, "httpx", SimpleNamespace(post=fake_post, HTTPError=httpx.HTTPError))
    outcome = observe_voice(b"mp3-bytes", paths=_fake_paths(tmp_path), mode="tg_r18")

    assert outcome["nvv"]["events"][0]["candidates"]["moan"] == 0.68
    assert outcome["nvv"]["events"][0]["pitch_relative"] == "higher"
    assert outcome["nvv"]["note"] == (
        "<voice>\n[moan: 气声柔 · 起伏不稳 · 拖长 · 渐弱 · 比平时音高偏高 · 起音渐入"
        " | 偏moan，或为叹息]\n走向: 有声呼气\n</voice>"
    )
    candidate_schema = sent["schema"]["properties"]["events"]["items"]["properties"]["candidates"]
    assert candidate_schema["maxItems"] == 3
    assert set(candidate_schema["items"]["properties"]["label"]["enum"]) == set(R18_EVENT_NAMES)
    assert render_r18_note(validate_r18_observation(_r18_observation())) == outcome["nvv"]["note"]


def test_r18_note_inlines_timed_transcript_and_keeps_candidate_ambiguity():
    observed = validate_r18_observation(_r18_observation())
    note = render_r18_note(
        observed,
        transcript="你听我说 嗯",
        segments=[
            {"start_ms": 0, "end_ms": 1200, "text": "你听我说"},
            {"start_ms": 3000, "end_ms": 3600, "text": "嗯"},
        ],
    )
    assert note == (
        "<voice>\n“你听我说” [moan: 气声柔 · 起伏不稳 · 拖长 · 渐弱 · 比平时音高偏高 · 起音渐入"
        " | 偏moan，或为叹息] “嗯”\n走向: 说话 → 有声呼气\n</voice>"
    )


# --- baseline storage --------------------------------------------------------


def test_observations_accumulate_first_write_wins(tmp_path):
    database = Database(_fake_paths(tmp_path).database)
    database.initialize()
    database.record_voice_observation(
        "abc", observed=_observation(speed="slow"), voice_note="[声音: 语速偏慢 · 背景安静]", model="m1"
    )
    # A retry (or a later model) must not rewrite the row earlier comparisons used.
    database.record_voice_observation(
        "abc", observed=_observation(speed="fast"), voice_note="[声音: 语速偏快 · 背景安静]", model="m2"
    )
    row = database.get_voice_observation("abc")
    assert row["voice_note"] == "[声音: 语速偏慢 · 背景安静]"
    assert json.loads(row["observed_json"])["speed"] == "slow"
    assert row["model"] == "m1"
    assert database.get_voice_observation("missing") is None


# --- the transcribe endpoint side-channel ------------------------------------


def _app(tmp_path, monkeypatch):
    paths = _fake_paths(tmp_path)
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
        "CMX_WHISPER_LANGUAGE",
        "CMX_WHISPER_INITIAL_PROMPT",
        "CMX_WHISPER_HOTWORDS",
        "CMX_WHISPER_BEAM_SIZE",
        "CMX_WORKER_POLL_SECONDS",
        "CMX_WHISPER_MAX_SECONDS",
        "CMX_WORKER_MAX_AUDIO_BYTES",
        "CMX_VOICE_OBSERVER",
    ):
        monkeypatch.delenv(name, raising=False)
    model_dir = tmp_path / "model"
    model_dir.mkdir(parents=True, exist_ok=True)
    (model_dir / "model.bin").write_bytes(b"weights")
    monkeypatch.setenv("CMX_WHISPER_MODEL_DIR", str(model_dir))
    monkeypatch.setattr("cmx_mcp.remote.Runtime", FakeRuntime)
    monkeypatch.setattr(remote_module, "_verify_mastodon_bearer", lambda base, token: True)
    monkeypatch.setattr(
        remote_module,
        "transcribe_file",
        lambda path, **kwargs: {"text": "没事", "engine": "local", "elapsed_ms": 5},
    )
    return create_remote_app(paths), paths, database


def _post_audio(client, payload: bytes = b"fake-audio"):
    return client.post(
        "/files/transcribe",
        headers={"Authorization": "Bearer web"},
        files={"file": ("voice.webm", io.BytesIO(payload), "audio/webm")},
    )


def _fake_remux(monkeypatch, captured=None):
    def fake_to_mp3(source, target, *, max_seconds=None):
        if captured is not None:
            captured["max_seconds"] = max_seconds
        Path(target).write_bytes(b"mp3:" + Path(source).read_bytes())
        return {}

    monkeypatch.setattr(remote_module, "to_mp3", fake_to_mp3)


def _fake_nvv_result():
    return {
        "events": [],
        "trajectory": "speech",
        "baseline_delta": {},
        "version": 1,
        "baseline_update": None,
    }


def test_nvv_requires_all_three_gates_and_non_loopback_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("CMX_VOICE_NVV", "1")
    app, _paths_, _db = _app(tmp_path, monkeypatch)
    called = []
    monkeypatch.setattr(remote_module, "observe_voice", lambda *a, **k: called.append("nvv") or {})
    with TestClient(app, base_url="https://pi.example") as client:
        response = _post_audio(client)
        explicit_but_public = client.post(
            "/files/transcribe",
            headers={"Authorization": "Bearer web"},
            files={"file": ("voice.webm", io.BytesIO(b"fake-audio"), "audio/webm")},
            data={"nvv": "1"},
        )
    assert response.json() == {"text": "没事", "engine": "local"}
    assert explicit_but_public.json() == {"text": "没事", "engine": "local"}
    assert called == []


def test_loopback_explicit_nvv_returns_compact_result_and_reuses_cache(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CMX_LOCAL_TRUSTED_MEDIA", "1")
    monkeypatch.setenv("CMX_VOICE_NVV", "1")
    app, paths, database = _app(tmp_path, monkeypatch)
    monkeypatch.setattr(remote_module, "gemini_key_configured", lambda p: True)
    _fake_remux(monkeypatch)
    observed_calls = []
    expected = {"events": [], "trajectory": [], "note": "<voice>\n整体：未检出明确非语言事件\n</voice>"}
    monkeypatch.setattr(
        remote_module,
        "observe_voice",
        lambda *a, **k: observed_calls.append(1) or {"nvv": expected},
    )
    with TestClient(app, base_url="https://pi.example") as client:
        first = client.post(
            "/files/transcribe",
            headers={"Host": "127.0.0.1:8766"},
            files={"file": ("voice.webm", io.BytesIO(b"fake-audio"), "audio/webm")},
            data={"nvv": "1"},
        )
        second = client.post(
            "/files/transcribe",
            headers={"Host": "127.0.0.1:8766"},
            files={"file": ("voice.webm", io.BytesIO(b"fake-audio"), "audio/webm")},
            data={"nvv": "1"},
        )
    assert first.status_code == second.status_code == 200
    assert first.json()["nvv"] == second.json()["nvv"] == expected
    assert observed_calls == [1]
    row = database.get_voice_nvv_observation(
        __import__("hashlib").sha256(b"fake-audio").hexdigest()
    )
    assert row is not None and row["nvv_note"] == expected["note"]
    assert list((paths.runtime / "voice-tmp").glob("*")) == []


def test_without_a_gemini_key_the_response_is_exactly_the_old_one(tmp_path, monkeypatch):
    app, _paths_, _db = _app(tmp_path, monkeypatch)
    with TestClient(app, base_url="https://pi.example") as client:
        response = _post_audio(client)
    assert response.status_code == 200
    assert response.json() == {"text": "没事", "engine": "local"}


def test_a_successful_observation_rides_the_response_and_lands_in_the_baseline(
    tmp_path, monkeypatch
):
    app, paths, database = _app(tmp_path, monkeypatch)
    monkeypatch.setattr(remote_module, "gemini_key_configured", lambda p: True)
    _fake_remux(monkeypatch)
    seen = {}

    def fake_observe(audio_bytes, *, paths, mime_type="audio/mp3"):
        seen["bytes"] = audio_bytes
        observed = _observation(speed="slow", pause="many", volume="soft", breathy=True)
        return {
            "observed": observed,
            "voice_note": "[声音: 语速偏慢 · 停顿多 · 音量轻 · 气声 · 背景安静]",
        }

    monkeypatch.setattr(remote_module, "observe_voice", fake_observe)
    with TestClient(app, base_url="https://pi.example") as client:
        response = _post_audio(client)

    assert response.status_code == 200
    assert response.json() == {
        "text": "没事",
        "engine": "local",
        "voice_note": "[声音: 语速偏慢 · 停顿多 · 音量轻 · 气声 · 背景安静]",
    }
    # The observer heard the remuxed clip, not the raw WebM Gemini cannot read.
    assert seen["bytes"] == b"mp3:fake-audio"
    # And the enum form is in the baseline, keyed by the uploaded bytes.
    import hashlib

    row = database.get_voice_observation(hashlib.sha256(b"fake-audio").hexdigest())
    assert row is not None
    assert json.loads(row["observed_json"])["pause"] == "many"
    # The temp files are gone either way.
    assert list((paths.runtime / "voice-tmp").glob("*")) == []


def test_a_long_clip_is_remuxed_down_to_the_observer_ceiling(tmp_path, monkeypatch):
    # A minutes-long note used to time out the Gemini call and yield no
    # voice_note; the observer now caps the clip it hands the remux so the call
    # stays inside its timeout.
    from cmx_mcp.voice_observer import MAX_OBSERVER_AUDIO_SECONDS

    app, _paths_, _db = _app(tmp_path, monkeypatch)
    monkeypatch.setattr(remote_module, "gemini_key_configured", lambda p: True)
    captured: dict = {}
    _fake_remux(monkeypatch, captured)
    monkeypatch.setattr(
        remote_module,
        "observe_voice",
        lambda audio_bytes, *, paths, mime_type="audio/mp3": {
            "observed": _observation(),
            "voice_note": "[声音: 语速中等 · 背景安静]",
        },
    )
    with TestClient(app, base_url="https://pi.example") as client:
        response = _post_audio(client)
    assert response.status_code == 200
    assert captured["max_seconds"] == MAX_OBSERVER_AUDIO_SECONDS


def test_the_same_clip_reuses_the_stored_observation(tmp_path, monkeypatch):
    app, _paths_, database = _app(tmp_path, monkeypatch)
    monkeypatch.setattr(remote_module, "gemini_key_configured", lambda p: True)
    _fake_remux(monkeypatch)
    import hashlib

    database.record_voice_observation(
        hashlib.sha256(b"fake-audio").hexdigest(),
        observed=_observation(),
        voice_note="[声音: 语速中等 · 背景安静]",
        model="test",
    )

    def exploding_observe(*args, **kwargs):
        raise AssertionError("a cached clip must not spend a second Gemini call")

    monkeypatch.setattr(remote_module, "observe_voice", exploding_observe)
    with TestClient(app, base_url="https://pi.example") as client:
        response = _post_audio(client)
    assert response.json() == {
        "text": "没事",
        "engine": "local",
        "voice_note": "[声音: 语速中等 · 背景安静]",
    }


def test_observer_failure_never_touches_the_transcript(tmp_path, monkeypatch):
    app, _paths_, _db = _app(tmp_path, monkeypatch)
    monkeypatch.setattr(remote_module, "gemini_key_configured", lambda p: True)
    _fake_remux(monkeypatch)
    monkeypatch.setattr(
        remote_module, "observe_voice", lambda *a, **k: {"error": "unavailable", "detail": "down"}
    )
    with TestClient(app, base_url="https://pi.example") as client:
        response = _post_audio(client)
    assert response.status_code == 200
    assert response.json() == {"text": "没事", "engine": "local"}


def test_the_daily_limit_gates_the_observer_too(tmp_path, monkeypatch):
    # Before the app exists: the limit is read into InstanceSettings at startup.
    monkeypatch.setenv("CMX_GEMINI_DAILY_LIMIT", "0")
    app, _paths_, _db = _app(tmp_path, monkeypatch)
    monkeypatch.setattr(remote_module, "gemini_key_configured", lambda p: True)
    _fake_remux(monkeypatch)
    monkeypatch.setattr(
        remote_module,
        "observe_voice",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("limit must stop the call")),
    )
    with TestClient(app, base_url="https://pi.example") as client:
        response = _post_audio(client)
    assert response.json() == {"text": "没事", "engine": "local"}


def test_the_kill_switch_disables_the_observer_alone(tmp_path, monkeypatch):
    app, _paths_, _db = _app(tmp_path, monkeypatch)
    monkeypatch.setattr(remote_module, "gemini_key_configured", lambda p: True)
    monkeypatch.setenv("CMX_VOICE_OBSERVER", "off")
    monkeypatch.setattr(
        remote_module,
        "observe_voice",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("switch must stop the call")),
    )
    with TestClient(app, base_url="https://pi.example") as client:
        response = _post_audio(client)
    assert response.json() == {"text": "没事", "engine": "local"}


def test_the_widget_puts_the_note_in_the_alt_never_the_body():
    from cmx_mcp.voice_widget import VOICE_WIDGET_JS

    # The transcript edit builds a separate alt when a voice_note is present…
    assert "voice_note" in VOICE_WIDGET_JS
    assert 'voiceNote' in VOICE_WIDGET_JS
    assert '"\\n" + voiceNote' in VOICE_WIDGET_JS
    # …and the body stays the clipped transcript alone.
    assert "status: clip(text, STATUS_MAX_CHARS)" in VOICE_WIDGET_JS
