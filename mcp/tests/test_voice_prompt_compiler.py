"""Pure mapping tests for the deferred ElevenLabs v3 adapter prototype."""

from __future__ import annotations

import pytest

from cmx_mcp.voice_prompt_compiler import compile_voice_prompt


def test_experimental_tags_compile_in_a_frozen_order():
    prompt = compile_voice_prompt(
        "Stay with me.",
        {
            "breathiness": 0.75,
            "softness": 0.82,
            "pitch_motion": "wavering",
            "release": "fading",
        },
    )

    assert prompt == "[softly] [breathy] [wavering] Stay with me…"


def test_ineffective_experimental_tags_use_stable_tag_and_punctuation():
    prompt = compile_voice_prompt(
        "Stay with me.",
        {
            "breathiness": 0.75,
            "softness": 0.82,
            "pitch_motion": "wavering",
            "release": "fading",
        },
        experimental_tags=False,
    )

    assert prompt == "[whispers] …Stay with me…"
    assert "[softly]" not in prompt
    assert "[breathy]" not in prompt
    assert "[wavering]" not in prompt


def test_documented_release_tag_and_cut_punctuation_are_stable():
    assert compile_voice_prompt(
        "All right.", {"release": "breath_release"}, experimental_tags=False
    ) == "All right. [exhales]"
    assert compile_voice_prompt(
        "Wait!", {"release": "cut"}, experimental_tags=False
    ) == "Wait—"


def test_neutral_delivery_preserves_text_and_ignores_future_schema_fields():
    assert compile_voice_prompt(
        "  Plain speech.  ",
        {"pitch_motion": "stable", "release": "voiced_release", "tempo": "medium"},
    ) == "Plain speech."


@pytest.mark.parametrize(
    ("delivery", "field"),
    [
        ({"softness": -0.1}, "softness"),
        ({"breathiness": 1.1}, "breathiness"),
        ({"breathiness": True}, "breathiness"),
        ({"pitch_motion": "spiral"}, "pitch_motion"),
        ({"release": "echo"}, "release"),
    ],
)
def test_invalid_delivery_values_fail_closed(delivery, field):
    with pytest.raises(ValueError, match=field):
        compile_voice_prompt("Text", delivery)


def test_inputs_are_not_mutated_and_results_are_deterministic():
    delivery = {"softness": 0.7, "release": "fading"}
    before = dict(delivery)

    first = compile_voice_prompt("Quiet.", delivery)
    second = compile_voice_prompt("Quiet.", delivery)

    assert first == second == "[softly] Quiet…"
    assert delivery == before

