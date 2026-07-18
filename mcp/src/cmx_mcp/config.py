from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

_HOST_RE = re.compile(r"^[A-Za-z0-9.-]+(?::[0-9]{1,5})?$")
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
REMOTE_PROFILES = frozenset({"disabled", "reader", "social", "social_plus"})


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

    @property
    def public_base_url(self) -> str:
        return f"https://{self.host_header}"

    @classmethod
    def load(cls, paths: Paths) -> "InstanceSettings":
        values: dict[str, str] = {}
        # Docker Compose uses .env for interpolation and .env.production for
        # Mastodon itself. Read both so MCP works regardless of which file
        # carries WEB_DOMAIN on the target machine.
        for env_path in (paths.home.parent / ".env", paths.home.parent / ".env.production"):
            values.update(_read_env_file(env_path))

        host = (
            os.getenv("CMX_MASTODON_HOST", "").strip()
            or os.getenv("WEB_DOMAIN", "").strip()
            or values.get("WEB_DOMAIN", "").strip()
        )
        if not host:
            raise RuntimeError(
                "CMX_MASTODON_HOST, WEB_DOMAIN, or repository .env.production WEB_DOMAIN is required"
            )
        if "://" in host or "/" in host or not _HOST_RE.fullmatch(host):
            raise RuntimeError("CMX_MASTODON_HOST must be a hostname, optionally with a port")

        # Use the already-working public HTTPS endpoint by default. A local
        # loopback endpoint remains available as an explicit opt-in for
        # deployments whose reverse proxy has been verified for API traffic.
        base_url = os.getenv("CMX_MASTODON_BASE_URL", f"https://{host}").rstrip("/")
        parsed = urlparse(base_url)
        if parsed.path not in ("", "/") or parsed.params or parsed.query or parsed.fragment:
            raise RuntimeError("CMX_MASTODON_BASE_URL must not contain a path, query, or fragment")
        if parsed.scheme == "http":
            if parsed.hostname not in _LOOPBACK:
                raise RuntimeError("An http CMX_MASTODON_BASE_URL must be loopback only")
        elif parsed.scheme == "https":
            if parsed.netloc.lower() != host.lower():
                raise RuntimeError("An https CMX_MASTODON_BASE_URL must match CMX_MASTODON_HOST")
        else:
            raise RuntimeError("CMX_MASTODON_BASE_URL must use https, or loopback http")

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


@dataclass(frozen=True, slots=True)
class RemoteCapabilities:
    polls: bool = True
    boosts: bool = False
    notifications: bool = False


def validate_remote_profile(profile: str, capabilities: RemoteCapabilities | None = None) -> tuple[str, RemoteCapabilities]:
    if profile not in REMOTE_PROFILES:
        raise ValueError("remote_profile must be disabled, reader, social, or social_plus")
    caps = capabilities or RemoteCapabilities()
    if not isinstance(caps.polls, bool) or not isinstance(caps.boosts, bool) or not isinstance(caps.notifications, bool):
        raise ValueError("remote capabilities must be boolean")
    return profile, caps


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
