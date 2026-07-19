from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from pathlib import Path
from typing import Any

CRYPTPROTECT_UI_FORBIDDEN = 0x1


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


_crypt32: Any | None = None
_kernel32: Any | None = None


def _require_windows() -> None:
    if sys.platform != "win32":
        raise RuntimeError("Windows DPAPI is not supported on this platform")


def _dpapi() -> tuple[Any, Any]:
    """Load and configure Windows DPAPI on first use, never during module import."""
    global _crypt32, _kernel32
    _require_windows()
    if _crypt32 is not None and _kernel32 is not None:
        return _crypt32, _kernel32
    crypt32 = ctypes.WinDLL("Crypt32.dll")
    kernel32 = ctypes.WinDLL("Kernel32.dll")
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(DATA_BLOB), wintypes.LPCWSTR, ctypes.POINTER(DATA_BLOB),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DATA_BLOB),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(DATA_BLOB), ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(DATA_BLOB), ctypes.c_void_p, ctypes.c_void_p,
        wintypes.DWORD, ctypes.POINTER(DATA_BLOB),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    _crypt32, _kernel32 = crypt32, kernel32
    return crypt32, kernel32


def _blob(data: bytes) -> tuple[DATA_BLOB, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data)
    return (
        DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))),
        buffer,
    )


def protect_for_current_user(secret: str) -> bytes:
    crypt32, kernel32 = _dpapi()
    if not secret:
        raise ValueError("secret cannot be empty")
    in_blob, in_buffer = _blob(secret.encode("utf-8"))
    out_blob = DATA_BLOB()
    ok = crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        "CMX MCP token",
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    )
    _ = in_buffer
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(out_blob.pbData, ctypes.c_void_p))


def unprotect_for_current_user(payload: bytes) -> str:
    crypt32, kernel32 = _dpapi()
    in_blob, in_buffer = _blob(payload)
    out_blob = DATA_BLOB()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    )
    _ = in_buffer
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData).decode("utf-8")
    finally:
        kernel32.LocalFree(ctypes.cast(out_blob.pbData, ctypes.c_void_p))


def write_secret(path: Path, secret: str) -> None:
    _require_windows()
    if (
        len(secret) < 20
        or secret != secret.strip()
        or any(ord(character) < 33 or ord(character) == 127 for character in secret)
    ):
        raise ValueError(
            "credential looks invalid; control characters and pasted Ctrl+V are not accepted"
        )
    encrypted = protect_for_current_user(secret)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(encrypted)
    temporary.replace(path)


def read_secret(path: Path) -> str:
    _require_windows()
    if not path.exists():
        raise RuntimeError(f"Credential file is missing: {path}")
    return unprotect_for_current_user(path.read_bytes())
