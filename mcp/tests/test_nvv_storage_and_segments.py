from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from types import ModuleType

from cmx_mcp.db import Database
from cmx_mcp import transcribe as transcribe_module


class _Segment:
    def __init__(self, text: str, start: float, end: float):
        self.text = text
        self.start = start
        self.end = end


def _install_whisper(segments: list[_Segment]) -> tuple[bool, ModuleType | None]:
    module = ModuleType("faster_whisper")

    class WhisperModel:
        def __init__(self, *args, **kwargs):
            pass

        def transcribe(self, *args, **kwargs):
            return iter(segments), object()

    module.WhisperModel = WhisperModel
    had = "faster_whisper" in sys.modules
    previous = sys.modules.get("faster_whisper")
    sys.modules["faster_whisper"] = module
    return had, previous


def _restore_whisper(had: bool, previous: ModuleType | None) -> None:
    transcribe_module._MODEL_CACHE.clear()
    if had:
        sys.modules["faster_whisper"] = previous
    else:
        sys.modules.pop("faster_whisper", None)


def test_whisper_segments_are_opt_in_and_report_milliseconds(tmp_path, monkeypatch):
    model = tmp_path / "model"
    model.mkdir()
    (model / "model.bin").write_bytes(b"weights")
    monkeypatch.setattr(transcribe_module, "_transcribe_with_qwen", lambda *a, **k: None)
    segments = [_Segment(" 你 好 ", 0.125, 1.5), _Segment("世 界", 1.75, 2.25)]

    had, previous = _install_whisper(segments)
    try:
        default = transcribe_module.transcribe_file("a.ogg", model_dir=model)
        opted_in = transcribe_module.transcribe_file(
            "b.ogg", model_dir=model, include_segments=True
        )
    finally:
        _restore_whisper(had, previous)

    assert "segments" not in default
    assert opted_in["text"] == "你好世界"
    assert opted_in["segments"] == [
        {"start_ms": 125, "end_ms": 1500, "text": "你好"},
        {"start_ms": 1750, "end_ms": 2250, "text": "世界"},
    ]


def test_qwen_segments_degrade_to_whole_clip(tmp_path, monkeypatch):
    monkeypatch.setattr(
        transcribe_module,
        "_transcribe_with_qwen",
        lambda *a, **k: {"text": " 整 条 ", "duration": 1.234},
    )
    result = transcribe_module.transcribe_file(
        "a.ogg", model_dir=tmp_path / "unused", include_segments=True
    )
    assert result["segments"] == [
        {"start_ms": 0, "end_ms": 1234, "text": "整条"}
    ]


def test_nvv_cache_is_first_write_wins_and_baseline_is_mutable(tmp_path):
    database = Database(tmp_path / "cmx.sqlite3")
    database.initialize()

    database.record_voice_nvv_observation(
        "abc", nvv={"version": 1, "events": ["first"]}, nvv_note="第一条", model="m1"
    )
    database.record_voice_nvv_observation(
        "abc", nvv={"version": 1, "events": ["second"]}, nvv_note="第二条", model="m2"
    )
    row = database.get_voice_nvv_observation("abc")
    assert row is not None
    assert json.loads(row["nvv_json"])["events"] == ["first"]
    assert row["nvv_note"] == "第一条"
    assert row["model"] == "m1"
    assert database.get_voice_nvv_observation("missing") is None

    assert database.get_voice_nvv_baseline() is None
    database.set_voice_nvv_baseline({"pitch_median": 120.0, "sample_count": 2})
    assert database.get_voice_nvv_baseline() == {
        "pitch_median": 120.0,
        "sample_count": 2,
    }
    database.set_voice_nvv_baseline({"pitch_median": 125.0, "sample_count": 3})
    assert database.get_voice_nvv_baseline() == {
        "pitch_median": 125.0,
        "sample_count": 3,
    }

    with sqlite3.connect(database.path) as raw:
        assert raw.execute("SELECT version FROM schema_version").fetchone()[0] == 9
        columns = {
            row[1] for row in raw.execute("PRAGMA table_info(voice_nvv_observations)")
        }
    assert columns == {"audio_sha256", "nvv_json", "nvv_note", "model", "created_at"}
