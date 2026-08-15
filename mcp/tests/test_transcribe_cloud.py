"""The opt-in cloud engine, and the guarantee that nothing else reaches it.

Every test here runs offline: the one function that would make a paid call is
monkeypatched. What is being pinned is the routing and the degradation, not
Alibaba's transcription quality.
"""

from __future__ import annotations

import pytest

from cmx_mcp import transcribe as transcribe_module


def _no_local_engines(monkeypatch):
    """Silence both local engines so a test can observe routing alone."""
    monkeypatch.setattr(transcribe_module, "_transcribe_with_qwen", lambda *a, **k: None)
    monkeypatch.setattr(transcribe_module, "model_dir_ready", lambda directory: False)


def test_cloud_is_not_configured_without_a_key_file(monkeypatch):
    monkeypatch.delenv(transcribe_module.CLOUD_KEY_FILE_ENV, raising=False)
    assert transcribe_module.cloud_asr_configured() is False
    assert transcribe_module.transcribe_cloud("ignored.oga") == {"error": "cloud_not_configured"}


def test_cloud_credentials_come_from_the_gbk_csv(tmp_path, monkeypatch):
    # Alibaba's console writes this file in the local ANSI codepage; a UTF-8
    # read raises partway through and would look like "no credentials".
    key_file = tmp_path / "qwen.csv"
    key_file.write_bytes("id,1\napiKey,sk-secret\napiHost,ws-abc.cn-beijing.maas.aliyuncs.com\n说明,中文行\n".encode("gbk"))
    monkeypatch.setenv(transcribe_module.CLOUD_KEY_FILE_ENV, str(key_file))
    monkeypatch.delenv(transcribe_module.CLOUD_HOST_ENV, raising=False)
    assert transcribe_module._load_cloud_credentials() == (
        "sk-secret",
        "ws-abc.cn-beijing.maas.aliyuncs.com",
    )
    assert transcribe_module.cloud_asr_configured() is True


def test_an_unreadable_key_file_is_not_configured_rather_than_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv(transcribe_module.CLOUD_KEY_FILE_ENV, str(tmp_path / "absent.csv"))
    assert transcribe_module.cloud_asr_configured() is False


def test_the_default_engine_never_calls_the_cloud(monkeypatch):
    called: list[str] = []
    monkeypatch.setattr(
        transcribe_module,
        "transcribe_cloud",
        lambda path, **kwargs: called.append("cloud") or {"text": "cloud"},
    )
    monkeypatch.setattr(
        transcribe_module, "_transcribe_with_qwen", lambda *a, **k: {"text": "local"}
    )
    result = transcribe_module.transcribe_file("voice.oga", model_dir="ignored")
    assert result["engine"] == "qwen3-asr"
    assert called == []


def test_cloud_engine_result_is_returned_with_its_own_engine_name(monkeypatch):
    monkeypatch.setattr(
        transcribe_module,
        "transcribe_cloud",
        lambda path, **kwargs: {"text": "云端结果", "engine": "qwen3-asr-flash", "duration": 3.0},
    )
    result = transcribe_module.transcribe_file("voice.oga", model_dir="ignored", engine="cloud")
    assert result["text"] == "云端结果"
    assert result["engine"] == "qwen3-asr-flash"
    assert "cloud_error" not in result


def test_a_cloud_failure_degrades_to_local_and_says_why(monkeypatch):
    monkeypatch.setattr(
        transcribe_module, "transcribe_cloud", lambda path, **kwargs: {"error": "cloud_http_429"}
    )
    monkeypatch.setattr(
        transcribe_module, "_transcribe_with_qwen", lambda *a, **k: {"text": "本机结果"}
    )
    result = transcribe_module.transcribe_file("voice.oga", model_dir="ignored", engine="cloud")
    # A caller that asked for a better transcript is better served by a worse
    # one than by nothing -- but it must be able to see that this happened.
    assert result["text"] == "本机结果"
    assert result["engine"] == "qwen3-asr"
    assert result["cloud_error"] == "cloud_http_429"


def test_a_cloud_failure_with_no_local_model_still_reports_the_cloud_error(monkeypatch):
    _no_local_engines(monkeypatch)
    monkeypatch.setattr(
        transcribe_module, "transcribe_cloud", lambda path, **kwargs: {"error": "cloud_request_failed"}
    )
    result = transcribe_module.transcribe_file("voice.oga", model_dir="absent", engine="cloud")
    assert result["error"] == "model_missing"
    assert result["cloud_error"] == "cloud_request_failed"


@pytest.mark.parametrize("engine", ["", "local", "LOCAL", None])
def test_only_the_exact_cloud_token_routes_to_the_cloud(engine, monkeypatch):
    called: list[str] = []
    monkeypatch.setattr(
        transcribe_module, "transcribe_cloud", lambda path, **kwargs: called.append("cloud") or {}
    )
    monkeypatch.setattr(
        transcribe_module, "_transcribe_with_qwen", lambda *a, **k: {"text": "local"}
    )
    kwargs = {} if engine is None else {"engine": engine}
    transcribe_module.transcribe_file("voice.oga", model_dir="ignored", **kwargs)
    assert called == []


def test_wav_packing_is_16k_mono_pcm16():
    numpy = pytest.importorskip("numpy")
    audio = numpy.zeros(16000, dtype=numpy.float32)
    wav_bytes = transcribe_module._wav_bytes_16k(audio, numpy)
    assert wav_bytes[:4] == b"RIFF" and wav_bytes[8:12] == b"WAVE"
    # 1 second of 16 kHz mono PCM16 is 32000 bytes of payload plus the header.
    assert len(wav_bytes) == 32000 + 44
