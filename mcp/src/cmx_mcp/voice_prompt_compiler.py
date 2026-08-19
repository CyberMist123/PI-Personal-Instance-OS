"""Compile provider-neutral delivery wishes into an ElevenLabs v3 prompt.

This is deliberately only a pure adapter prototype.  Whether experimental
tags work is voice-specific, so the caller supplies that already-tested fact;
the compiler never probes ElevenLabs or performs TTS itself.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


_SCORE_FIELDS = ("softness", "breathiness")
_PITCH_TAGS = {
    "stable": None,
    "rising": "[rising]",
    "falling": "[falling]",
    "arch": "[arching]",
    "dip": "[dipping]",
    "wavering": "[wavering]",
}
_RELEASES = {"cut", "fading", "breath_release", "voiced_release"}
_EXPERIMENTAL_THRESHOLD = 0.6


def _score(delivery: Mapping[str, Any], name: str) -> float:
    value = delivery.get(name, 0.0)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number from 0 to 1")
    value = float(value)
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be a number from 0 to 1")
    return value


def _enum(delivery: Mapping[str, Any], name: str, allowed: set[str], default: str) -> str:
    value = delivery.get(name, default)
    if not isinstance(value, str) or value not in allowed:
        choices = ", ".join(sorted(allowed))
        raise ValueError(f"{name} must be one of: {choices}")
    return value


def _with_terminal(text: str, mark: str) -> str:
    """Replace terminal delivery punctuation without changing spoken words."""
    return text.rstrip(" .!?。！？…—-") + mark


def compile_voice_prompt(
    text: str,
    desired_delivery: Mapping[str, Any],
    *,
    experimental_tags: bool = True,
) -> str:
    """Return ElevenLabs v3 audio tags plus dialogue text.

    ``experimental_tags=False`` is the per-voice fallback path.  It uses only
    tags explicitly documented by ElevenLabs and punctuation documented for
    pauses, trailing delivery, and interruptions.  Unknown delivery keys are
    intentionally ignored so the provider-neutral schema can grow separately.
    """
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if not isinstance(desired_delivery, Mapping):
        raise TypeError("desired_delivery must be a mapping")

    scores = {name: _score(desired_delivery, name) for name in _SCORE_FIELDS}
    pitch_motion = _enum(
        desired_delivery, "pitch_motion", set(_PITCH_TAGS), "stable"
    )
    release = _enum(desired_delivery, "release", _RELEASES, "voiced_release")

    tags: list[str] = []
    rendered_text = text.strip()

    if experimental_tags:
        if scores["softness"] >= _EXPERIMENTAL_THRESHOLD:
            tags.append("[softly]")
        if scores["breathiness"] >= _EXPERIMENTAL_THRESHOLD:
            tags.append("[breathy]")
        pitch_tag = _PITCH_TAGS[pitch_motion]
        if pitch_tag:
            tags.append(pitch_tag)
    else:
        # [whispers] is a documented delivery tag and is the closest stable
        # expression of high softness.  Breathiness and a wavering contour have
        # no documented stable equivalents, so ellipses carry the fallback.
        if scores["softness"] >= _EXPERIMENTAL_THRESHOLD:
            tags.append("[whispers]")
        if (
            scores["breathiness"] >= _EXPERIMENTAL_THRESHOLD
            or pitch_motion in {"arch", "dip", "wavering"}
        ) and rendered_text:
            rendered_text = f"…{rendered_text}"

    if release == "fading" and rendered_text:
        rendered_text = _with_terminal(rendered_text, "…")
    elif release == "cut" and rendered_text:
        rendered_text = _with_terminal(rendered_text, "—")
    elif release == "breath_release":
        tags_after = "[exhales]"
    else:
        tags_after = ""

    prefix = " ".join(tags)
    compiled = " ".join(part for part in (prefix, rendered_text) if part)
    if release == "breath_release":
        compiled = " ".join(part for part in (compiled, tags_after) if part)
    return compiled

