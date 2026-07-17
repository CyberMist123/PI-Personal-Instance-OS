from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

_HOST_RE = re.compile(r"^[A-Za-z0-9.-]+(?::[0-9]{1,5})?$")
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


@dataclass(frozen=True, slots=True)
class Paths:
    home: Path
    runtime: Path
    database: Path
    secrets: Path
    logs: Path

    @classmethod
    def discover(cls) -> "Paths":
        default_home = Path(__file__).resolve().parents[2]
        home = Path(os.getenv("CMX_MCP_HOME", str(default_home))).resolve()
        runtime = home / "runtime"
        return cls(
            home=home,
            runtime=runtime,
            database=runtime / "cmx.sqlite3",
            secrets=runtime / "secrets",
            logs=runtime / "logs",
        )

    def ensure(self) -> None:
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.secrets.mkdir(parents=True, exist_ok=True)
        self.logs.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True, slots=True)
class InstanceSettings:
    base_url: str
    host_header: str
    timeout_seconds: float = 20.0
    max_items: int = 30
    max_context_ancestors: int = 10
    max_context_descendants: int = 20
    max_context_chars: int = 16000
    max_media_bytes: int = 20 * 1024 * 1024

    @classmethod
    def load(cls, paths: Paths) -> "InstanceSettings":
        values = _read_env_file(paths.home.parent / ".env")
        base_url = os.getenv("CMX_MASTODON_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
        parsed = urlparse(base_url)
        if parsed.scheme != "http" or parsed.hostname not in _LOOPBACK:
            raise RuntimeError("CMX_MASTODON_BASE_URL must be a loopback http URL")

        host = os.getenv("CMX_MASTODON_HOST", "").strip() or values.get("WEB_DOMAIN", "").strip()
        if not host:
            raise RuntimeError("CMX_MASTODON_HOST or parent .env WEB_DOMAIN is required")
        if "://" in host or "/" in host or not _HOST_RE.fullmatch(host):
            raise RuntimeError("CMX_MASTODON_HOST must be a hostname, optionally with a port")

        return cls(
            base_url=base_url,
            host_header=host,
            timeout_seconds=_bounded_float("CMX_TIMEOUT_SECONDS", 20.0, 1.0, 120.0),
            max_items=_bounded_int("CMX_MAX_ITEMS", 30, 1, 100),
            max_context_ancestors=_bounded_int("CMX_CONTEXT_ANCESTORS", 10, 1, 30),
            max_context_descendants=_bounded_int("CMX_CONTEXT_DESCENDANTS", 20, 1, 50),
            max_context_chars=_bounded_int("CMX_CONTEXT_CHARS", 16000, 1000, 100000),
            max_media_bytes=_bounded_int(
                "CMX_MAX_MEDIA_BYTES", 20 * 1024 * 1024, 1024, 100 * 1024 * 1024
            ),
        )


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    result: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    try:
        value = default if raw is None else int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return value


def _bounded_float(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = os.getenv(name)
    try:
        value = default if raw is None else float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be numeric") from exc
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return value
