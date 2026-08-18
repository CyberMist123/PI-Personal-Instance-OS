from __future__ import annotations

import csv
import io
import re
import asyncio
import base64
import json
import logging
import os
import threading
import time
import wave
from pathlib import Path
from typing import Any

# The default path is local-only: a resident Qwen3-ASR service, falling back to
# a local CTranslate2 model directory. The worker never downloads a model, and
# on that path no audio leaves this machine.
#
# There is exactly one exception, and it only fires when a caller asks for it by
# name: engine="cloud" sends the audio to Alibaba's qwen3-asr-flash. It exists
# because the 1.7B local model garbles clause endings often enough that the
# owner wants a deliberate second opinion available -- see `transcribe_cloud`.
# Nothing routes there automatically, and a machine with no cloud credentials
# configured simply reports cloud_not_configured and keeps working locally.

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

# --- Cloud second opinion (opt-in per call) ---------------------------------
#
# The credential is read from a CSV file path, not from an environment
# variable holding the key itself: the same discipline vision_cloud.py applies
# with DPAPI. The file is the one the owner already keeps for the Qwen vision
# calls, so enabling this adds no second copy of the secret.
CLOUD_KEY_FILE_ENV = "CMX_CLOUD_ASR_KEY_FILE"
CLOUD_HOST_ENV = "CMX_CLOUD_ASR_HOST"
CLOUD_MODEL_ENV = "CMX_CLOUD_ASR_MODEL"
CLOUD_TIMEOUT_ENV = "CMX_CLOUD_ASR_TIMEOUT"
DEFAULT_CLOUD_MODEL = "qwen3-asr-flash"
# Both limits are the provider's, and both are checked against the transcoded
# 16 kHz mono WAV rather than the source file: a 200 KB Opus note expands to
# several MB of PCM, so the source size says nothing useful about either.
CLOUD_MAX_AUDIO_SECONDS = 300.0
CLOUD_MAX_WAV_BYTES = 10 * 1024 * 1024
QWEN_MIN_AUDIO_SECONDS = 0.35
QWEN_ACTIVITY_FRAME_SAMPLES = 1600
QWEN_ACTIVITY_HOP_SAMPLES = 800
QWEN_ACTIVITY_RMS = 0.01
QWEN_ACTIVITY_PEAK = 0.03

# CapsWriter's Qwen endpoint returns an early is_final on long clips: a 190 s
# note came back transcribed only through ~70 s, silently dropping two thirds of
# the words. Sending the audio in bounded chunks keeps every request inside the
# length the service transcribes in full. Each cut is snapped to the quietest
# point near its boundary so a word is not split across two requests. The limit
# is env-tunable without a code change; a clip within it is sent unchanged.
QWEN_CHUNK_SAMPLE_RATE = 16000
QWEN_CHUNK_SECONDS_ENV = "CMX_QWEN_ASR_CHUNK_SECONDS"
QWEN_CHUNK_SNAP_SECONDS = 4.0
QWEN_CHUNK_SNAP_FRAME_SAMPLES = 400  # 25 ms at 16 kHz


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
    engine: str = "local",
) -> dict[str, Any]:
    """Transcribe locally with the resident Qwen service, then Whisper as fallback.

    Qwen is an optional local CapsWriter WebSocket service. The existing
    faster-whisper model remains the fallback and is cached for this process.

    ``engine="cloud"`` asks for the paid second opinion instead. A cloud failure
    is not fatal: it degrades to the local chain and reports what went wrong in
    ``cloud_error``, because a caller that asked for a better transcript is
    better served by a worse one than by nothing.
    """
    started = time.monotonic()
    if str(engine or "").strip().lower() == "cloud":
        cloud_result = transcribe_cloud(path, language=language)
        if not cloud_result.get("error"):
            cloud_result["elapsed_ms"] = int((time.monotonic() - started) * 1000)
            _LOGGER.info(
                "ASR engine=%s elapsed_ms=%s path=%s",
                cloud_result.get("engine"),
                cloud_result["elapsed_ms"],
                path,
            )
            return cloud_result
        cloud_error = str(cloud_result["error"])
        _LOGGER.warning("ASR engine=cloud failed (%s); falling back to local", cloud_error)
        local_result = transcribe_file(
            path,
            model_dir=model_dir,
            device=device,
            compute_type=compute_type,
            language=language,
            initial_prompt=initial_prompt,
            hotwords=hotwords,
            beam_size=beam_size,
            max_audio_seconds=max_audio_seconds,
            max_output_chars=max_output_chars,
        )
        local_result["cloud_error"] = cloud_error
        return local_result

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


def cloud_asr_configured() -> bool:
    """Whether a cloud second opinion could run on this machine."""
    key, host = _load_cloud_credentials()
    return bool(key and host)


def _load_cloud_credentials() -> tuple[str, str]:
    """Read (api_key, api_host) out of the owner's Qwen credential CSV.

    The file is two-column ``name,value`` rows and is written by Alibaba's
    console in the local ANSI codepage, not UTF-8, so it is decoded as GBK --
    reading it as UTF-8 raises on the Chinese filename's sibling rows. A
    missing or unreadable file is "not configured", never an exception: the
    local path must keep working on a machine that never opted in.
    """
    path = os.getenv(CLOUD_KEY_FILE_ENV, "").strip()
    if not path:
        return "", ""
    key = ""
    host = os.getenv(CLOUD_HOST_ENV, "").strip()
    try:
        with open(path, newline="", encoding="gbk", errors="replace") as handle:
            for row in csv.reader(handle):
                if len(row) < 2:
                    continue
                name = row[0].strip()
                if name == "apiKey" and not key:
                    key = row[1].strip()
                elif name == "apiHost" and not host:
                    host = row[1].strip()
    except OSError:
        return "", ""
    return key, host


def _cloud_timeout_seconds() -> float:
    raw = os.getenv(CLOUD_TIMEOUT_ENV, "90").strip()
    try:
        return max(5.0, min(float(raw), 600.0))
    except ValueError:
        return 90.0


def transcribe_cloud(path: str | Path, *, language: str = "zh") -> dict[str, Any]:
    """One qwen3-asr-flash call for *path*, or a dict with an ``error`` key.

    qwen3-asr-flash accepts WAV/MP3 only, and a Telegram voice note is
    Ogg/Opus, so the audio is decoded and re-encoded here rather than uploaded
    as-is. That reuses the PyAV decode the local Qwen path already depends on;
    no new dependency and no ffmpeg subprocess.

    Like every other failure in this module, provider trouble comes back as a
    returned dict, never a raised exception -- the caller degrades to local.
    """
    key, host = _load_cloud_credentials()
    if not key or not host:
        return {"error": "cloud_not_configured"}

    try:
        import av
        import httpx
        import numpy as np
    except Exception as exc:
        return {"error": "cloud_dependency_missing", "detail": _short(exc)}

    started = time.monotonic()
    try:
        audio = _decode_audio_16k(path, av, np)
    except Exception as exc:
        return {"error": "cloud_decode_failed", "detail": _short(exc)}
    seconds = audio.size / 16000.0
    if seconds > CLOUD_MAX_AUDIO_SECONDS:
        return {"error": "cloud_audio_too_long", "detail": f"{seconds:.0f}s"}
    wav_bytes = _wav_bytes_16k(audio, np)
    if len(wav_bytes) > CLOUD_MAX_WAV_BYTES:
        return {"error": "cloud_audio_too_large", "detail": str(len(wav_bytes))}

    model = os.getenv(CLOUD_MODEL_ENV, "").strip() or DEFAULT_CLOUD_MODEL
    data_url = "data:audio/wav;base64," + base64.b64encode(wav_bytes).decode("ascii")
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [{"type": "input_audio", "input_audio": {"data": data_url}}],
            }
        ],
    }
    if language:
        payload["asr_options"] = {"language": language}
    try:
        response = httpx.post(
            f"https://{host}/compatible-mode/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=payload,
            timeout=_cloud_timeout_seconds(),
        )
    except Exception as exc:
        return {"error": "cloud_request_failed", "detail": _short(exc)}
    if response.status_code != 200:
        # The body can carry the key back in an echoed request on some error
        # shapes, so only the status and the provider's short code travel on.
        return {"error": f"cloud_http_{response.status_code}", "detail": _cloud_error_code(response)}

    try:
        body = response.json()
        message = body["choices"][0]["message"]
    except Exception as exc:
        return {"error": "cloud_invalid_response", "detail": _short(exc)}

    text = _normalize_chinese(str(message.get("content") or ""))
    if not text:
        return {"error": "no_speech", "detail": "cloud_empty_result", "text": ""}
    result: dict[str, Any] = {
        "text": text,
        "engine": model,
        "duration": seconds,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }
    for annotation in message.get("annotations") or []:
        if isinstance(annotation, dict) and annotation.get("type") == "audio_info":
            # Detected language and speaker emotion are the two fields the local
            # engines cannot produce at all; they are what makes a second
            # opinion worth reading beyond the words themselves.
            result["detected_language"] = str(annotation.get("language") or "")
            result["emotion"] = str(annotation.get("emotion") or "")
            break
    return result


def _cloud_error_code(response: Any) -> str:
    try:
        body = response.json()
    except Exception:
        return ""
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        return str(error.get("code") or error.get("type") or "")[:80]
    return ""


def _wav_bytes_16k(audio: Any, np: Any) -> bytes:
    """Pack float32 mono samples into a 16 kHz PCM16 WAV container."""
    pcm16 = (np.clip(audio, -1.0, 1.0) * 32767.0).astype(np.int16)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16000)
        writer.writeframes(pcm16.tobytes())
    return buffer.getvalue()


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
        # One request for a short note; several for a long one. Each chunk is a
        # view into the already-decoded samples, so splitting costs no re-decode.
        spans = _qwen_chunk_spans(audio, np)
        parts: list[str] = []
        total_duration = 0.0
        for start, end in spans:
            response = asyncio.run(
                _qwen_request(
                    websockets,
                    url,
                    audio[start:end],
                    language=language,
                    initial_prompt=initial_prompt,
                    hotwords=hotwords,
                    timeout_seconds=timeout_seconds,
                )
            )
            piece = _normalize_chinese(str(response.get("text") or ""))
            # A context echo or an empty reply is this chunk saying nothing (a
            # pause between words can land a whole chunk in silence); it drops
            # out without failing the note. Only an all-silent note is no_speech.
            if piece and not _qwen_result_is_context_echo(piece, initial_prompt, hotwords):
                parts.append(piece)
            reported = response.get("duration")
            total_duration += (
                float(reported) if reported else (end - start) / QWEN_CHUNK_SAMPLE_RATE
            )
        text = "".join(parts).strip()
        if not text:
            return {"error": "no_speech", "detail": "qwen_empty_result", "text": ""}
        return {"text": text, "duration": total_duration}
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


def _qwen_chunk_seconds() -> float:
    raw = os.getenv(QWEN_CHUNK_SECONDS_ENV, "50").strip()
    try:
        return max(15.0, min(float(raw), 120.0))
    except ValueError:
        return 50.0


def _qwen_chunk_spans(audio: Any, np: Any) -> list[tuple[int, int]]:
    """Split decoded 16 kHz mono *audio* into ``(start, end)`` sample spans.

    A clip within the chunk limit yields a single span, so the common short
    note is sent exactly as before. A longer note is cut into contiguous spans
    that together cover the whole clip, each no longer than the limit, with the
    boundary snapped to the quietest nearby point to keep words off the seams.
    """
    total = int(audio.size)
    chunk = int(_qwen_chunk_seconds() * QWEN_CHUNK_SAMPLE_RATE)
    if total <= chunk or chunk <= 0:
        return [(0, total)]
    snap = int(QWEN_CHUNK_SNAP_SECONDS * QWEN_CHUNK_SAMPLE_RATE)
    spans: list[tuple[int, int]] = []
    start = 0
    while start < total:
        if total - start <= chunk:
            spans.append((start, total))
            break
        target = start + chunk
        cut = _snap_cut_to_quiet(audio, np, target=target, snap=snap, floor=start + chunk // 2)
        spans.append((start, cut))
        start = cut
    return spans


def _snap_cut_to_quiet(audio: Any, np: Any, *, target: int, snap: int, floor: int) -> int:
    """Return a cut index at or before *target*, at the lowest-energy short frame
    within the *snap* seconds leading up to it and never before *floor*. Searching
    backward keeps the span it closes no longer than the chunk limit. Falls back
    to *target* when the window is too small to analyse, so the caller always
    makes forward progress.
    """
    total = int(audio.size)
    frame = QWEN_CHUNK_SNAP_FRAME_SAMPLES
    lo = max(int(floor), int(target) - snap)
    hi = min(total, int(target))
    if hi - lo < 2 * frame:
        return min(int(target), total)
    window = audio[lo:hi]
    count = window.size // frame
    frames = window[: count * frame].reshape(count, frame)
    energy = np.mean(np.square(frames), axis=1)
    quietest = int(np.argmin(energy))
    cut = lo + quietest * frame + frame // 2
    return min(max(cut, int(floor) + 1), total)


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
