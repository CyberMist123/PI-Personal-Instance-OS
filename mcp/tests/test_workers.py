from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from cmx_mcp import workers
from cmx_mcp.db import Database
from cmx_mcp.mastodon_client import MastodonApiError, MastodonClient
from cmx_mcp.transcribe import transcribe_file
from cmx_mcp.workers import WorkerConfig, run_once

AUDIO_URL = "https://mastodon.example/media/voice.ogg"


class FakeClient:
    def __init__(self, pages, *, download_error: str | None = None):
        self.pages = list(pages)
        self.calls: list[dict] = []
        self.published: list[dict] = []
        self.downloads: list[str] = []
        self.download_error = download_error

    def home_timeline(self, *, limit, max_id=None, since_id=None, min_id=None):
        self.calls.append({"limit": limit, "min_id": min_id})
        items = self.pages.pop(0) if self.pages else []
        return SimpleNamespace(items=items, next_cursor=None)

    def verify_credentials(self):
        return {"id": "worker-account", "acct": "gpt"}

    def download_file(self, url, dest_path, *, max_bytes):
        self.downloads.append(url)
        if self.download_error:
            raise MastodonApiError(self.download_error)
        Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
        Path(dest_path).write_bytes(b"fake-audio")
        return 10

    def publish(self, *, text, visibility, reply_to_id, media_ids, idempotency_key, **extra):
        self.published.append({
            "text": text,
            "visibility": visibility,
            "reply_to_id": reply_to_id,
            "media_ids": list(media_ids),
            "idempotency_key": idempotency_key,
        })
        return {"id": f"reply-{reply_to_id}"}


def _runtime(tmp_path: Path, client: FakeClient):
    database = Database(tmp_path / "cmx.sqlite3")
    database.initialize()
    audits: list[tuple] = []
    return SimpleNamespace(
        bot=SimpleNamespace(bot_id="gpt"),
        settings=SimpleNamespace(max_status_chars=5000),
        paths=SimpleNamespace(runtime=tmp_path / "runtime"),
        db=database,
        client=client,
        audit=lambda tool, action, **kwargs: audits.append((tool, action, kwargs)),
        _audits=audits,
    )


def _config(tmp_path: Path) -> WorkerConfig:
    model_dir = tmp_path / "model"
    model_dir.mkdir(parents=True, exist_ok=True)
    return WorkerConfig(model_dir=str(model_dir))


def _status(status_id: str, *, audio: bool = True, visibility: str = "private", account: str = "alice"):
    attachments = [{"id": "m1", "type": "audio", "url": AUDIO_URL}] if audio else [
        {"id": "m1", "type": "image", "url": "https://mastodon.example/media/x.png"}
    ]
    return {
        "id": status_id,
        "visibility": visibility,
        "account": {"id": account, "acct": account},
        "media_attachments": attachments,
    }


def _fake_transcribe(monkeypatch, result):
    seen: list[dict] = []

    def fake(path, **kwargs):
        seen.append({"path": str(path), **kwargs})
        return dict(result)

    monkeypatch.setattr(workers, "transcribe_file", fake)
    return seen


def test_audio_status_is_transcribed_and_replied_with_source_visibility(tmp_path, monkeypatch):
    client = FakeClient([[_status("101", visibility="unlisted")]])
    runtime = _runtime(tmp_path, client)
    calls = _fake_transcribe(monkeypatch, {"text": "今天天气不错", "elapsed_ms": 12})

    summary = run_once(runtime, _config(tmp_path))

    assert summary == {"seen": 1, "transcribed": 1}
    assert client.calls == [{"limit": 30, "min_id": None}]
    assert len(client.published) == 1
    reply = client.published[0]
    assert reply["text"] == "🎙️ 语音转写：\n今天天气不错"
    assert reply["visibility"] == "unlisted"
    assert reply["reply_to_id"] == "101"
    assert reply["media_ids"] == []
    assert len(reply["idempotency_key"]) == 64
    assert runtime.db.get_setting("worker_watermark_gpt") == "101"
    assert runtime.db.worker_is_done("gpt", "101") is True
    assert calls and calls[0]["model_dir"] == str(tmp_path / "model")
    # The temporary audio file is removed after the pass.
    assert list((tmp_path / "runtime" / "worker-tmp").glob("*")) == []


def test_second_pass_skips_an_already_transcribed_status(tmp_path, monkeypatch):
    client = FakeClient([[_status("101")], [_status("101")]])
    runtime = _runtime(tmp_path, client)
    _fake_transcribe(monkeypatch, {"text": "hello", "elapsed_ms": 1})
    config = _config(tmp_path)

    run_once(runtime, config)
    run_once(runtime, config)

    assert len(client.published) == 1
    assert client.calls[1] == {"limit": 30, "min_id": "101"}


def test_non_audio_status_is_marked_done_without_transcription(tmp_path, monkeypatch):
    client = FakeClient([[_status("101", audio=False)]])
    runtime = _runtime(tmp_path, client)
    calls = _fake_transcribe(monkeypatch, {"text": "unused", "elapsed_ms": 1})

    summary = run_once(runtime, _config(tmp_path))

    assert summary == {"seen": 1, "transcribed": 0}
    assert calls == [] and client.published == [] and client.downloads == []
    assert runtime.db.worker_is_done("gpt", "101") is True
    assert runtime.db.get_setting("worker_watermark_gpt") == "101"


def test_worker_own_audio_status_is_skipped(tmp_path, monkeypatch):
    client = FakeClient([[_status("101", account="worker-account")]])
    runtime = _runtime(tmp_path, client)
    calls = _fake_transcribe(monkeypatch, {"text": "loop", "elapsed_ms": 1})

    run_once(runtime, _config(tmp_path))

    assert calls == [] and client.published == []
    assert runtime.db.worker_is_done("gpt", "101") is True


def test_transcriber_error_publishes_nothing_but_marks_done(tmp_path, monkeypatch):
    client = FakeClient([[_status("101")]])
    runtime = _runtime(tmp_path, client)
    _fake_transcribe(monkeypatch, {"error": "transcription_failed", "detail": "boom"})

    summary = run_once(runtime, _config(tmp_path))

    assert summary == {"seen": 1, "transcribed": 0}
    assert client.published == []
    assert runtime.db.worker_is_done("gpt", "101") is True
    assert runtime.db.get_setting("worker_watermark_gpt") == "101"


def test_oversize_download_is_survived_and_marked_done(tmp_path, monkeypatch):
    client = FakeClient([[_status("101")]], download_error="download exceeds size limit")
    runtime = _runtime(tmp_path, client)
    calls = _fake_transcribe(monkeypatch, {"text": "never", "elapsed_ms": 1})

    summary = run_once(runtime, _config(tmp_path))

    assert summary == {"seen": 1, "transcribed": 0}
    assert calls == [] and client.published == []
    assert runtime.db.worker_is_done("gpt", "101") is True


def test_reblog_is_unwrapped_and_batch_is_processed_oldest_first(tmp_path, monkeypatch):
    boosted = _status("55", visibility="private")
    page = [
        {"id": "103", "account": {"id": "bob"}, "reblog": boosted, "media_attachments": []},
        _status("102", visibility="direct"),
    ]
    client = FakeClient([page])
    runtime = _runtime(tmp_path, client)
    _fake_transcribe(monkeypatch, {"text": "ok", "elapsed_ms": 1})

    run_once(runtime, _config(tmp_path))

    assert [item["reply_to_id"] for item in client.published] == ["102", "55"]
    assert client.published[0]["visibility"] == "direct"
    assert runtime.db.get_setting("worker_watermark_gpt") == "103"


def test_long_transcript_is_truncated_to_the_status_limit(tmp_path, monkeypatch):
    client = FakeClient([[_status("101")]])
    runtime = _runtime(tmp_path, client)
    runtime.settings = SimpleNamespace(max_status_chars=60)
    _fake_transcribe(monkeypatch, {"text": "字" * 200, "elapsed_ms": 1})

    run_once(runtime, _config(tmp_path))

    text = client.published[0]["text"]
    assert len(text) == 40 and text.endswith("…")


def test_worker_done_roundtrip_is_isolated_by_bot(tmp_path):
    database = Database(tmp_path / "cmx.sqlite3")
    database.initialize()
    assert database.worker_is_done("gpt", "1") is False
    database.worker_mark_done("gpt", "1")
    database.worker_mark_done("gpt", "1")
    assert database.worker_is_done("gpt", "1") is True
    assert database.worker_is_done("other", "1") is False


def _client() -> MastodonClient:
    return MastodonClient(
        base_url="https://mastodon.example", host_header="mastodon.example",
        token="t", timeout=5.0,
    )


def test_download_file_rejects_foreign_hosts_and_plain_http(tmp_path):
    client = _client()
    for url in ("https://evil.example/a.ogg", "http://mastodon.example/a.ogg", "/relative.ogg"):
        with pytest.raises(MastodonApiError):
            client.download_file(url, tmp_path / "out.ogg", max_bytes=1024)
    assert not (tmp_path / "out.ogg").exists()


def test_download_file_streams_and_enforces_the_size_limit(tmp_path):
    client = _client()
    client._client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"0123456789")),
        base_url="https://mastodon.example",
    )
    destination = tmp_path / "nested" / "voice.ogg"
    assert client.download_file(AUDIO_URL, destination, max_bytes=1024) == 10
    assert destination.read_bytes() == b"0123456789"

    with pytest.raises(MastodonApiError, match="exceeds size limit"):
        client.download_file(AUDIO_URL, tmp_path / "big.ogg", max_bytes=4)
    assert not (tmp_path / "big.ogg").exists()


class _FakeSegment:
    def __init__(self, text: str, end: float):
        self.text = text
        self.end = end


def _install_fake_whisper(segments, *, recorder: dict | None = None):
    module = type(sys)("faster_whisper")

    class WhisperModel:
        def __init__(self, model_path, **kwargs):
            if recorder is not None:
                recorder.update({"model_path": model_path, **kwargs})

        def transcribe(self, path, **kwargs):
            if recorder is not None:
                recorder["audio"] = path
                recorder["transcribe_kwargs"] = kwargs
            return iter(segments), SimpleNamespace(language="zh")

    module.WhisperModel = WhisperModel
    previous = sys.modules.get("faster_whisper", None)
    had = "faster_whisper" in sys.modules
    sys.modules["faster_whisper"] = module
    return had, previous


def _restore_whisper(had, previous):
    if had:
        sys.modules["faster_whisper"] = previous
    else:
        sys.modules.pop("faster_whisper", None)


def test_transcribe_returns_model_missing_when_directory_is_absent(tmp_path):
    result = transcribe_file(tmp_path / "a.ogg", model_dir=tmp_path / "no-such-model")
    assert result == {"error": "model_missing", "detail": str(tmp_path / "no-such-model")}
    assert transcribe_file(tmp_path / "a.ogg", model_dir="")["error"] == "model_missing"


def test_transcribe_reports_missing_provider_dependency(tmp_path):
    (tmp_path / "model").mkdir()
    had = "faster_whisper" in sys.modules
    previous = sys.modules.get("faster_whisper", None)
    sys.modules["faster_whisper"] = None
    try:
        result = transcribe_file(tmp_path / "a.ogg", model_dir=tmp_path / "model")
    finally:
        _restore_whisper(had, previous)
    assert result["error"] == "provider_dependency_missing"


def test_transcribe_success_joins_segments_and_never_downloads_models(tmp_path):
    (tmp_path / "model").mkdir()
    recorder: dict = {}
    had, previous = _install_fake_whisper(
        [_FakeSegment(" 你好", 2.0), _FakeSegment("世界 ", 4.0)], recorder=recorder
    )
    try:
        result = transcribe_file(
            tmp_path / "a.ogg", model_dir=tmp_path / "model", language="zh", compute_type="int8"
        )
    finally:
        _restore_whisper(had, previous)
    assert result["text"] == "你好世界"
    assert isinstance(result["elapsed_ms"], int)
    assert recorder["model_path"] == str(tmp_path / "model")
    assert recorder["local_files_only"] is True
    assert recorder["compute_type"] == "int8"
    assert recorder["transcribe_kwargs"]["language"] == "zh"


def test_transcribe_enforces_duration_and_output_limits(tmp_path):
    (tmp_path / "model").mkdir()
    had, previous = _install_fake_whisper([_FakeSegment("a", 10.0), _FakeSegment("b", 99.0)])
    try:
        long_audio = transcribe_file(
            tmp_path / "a.ogg", model_dir=tmp_path / "model", max_audio_seconds=30.0
        )
    finally:
        _restore_whisper(had, previous)
    assert long_audio["error"] == "audio_duration_limit"

    had, previous = _install_fake_whisper([_FakeSegment("x" * 20, 1.0), _FakeSegment("y" * 20, 2.0)])
    try:
        long_text = transcribe_file(
            tmp_path / "a.ogg", model_dir=tmp_path / "model", max_output_chars=25
        )
    finally:
        _restore_whisper(had, previous)
    assert long_text["error"] == "output_limit"


def test_transcribe_wraps_provider_failures(tmp_path):
    (tmp_path / "model").mkdir()
    module = type(sys)("faster_whisper")

    class Broken:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("no such CTranslate2 model")

    module.WhisperModel = Broken
    had = "faster_whisper" in sys.modules
    previous = sys.modules.get("faster_whisper", None)
    sys.modules["faster_whisper"] = module
    try:
        result = transcribe_file(tmp_path / "a.ogg", model_dir=tmp_path / "model")
    finally:
        _restore_whisper(had, previous)
    assert result["error"] == "transcription_failed"


def test_worker_config_reads_bounded_environment(monkeypatch):
    monkeypatch.setenv("CMX_WHISPER_MODEL_DIR", "C:\\models\\small")
    monkeypatch.setenv("CMX_WORKER_POLL_SECONDS", "300")
    monkeypatch.setenv("CMX_WHISPER_MAX_SECONDS", "600")
    monkeypatch.setenv("CMX_WORKER_MAX_AUDIO_BYTES", str(5 * 1024 * 1024))
    config = WorkerConfig.load()
    assert config.model_dir == "C:\\models\\small"
    assert (config.poll_seconds, config.max_audio_seconds) == (300, 600)
    assert config.max_audio_bytes == 5 * 1024 * 1024
    assert (config.device, config.compute_type, config.language) == ("cpu", "int8", "")

    monkeypatch.setenv("CMX_WORKER_POLL_SECONDS", "5")
    with pytest.raises(RuntimeError, match="between 30 and 3600"):
        WorkerConfig.load()
