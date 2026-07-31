from __future__ import annotations

import json

import httpx
import pytest

from cmx_mcp import vision_cloud
from cmx_mcp.vision_cloud import (
    GEMINI_THINKING_CONFIG,
    MAX_IMAGE_BYTES,
    gemini_model,
    recognize_image,
)

FAKE_KEY = "fake-gemini-key-should-never-appear-in-output"


def _configure_key(monkeypatch, key: str = FAKE_KEY) -> None:
    monkeypatch.setattr(vision_cloud, "load_gemini_key", lambda paths: key)


def _forbid_network(monkeypatch) -> None:
    def _fail(*args, **kwargs):
        raise AssertionError("httpx.post must not be called")

    monkeypatch.setattr(vision_cloud.httpx, "post", _fail)


def _install_fake_post(monkeypatch, *, response=None, raises: Exception | None = None, recorder: dict | None = None):
    def fake_post(url, *, headers, json, timeout):
        if recorder is not None:
            recorder["url"] = url
            recorder["headers"] = headers
            recorder["json"] = json
            recorder["timeout"] = timeout
        if raises is not None:
            raise raises
        return response

    monkeypatch.setattr(vision_cloud.httpx, "post", fake_post)


def _gemini_response(status_code: int, payload: dict) -> httpx.Response:
    return httpx.Response(status_code, json=payload)


def _recognition_payload(**fields) -> dict:
    inner = {
        "corrected_text": "",
        "description": "",
        "keywords": "",
        "uncertain_text": "",
    }
    inner.update(fields)
    return {
        "candidates": [
            {"content": {"parts": [{"text": json.dumps(inner)}]}}
        ]
    }


# -- missing key ---------------------------------------------------------


def test_recognize_image_not_configured_when_no_key(monkeypatch):
    monkeypatch.setattr(vision_cloud, "load_gemini_key", lambda paths: None)
    _forbid_network(monkeypatch)

    result = recognize_image(b"jpeg-bytes", local_ocr_text="hello", paths=object())

    assert result == {"error": "not_configured"}


# -- oversized image, rejected before any network call --------------------


def test_recognize_image_rejects_oversized_image_without_a_network_call(monkeypatch):
    _configure_key(monkeypatch)
    _forbid_network(monkeypatch)

    oversized = b"x" * (MAX_IMAGE_BYTES + 1)
    result = recognize_image(oversized, local_ocr_text="", paths=object())

    assert result["error"] == "oversized"
    assert str(MAX_IMAGE_BYTES + 1) in result["detail"]


# -- happy path -------------------------------------------------------------


def test_recognize_image_happy_path_parses_structured_response(monkeypatch):
    _configure_key(monkeypatch)
    recorder: dict = {}
    payload = _recognition_payload(
        corrected_text="Duck rice noodle soup - 12 yuan",
        description="A handwritten restaurant menu board.",
        keywords="menu restaurant noodle soup price",
        uncertain_text="last digit of the price is unclear",
    )
    _install_fake_post(monkeypatch, response=_gemini_response(200, payload), recorder=recorder)

    result = recognize_image(
        b"jpeg-bytes",
        local_ocr_text="Duck rice noodle soup - 1? yuan",
        paths=object(),
    )

    assert result == {
        "corrected_text": "Duck rice noodle soup - 12 yuan",
        "description": "A handwritten restaurant menu board.",
        "keywords": "menu restaurant noodle soup price",
        "uncertain_text": "last digit of the price is unclear",
    }

    # The request actually carries the local OCR text and the thinking setting.
    request_body = recorder["json"]
    prompt_text = request_body["contents"][0]["parts"][0]["text"]
    assert "Duck rice noodle soup - 1? yuan" in prompt_text
    assert request_body["generationConfig"]["thinkingConfig"] == GEMINI_THINKING_CONFIG
    assert request_body["generationConfig"]["responseMimeType"] == "application/json"
    assert recorder["headers"]["x-goog-api-key"] == FAKE_KEY
    assert gemini_model() in recorder["url"]
    assert "generateContent" in recorder["url"]


# -- 429 -> quota_exhausted ---------------------------------------------------


def test_recognize_image_429_returns_quota_exhausted(monkeypatch):
    _configure_key(monkeypatch)
    response = _gemini_response(
        429,
        {"error": {"code": 429, "message": "Resource has been exhausted.", "status": "RESOURCE_EXHAUSTED"}},
    )
    _install_fake_post(monkeypatch, response=response)

    result = recognize_image(b"jpeg-bytes", local_ocr_text="", paths=object())

    assert result == {"error": "quota_exhausted"}


def test_recognize_image_quota_shaped_4xx_message_also_returns_quota_exhausted(monkeypatch):
    """429 is not the only quota shape: Gemini also reports quota trouble as a
    403 with a message rather than the RESOURCE_EXHAUSTED status."""
    _configure_key(monkeypatch)
    response = _gemini_response(
        403,
        {"error": {"code": 403, "message": "You have exceeded your quota.", "status": "PERMISSION_DENIED"}},
    )
    _install_fake_post(monkeypatch, response=response)

    result = recognize_image(b"jpeg-bytes", local_ocr_text="", paths=object())

    assert result == {"error": "quota_exhausted"}


def test_recognize_image_ordinary_4xx_returns_unavailable_not_quota_exhausted(monkeypatch):
    _configure_key(monkeypatch)
    response = _gemini_response(
        400,
        {"error": {"code": 400, "message": "Request contains an invalid argument.", "status": "INVALID_ARGUMENT"}},
    )
    _install_fake_post(monkeypatch, response=response)

    result = recognize_image(b"jpeg-bytes", local_ocr_text="", paths=object())

    assert result["error"] == "unavailable"


# -- timeout / connection failure -> unavailable ------------------------------


def test_recognize_image_timeout_returns_unavailable(monkeypatch):
    _configure_key(monkeypatch)
    _install_fake_post(monkeypatch, raises=httpx.ConnectTimeout("timed out"))

    result = recognize_image(b"jpeg-bytes", local_ocr_text="", paths=object())

    assert result["error"] == "unavailable"


def test_recognize_image_connection_error_returns_unavailable(monkeypatch):
    _configure_key(monkeypatch)
    _install_fake_post(monkeypatch, raises=httpx.ConnectError("connection refused"))

    result = recognize_image(b"jpeg-bytes", local_ocr_text="", paths=object())

    assert result["error"] == "unavailable"


# -- malformed / non-JSON response bodies, handled without raising -----------


def test_recognize_image_non_json_response_body_returns_invalid_response(monkeypatch):
    _configure_key(monkeypatch)
    response = httpx.Response(200, content=b"<html>not json at all</html>")
    _install_fake_post(monkeypatch, response=response)

    result = recognize_image(b"jpeg-bytes", local_ocr_text="", paths=object())

    assert result["error"] == "invalid_response"


def test_recognize_image_missing_candidates_returns_invalid_response(monkeypatch):
    _configure_key(monkeypatch)
    response = _gemini_response(200, {"unexpected": "shape"})
    _install_fake_post(monkeypatch, response=response)

    result = recognize_image(b"jpeg-bytes", local_ocr_text="", paths=object())

    assert result["error"] == "invalid_response"


def test_recognize_image_non_json_inner_text_returns_invalid_response(monkeypatch):
    _configure_key(monkeypatch)
    response = _gemini_response(
        200,
        {"candidates": [{"content": {"parts": [{"text": "this is not JSON"}]}}]},
    )
    _install_fake_post(monkeypatch, response=response)

    result = recognize_image(b"jpeg-bytes", local_ocr_text="", paths=object())

    assert result["error"] == "invalid_response"


# -- the key must never appear in any returned error string -------------------


def test_recognize_image_never_leaks_the_key_on_a_network_failure(monkeypatch):
    _configure_key(monkeypatch)
    _install_fake_post(
        monkeypatch,
        raises=httpx.ConnectError(f"connection failed while using key={FAKE_KEY}"),
    )

    result = recognize_image(b"jpeg-bytes", local_ocr_text="", paths=object())

    assert result["error"] == "unavailable"
    assert FAKE_KEY not in json.dumps(result)


def test_recognize_image_never_leaks_the_key_on_an_http_error_body(monkeypatch):
    _configure_key(monkeypatch)
    response = _gemini_response(
        400,
        {"error": {"code": 400, "message": f"bad request for key {FAKE_KEY}", "status": "INVALID_ARGUMENT"}},
    )
    _install_fake_post(monkeypatch, response=response)

    result = recognize_image(b"jpeg-bytes", local_ocr_text="", paths=object())

    assert FAKE_KEY not in json.dumps(result)


# -- model name resolution -----------------------------------------------------


def test_gemini_model_prefers_env_var_then_falls_back_to_default(monkeypatch):
    monkeypatch.delenv("CMX_GEMINI_MODEL", raising=False)
    assert gemini_model() == vision_cloud.DEFAULT_GEMINI_MODEL

    monkeypatch.setenv("CMX_GEMINI_MODEL", "gemini-custom-test-model")
    assert gemini_model() == "gemini-custom-test-model"
