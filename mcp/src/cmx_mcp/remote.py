from __future__ import annotations

import argparse
import html
import json
import os
import re
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import uvicorn
from mcp.server.auth.routes import create_auth_routes
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from .config import InstanceSettings, Paths, validate_remote_profile
from .db import Database
from .remote_auth import CmxOAuthProvider, OAuthStore, READ_SCOPE, SOCIAL_SCOPE
from .server import Runtime, build_server


_BOT_ID_RE = re.compile(r"^[a-z0-9_-]+$")
_MCP_PATH_RE = re.compile(r"^/mcp/([a-z0-9_-]+)$")
_BEARER_RE = re.compile(r"^Bearer\s+([^\s]+)$", re.IGNORECASE)
MAX_REQUEST_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class RemoteSettings:
    bind_host: str
    port: int
    public_origin: str

    @property
    def approval_origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def public_host(self) -> str:
        return str(urlparse(self.public_origin).netloc).lower()

    def resource_url(self, bot_id: str) -> str:
        return f"{self.public_origin}/mcp/{bot_id}"

    def resource_to_bot(self, resource: str) -> str | None:
        prefix = f"{self.public_origin}/mcp/"
        normalized = resource.rstrip("/")
        if not normalized.startswith(prefix):
            return None
        bot_id = normalized[len(prefix) :]
        if not _BOT_ID_RE.fullmatch(bot_id) or normalized != self.resource_url(bot_id):
            return None
        return bot_id

    @classmethod
    def load(cls, paths: Paths) -> "RemoteSettings":
        instance = InstanceSettings.load(paths)
        bind_host = os.getenv("CMX_MCP_HTTP_BIND", "127.0.0.1").strip()
        if bind_host not in {"127.0.0.1", "localhost", "::1"}:
            raise RuntimeError("CMX_MCP_HTTP_BIND must stay on loopback")
        try:
            port = int(os.getenv("CMX_MCP_HTTP_PORT", "8766"))
        except ValueError as exc:
            raise RuntimeError("CMX_MCP_HTTP_PORT must be an integer") from exc
        if not 1024 <= port <= 65535:
            raise RuntimeError("CMX_MCP_HTTP_PORT must be between 1024 and 65535")
        return cls(
            bind_host=bind_host,
            port=port,
            public_origin=instance.public_base_url.rstrip("/"),
        )


def create_remote_app(paths: Paths | None = None) -> Starlette:
    paths = paths or Paths.discover()
    paths.ensure()
    settings = RemoteSettings.load(paths)
    database = Database(paths.database)
    database.initialize()
    oauth_store = OAuthStore(paths.database)
    oauth_store.initialize()

    def bot_is_enabled(bot_id: str) -> bool:
        try:
            bot = database.get_bot(bot_id)
            return bot.enabled and bot.remote_profile != "disabled"
        except RuntimeError:
            return False

    provider = CmxOAuthProvider(
        store=oauth_store,
        approval_origin=settings.approval_origin,
        resource_to_bot=settings.resource_to_bot,
        bot_is_enabled=bot_is_enabled,
    )

    runtimes: dict[str, Runtime] = {}
    servers = []
    mcp_routes = []
    transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    )
    for bot in database.list_bots():
        if not bot.enabled or bot.remote_profile == "disabled":
            continue
        validate_remote_profile(bot.remote_profile)
        runtime = Runtime(bot.bot_id)
        server = build_server(
            runtime,
            remote_profile=bot.remote_profile,
            remote_capabilities=bot,
            streamable_http_path=f"/mcp/{bot.bot_id}",
            stateless_http=True,
            json_response=True,
            transport_security=transport_security,
        )
        child = server.streamable_http_app()
        runtimes[bot.bot_id] = runtime
        servers.append(server)
        mcp_routes.extend(child.routes)

    async def protected_resource(request: Request) -> Response:
        bot_id = str(request.path_params.get("bot_id") or "")
        if bot_id not in runtimes:
            return JSONResponse({"error": "not_found"}, status_code=404)
        resource = settings.resource_url(bot_id)
        return JSONResponse(
            {
                "resource": resource,
                "authorization_servers": [settings.public_origin],
                "bearer_methods_supported": ["header"],
                "scopes_supported": [READ_SCOPE, SOCIAL_SCOPE],
                "resource_name": f"CMX resident {bot_id} ({database.get_bot(bot_id).remote_profile} profile)",
            },
            headers={"Cache-Control": "no-store"},
        )

    async def approve(request: Request) -> Response:
        if not _is_loopback_host(request.headers.get("host", ""), settings.port):
            return Response(status_code=404)
        if request.method == "POST":
            origin = request.headers.get("origin", "")
            if origin and origin.rstrip("/") != settings.approval_origin:
                return JSONResponse({"error": "invalid_origin"}, status_code=403)
            form = await request.form()
            request_id = str(form.get("request") or "")
            approved = str(form.get("decision") or "") == "allow"
            try:
                target = provider.complete(request_id, approved=approved)
            except RuntimeError as exc:
                return _approval_error(str(exc))
            return RedirectResponse(target, status_code=303, headers={"Cache-Control": "no-store"})

        request_id = str(request.query_params.get("request") or "")
        pending = provider.pending(request_id)
        if pending is None:
            return _approval_error("This authorization request expired or was already used")
        bot = database.get_bot(pending.bot_id)
        requested_scope_text = html.escape(" ".join(pending.scopes))
        permission_text = (
            "cmx:read：读取该居民有权查看的身份、时间线、动态和本地搜索索引。"
            if SOCIAL_SCOPE not in pending.scopes
            else "cmx:read：读取内容；cmx:social：未来社交写操作的授权基础。本 Phase 0 远程端仍不会暴露写工具。"
        )
        body = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CMX MCP 授权</title>
<style>body{{font-family:system-ui;margin:0;background:#111827;color:#f9fafb}}
main{{max-width:560px;margin:8vh auto;padding:32px;background:#1f2937;border-radius:18px}}
.muted{{color:#9ca3af}}button{{border:0;border-radius:10px;padding:12px 20px;margin-right:10px}}
.allow{{background:#22c55e;color:#052e16}}.deny{{background:#374151;color:#fff}}</style></head>
<body><main><h1>允许只读 MCP 连接？</h1>
<p><strong>{html.escape(pending.client_name)}</strong> 请求连接 AI 居民
<strong>{html.escape(bot.display_name or bot.bot_id)}</strong>。</p>
<p><strong>Requested scopes:</strong> {requested_scope_text}</p>
<p class="muted">{permission_text}</p>
<form method="post" action="/oauth/approve">
<input type="hidden" name="request" value="{html.escape(request_id)}">
<button class="allow" name="decision" value="allow">允许</button>
<button class="deny" name="decision" value="deny">取消</button>
</form></main></body></html>"""
        return HTMLResponse(body, headers={"Cache-Control": "no-store"})

    async def health(_request: Request) -> Response:
        return JSONResponse(
            {
                "ok": True,
                "transport": "streamable-http",
                "mode": "read-only",
            },
            headers={"Cache-Control": "no-store"},
        )

    oauth_routes = create_auth_routes(
        provider=provider,
        issuer_url=AnyHttpUrl(settings.public_origin),
        client_registration_options=ClientRegistrationOptions(
            enabled=True,
            client_secret_expiry_seconds=365 * 86400,
            valid_scopes=[READ_SCOPE, SOCIAL_SCOPE],
            default_scopes=[READ_SCOPE],
        ),
        revocation_options=RevocationOptions(enabled=True),
    )

    routes = [
        *oauth_routes,
        Route(
            "/.well-known/oauth-protected-resource/mcp/{bot_id}",
            protected_resource,
            methods=["GET", "OPTIONS"],
        ),
        Route("/oauth/approve", approve, methods=["GET", "POST"]),
        Route("/_cmx/mcp-health", health, methods=["GET"]),
        *mcp_routes,
    ]

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        async with AsyncExitStack() as stack:
            for server in servers:
                await stack.enter_async_context(server.session_manager.run())
            try:
                yield
            finally:
                for runtime in runtimes.values():
                    runtime.close()

    middleware = [
        Middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
            allow_headers=["*"],
            expose_headers=["Mcp-Session-Id", "WWW-Authenticate"],
        ),
        Middleware(
            RemoteBoundaryMiddleware,
            provider=provider,
            database=database,
            runtimes=runtimes,
            settings=settings,
        ),
        Middleware(RequestSizeLimitMiddleware, max_bytes=MAX_REQUEST_BYTES),
    ]
    return Starlette(routes=routes, middleware=middleware, lifespan=lifespan)


class RequestSizeLimitMiddleware:
    def __init__(self, app: Any, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") == "http":
            headers = {key.lower(): value for key, value in scope.get("headers", [])}
            try:
                length = int(headers.get(b"content-length", b"0") or b"0")
            except ValueError:
                length = self.max_bytes + 1
            if length > self.max_bytes:
                await _asgi_json(send, 413, {"error": "request_too_large"})
                return
        await self.app(scope, receive, send)


class RemoteBoundaryMiddleware:
    def __init__(
        self,
        app: Any,
        *,
        provider: CmxOAuthProvider,
        database: Database,
        runtimes: dict[str, Runtime],
        settings: RemoteSettings,
    ) -> None:
        self.app = app
        self.provider = provider
        self.database = database
        self.runtimes = runtimes
        self.settings = settings

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path") or "")
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        host = headers.get(b"host", b"").decode("latin-1").lower()
        if path == "/oauth/approve":
            await self.app(scope, receive, send)
            return
        if host not in {self.settings.public_host, f"127.0.0.1:{self.settings.port}", f"localhost:{self.settings.port}"}:
            await _asgi_json(send, 421, {"error": "invalid_host"})
            return

        match = _MCP_PATH_RE.fullmatch(path)
        if match:
            bot_id = match.group(1)
            if bot_id not in self.runtimes:
                await _asgi_json(send, 404, {"error": "unknown_resident"})
                return
            try:
                bot = self.database.get_bot(bot_id)
            except RuntimeError:
                await _asgi_json(send, 404, {"error": "unknown_resident"})
                return
            if not bot.enabled:
                await _asgi_json(send, 403, {"error": "resident_disabled"})
                return

            auth = headers.get(b"authorization", b"").decode("latin-1")
            bearer = _BEARER_RE.fullmatch(auth.strip())
            access = await self.provider.load_access_token(bearer.group(1)) if bearer else None
            expected_resource = self.settings.resource_url(bot_id)
            if (
                access is None
                or access.subject != bot_id
                or str(access.resource or "").rstrip("/") != expected_resource
                or READ_SCOPE not in access.scopes
            ):
                metadata = (
                    f"{self.settings.public_origin}/.well-known/"
                    f"oauth-protected-resource/mcp/{bot_id}"
                )
                await _asgi_json(
                    send,
                    401,
                    {"error": "unauthorized", "resource_metadata": metadata},
                    extra_headers=[
                        (
                            b"www-authenticate",
                            (
                                'Bearer realm="CMX", '
                                f'resource_metadata="{metadata}", scope="{READ_SCOPE}"'
                            ).encode("ascii"),
                        )
                    ],
                )
                return
            scope_state = scope.setdefault("state", {})
            if isinstance(scope_state, dict):
                scope_state["cmx_scopes"] = list(access.scopes)
        await self.app(scope, receive, send)


async def _asgi_json(
    send: Any,
    status: int,
    payload: dict[str, Any],
    *,
    extra_headers: list[tuple[bytes, bytes]] | None = None,
) -> None:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = [
        (b"content-type", b"application/json; charset=utf-8"),
        (b"cache-control", b"no-store"),
        (b"content-length", str(len(body)).encode("ascii")),
        *(extra_headers or []),
    ]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


def _is_loopback_host(host: str, port: int) -> bool:
    return host.lower() in {
        f"127.0.0.1:{port}",
        f"localhost:{port}",
        f"[::1]:{port}",
    }


def _approval_error(message: str) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><meta charset='utf-8'><title>CMX MCP</title>"
        f"<main style='font-family:system-ui;padding:40px'><h1>无法授权</h1><p>{html.escape(message)}</p></main>",
        status_code=400,
        headers={"Cache-Control": "no-store"},
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the read-only CMX remote MCP")
    parser.parse_args()
    paths = Paths.discover()
    settings = RemoteSettings.load(paths)
    uvicorn.run(
        create_remote_app(paths),
        host=settings.bind_host,
        port=settings.port,
        log_level="info",
        access_log=False,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
