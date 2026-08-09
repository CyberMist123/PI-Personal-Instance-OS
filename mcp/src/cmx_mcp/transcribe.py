from __future__ import annotations

import re
import asyncio
import base64
import json
import logging
import os
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
_LOGGER = logging.getLogger(__name__)
QWEN_ASR_URL_ENV = "CMX_QWEN_ASR_URL"
QWEN_ASR_TIMEOUT_ENV = "CMX_QWEN_ASR_TIMEOUT"
QWEN_MIN_AUDIO_SECONDS = 0.35
QWEN_ACTIVITY_FRAME_SAMPLES = 1600
QWEN_ACTIVITY_HOP_SAMPLES = 800
QWEN_ACTIVITY_RMS = 0.01
QWEN_ACTIVITY_PEAK = 0.03


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
    """Transcribe locally with the resident Qwen service, then Whisper as fallback.

    Qwen is an optional local CapsWriter WebSocket service. The existing
    faster-whisper model remains the fallback and is cached for this process.
    """
    started = time.monotonic()
    qwen_result = _transcribe_with_qwen(
        path,
        language=language,
        initial_prompt=initial_prompt,
        hotwords=hotwords,
        timeout_seconds=_qwen_timeout_seconds(),
    )
    if qwen_result is not None:
        qwen_result["text"] = _normalize_chinese(str(qwen_result.get("text") or ""))
        qwen_result["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        qwen_result["engine"] = "qwen3-asr"
        _LOGGER.info("ASR engine=qwen3-asr elapsed_ms=%s path=%s", qwen_result["elapsed_ms"], path)
        return qwen_result

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

    result = {
        "text": _normalize_chinese("".join(parts)),
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "engine": "faster-whisper",
    }
    _LOGGER.info("ASR engine=faster-whisper elapsed_ms=%s path=%s", result["elapsed_ms"], path)
    return result


def _qwen_timeout_seconds() -> float:
    raw = os.getenv(QWEN_ASR_TIMEOUT_ENV, "30").strip()
    try:
        return max(1.0, min(float(raw), 300.0))
    except ValueError:
        return 30.0


def _transcribe_with_qwen(
    path: str | Path,
    *,
    language: str,
    initial_prompt: str,
    hotwords: str,
    timeout_seconds: float,
) -> dict[str, Any] | None:
    """Call the installed local CapsWriter Qwen WebSocket service once.

    ``None`` means the service is disabled or unavailable, so the caller may
    safely use the existing Whisper fallback. Qwen's model stays resident in
    the separate CapsWriter process between requests.
    """
    url = os.getenv(QWEN_ASR_URL_ENV, "").strip()
    if not url:
        return None

    try:
        import av
        import numpy as np
        import websockets
    except Exception as exc:
        _LOGGER.warning("ASR engine=qwen3-asr unavailable: %s", _short(exc))
        return None

    try:
        audio = _decode_audio_16k(path, av, np)
        activity_error = _qwen_audio_activity_error(audio, np)
        if activity_error:
            return {"error": "no_speech", "detail": activity_error, "text": ""}
        response = asyncio.run(
            _qwen_request(
                websockets,
                url,
                audio,
                language=language,
                initial_prompt=initial_prompt,
                hotwords=hotwords,
                timeout_seconds=timeout_seconds,
            )
        )
        text = _normalize_chinese(str(response.get("text") or ""))
        if not text:
            return {"error": "no_speech", "detail": "qwen_empty_result", "text": ""}
        if _qwen_result_is_context_echo(text, initial_prompt, hotwords):
            return {"error": "no_speech", "detail": "qwen_context_echo", "text": ""}
        return {"text": text, "duration": response.get("duration")}
    except Exception as exc:
        _LOGGER.warning("ASR engine=qwen3-asr unavailable; fallback=faster-whisper: %s", _short(exc))
        return None


def _decode_audio_16k(path: str | Path, av: Any, np: Any) -> Any:
    container = av.open(str(path))
    resampler = av.AudioResampler(format="flt", layout="mono", rate=16000)
    chunks = []
    try:
        for frame in container.decode(audio=0):
            for resampled in resampler.resample(frame):
                chunks.append(resampled.to_ndarray().reshape(-1))
        for resampled in resampler.resample(None):
            chunks.append(resampled.to_ndarray().reshape(-1))
    finally:
        container.close()
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(chunks).astype(np.float32, copy=False)


def _qwen_audio_activity_error(audio: Any, np: Any) -> str | None:
    """Reject silence, very short clips, and low-level stationary noise before Qwen.

    CapsWriter's Qwen endpoint has no usable confidence or no-speech field and
    can decode the supplied context as text. This deliberately small VAD-like
    gate uses only the already-decoded 16 kHz mono samples; it is not a second
    audio framework and leaves the existing Whisper path unchanged.
    """
    if audio.size < int(16000 * QWEN_MIN_AUDIO_SECONDS):
        return "audio_too_short"
    frames = _audio_activity_frames(audio, np)
    if frames.size == 0:
        return "audio_silent"
    frame_rms = np.sqrt(np.mean(np.square(frames), axis=1, dtype=np.float64))
    peak = float(np.max(np.abs(audio)))
    if peak < QWEN_ACTIVITY_PEAK:
        return "audio_silent"
    active_frames = int(np.count_nonzero(frame_rms >= QWEN_ACTIVITY_RMS))
    required_frames = max(3, int(np.ceil(frame_rms.size * 0.05)))
    if active_frames < required_frames:
        return "audio_no_voice_activity"
    return None


def _audio_activity_frames(audio: Any, np: Any) -> Any:
    frame_size = QWEN_ACTIVITY_FRAME_SAMPLES
    hop = QWEN_ACTIVITY_HOP_SAMPLES
    if audio.size < frame_size:
        return np.empty((0, frame_size), dtype=np.float32)
    count = 1 + (audio.size - frame_size) // hop
    indices = np.arange(frame_size)[None, :] + hop * np.arange(count)[:, None]
    return audio[indices]


def _qwen_result_is_context_echo(text: str, initial_prompt: str, hotwords: str) -> bool:
    """Reject the context-only replies observed from silent/noise inputs."""
    value = _normalize_chinese(text).strip()
    candidates = {
        _normalize_chinese(initial_prompt).strip(),
        _normalize_chinese(
            f"常用专有名词：{hotwords.strip()}。" if hotwords.strip() else ""
        ).strip(),
    }
    return bool(value) and value in {candidate for candidate in candidates if candidate}


async def _qwen_request(
    websockets: Any,
    url: str,
    audio: Any,
    *,
    language: str,
    initial_prompt: str,
    hotwords: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    kwargs = {
        "uri": url,
        "subprotocols": ["binary"],
        "max_size": None,
        "open_timeout": timeout_seconds,
        "close_timeout": timeout_seconds,
    }
    if tuple(int(value) for value in websockets.__version__.split(".")[:2]) >= (14,):
        kwargs["proxy"] = None
    context = initial_prompt.strip()
    if hotwords.strip():
        context = f"{context}\n常用专有名词：{hotwords.strip()}" if context else f"常用专有名词：{hotwords.strip()}"
    message = {
        "task_id": f"cmx-{time.time_ns()}",
        "source": "file",
        "data": base64.b64encode(audio.tobytes()).decode("ascii"),
        "is_final": True,
        "time_start": time.time(),
        "seg_duration": 15.0,
        "seg_overlap": 2.0,
        "context": context,
        "language": "Chinese",
    }
    async with websockets.connect(**kwargs) as socket:
        await socket.send(json.dumps(message, ensure_ascii=False))
        while True:
            raw = await asyncio.wait_for(socket.recv(), timeout=timeout_seconds)
            result = json.loads(raw)
            if result.get("is_final"):
                return result


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
