from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator

FILE_ATTRIBUTE_REPARSE_POINT = 0x400

_MAGIC = {
    "image/jpeg": lambda head: head.startswith(b"\xff\xd8\xff"),
    "image/png": lambda head: head.startswith(b"\x89PNG\r\n\x1a\n"),
    "image/gif": lambda head: head.startswith((b"GIF87a", b"GIF89a")),
    "image/webp": lambda head: head.startswith(b"RIFF") and head[8:12] == b"WEBP",
}

_EXTENSIONS = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


@dataclass(slots=True)
class OpenMedia:
    stream: BinaryIO
    filename: str
    mime_type: str
    size_bytes: int


@contextmanager
def open_safe_image(
    *,
    media_root: Path,
    relative_path: str,
    max_bytes: int,
) -> Iterator[OpenMedia]:
    requested = Path(relative_path)
    if requested.is_absolute() or str(requested).startswith(("\\\\", "//")):
        raise ValueError("media path must be relative to this bot's media directory")
    if any(part in {"", ".", ".."} for part in requested.parts):
        raise ValueError("media path contains an invalid segment")

    root = media_root.resolve(strict=True)
    candidate = (root / requested).resolve(strict=True)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise PermissionError("media path escapes the configured media directory") from exc

    pre = candidate.stat()
    if not candidate.is_file():
        raise ValueError("media path is not a regular file")
    if getattr(pre, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT:
        raise PermissionError("reparse-point media files are not allowed")
    if pre.st_nlink > 1:
        raise PermissionError("hard-linked media files are not allowed")
    if pre.st_size < 1 or pre.st_size > max_bytes:
        raise ValueError(f"media size must be between 1 and {max_bytes} bytes")

    expected_mime = _EXTENSIONS.get(candidate.suffix.lower())
    if not expected_mime:
        raise ValueError("only jpg, png, gif, and webp images are allowed")

    stream = candidate.open("rb")
    try:
        current = os.fstat(stream.fileno())
        if (current.st_dev, current.st_ino, current.st_size) != (
            pre.st_dev,
            pre.st_ino,
            pre.st_size,
        ):
            raise PermissionError("media file changed during validation")
        head = stream.read(16)
        if not _MAGIC[expected_mime](head):
            raise ValueError("file content does not match its image extension")
        stream.seek(0)
        yield OpenMedia(
            stream=stream,
            filename=candidate.name,
            mime_type=expected_mime,
            size_bytes=current.st_size,
        )
    finally:
        stream.close()
