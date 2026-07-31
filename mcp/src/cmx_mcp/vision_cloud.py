"""Credential handling for the cloud vision pass.

The key is DPAPI-sealed under `runtime/secrets/`, the same place and the same
protection as a resident's Mastodon token: readable only by the Windows user
that wrote it, never in Git, never in an environment variable that a crash dump
or a child process would carry. `cmx-admin gemini-key` is the only writer.

Local OCR never needs any of this. A machine with no key still runs the local
pass on every image and simply leaves the cloud columns unfilled, which is why
`load_gemini_key` returns None rather than raising when nothing is configured —
an absent key is a supported state, not a failure.
"""

from __future__ import annotations

from pathlib import Path

from .config import Paths
from .secrets import read_secret

GEMINI_KEY_FILENAME = "gemini.key.dpapi"


def gemini_key_path(paths: Paths) -> Path:
    return paths.secrets / GEMINI_KEY_FILENAME


def gemini_key_configured(paths: Paths) -> bool:
    path = gemini_key_path(paths)
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def load_gemini_key(paths: Paths) -> str | None:
    """Return the stored key, or None when the cloud pass is not configured.

    A key that exists but cannot be decrypted is a different thing entirely —
    usually a file copied from another Windows account — and is raised rather
    than reported as "not configured", so the fix is not mistaken for setup.
    """
    if not gemini_key_configured(paths):
        return None
    return read_secret(gemini_key_path(paths)).strip() or None
