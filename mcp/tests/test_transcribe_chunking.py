from __future__ import annotations

import numpy as np

from cmx_mcp import transcribe as t

SR = t.QWEN_CHUNK_SAMPLE_RATE


def test_short_audio_stays_a_single_span(monkeypatch):
    monkeypatch.setattr(t, "_qwen_chunk_seconds", lambda: 50.0)
    audio = np.full(10 * SR, 0.5, dtype=np.float32)  # 10 s, under the 50 s limit
    assert t._qwen_chunk_spans(audio, np) == [(0, audio.size)]


def test_the_configured_chunk_limit_has_a_sane_floor():
    # The env is clamped so a stray tiny value can never shard a note into
    # hundreds of requests.
    import os

    os.environ["CMX_QWEN_ASR_CHUNK_SECONDS"] = "2"
    try:
        assert t._qwen_chunk_seconds() >= 15.0
    finally:
        del os.environ["CMX_QWEN_ASR_CHUNK_SECONDS"]


def test_long_audio_is_cut_at_quiet_points_within_the_limit(monkeypatch):
    monkeypatch.setattr(t, "_qwen_chunk_seconds", lambda: 2.0)  # 2 s chunks
    monkeypatch.setattr(t, "QWEN_CHUNK_SNAP_SECONDS", 0.5)  # search 0.5 s back

    audio = np.full(5 * SR, 0.5, dtype=np.float32)  # 5 s of speech-level energy
    # A silent pause just before each 2 s / 4 s boundary is where a cut belongs.
    audio[int(1.8 * SR) : int(2.0 * SR)] = 0.0
    audio[int(3.8 * SR) : int(4.0 * SR)] = 0.0

    spans = t._qwen_chunk_spans(audio, np)

    # Contiguous and covering the whole clip, with no gaps or overlaps.
    assert spans[0][0] == 0
    assert spans[-1][1] == audio.size
    for (_, prev_end), (next_start, _) in zip(spans, spans[1:]):
        assert prev_end == next_start

    # Every span is within the chunk limit, so no request exceeds the safe length.
    chunk = int(2 * SR)
    assert all(0 < end - start <= chunk for start, end in spans)

    # The first two cuts land inside the silent pauses, not mid-word.
    assert int(1.8 * SR) <= spans[0][1] <= int(2.0 * SR)
    assert int(3.8 * SR) <= spans[1][1] <= int(4.0 * SR)
