from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any

# Every OCR result is produced by local PP-OCRv6 ONNX weights already sitting
# on disk. This module never downloads model weights and never sends image
# bytes to a cloud provider.
#
# Return shape (stable across RapidOCR upgrades — call sites must depend on
# this, not on RapidOCR's own output dataclasses, which is the whole point of
# wrapping the library):
#
#   {
#       "text": str,                # recognized lines joined by "\n"
#       "lines": [{"text": str, "confidence": float}, ...],
#       "line_count": int,
#       "mean_confidence": float,    # 0.0 when no lines were recognized
#       "elapsed_ms": int,
#   }
#
# On failure a dict with an "error" key is returned instead of a partial
# result: "config_invalid", "model_missing", "provider_dependency_missing",
# or "ocr_failed".

# PP-OCRv6 is pinned. RapidOCR 3.9.2 also ships PP-OCRv4/v5 weights and would
# silently accept them via Det/Rec.ocr_version, so the version is hardcoded
# here rather than threaded through as a parameter.
DET_MODEL_TEMPLATE = "PP-OCRv6_det_{tier}.onnx"
REC_MODEL_TEMPLATE = "PP-OCRv6_rec_{tier}.onnx"
VALID_TIERS = ("tiny", "small", "medium")
# `small` is the default because RapidOCR's wheel already ships those two files,
# so a machine can be brought up by copying them into the model directory with no
# download at all. `medium` is more accurate and roughly 2x slower; switching is
# a matter of fetching its weights and setting CMX_OCR_MODEL_TIER.
DEFAULT_TIER = "small"
DEFAULT_MODEL_DIR = r"D:\AI\models\rapidocr"

# Verified against the installed package source
# (.venv/Lib/site-packages/rapidocr/inference_engine/onnxruntime/main.py,
# `_init_sess_opts`, and the defaults in rapidocr/config.yaml under
# `EngineConfig.onnxruntime`): onnxruntime's SessionOptions exposes exactly
# these two knobs, and RapidOCR forwards `EngineConfig.onnxruntime.*` params
# straight into them. Fixed at 1/1 rather than left configurable: the owner's
# constraint is CPU politeness, not throughput.
ONNX_INTRA_OP_NUM_THREADS = 1
ONNX_INTER_OP_NUM_THREADS = 1

_ENGINE_CACHE: dict[tuple[Any, str, str, int, int], tuple[Any, threading.Lock]] = {}
_ENGINE_CACHE_LOCK = threading.Lock()


def _det_filename(tier: str) -> str:
    return DET_MODEL_TEMPLATE.format(tier=tier)


def _rec_filename(tier: str) -> str:
    return REC_MODEL_TEMPLATE.format(tier=tier)


def model_dir_ready(model_dir: str | Path | None, tier: str = DEFAULT_TIER) -> bool:
    """Return whether *model_dir* holds the actual PP-OCRv6 det+rec weights for *tier*.

    A directory is identified by its weight files, not by existing. Checking
    only is_dir() would let a plausible-but-wrong folder through and turn a
    configuration mistake into a confusing failure at OCR time instead of a
    plain "not configured" — the same bug class the voice transcriber hit
    with CMX_WHISPER_MODEL_DIR.
    """
    if model_dir is None or not str(model_dir):
        return False
    if tier not in VALID_TIERS:
        return False
    directory = Path(model_dir)
    for filename in (_det_filename(tier), _rec_filename(tier)):
        model_file = directory / filename
        try:
            if not (model_file.is_file() and model_file.stat().st_size > 0):
                return False
        except OSError:
            return False
    return True


def resolve_model_dir(model_dir: str | Path | None = None) -> str:
    """Resolve the model directory: explicit argument, then CMX_OCR_MODEL_DIR, then the default."""
    if model_dir:
        return str(model_dir)
    return os.getenv("CMX_OCR_MODEL_DIR", "").strip() or DEFAULT_MODEL_DIR


def resolve_tier(tier: str | None = None) -> str:
    """Resolve the det/rec model tier: explicit argument, then CMX_OCR_MODEL_TIER, then the default.

    Raises ValueError on an unknown tier rather than silently falling back to
    a different one — a typo in the env var should surface as a clear
    configuration error, not as a mysteriously wrong (or missing) model.
    """
    value = (tier or os.getenv("CMX_OCR_MODEL_TIER", "").strip() or DEFAULT_TIER).strip()
    if value not in VALID_TIERS:
        raise ValueError(f"CMX_OCR_MODEL_TIER must be one of {VALID_TIERS}, got {value!r}")
    return value


def ocr_image(
    path: str | Path,
    *,
    model_dir: str | Path | None = None,
    tier: str | None = None,
) -> dict[str, Any]:
    """Run local PP-OCRv6 OCR on one image with RapidOCR, or return an error dict.

    The heavyweight det/rec sessions are cached for the life of the process,
    same rationale as the whisper worker: later images avoid paying the
    onnxruntime session cold-start cost.
    """
    started = time.monotonic()
    directory_value = resolve_model_dir(model_dir)
    try:
        resolved_tier = resolve_tier(tier)
    except ValueError as exc:
        return {"error": "config_invalid", "detail": _short(exc)}

    if not model_dir_ready(directory_value, resolved_tier):
        return {"error": "model_missing", "detail": str(directory_value)}
    directory = Path(directory_value)

    try:
        from rapidocr import RapidOCR
    except Exception as exc:  # ImportError, or a broken onnxruntime install
        return {"error": "provider_dependency_missing", "detail": _short(exc)}

    try:
        engine, run_lock = _cached_engine(RapidOCR, directory=directory, tier=resolved_tier)
    except Exception as exc:
        return {"error": "ocr_failed", "detail": _short(exc)}

    try:
        # One resident's OCR run at a time: predictable CPU/memory beats two
        # overlapping onnxruntime sessions fighting for the same core budget.
        with run_lock:
            result = engine(str(Path(path)))
    except Exception as exc:
        return {"error": "ocr_failed", "detail": _short(exc)}

    normalized = _normalize_result(result)
    normalized["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    return normalized


def _cached_engine(engine_type: Any, *, directory: Path, tier: str) -> tuple[Any, threading.Lock]:
    key = (
        engine_type,
        str(directory.resolve()),
        tier,
        ONNX_INTRA_OP_NUM_THREADS,
        ONNX_INTER_OP_NUM_THREADS,
    )
    with _ENGINE_CACHE_LOCK:
        cached = _ENGINE_CACHE.get(key)
        if cached is None:
            engine = engine_type(params=_engine_params(directory, tier))
            cached = (engine, threading.Lock())
            _ENGINE_CACHE[key] = cached
        return cached


def _engine_params(directory: Path, tier: str) -> dict[str, Any]:
    from rapidocr.utils.typings import ModelType, OCRVersion

    model_type = ModelType(tier)
    return {
        "Det.ocr_version": OCRVersion.PPOCRV6,
        "Det.model_type": model_type,
        "Det.model_path": str(directory / _det_filename(tier)),
        "Rec.ocr_version": OCRVersion.PPOCRV6,
        "Rec.model_type": model_type,
        "Rec.model_path": str(directory / _rec_filename(tier)),
        "EngineConfig.onnxruntime.intra_op_num_threads": ONNX_INTRA_OP_NUM_THREADS,
        "EngineConfig.onnxruntime.inter_op_num_threads": ONNX_INTER_OP_NUM_THREADS,
    }


def _normalize_result(result: Any) -> dict[str, Any]:
    txts = getattr(result, "txts", None) or ()
    scores = getattr(result, "scores", None) or ()
    lines = [
        {"text": str(txt), "confidence": float(score)} for txt, score in zip(txts, scores)
    ]
    mean_confidence = sum(line["confidence"] for line in lines) / len(lines) if lines else 0.0
    return {
        "text": "\n".join(line["text"] for line in lines),
        "lines": lines,
        "line_count": len(lines),
        "mean_confidence": mean_confidence,
    }


def _short(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"[:200]
