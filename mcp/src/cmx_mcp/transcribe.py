from __future__ import annotations

import re
import threading
import time
from pathlib import Path
from typing import Any

# Every transcript is produced by a local CTranslate2 model directory. The
# worker never downloads a model and never sends audio to a cloud provider.

# A CTranslate2 model directory is identified by its weights, not by existing.
# Checking only is_dir() lets a plausible-but-wrong folder through and turns a
# configuration mistake into a 502 at transcription time, which reads like an
# intermittent fault instead of "this was never pointed at a model".
MODEL_MARKER = "model.bin"

SIMPLIFIED_PROMPT = (
    "以下是普通话语音记录，使用简体中文、自然标点和阿拉伯数字。"
    "专有名词包括 CMX、PI OS。"
)
DEFAULT_HOTWORDS = "CMX, PI OS"

_CJK_SPACE_RE = re.compile(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])")
_WHITESPACE_RE = re.compile(r"[ \t\r\f\v]+")
_MODEL_CACHE: dict[tuple[Any, str, str, str], tuple[Any, threading.Lock]] = {}
_MODEL_CACHE_LOCK = threading.Lock()
_OPENCC: Any | None = None
_OPENCC_CHECKED = False


def model_dir_ready(model_dir: str | Path | None) -> bool:
    """Return whether *model_dir* contains an actual local CTranslate2 model."""
    if not str(model_dir):
        return False
    if model_dir is None:
        return False
    model_file = Path(model_dir) / MODEL_MARKER
    try:
        return model_file.is_file() and model_file.stat().st_size > 0
    except OSError:
        return False


def transcribe_file(
    path: str | Path,
    *,
    model_dir: str | Path,
    device: str = "cpu",
    compute_type: str = "int8",
    language: str = "zh",
    initial_prompt: str = SIMPLIFIED_PROMPT,
    hotwords: str = DEFAULT_HOTWORDS,
    beam_size: int = 5,
    max_audio_seconds: float = 1800.0,
    max_output_chars: int = 8000,
) -> dict[str, Any]:
    """Transcribe one local audio file with faster-whisper, or return an error dict.

    The heavyweight model is cached for the life of the process. This matters for
    the HTTP recorder path: later clips avoid paying the model cold-start cost.
    """
    started = time.monotonic()
    if not model_dir_ready(model_dir):
        return {"error": "model_missing", "detail": str(model_dir)}
    directory = Path(model_dir)

    try:
        from faster_whisper import WhisperModel
    except Exception as exc:  # ImportError, or a broken native ctranslate2 wheel
        return {"error": "provider_dependency_missing", "detail": _short(exc)}

    try:
        model, run_lock = _cached_model(
            WhisperModel,
            directory=directory,
            device=device,
            compute_type=compute_type,
        )
    except Exception as exc:
        return {"error": "transcription_failed", "detail": _short(exc)}

    parts: list[str] = []
    total_chars = 0
    try:
        # A single-owner service gains more from predictable memory use than
        # overlapping two decoder runs. The lock still lets uploads and all other
        # HTTP work continue while one local transcription is active.
        with run_lock:
            segments, _info = model.transcribe(
                str(Path(path)),
                language=language or None,
                task="transcribe",
                beam_size=beam_size,
                initial_prompt=initial_prompt or None,
                hotwords=hotwords or None,
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
        "text": _normalize_chinese("".join(parts)),
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }


def _cached_model(
    model_type: Any,
    *,
    directory: Path,
    device: str,
    compute_type: str,
) -> tuple[Any, threading.Lock]:
    key = (model_type, str(directory.resolve()), device, compute_type)
    with _MODEL_CACHE_LOCK:
        cached = _MODEL_CACHE.get(key)
        if cached is None:
            model = model_type(
                str(directory),
                device=device,
                compute_type=compute_type,
                local_files_only=True,
            )
            cached = (model, threading.Lock())
            _MODEL_CACHE[key] = cached
        return cached


def _normalize_chinese(text: str) -> str:
    value = _to_simplified(text)
    value = _WHITESPACE_RE.sub(" ", value)
    value = _CJK_SPACE_RE.sub("", value)
    return value.strip()


def _to_simplified(text: str) -> str:
    global _OPENCC, _OPENCC_CHECKED
    if not _OPENCC_CHECKED:
        try:
            from opencc import OpenCC

            _OPENCC = OpenCC("t2s")
        except Exception:
            # Keep transcription usable if an old optional installation has not
            # yet gained OpenCC; the simplified-Chinese decoder prompt still runs.
            _OPENCC = None
        _OPENCC_CHECKED = True
    if _OPENCC is None:
        return text
    try:
        return str(_OPENCC.convert(text))
    except Exception:
        return text


def _short(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"[:200]
