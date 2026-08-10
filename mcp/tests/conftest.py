from __future__ import annotations

import os

import pytest

# The suite is run on the same Windows box that runs the service, and that box
# exports real CMX_* settings (model tier, trusted-loopback flag, Qwen URL,
# whisper model dir). Without this fixture those leak into every test: the OCR
# cases wrote `small` weights but resolve_tier() picked up CMX_OCR_MODEL_TIER=medium
# and returned "model_missing", and the two "when the flag is off" auth cases
# saw CMX_LOCAL_TRUSTED_MEDIA=1 and got 502/503 where they asserted 401.
#
# Clearing by prefix rather than by name is deliberate: the next env var someone
# adds to their shell should not be able to quietly re-open this hole. Tests that
# want a setting still set it themselves with monkeypatch.setenv, which runs
# after this autouse fixture.


@pytest.fixture(autouse=True)
def _isolate_cmx_environment(monkeypatch):
    for name in [key for key in os.environ if key.startswith("CMX_")]:
        monkeypatch.delenv(name, raising=False)
