from __future__ import annotations

from collections.abc import Iterable
from typing import Any


READ_SCOPE = "cmx:read"
SOCIAL_SCOPE = "cmx:social"


def require_request_scope(context: Any, required: str) -> None:
    """Enforce authorization from the current MCP request, never process state."""
    request_context = getattr(context, "request_context", None)
    if request_context is None:
        raise PermissionError("insufficient_scope")
    request = getattr(request_context, "request", None) if request_context else None
    if request is None:
        raise PermissionError("insufficient_scope")
    state = getattr(request, "state", None)
    scopes = getattr(state, "cmx_scopes", None) if state is not None else None
    if scopes is None:
        raw_scope = getattr(request, "scope", {}).get("state", {})
        scopes = raw_scope.get("cmx_scopes") if isinstance(raw_scope, dict) else None
    if required not in set(_normalize(scopes or [])):
        raise PermissionError("insufficient_scope")


def _normalize(scopes: Iterable[str] | str) -> list[str]:
    values = scopes.split() if isinstance(scopes, str) else [str(value) for value in scopes]
    return sorted(set(value for value in values if value))
