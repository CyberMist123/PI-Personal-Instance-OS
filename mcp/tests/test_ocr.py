from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from cmx_mcp import ocr as ocr_module
from cmx_mcp.ocr import (
    DEFAULT_MODEL_DIR,
    DEFAULT_TIER,
    VALID_TIERS,
    model_dir_ready,
    ocr_image,
    resolve_model_dir,
    resolve_tier,
)


def _write_weights(directory, tier: str, *, det: bool = True, rec: bool = True, empty: bool = False):
    directory.mkdir(parents=True, exist_ok=True)
    payload = b"" if empty else b"onnx-weights"
    if det:
        (directory / f"PP-OCRv6_det_{tier}.onnx").write_bytes(payload)
    if rec:
        (directory / f"PP-OCRv6_rec_{tier}.onnx").write_bytes(payload)


class FakeOCRVersion:
    PPOCRV6 = "PP-OCRv6"


def _fake_model_type(value):
    return f"ModelType:{value}"


def _install_fake_rapidocr(
    monkeypatch,
    *,
    call_result=None,
    init_error: Exception | None = None,
    call_error: Exception | None = None,
    recorder: dict | None = None,
):
    rapidocr_module = type(sys)("rapidocr")
    utils_module = type(sys)("rapidocr.utils")
    typings_module = type(sys)("rapidocr.utils.typings")

    typings_module.OCRVersion = FakeOCRVersion
    typings_module.ModelType = _fake_model_type
    utils_module.typings = typings_module

    class FakeRapidOCR:
        def __init__(self, params=None):
            if recorder is not None:
                recorder["init_count"] = recorder.get("init_count", 0) + 1
                recorder["params"] = params
            if init_error is not None:
                raise init_error

        def __call__(self, img_path):
            if recorder is not None:
                recorder["called_with"] = img_path
            if call_error is not None:
                raise call_error
            return call_result

    rapidocr_module.RapidOCR = FakeRapidOCR
    rapidocr_module.utils = utils_module

    monkeypatch.setitem(sys.modules, "rapidocr", rapidocr_module)
    monkeypatch.setitem(sys.modules, "rapidocr.utils", utils_module)
    monkeypatch.setitem(sys.modules, "rapidocr.utils.typings", typings_module)


# -- model_dir_ready: the guard --------------------------------------------


def test_model_dir_ready_rejects_none_and_empty():
    assert model_dir_ready(None) is False
    assert model_dir_ready("") is False


def test_model_dir_ready_requires_both_det_and_rec_weights(tmp_path):
    """Regression class covered for faster-whisper: a directory that exists
    but doesn't hold the real weights must not pass the guard, or a
    configuration mistake surfaces as a confusing failure at OCR time
    instead of a plain 'not configured'."""
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    assert model_dir_ready(empty_dir) is False

    det_only = tmp_path / "det-only"
    _write_weights(det_only, DEFAULT_TIER, rec=False)
    assert model_dir_ready(det_only) is False

    rec_only = tmp_path / "rec-only"
    _write_weights(rec_only, DEFAULT_TIER, det=False)
    assert model_dir_ready(rec_only) is False

    zero_byte = tmp_path / "zero-byte"
    _write_weights(zero_byte, DEFAULT_TIER, empty=True)
    assert model_dir_ready(zero_byte) is False

    real = tmp_path / "real"
    _write_weights(real, DEFAULT_TIER)
    assert model_dir_ready(real) is True


def test_model_dir_ready_is_tier_specific(tmp_path):
    directory = tmp_path / "tiny-only"
    _write_weights(directory, "tiny")
    assert model_dir_ready(directory, "tiny") is True
    assert model_dir_ready(directory, "medium") is False
    assert model_dir_ready(directory, DEFAULT_TIER) is False


def test_model_dir_ready_rejects_unknown_tier(tmp_path):
    directory = tmp_path / "real"
    _write_weights(directory, "medium")
    assert model_dir_ready(directory, "huge") is False


# -- config resolution -------------------------------------------------------


def test_resolve_model_dir_prefers_explicit_arg_then_env_then_default(monkeypatch):
    monkeypatch.delenv("CMX_OCR_MODEL_DIR", raising=False)
    assert resolve_model_dir() == DEFAULT_MODEL_DIR
    assert resolve_model_dir("C:\\explicit\\dir") == "C:\\explicit\\dir"

    monkeypatch.setenv("CMX_OCR_MODEL_DIR", "C:\\from\\env")
    assert resolve_model_dir() == "C:\\from\\env"
    assert resolve_model_dir("C:\\explicit\\dir") == "C:\\explicit\\dir"


def test_resolve_tier_prefers_explicit_arg_then_env_then_default(monkeypatch):
    monkeypatch.delenv("CMX_OCR_MODEL_TIER", raising=False)
    assert resolve_tier() == DEFAULT_TIER
    assert resolve_tier("tiny") == "tiny"

    monkeypatch.setenv("CMX_OCR_MODEL_TIER", "small")
    assert resolve_tier() == "small"
    assert resolve_tier("tiny") == "tiny"


def test_resolve_tier_rejects_unknown_value(monkeypatch):
    monkeypatch.delenv("CMX_OCR_MODEL_TIER", raising=False)
    with pytest.raises(ValueError, match="CMX_OCR_MODEL_TIER"):
        resolve_tier("huge")

    monkeypatch.setenv("CMX_OCR_MODEL_TIER", "huge")
    with pytest.raises(ValueError):
        resolve_tier()


def test_valid_tiers_is_tiny_small_medium():
    assert VALID_TIERS == ("tiny", "small", "medium")


# -- ocr_image: fail-closed error paths --------------------------------------


def test_ocr_image_returns_model_missing_when_directory_is_absent(tmp_path):
    missing = tmp_path / "no-such-model"
    result = ocr_image(tmp_path / "a.png", model_dir=missing)
    assert result == {"error": "model_missing", "detail": str(missing)}


def test_ocr_image_returns_model_missing_when_weights_are_incomplete(tmp_path):
    directory = tmp_path / "incomplete"
    _write_weights(directory, DEFAULT_TIER, rec=False)
    result = ocr_image(tmp_path / "a.png", model_dir=directory)
    assert result["error"] == "model_missing"


def test_ocr_image_returns_config_invalid_before_touching_the_directory(tmp_path):
    # A bad tier is a configuration mistake, not a missing-model condition;
    # it must be reported distinctly and never silently coerced to a
    # different tier.
    result = ocr_image(tmp_path / "a.png", model_dir=tmp_path / "whatever", tier="huge")
    assert result["error"] == "config_invalid"
    assert "CMX_OCR_MODEL_TIER" in result["detail"]


def test_ocr_image_reports_missing_provider_dependency(tmp_path, monkeypatch):
    directory = tmp_path / "model"
    _write_weights(directory, DEFAULT_TIER)
    monkeypatch.setitem(sys.modules, "rapidocr", None)

    result = ocr_image(tmp_path / "a.png", model_dir=directory)
    assert result["error"] == "provider_dependency_missing"


def test_ocr_image_wraps_engine_construction_failures(tmp_path, monkeypatch):
    directory = tmp_path / "model"
    _write_weights(directory, DEFAULT_TIER)
    _install_fake_rapidocr(monkeypatch, init_error=RuntimeError("bad onnx session"))

    result = ocr_image(tmp_path / "a.png", model_dir=directory)
    assert result["error"] == "ocr_failed"
    assert "bad onnx session" in result["detail"]


def test_ocr_image_wraps_provider_call_failures(tmp_path, monkeypatch):
    directory = tmp_path / "model"
    _write_weights(directory, DEFAULT_TIER)
    _install_fake_rapidocr(monkeypatch, call_error=RuntimeError("inference blew up"))

    result = ocr_image(tmp_path / "a.png", model_dir=directory)
    assert result["error"] == "ocr_failed"
    assert "inference blew up" in result["detail"]


# -- ocr_image: success path, engine wiring, and reuse -----------------------


def test_ocr_image_pins_ppocrv6_points_at_the_tier_files_and_sets_thread_limits(
    tmp_path, monkeypatch
):
    directory = tmp_path / "model"
    _write_weights(directory, "small")
    recorder: dict = {}
    fake_result = SimpleNamespace(txts=("hello",), scores=(0.9,))
    _install_fake_rapidocr(monkeypatch, call_result=fake_result, recorder=recorder)

    ocr_image(tmp_path / "a.png", model_dir=directory, tier="small")

    params = recorder["params"]
    assert params["Det.ocr_version"] == FakeOCRVersion.PPOCRV6
    assert params["Rec.ocr_version"] == FakeOCRVersion.PPOCRV6
    assert params["Det.model_path"] == str(directory / "PP-OCRv6_det_small.onnx")
    assert params["Rec.model_path"] == str(directory / "PP-OCRv6_rec_small.onnx")
    assert params["EngineConfig.onnxruntime.intra_op_num_threads"] == 1
    assert params["EngineConfig.onnxruntime.inter_op_num_threads"] == 1


def test_ocr_image_reuses_the_cached_engine_across_calls(tmp_path, monkeypatch):
    directory = tmp_path / "model"
    _write_weights(directory, DEFAULT_TIER)
    recorder: dict = {}
    fake_result = SimpleNamespace(txts=(), scores=())
    _install_fake_rapidocr(monkeypatch, call_result=fake_result, recorder=recorder)

    ocr_image(tmp_path / "a.png", model_dir=directory)
    ocr_image(tmp_path / "b.png", model_dir=directory)

    assert recorder["init_count"] == 1


def test_ocr_image_success_returns_the_stable_shape_with_plain_python_types(
    tmp_path, monkeypatch
):
    numpy = pytest.importorskip("numpy")
    directory = tmp_path / "model"
    _write_weights(directory, DEFAULT_TIER)
    fake_result = SimpleNamespace(
        txts=("你好", "世界"),
        scores=(numpy.float32(0.95), numpy.float32(0.85)),
    )
    _install_fake_rapidocr(monkeypatch, call_result=fake_result)

    result = ocr_image(tmp_path / "a.png", model_dir=directory)

    assert result["text"] == "你好\n世界"
    assert result["lines"] == [
        {"text": "你好", "confidence": pytest.approx(0.95)},
        {"text": "世界", "confidence": pytest.approx(0.85)},
    ]
    assert all(type(line["confidence"]) is float for line in result["lines"])
    assert all(type(line["text"]) is str for line in result["lines"])
    assert result["line_count"] == 2
    assert result["mean_confidence"] == pytest.approx(0.9)
    assert isinstance(result["elapsed_ms"], int)
    assert "error" not in result


# -- _normalize_result: pure logic, no RapidOCR involved ---------------------


def test_normalize_result_handles_the_empty_output_case():
    empty = SimpleNamespace(txts=None, scores=None)
    normalized = ocr_module._normalize_result(empty)
    assert normalized == {
        "text": "",
        "lines": [],
        "line_count": 0,
        "mean_confidence": 0.0,
    }


def test_normalize_result_converts_a_fabricated_rapidocr_shaped_value():
    fabricated = SimpleNamespace(txts=("a", "b", "c"), scores=(1.0, 0.5, 0.0))
    normalized = ocr_module._normalize_result(fabricated)
    assert normalized["text"] == "a\nb\nc"
    assert normalized["line_count"] == 3
    assert normalized["mean_confidence"] == pytest.approx(0.5)
