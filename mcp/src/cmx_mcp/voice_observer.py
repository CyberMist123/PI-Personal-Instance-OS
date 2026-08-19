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

# The same model the vision pass uses. A live smoke test (2026-08-15, the
# owner's first real voice clip) settled two guesses at once: the earlier
# `gemini-3.6-flash` guess does not exist and returns HTTP 400, while
# `gemini-3.1-flash-lite` handles audio + the enum responseSchema fine and
# returns a clean closed-vocabulary form. Override with CMX_VOICE_OBSERVER_MODEL
# if a stronger tier ever proves worth the cost on hard clips.
DEFAULT_VOICE_OBSERVER_MODEL = "gemini-3.1-flash-lite"

VOICE_OBSERVER_ENV = "CMX_VOICE_OBSERVER"
VOICE_OBSERVER_MODEL_ENV = "CMX_VOICE_OBSERVER_MODEL"
TG_R18_ENV = "CMX_VOICE_NVV"
TG_R18_MODEL_ENV = "CMX_TG_R18_MODEL"
DEFAULT_TG_R18_MODEL = "gemini-3.6-flash"
TG_R18_TIMEOUT_SECONDS = 180.0

# Same ceiling and rationale as vision_cloud.MAX_IMAGE_BYTES: inline base64
# must stay under Gemini's per-request limit with headroom. Voice clips are
# MP3 by the time they reach the observer, so a minutes-long message is still
# well under a single MiB.
MAX_OBSERVER_AUDIO_BYTES = 8 * 1024 * 1024

# The Gemini call has a fixed 30 s timeout, and processing time grows with clip
# length: a ~36 s note returns in time, a ~64 s note does not, so a minutes-long
# note always timed out and produced no voice_note at all. The observer reads
# how the speaker sounds, which the opening captures well enough, so a long note
# is remuxed down to this many seconds before the call rather than skipped.
MAX_OBSERVER_AUDIO_SECONDS = 30.0

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

# R18 is deliberately separate from the legacy one-line observer above.  It
# is only selected by the trusted Telegram bridge's explicit nvv=1 request;
# browser uploads continue to use the frozen legacy schema.
R18_EVENT_NAMES: tuple[str, ...] = (
    "moan", "gasp", "pant", "breath", "sigh", "whimper", "groan",
    "nonlexical_vowel", "laugh", "cough", "throat_clear", "yawn",
    "exercise_breathing", "noise", "speech",
)
R18_PERCEPTUAL: tuple[str, ...] = (
    "breathy", "airy", "soft", "sharp", "husky", "rough", "trembling",
    "shaky", "strained", "suppressed", "muffled", "drawn_out", "abrupt",
    "wavering", "rising_tail", "falling_tail", "fading", "clipped",
    "heavy_breathing", "rapid_breathing", "broken_breath",
)
R18_PITCH = ("lower", "similar", "higher", "clearly_higher")
R18_INTENSITY = ("soft", "medium", "strong")
R18_ATTACK = ("gradual", "abrupt", "none")
R18_RELEASE = ("fading", "clipped", "sustained", "none")
_R18_LABEL_ZH = {
    "moan": "呻吟", "gasp": "吸气", "pant": "喘息", "breath": "呼吸",
    "sigh": "叹息", "whimper": "呜咽", "groan": "低吟",
    "nonlexical_vowel": "非词汇元音", "laugh": "笑声", "cough": "咳嗽",
    "throat_clear": "清嗓", "yawn": "哈欠", "exercise_breathing": "运动喘气",
    "noise": "噪声", "speech": "说话",
}
_R18_FEATURE_ZH = {
    "breathy": "气声", "airy": "轻空气感", "soft": "柔", "sharp": "尖锐",
    "husky": "沙哑", "rough": "粗糙", "trembling": "发颤", "shaky": "发抖",
    "strained": "发紧", "suppressed": "压着", "muffled": "闷", "drawn_out": "拖长",
    "abrupt": "短促", "wavering": "起伏不稳", "rising_tail": "尾部上扬",
    "falling_tail": "尾部下落", "fading": "渐弱", "clipped": "收束很快",
    "heavy_breathing": "呼吸较重", "rapid_breathing": "呼吸较快", "broken_breath": "断续呼吸",
}

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


def tg_r18_enabled() -> bool:
    """R18 is off until the owner enables the TG-only caller explicitly."""
    return os.getenv(TG_R18_ENV, "").strip().lower() in {"1", "on", "true", "yes"}


def tg_r18_model() -> str:
    return os.getenv(TG_R18_MODEL_ENV, "").strip() or DEFAULT_TG_R18_MODEL


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


def validate_r18_observation(value: Any) -> dict[str, Any] | None:
    """Validate perceptual labels only; no free prose or fake measurements."""
    if not isinstance(value, dict) or not isinstance(value.get("events"), list):
        return None
    events: list[dict[str, Any]] = []
    for raw in value["events"][:12]:
        if not isinstance(raw, dict):
            return None
        try:
            start_ms = max(0, int(raw["start_ms"]))
            end_ms = max(start_ms, int(raw["end_ms"]))
        except (KeyError, TypeError, ValueError):
            return None
        candidates_raw = raw.get("candidates")
        if isinstance(candidates_raw, list):
            candidate_items = []
            for item in candidates_raw:
                if not isinstance(item, dict):
                    return None
                candidate_items.append((item.get("label"), item.get("confidence")))
        elif isinstance(candidates_raw, dict):
            # Backward compatibility for already-cached rows and old tests.
            candidate_items = list(candidates_raw.items())
        else:
            return None
        candidates: dict[str, float] = {}
        for name, score in candidate_items:
            if name not in R18_EVENT_NAMES or isinstance(score, bool):
                return None
            try:
                number = float(score)
            except (TypeError, ValueError):
                return None
            if not 0 <= number <= 1:
                return None
            if number > 0:
                candidates[name] = round(number, 3)
        if not candidates:
            continue
        perceptual_raw = raw.get("perceptual")
        if not isinstance(perceptual_raw, list) or any(item not in R18_PERCEPTUAL for item in perceptual_raw):
            return None
        pitch = raw.get("pitch_relative")
        intensity = raw.get("intensity")
        attack = raw.get("attack")
        release = raw.get("release")
        if pitch not in R18_PITCH or intensity not in R18_INTENSITY or attack not in R18_ATTACK or release not in R18_RELEASE:
            return None
        events.append({
            "start_ms": start_ms,
            "end_ms": end_ms,
            "candidates": dict(sorted(candidates.items(), key=lambda item: (-item[1], item[0]))[:3]),
            "perceptual": list(dict.fromkeys(perceptual_raw))[:6],
            "pitch_relative": pitch,
            "intensity": intensity,
            "attack": attack,
            "release": release,
        })
    trajectory_raw = value.get("trajectory", [])
    if not isinstance(trajectory_raw, list) or any(item not in R18_EVENT_NAMES for item in trajectory_raw):
        return None
    trajectory = [str(item) for item in trajectory_raw[:8]]
    if not trajectory:
        trajectory = [max(event["candidates"], key=event["candidates"].get) for event in events]
    return {"events": events, "trajectory": trajectory}


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


def _r18_event_note(event: dict[str, Any], *, compact: bool = False) -> str:
    ranked = sorted(event["candidates"].items(), key=lambda item: (-float(item[1]), item[0]))
    primary = ranked[0][0]
    if compact:
        return f"[{primary}]"
    perceptual = list(event.get("perceptual") or [])
    features: list[str] = []
    if "breathy" in perceptual and "soft" in perceptual:
        features.append("气声柔")
        perceptual = [item for item in perceptual if item not in {"breathy", "soft"}]
    features.extend(_R18_FEATURE_ZH[item] for item in perceptual if item in _R18_FEATURE_ZH)
    pitch = event.get("pitch_relative")
    if pitch != "similar":
        features.append({"lower": "比平时音高偏低", "higher": "比平时音高偏高", "clearly_higher": "比平时音高明显偏高"}.get(pitch, ""))
    if event.get("attack") == "gradual":
        features.append("起音渐入")
    if event.get("release") == "fading":
        features.append("渐弱")
    details = " · ".join(item for item in dict.fromkeys(features) if item) or "质感较平"
    ambiguity = ""
    if len(ranked) > 1 and float(ranked[1][1]) >= 0.10:
        ambiguity = f" | 偏{primary}，或为{_R18_LABEL_ZH[ranked[1][0]]}"
    return f"[{primary}: {details}{ambiguity}]"


def render_r18_note(
    observed: dict[str, Any],
    *,
    transcript: str = "",
    segments: list[dict[str, Any]] | None = None,
) -> str:
    """Render the issue #39 surface: transcript timeline, ambiguity, trajectory."""
    events = list(observed.get("events") or [])[:5]
    if not events:
        return "<voice>\n整体：未检出明确非语言事件\n</voice>"
    timeline: list[tuple[int, int, str]] = []
    for segment in segments or []:
        text = str(segment.get("text") or "").strip()
        if text:
            timeline.append((int(segment.get("start_ms") or 0), 0, f"“{text}”"))
    if not timeline and transcript.strip():
        timeline.append((0, 0, f"“{transcript.strip()}”"))
    expanded: set[str] = set()
    for event in events:
        primary = max(event["candidates"], key=event["candidates"].get)
        timeline.append(
            (int(event.get("start_ms") or 0), 1, _r18_event_note(event, compact=primary in expanded))
        )
        expanded.add(primary)
    timeline.sort(key=lambda item: (item[0], item[1]))
    lines = [" ".join(item[2] for item in timeline)]
    trajectory = observed.get("trajectory") or [max(event["candidates"], key=event["candidates"].get) for event in events]
    family_tokens = {
        "moan": "有声呼气", "sigh": "有声呼气", "groan": "非词汇发声",
        "whimper": "非词汇发声", "nonlexical_vowel": "非词汇发声",
        "gasp": "吸气", "pant": "呼吸", "breath": "呼吸",
        "exercise_breathing": "呼吸", "laugh": "笑声", "cough": "咳嗽",
        "throat_clear": "清嗓", "yawn": "哈欠", "noise": "噪声", "speech": "说话",
    }
    steps = ["说话"] if transcript.strip() else []
    steps.extend(family_tokens[item] for item in trajectory[:8] if item in family_tokens)
    steps = [item for index, item in enumerate(steps) if index == 0 or item != steps[index - 1]]
    if steps:
        lines.append("走向: " + " → ".join(steps))
    return "<voice>\n" + "\n".join(lines) + "\n</voice>"


def _r18_response_schema() -> dict[str, Any]:
    candidate = {
        "type": "OBJECT",
        "properties": {
            "label": {"type": "STRING", "enum": list(R18_EVENT_NAMES)},
            "confidence": {"type": "NUMBER", "minimum": 0, "maximum": 1},
        },
        "required": ["label", "confidence"],
    }
    event = {
        "type": "OBJECT",
        "properties": {
            "start_ms": {"type": "INTEGER"},
            "end_ms": {"type": "INTEGER"},
            "candidates": {"type": "ARRAY", "items": candidate, "minItems": 1, "maxItems": 3},
            "perceptual": {"type": "ARRAY", "items": {"type": "STRING", "enum": list(R18_PERCEPTUAL)}},
            "pitch_relative": {"type": "STRING", "enum": list(R18_PITCH)},
            "intensity": {"type": "STRING", "enum": list(R18_INTENSITY)},
            "attack": {"type": "STRING", "enum": list(R18_ATTACK)},
            "release": {"type": "STRING", "enum": list(R18_RELEASE)},
        },
        "required": ["start_ms", "end_ms", "candidates", "perceptual", "pitch_relative", "intensity", "attack", "release"],
    }
    return {
        "type": "OBJECT",
        "properties": {
            "events": {"type": "ARRAY", "items": event},
            "trajectory": {"type": "ARRAY", "items": {"type": "STRING", "enum": list(R18_EVENT_NAMES)}},
        },
        "required": ["events", "trajectory"],
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

_R18_PROMPT = """\
你是私人 TG 语音的 R18 非语言声音观察器。只根据可直接听见的声音标注，不推断情绪、动机、关系、成人语义或性唤起；不输出物理测量值。
每个事件填时间范围、多个候选与 0–1 置信度、受控感知特征、离散相对音高、强弱、attack/release。候选必须同时比较：moan、gasp、pant、breath、sigh、whimper、groan、nonlexical_vowel，以及 laugh、cough、throat_clear、yawn、exercise_breathing、noise、speech。它们完全平级。
moan=持续有音高的非词汇元音；pant=连续快速重复吸呼循环；gasp=单次突然短促强吸气；sigh=较长呼气释放。不要把咳嗽标为 gasp，不要把连续喘气统称 breath，不要把有音高的呻吟统称 sigh。时间相对整条音频，以毫秒表示。没有明确事件时 events 为空。
只按 response schema 返回 JSON；不写解释、Markdown、原始 F0、breathiness 数值或任何情绪/性唤起判断。
"""


def observe_voice(
    audio_bytes: bytes,
    *,
    paths: Paths,
    mime_type: str = "audio/mp3",
    mode: str = "default",
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
    if mode not in {"default", "tg_r18"}:
        return {"error": "invalid_mode"}
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
                    {"text": _R18_PROMPT if mode == "tg_r18" else _PROMPT},
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
            "responseSchema": _r18_response_schema() if mode == "tg_r18" else _response_schema(),
            "thinkingConfig": (
                {"thinkingLevel": "minimal"}
                if mode == "tg_r18"
                else GEMINI_THINKING_CONFIG
            ),
        },
    }

    model = tg_r18_model() if mode == "tg_r18" else observer_model()
    url = f"{GEMINI_API_BASE}/models/{model}:generateContent"
    try:
        response = httpx.post(
            url,
            headers={"x-goog-api-key": key, "Content-Type": "application/json"},
            json=body,
            timeout=TG_R18_TIMEOUT_SECONDS if mode == "tg_r18" else GEMINI_TIMEOUT_SECONDS,
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

    observed = validate_r18_observation(fields) if mode == "tg_r18" else validate_observation(fields)
    if observed is None:
        return {"error": "invalid_response", "detail": "reply strayed outside the closed vocabulary"}

    if mode == "tg_r18":
        note = render_r18_note(observed)
        return {"observed": observed, "voice_note": note, "nvv": {"note": note, **observed}}
    return {"observed": observed, "voice_note": render_voice_note(observed)}
