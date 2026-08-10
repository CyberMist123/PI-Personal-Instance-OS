"""Shared harness for the Clipboard API tests.

Only identity verification is stubbed. Everything else — SQLite, staging, atomic
promotion, Origin checks — is the real implementation.
"""

from __future__ import annotations

import io
from typing import Any

from starlette.applications import Starlette
from starlette.testclient import TestClient

from cmx_mcp import clipboard_api
from cmx_mcp.clipboard_api import build_clipboard_routes
from cmx_mcp.web_auth import WebIdentity

ORIGIN = "https://pi.test"
TOKENS = {
    "tok-a": WebIdentity(account_id="acct-a", acct="alice"),
    "tok-b": WebIdentity(account_id="acct-b", acct="bob"),
}


def make_client(tmp_path, monkeypatch) -> tuple[TestClient, Any]:
    routes, api = build_clipboard_routes(
        runtime=tmp_path,
        base_url="https://pi.invalid",
        allowed_origins={ORIGIN},
    )
    monkeypatch.setattr(
        clipboard_api, "verify_web_identity", lambda base, token: TOKENS.get(token)
    )
    return TestClient(Starlette(routes=routes)), api


def headers(token: str = "tok-a", *, origin: str | None = ORIGIN) -> dict[str, str]:
    result = {"Authorization": f"Bearer {token}"}
    if origin is not None:
        result["Origin"] = origin
    return result


def post_entry(
    client: TestClient,
    *,
    token: str = "tok-a",
    text: str = "",
    files: list[tuple[str, bytes, str]] | None = None,
    origin: str | None = ORIGIN,
):
    payload = [("files", (name, io.BytesIO(data), ctype)) for name, data, ctype in files or []]
    return client.post(
        "/clipboard-api/entries",
        data={"text": text},
        files=payload or None,
        headers=headers(token, origin=origin),
    )


def entry_ids(client: TestClient, token: str = "tok-a", **params) -> list[str]:
    response = client.get(
        "/clipboard-api/entries", params=params, headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200, response.text
    return [e["entry_id"] for e in response.json()["entries"]]
