"""Clipboard file bytes: staging, one atomic rename, and deletion.

Layout under the (git-ignored) runtime directory:

    runtime/clipboard/staging/<batch_id>/<file_id>
    runtime/clipboard/objects/<entry_id>/<file_id>

An upload is written entirely into one staging directory, then promoted with a
SINGLE os.replace of that directory. There is no window in which an entry has
some of its files: either the whole batch is in place or none of it is.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
from pathlib import Path
from typing import Any, BinaryIO

from .clipboard_db import ClipboardError

COPY_CHUNK = 1024 * 1024
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_TYPE_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+/[A-Za-z0-9!#$%&'*+.^_`|~-]+$")
DEFAULT_TYPE = "application/octet-stream"


def safe_filename(value: str) -> str:
    """Strip anything that could escape a directory or forge a header line.

    The result is only ever used as a display name and as the download
    filename; the path on disk is the server-generated file_id, never this.
    """
    name = str(value or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = _CONTROL_RE.sub("", name).strip().strip(".")
    name = name[:120]
    return name or "file"


def safe_content_type(value: str) -> str:
    candidate = _CONTROL_RE.sub("", str(value or "")).split(";", 1)[0].strip()
    return candidate if _TYPE_RE.fullmatch(candidate) else DEFAULT_TYPE


class ClipboardFiles:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.staging = self.root / "staging"
        self.objects = self.root / "objects"

    def ensure(self) -> None:
        self.staging.mkdir(parents=True, exist_ok=True)
        self.objects.mkdir(parents=True, exist_ok=True)

    def cleanup_staging(self) -> int:
        """Drop batches left behind by a crash. Called once at startup."""
        self.ensure()
        removed = 0
        for child in self.staging.iterdir():
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
                removed += 1
        return removed

    # ---------- staging ----------

    def new_batch(self) -> str:
        self.ensure()
        batch_id = secrets.token_urlsafe(12)
        (self.staging / batch_id).mkdir(parents=True, exist_ok=False)
        return batch_id

    def stage(
        self, batch_id: str, stream: BinaryIO, *, filename: str, content_type: str, budget: int
    ) -> dict[str, Any]:
        """Copy one upload into the batch, refusing to exceed `budget` bytes.

        The budget is checked while streaming, not afterwards: a caller must not
        be able to make us write a 5 GiB temp file before we reject it.
        """
        file_id = secrets.token_urlsafe(16)
        target = self.staging / batch_id / file_id
        written = 0
        with open(target, "wb") as out:
            while True:
                chunk = stream.read(COPY_CHUNK)
                if not chunk:
                    break
                written += len(chunk)
                if written > budget:
                    out.close()
                    target.unlink(missing_ok=True)
                    raise ClipboardError("entry_too_large", status=413, max_bytes=budget)
                out.write(chunk)
        if written < 1:
            target.unlink(missing_ok=True)
            raise ClipboardError("empty_file")
        return {
            "file_id": file_id,
            "original_name": safe_filename(filename),
            "safe_name": file_id,
            "content_type": safe_content_type(content_type),
            "size_bytes": written,
        }

    def promote(self, batch_id: str, entry_id: str) -> None:
        """One atomic directory rename: staging/<batch> -> objects/<entry>."""
        self.ensure()
        source = self.staging / batch_id
        target = self.objects / entry_id
        if not source.is_dir():
            raise ClipboardError("staging_missing", status=500)
        if target.exists():
            raise ClipboardError("entry_collision", status=500)
        os.replace(source, target)

    def discard(self, batch_id: str) -> None:
        shutil.rmtree(self.staging / batch_id, ignore_errors=True)

    # ---------- objects ----------

    def object_path(self, entry_id: str, file_id: str) -> Path:
        # Both ids are server-generated token_urlsafe values; reject anything
        # else outright rather than trying to sanitize a caller-shaped path.
        if not _is_token(entry_id) or not _is_token(file_id):
            raise ClipboardError("not_found", status=404)
        return self.objects / entry_id / file_id

    def delete_entry(self, entry_id: str) -> None:
        if not _is_token(entry_id):
            return
        shutil.rmtree(self.objects / entry_id, ignore_errors=True)

    def delete_file(self, entry_id: str, file_id: str) -> None:
        if not _is_token(entry_id) or not _is_token(file_id):
            return
        (self.objects / entry_id / file_id).unlink(missing_ok=True)
        directory = self.objects / entry_id
        try:
            if directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()
        except OSError:
            pass


_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _is_token(value: str) -> bool:
    return bool(_TOKEN_RE.fullmatch(str(value or "")))
