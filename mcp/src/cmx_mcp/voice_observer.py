"""The voice observer: a closed-vocabulary paralinguistic pass over one voice clip.

Transcription throws away *how* something was said — speed, pauses, volume,
breathiness, laughter, background — and this module hands exactly that layer
back to the chat AI as one short line, the `voice_note`:

    [声音: 语速偏慢 · 停顿多 · 音量轻 · 气声 · 背景安静]

The format decisions are the owner's chat AI's own answers to the 2026-08-11
format questionnaire, and they are load-bearing:

- **Every clip gets a note, and the vocabulary is closed.** The reader's goal
  is cross-time comparison ("more pauses than last week"), which only works if
  the same phenomenon always produces the same word. So the model never writes
  prose here: it fills an enum form (`responseSchema` rejects anything outside
  the vocabulary), and the Chinese line is rendered from that form by
  `render_voice_note`, in code. Do not "improve" a token's wording — renaming
  one breaks every comparison against previously stored observations.
- **No emotion words.** `tired`/`soft`/`低落` are conclusions, and the reader —
  who knows the speaker — reserves those for itself. The form has no field an
  emotion could fit in, and the prompt forbids inferring any.
- **Background sound and restarts/self-corrections are first-class**: they were
  the two dimensions the reader asked for that no draft format had.

The raw enum form is stored per clip (`Database.record_voice_observation`) so a
baseline can accumulate in the background; once it is deep enough, a future
deviation-only mode ("版本五") can be computed from the stored forms without
re-listening to anything.

Error discipline mirrors vision_cloud.py: every failure is a returned dict with
an "error" key, never an exception, and the caller treats any failure as "no
voice_note this time" — observation must never block or fail a transcription.
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any

import httpx

from .config import Paths
from .vision_cloud import (
    GEMINI_API_BASE,
    GEMINI_THINKING_CONFIG,
    GEMINI_TIMEOUT_SECONDS,
    _is_quota_error,
    _redact,
    _short,
    _status_detail,
    _truncate,
    load_gemini_key,
)

# Audio understanding needs the full Flash tier: the vision default
# (flash-lite) is priced for OCR-shaped work, and what is asked here — hearing
# a hesitation, a restart, wind behind a voice — is exactly what the lite tier
# drops first. Like vision_cloud's model constant this is unconfirmed against a
# live call; the owner's smoke test decides what it becomes.
DEFAULT_VOICE_OBSERVER_MODEL = "gemini-3.6-flash"

VOICE_OBSERVER_ENV = "CMX_VOICE_OBSERVER"
VOICE_OBSERVER_MODEL_ENV = "CMX_VOICE_OBSERVER_MODEL"

# Same ceiling and rationale as vision_cloud.MAX_IMAGE_BYTES: inline base64
# must stay under Gemini's per-request limit with headroom. Voice clips are
# MP3 by the time they reach the observer, so a minutes-long message is still
# well under a single MiB.
MAX_OBSERVER_AUDIO_BYTES = 8 * 1024 * 1024

# The closed vocabulary. Enum fields default to their first value, which is
# also the "nothing notable" answer the prompt tells the model to prefer when
# unsure — an uncertain observer must fall back to silence, not to a guess.
ENUM_FIELDS: dict[str, tuple[str, ...]] = {
    "speed": ("medium", "slow", "fast"),
    "speed_change": ("none", "speeding_up", "slowing_down"),
    "pause": ("few", "many"),
    "volume": ("medium", "soft", "loud"),
    "pitch_range": ("normal", "flat", "varied"),
    "laugh": ("none", "light", "clear"),
    "voice_quality": ("normal", "tense", "trembling"),
    "background": ("quiet", "voices", "noisy", "outdoor", "wind", "music"),
}
BOOL_FIELDS: tuple[str, ...] = (
    "breathy",
    "sigh",
    "breath_audible",
    "restart",
    "self_correction",
)

# What each enum value renders as. An absent key renders as nothing: "medium
# speed", "few pauses" and their like stay silent so a typical line keeps to
# the three-to-five tokens the reader said it will actually keep reading.
# `speed` and `background` always render (their "normal" value is still
# information: she is somewhere quiet, speaking at her usual pace).
_ENUM_TOKENS: dict[str, dict[str, str]] = {
    "speed": {"slow": "语速偏慢", "medium": "语速中等", "fast": "语速偏快"},
    "speed_change": {"speeding_up": "越说越快", "slowing_down": "越说越慢"},
    "pause": {"many": "停顿多"},
    "volume": {"soft": "音量轻", "loud": "音量大"},
    "pitch_range": {"flat": "起伏小", "varied": "起伏大"},
    "laugh": {"light": "轻笑", "clear": "笑声"},
    "voice_quality": {"tense": "声音发紧", "trembling": "声音发抖"},
    "background": {
        "quiet": "背景安静",
        "voices": "背景人声",
        "noisy": "背景嘈杂",
        "outdoor": "背景户外",
        "wind": "背景风声",
        "music": "背景音乐",
    },
}
_BOOL_TOKENS: dict[str, str] = {
    "breathy": "气声",
    "sigh": "叹气",
    "breath_audible": "吸气明显",
    "restart": "重说",
    "self_correction": "改口",
}

# Render order: pace first, voice itself second, events third, environment
# last — the reader scans left to right and the leftmost tokens are the ones
# that change most often.
_RENDER_ORDER: tuple[str, ...] = (
    "speed",
    "speed_change",
    "pause",
    "volume",
    "pitch_range",
    "breathy",
    "voice_quality",
    "laugh",
    "sigh",
    "breath_audible",
    "restart",
    "self_correction",
    "background",
)


def observer_enabled() -> bool:
    """The observer is on unless CMX_VOICE_OBSERVER is explicitly "off"/"0".

    Default-on matches the image cloud pass: a configured Gemini key is the
    real switch, and this env var exists so the audio side can be turned off
    alone — it is the one cloud pass that carries the owner's voice.
    """
    return os.getenv(VOICE_OBSERVER_ENV, "").strip().lower() not in {"off", "0", "false"}


def observer_model() -> str:
    return os.getenv(VOICE_OBSERVER_MODEL_ENV, "").strip() or DEFAULT_VOICE_OBSERVER_MODEL


def validate_observation(fields: Any) -> dict[str, Any] | None:
    """Return the normalized enum form, or None if anything strays off-vocabulary.

    Strict on purpose: a value outside the vocabulary means the model ignored
    the schema, and rendering it anyway would put drifting words in front of a
    reader whose whole use of this line is word-for-word comparison over time.
    """
    if not isinstance(fields, dict):
        return None
    observed: dict[str, Any] = {}
    for name, allowed in ENUM_FIELDS.items():
        value = fields.get(name)
        if not isinstance(value, str) or value not in allowed:
            return None
        observed[name] = value
    for name in BOOL_FIELDS:
        value = fields.get(name)
        if not isinstance(value, bool):
            return None
        observed[name] = value
    return observed


def render_voice_note(observed: dict[str, Any]) -> str:
    """Render the one-line note from a validated enum form, deterministically."""
    tokens: list[str] = []
    for name in _RENDER_ORDER:
        if name in _BOOL_TOKENS:
            if observed.get(name) is True:
                tokens.append(_BOOL_TOKENS[name])
            continue
        token = _ENUM_TOKENS.get(name, {}).get(str(observed.get(name)))
        if token:
            tokens.append(token)
    return f"[声音: {' · '.join(tokens)}]"


def _response_schema() -> dict[str, Any]:
    properties: dict[str, Any] = {
        name: {"type": "STRING", "enum": list(allowed)} for name, allowed in ENUM_FIELDS.items()
    }
    properties.update({name: {"type": "BOOLEAN"} for name in BOOL_FIELDS})
    return {
        "type": "OBJECT",
        "properties": properties,
        "required": [*ENUM_FIELDS, *BOOL_FIELDS],
    }


_PROMPT = """\
你是"声音观察器"，不是心理分析器。听这段语音，只根据可以直接听到的现象填表。

规则：
- 每个字段只能从给定选项中选，不写任何其他内容。
- 只描述听得见的现象，不推断情绪、心理状态、人格、疾病、动机或原因。
- 不根据说话内容反推声音状态：判断依据是声音本身，不是她说了什么。
- 拿不准某个字段时，选它的默认档（medium / none / few / normal / quiet / false），\
不为了凑内容而虚构特征。

字段含义：
- speed：整体语速。slow=明显偏慢，fast=明显偏快，medium=普通。
- speed_change：过程中语速有没有持续变化。speeding_up=越说越快，slowing_down=越说越慢。
- pause：停顿。many=停顿或犹豫明显偏多（含说一半停住），few=正常。
- volume：整体音量。soft=明显偏轻，loud=明显偏大。
- pitch_range：语调起伏。flat=明显平，varied=明显起伏大。
- laugh：笑声。light=轻笑或含笑，clear=明显笑出声。
- voice_quality：声音质感。tense=明显发紧，trembling=明显发抖。
- background：背景声。quiet=安静，voices=有他人说话声，noisy=嘈杂环境音，\
outdoor=明显户外环境声，wind=明显风声或风噪，music=音乐或电视声。选最主要的一种。
- breathy：是否带明显气声。
- sigh：是否有叹气。
- breath_audible：吸气或呼吸声是否明显。
- restart：是否有把一句话推倒重说。
- self_correction：是否有说到一半改口换说法。
"""


def observe_voice(
    audio_bytes: bytes,
    *,
    paths: Paths,
    mime_type: str = "audio/mp3",
) -> dict[str, Any]:
    """Run one closed-vocabulary observation pass over *audio_bytes*.

    On success:

        {"observed": {<validated enum form>}, "voice_note": "[声音: …]"}

    On failure, a dict with an "error" key instead, never a raised exception —
    the same buckets as vision_cloud.recognize_image, plus "invalid_response"
    when the reply strays outside the closed vocabulary:

        {"error": "not_configured" | "oversized" | "quota_exhausted"
                | "unavailable" | "invalid_response", "detail": "..."}

    The API key never appears in a returned "detail" string, by the same
    two-line defense as recognize_image.
    """
    key = load_gemini_key(paths)
    if not key:
        return {"error": "not_configured"}

    if len(audio_bytes) > MAX_OBSERVER_AUDIO_BYTES:
        return {
            "error": "oversized",
            "detail": f"{len(audio_bytes)} bytes exceeds the {MAX_OBSERVER_AUDIO_BYTES} byte limit",
        }

    body = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": _PROMPT},
                    {
                        "inlineData": {
                            "mimeType": mime_type,
                            "data": base64.b64encode(audio_bytes).decode("ascii"),
                        }
                    },
                ],
            }
        ],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": _response_schema(),
            "thinkingConfig": GEMINI_THINKING_CONFIG,
        },
    }

    url = f"{GEMINI_API_BASE}/models/{observer_model()}:generateContent"
    try:
        response = httpx.post(
            url,
            headers={"x-goog-api-key": key, "Content-Type": "application/json"},
            json=body,
            timeout=GEMINI_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        return {"error": "unavailable", "detail": _redact(_short(exc), key)}

    payload: Any = None
    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError):
        payload = None

    if response.status_code >= 400:
        if _is_quota_error(response.status_code, payload):
            return {"error": "quota_exhausted"}
        return {"error": "unavailable", "detail": _redact(_status_detail(response, payload), key)}

    if not isinstance(payload, dict):
        return {"error": "invalid_response", "detail": _redact(_truncate(response.text), key)}

    try:
        parts = payload["candidates"][0]["content"]["parts"]
        text = "".join(part["text"] for part in parts if isinstance(part, dict) and "text" in part)
        fields = json.loads(text)
    except Exception as exc:  # candidates/parts shape and inner JSON are both untrusted
        return {"error": "invalid_response", "detail": _short(exc)}

    observed = validate_observation(fields)
    if observed is None:
        return {"error": "invalid_response", "detail": "reply strayed outside the closed vocabulary"}

    return {"observed": observed, "voice_note": render_voice_note(observed)}
