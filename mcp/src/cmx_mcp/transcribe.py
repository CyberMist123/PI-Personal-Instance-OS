from __future__ import annotations

import time
from pathlib import Path
from typing import Any

# Every transcript is produced by a local CTranslate2 model directory. The
# worker never downloads a model and never sends audio to a cloud provider.


def transcribe_file(
    path: str | Path,
    *,
    model_dir: str | Path,
    device: str = "cpu",
    compute_type: str = "int8",
    language: str = "",
    max_audio_seconds: float = 1800.0,
    max_output_chars: int = 8000,
) -> dict[str, Any]:
    """Transcribe one local audio file with faster-whisper, or return an error dict."""
    started = time.monotonic()
    directory = Path(model_dir) if str(model_dir) else None
    if directory is None or not directory.is_dir():
        return {"error": "model_missing", "detail": str(model_dir)}

    try:
        from faster_whisper import WhisperModel
    except Exception as exc:  # ImportError, or a broken native ctranslate2 wheel
        return {"error": "provider_dependency_missing", "detail": _short(exc)}

    parts: list[str] = []
    total_chars = 0
    try:
        model = WhisperModel(
            str(directory),
            device=device,
            compute_type=compute_type,
            local_files_only=True,
        )
        segments, _info = model.transcribe(
            str(Path(path)),
            language=language or None,
            vad_filter=True,
        )
        for segment in segments:
            end = float(getattr(segment, "end", 0.0) or 0.0)
            if end > max_audio_seconds:
                return {"error": "audio_duration_limit", "detail": f"{end:.0f}s"}
            text = str(getattr(segment, "text", "") or "")
            total_chars += len(text)
            if total_chars > max_output_chars:
                return {"error": "output_limit", "detail": str(total_chars)}
            parts.append(text)
    except Exception as exc:
        return {"error": "transcription_failed", "detail": _short(exc)}

    return {
        "text": "".join(parts).strip(),
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }


def _short(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"[:200]
