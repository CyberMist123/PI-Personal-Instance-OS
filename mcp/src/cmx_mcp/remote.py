from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import html
import json
import os
import re
import secrets
import shutil
import time
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import httpx
import uvicorn
from mcp.server.auth.routes import create_auth_routes
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from .clipboard_api import build_clipboard_routes
from .config import InstanceSettings, Paths, validate_remote_profile
from .compact import compact_status
from .db import Database
from .mastodon_client import MastodonApiError, MastodonClient
from .ocr import (
    model_dir_ready as ocr_model_dir_ready,
    ocr_image,
    resolve_model_dir as resolve_ocr_model_dir,
    resolve_tier as resolve_ocr_tier,
)
from .remote_auth import CmxOAuthProvider, OAuthStore, READ_SCOPE, SOCIAL_SCOPE
from .server import Runtime, build_server, refresh_search_cache
from .transcribe import cloud_asr_configured, model_dir_ready, transcribe_file
from .vision_cloud import MAX_IMAGE_BYTES, ask_image, gemini_key_configured, recognize_image
from .voice_media import MP3_MIME, MP3_SUFFIX, VoiceMediaError, to_mp3
from .voice_observer import (
    MAX_OBSERVER_AUDIO_SECONDS,
    observe_voice,
    observer_enabled,
    observer_model,
)
from .voice_widget import VOICE_WIDGET_JS, VOICE_WIDGET_VERSION
from .search_widget import SEARCH_WIDGET_JS, SEARCH_WIDGET_VERSION
from .web_auth import verify_web_bearer, verify_web_identity
from .workers import WorkerConfig

CLIPBOARD_SWEEP_SECONDS = 600
_LOGGER = logging.getLogger(__name__)


_BOT_ID_RE = re.compile(r"^[a-z0-9_-]+$")
_MCP_PATH_RE = re.compile(r"^/mcp/([a-z0-9_-]+)$")
_BEARER_RE = re.compile(r"^Bearer\s+([^\s]+)$", re.IGNORECASE)
_UPLOAD_SUFFIX_RE = re.compile(r"^\.[A-Za-z0-9]{1,8}$")
_FONT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}\.woff2$")
# Fonts ship with the code, not with the runtime directory: CMX_MCP_HOME
# points at mutable state, while this is a versioned build artefact.
ASSETS_DIR = Path(__file__).resolve().parents[2] / "assets"
MAX_REQUEST_BYTES = 1024 * 1024
VERIFY_TIMEOUT_SECONDS = 10.0
MAX_ASK_QUESTION_CHARS = 2000


def _append_vision_qa_log(paths: Paths, *, sha256: str, question: str, result: dict) -> None:
    """One JSONL line per /files/ask exchange, so the owner can read what was
    asked and answered without a database query. Best-effort: a logging
    failure never fails the request."""
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sha256": sha256,
        "question": question,
    }
    if result.get("error"):
        record["error"] = str(result["error"])
    else:
        record["answer"] = str(result.get("answer") or "")
    try:
        log_path = paths.runtime / "vision-qa.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _verify_mastodon_bearer(base_url: str, token: str) -> bool:
    """Ask the instance itself whether this browser session token is valid.

    Thin alias kept at this name because the voice-widget tests patch it here.
    The implementation moved to web_auth so Clipboard can reuse it and get the
    account identity back instead of just a boolean.
    """
    return verify_web_bearer(base_url, token)


def _visibility_failure(error: MastodonApiError) -> bool:
    return error.status_code in {401, 403, 404}


class _NoStoreResponse:
    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        async def send_no_store(message: dict) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() != b"cache-control"
                ]
                message = {**message, "headers": [*headers, (b"cache-control", b"no-store")]}
            await send(message)

        await self.app(scope, receive, send_no_store)


@dataclass(frozen=True, slots=True)
class RemoteSettings:
    bind_host: str
    port: int
    public_origin: str

    @property
    def approval_origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def oauth_issuer(self) -> str:
        """Canonical RFC 8414 issuer shared by every discovery document."""
        return f"{self.public_origin}/"

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
    instance_settings = InstanceSettings.load(paths)
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

    def bot_remote_profile(bot_id: str) -> str | None:
        try:
            bot = database.get_bot(bot_id)
            return bot.remote_profile if bot.enabled else None
        except RuntimeError:
            return None

    provider = CmxOAuthProvider(
        store=oauth_store,
        approval_origin=settings.approval_origin,
        resource_to_bot=settings.resource_to_bot,
        bot_is_enabled=bot_is_enabled,
        bot_remote_profile=bot_remote_profile,
        redeem_origin=settings.public_origin,
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
                "authorization_servers": [settings.oauth_issuer],
                "bearer_methods_supported": ["header"],
                "scopes_supported": ([READ_SCOPE, SOCIAL_SCOPE]
                                      if database.get_bot(bot_id).remote_profile in {"social", "social_plus"}
                                      else [READ_SCOPE]),
                "resource_name": f"CMX resident {bot_id} ({database.get_bot(bot_id).remote_profile} profile)",
            },
            headers={"Cache-Control": "no-store"},
        )

    async def approve(request: Request) -> Response:
        if not _is_loopback_host(request.headers.get("host", ""), settings.port):
            return Response(status_code=404)
        if request.method == "POST":
            origin = request.headers.get("origin", "")
            if origin and origin.rstrip("/") not in _loopback_origins(settings.port):
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
        consent_title, permission_text = _consent_copy(pending.scopes, bot)
        body = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CMX MCP 授权</title>
<style>body{{font-family:system-ui;margin:0;background:#111827;color:#f9fafb}}
main{{max-width:560px;margin:8vh auto;padding:32px;background:#1f2937;border-radius:18px}}
.muted{{color:#9ca3af}}button{{border:0;border-radius:10px;padding:12px 20px;margin-right:10px}}
.allow{{background:#22c55e;color:#052e16}}.deny{{background:#374151;color:#fff}}</style></head>
<body><main><h1>{consent_title}</h1>
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

    def _invite_form(request_id: str, pending: Any, *, error: str | None = None) -> HTMLResponse:
        bot = database.get_bot(pending.bot_id)
        if getattr(pending, "scopes_explicit", True):
            scope_text = html.escape(" ".join(pending.scopes)) + "（最终以邀请码为准）"
        else:
            scope_text = "由邀请码决定（客户端未指定）"
        error_html = (
            f'<p style="color:#f87171"><strong>{html.escape(error)}</strong></p>' if error else ""
        )
        body = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CMX MCP 邀请码</title>
<style>body{{font-family:system-ui;margin:0;background:#111827;color:#f9fafb}}
main{{max-width:560px;margin:8vh auto;padding:32px;background:#1f2937;border-radius:18px}}
.muted{{color:#9ca3af}}input{{width:100%;box-sizing:border-box;padding:12px;border-radius:10px;
border:1px solid #374151;background:#111827;color:#f9fafb;font-size:16px}}
button{{border:0;border-radius:10px;padding:12px 20px;margin-top:14px;background:#22c55e;color:#052e16;font-size:16px}}</style>
</head><body><main><h1>输入邀请码完成连接</h1>
<p><strong>{html.escape(pending.client_name)}</strong> 请求连接 AI 居民
<strong>{html.escape(bot.display_name or bot.bot_id)}</strong>。</p>
<p class="muted">Requested scopes: {scope_text}</p>
{error_html}
<form method="post" action="/oauth/invite">
<input type="hidden" name="request" value="{html.escape(request_id)}">
<input name="code" placeholder="cmx-…" autocomplete="off" autofocus>
<br><button type="submit">兑换并授权</button>
</form>
<p class="muted">邀请码由该实例的 Owner 生成，单次有效。Owner 本人也可在服务器本机打开
http://127.0.0.1:{settings.port}/oauth/approve?request={html.escape(request_id)} 直接批准。</p>
</main></body></html>"""
        return HTMLResponse(body, headers={"Cache-Control": "no-store"})

    async def invite(request: Request) -> Response:
        if request.method == "POST":
            origin = request.headers.get("origin", "")
            allowed_origins = {settings.public_origin, *_loopback_origins(settings.port)}
            if origin and origin.rstrip("/") not in allowed_origins:
                return JSONResponse({"error": "invalid_origin"}, status_code=403)
            form = await request.form()
            request_id = str(form.get("request") or "")
            raw_code = str(form.get("code") or "")
            try:
                target = provider.redeem(request_id, raw_code)
            except RuntimeError as exc:
                return _approval_error(str(exc))
            except ValueError as exc:
                pending = provider.pending(request_id)
                if pending is None:
                    return _approval_error("This authorization request expired or was already used")
                return _invite_form(request_id, pending, error=str(exc))
            return RedirectResponse(target, status_code=303, headers={"Cache-Control": "no-store"})

        request_id = str(request.query_params.get("request") or "")
        pending = provider.pending(request_id)
        if pending is None:
            return _approval_error("This authorization request expired or was already used")
        return _invite_form(request_id, pending)

    owner_upload_failures: list[float] = []

    async def _bearer_access(request: Request) -> Any | None:
        auth = request.headers.get("authorization", "")
        bearer = _BEARER_RE.fullmatch(auth.strip())
        if not bearer:
            return None
        return await provider.load_access_token(bearer.group(1))

    def _store_upload(bot_id: str, upload: Any) -> tuple[dict[str, Any] | None, JSONResponse | None]:
        name = _safe_filename(str(getattr(upload, "filename", "") or ""))
        stream = upload.file
        stream.seek(0, 2)
        size = stream.tell()
        stream.seek(0)
        if size < 1:
            return None, JSONResponse({"error": "empty_file"}, status_code=400)
        if size > instance_settings.filebox_max_bytes:
            return None, JSONResponse(
                {"error": "file_too_large", "max_bytes": instance_settings.filebox_max_bytes},
                status_code=413,
            )
        if database.filebox_usage(bot_id) + size > instance_settings.filebox_quota_bytes:
            return None, JSONResponse(
                {"error": "quota_exceeded", "quota_bytes": instance_settings.filebox_quota_bytes},
                status_code=413,
            )
        file_id = secrets.token_urlsafe(16)
        target_dir = paths.filebox / bot_id / file_id
        target_dir.mkdir(parents=True, exist_ok=True)
        with open(target_dir / name, "wb") as out:
            shutil.copyfileobj(stream, out)
        database.filebox_add(bot_id=bot_id, file_id=file_id, file_name=name, size_bytes=size)
        return {
            "ok": True,
            "bot": bot_id,
            "file_id": file_id,
            "name": name,
            "size_bytes": size,
            "url": f"{settings.public_origin}/files/{bot_id}/{file_id}/{quote(name)}",
        }, None

    async def filebox_upload(request: Request) -> Response:
        # File bytes never travel through MCP tools: this is a plain HTTP
        # endpoint, so residents can store files without them ever entering a
        # model context window.
        access = await _bearer_access(request)
        if access is None or SOCIAL_SCOPE not in access.scopes:
            return JSONResponse(
                {"error": "unauthorized", "hint": "a cmx:social bearer token is required"},
                status_code=401,
                headers={"Cache-Control": "no-store"},
            )
        form = await request.form()
        upload = form.get("file")
        if upload is None or not hasattr(upload, "filename"):
            return JSONResponse({"error": "multipart field 'file' is required"}, status_code=400)
        result, failure = _store_upload(str(access.subject or ""), upload)
        if failure is not None:
            return failure
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    async def filebox_download(request: Request) -> Response:
        bot_id = str(request.path_params.get("bot_id") or "")
        file_id = str(request.path_params.get("file_id") or "")
        name = str(request.path_params.get("name") or "")
        row = database.filebox_get(bot_id, file_id) if _BOT_ID_RE.fullmatch(bot_id.lstrip("_")) else None
        if row is None or row["file_name"] != name:
            return JSONResponse({"error": "not_found"}, status_code=404)
        path = paths.filebox / bot_id / file_id / name
        if not path.is_file():
            return JSONResponse({"error": "not_found"}, status_code=404)
        return FileResponse(path, filename=name)

    def _owner_page(message: str = "", link: str = "") -> HTMLResponse:
        link_html = (
            f'<p><strong>链接：</strong><br><code style="word-break:break-all">{html.escape(link)}</code></p>'
            if link
            else ""
        )
        message_html = f'<p style="color:#f87171">{html.escape(message)}</p>' if message else ""
        configured = database.get_setting("filebox_pass") is not None
        body_form = (
            """<form method="post" action="/files/up" enctype="multipart/form-data">
<input type="password" name="passphrase" placeholder="Owner 上传口令" autocomplete="off"><br><br>
<input type="file" name="file"><br><br>
<button type="submit">上传</button></form>"""
            if configured
            else "<p class=\"muted\">尚未设置上传口令。先在服务器运行：cmx-admin filebox-pass</p>"
        )
        body = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CMX 文件柜</title>
<style>body{{font-family:system-ui;margin:0;background:#111827;color:#f9fafb}}
main{{max-width:560px;margin:8vh auto;padding:32px;background:#1f2937;border-radius:18px}}
.muted{{color:#9ca3af}}input,button{{font-size:16px;padding:10px;border-radius:10px;border:1px solid #374151;
background:#111827;color:#f9fafb}}button{{background:#22c55e;color:#052e16;border:0;padding:10px 22px}}</style>
</head><body><main><h1>CMX 文件柜 · Owner 上传</h1>
{message_html}{link_html}{body_form}
<p class="muted">任意后缀，单文件上限 {instance_settings.filebox_max_bytes // (1024 * 1024)} MB。
上传后把链接贴进动态即可；AI 只会看到链接，不会读取文件内容。</p>
</main></body></html>"""
        return HTMLResponse(body, headers={"Cache-Control": "no-store"})

    async def filebox_owner(request: Request) -> Response:
        if request.method == "GET":
            return _owner_page()
        now = time.time()
        owner_upload_failures[:] = [value for value in owner_upload_failures if now - value < 600]
        if len(owner_upload_failures) >= 10:
            return JSONResponse({"error": "too_many_attempts"}, status_code=429)
        form = await request.form()
        stored = database.get_setting("filebox_pass")
        passphrase = str(form.get("passphrase") or "")
        if not stored or not _verify_passphrase(passphrase, stored):
            owner_upload_failures.append(now)
            return _owner_page(message="口令不正确")
        upload = form.get("file")
        if upload is None or not hasattr(upload, "filename"):
            return _owner_page(message="请选择一个文件")
        result, failure = _store_upload("_owner", upload)
        if failure is not None:
            detail = failure.body.decode("utf-8", "ignore") if hasattr(failure, "body") else "上传失败"
            return _owner_page(message=f"上传失败：{detail}")
        return _owner_page(message="上传成功 ✓", link=result["url"])

    async def voice_widget(_request: Request) -> Response:
        # Nginx sub_filter injects <script src="/files/voice.js" defer> into the
        # owner's own Mastodon HTML. The script is static and public: it carries
        # no credential, it reads the page's own web token at runtime.
        return Response(
            VOICE_WIDGET_JS,
            media_type="application/javascript",
            headers={
                # no-cache, not no-store: the browser and Cloudflare may keep the
                # copy but must revalidate against the ETag every time, so a
                # version bump lands immediately. max-age let two separate fixes
                # sit invisible behind a stale script for hours; a 304 on each
                # page load is the cheaper trade on a single-user instance.
                "Cache-Control": "no-cache",
                "ETag": f'"voice-{VOICE_WIDGET_VERSION}"',
            },
        )

    async def search_widget(_request: Request) -> Response:
        # Same static, credential-free shape as voice_widget above: this script
        # only patches window.fetch at runtime using the page's own token, and
        # carries none of its own.
        return Response(
            SEARCH_WIDGET_JS,
            media_type="application/javascript",
            headers={
                "Cache-Control": "no-cache",
                "ETag": f'"search-{SEARCH_WIDGET_VERSION}"',
            },
        )

    async def _observe_voice_clip(source_path: Path) -> str | None:
        # The paralinguistic side-channel next to transcription. Every exit
        # from here that is not a validated observation means simply "no
        # voice_note this time" — observation never blocks, delays failure
        # into, or changes the status of the transcript itself.
        if not observer_enabled() or not gemini_key_configured(paths):
            return None
        try:
            audio_bytes = source_path.read_bytes()
        except OSError:
            return None
        # Keyed by the uploaded bytes, not the remuxed ones: the widget's
        # outbox retries re-send the identical blob, and that retry must hit
        # this cache instead of spending a second Gemini call.
        sha256 = hashlib.sha256(audio_bytes).hexdigest()
        cached = database.get_voice_observation(sha256)
        if cached is not None:
            return str(cached["voice_note"])
        # Gemini's audio formats do not include WebM, and MediaRecorder emits
        # WebM or MP4, so the clip takes the same ffmpeg remux the upload path
        # uses. Remux precedes the daily-limit claim: a local conversion
        # failure never consumed an upstream request, so it must not count.
        mp3_path = source_path.with_suffix(".observer.mp3")
        try:
            await run_in_threadpool(
                to_mp3, source_path, mp3_path, max_seconds=MAX_OBSERVER_AUDIO_SECONDS
            )
            mp3_bytes = mp3_path.read_bytes()
        except (VoiceMediaError, OSError) as exc:
            _LOGGER.info("voice observer remux failed: %s", str(exc)[:120])
            return None
        finally:
            try:
                mp3_path.unlink(missing_ok=True)
            except OSError:
                pass
        if not database.claim_gemini_daily_attempt(instance_settings.gemini_daily_limit):
            return None
        outcome = await run_in_threadpool(observe_voice, mp3_bytes, paths=paths)
        if outcome.get("error"):
            _LOGGER.info("voice observer skipped: %s", outcome["error"])
            return None
        database.record_voice_observation(
            sha256,
            observed=outcome["observed"],
            voice_note=outcome["voice_note"],
            model=observer_model(),
        )
        return str(outcome["voice_note"])

    async def voice_transcribe(request: Request) -> Response:
        # Called by the injected widget after it publishes the voice status. The
        # transcript is then edited into that same status. The
        # bearer here is the caller's OWN Mastodon web session token, verified
        # against the instance and then dropped (never stored, never logged).
        # A same-machine caller (e.g. cyberboss on 127.0.0.1) may skip that
        # bearer entirely when CMX_LOCAL_TRUSTED_MEDIA is on; see
        # _local_trusted_media_enabled.
        if _is_loopback_host(
            request.headers.get("host", ""), settings.port
        ) and _local_trusted_media_enabled():
            verified = True
        else:
            bearer = _BEARER_RE.fullmatch(request.headers.get("authorization", "").strip())
            verified = bool(bearer) and await run_in_threadpool(
                _verify_mastodon_bearer, instance_settings.public_base_url, bearer.group(1)
            )
        if not verified:
            return JSONResponse(
                {"error": "unauthorized"}, status_code=401, headers={"Cache-Control": "no-store"}
            )

        form = await request.form()
        upload = form.get("file")
        if upload is None or not hasattr(upload, "filename"):
            return JSONResponse(
                {"error": "multipart field 'file' is required"},
                status_code=400,
                headers={"Cache-Control": "no-store"},
            )
        # Opt-in second opinion. Absent or "local" keeps the historical
        # local-only behaviour, so every existing caller is unaffected.
        engine = str(form.get("engine") or "local").strip().lower()
        if engine not in {"local", "cloud"}:
            return JSONResponse(
                {"error": "unknown_engine", "supported": ["local", "cloud"]},
                status_code=400,
                headers={"Cache-Control": "no-store"},
            )
        stream = upload.file
        stream.seek(0, 2)
        size = stream.tell()
        stream.seek(0)
        try:
            config = WorkerConfig.load()
        except RuntimeError:
            return JSONResponse(
                {"error": "transcriber_unavailable"},
                status_code=503,
                headers={"Cache-Control": "no-store"},
            )
        if size < 1:
            return JSONResponse(
                {"error": "empty_file"}, status_code=400, headers={"Cache-Control": "no-store"}
            )
        if size > config.max_audio_bytes:
            return JSONResponse(
                {"error": "file_too_large", "max_bytes": config.max_audio_bytes},
                status_code=413,
                headers={"Cache-Control": "no-store"},
            )
        if not model_dir_ready(config.model_dir) and not (
            engine == "cloud" and cloud_asr_configured()
        ):
            # A cloud request on a host with credentials does not need the local
            # weights at all; only the local chain does.
            # No usable local model on this host: the widget degrades to a plain
            # voice status and the worker's reply stays as the fallback. This
            # checks for the weights, not just the folder — CMX_WHISPER_MODEL_DIR
            # once pointed at an unrelated directory that existed, which turned a
            # misconfiguration into a 502 that looked like a flaky transcriber.
            return JSONResponse(
                {"error": "transcriber_unavailable"},
                status_code=503,
                headers={"Cache-Control": "no-store"},
            )

        temp_dir = paths.runtime / "voice-tmp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        temp_path = temp_dir / f"{secrets.token_urlsafe(12)}{_audio_suffix(upload.filename)}"
        try:
            with open(temp_path, "wb") as out:
                shutil.copyfileobj(stream, out)
            # Whisper is CPU-bound and synchronous: keep it off the event loop
            # or every other MCP request stalls for the length of the audio.
            result = await run_in_threadpool(
                transcribe_file,
                temp_path,
                model_dir=config.model_dir,
                device=config.device,
                compute_type=config.compute_type,
                language=config.language,
                initial_prompt=config.initial_prompt,
                hotwords=config.hotwords,
                beam_size=config.beam_size,
                max_audio_seconds=float(config.max_audio_seconds),
                engine=engine,
            )
            _LOGGER.info(
                "/files/transcribe requested=%s served=%s",
                engine,
                result.get("engine", "unknown"),
            )
            voice_note: str | None = None
            if not result.get("error"):
                # While the temp file is still on disk: the observer reads the
                # same clip the transcriber just did, and never re-persists it.
                # Independent of the ASR engine above — local or cloud, the
                # paralinguistic pass sees the same bytes either way.
                voice_note = await _observe_voice_clip(temp_path)
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        if result.get("error"):
            if result["error"] == "no_speech":
                return JSONResponse(
                    {"error": "no_speech", "text": ""},
                    headers={"Cache-Control": "no-store"},
                )
            return JSONResponse(
                {"error": str(result["error"])},
                status_code=502,
                headers={"Cache-Control": "no-store"},
            )
        # `engine` is the engine that actually produced this text, not the one
        # asked for. Without it a caller cannot tell a good local transcript
        # from a silent degradation to the small Whisper fallback, which is the
        # single most common cause of "the transcription got worse".
        payload: dict[str, Any] = {
            "text": str(result.get("text") or "").strip(),
            "engine": str(result.get("engine") or "unknown"),
        }
        duration = result.get("duration")
        if isinstance(duration, (int, float)) and duration > 0:
            payload["duration"] = round(float(duration), 2)
        for key in ("detected_language", "emotion", "cloud_error"):
            value = str(result.get(key) or "")
            if value:
                payload[key] = value
        # The observer's one-line note rides alongside the transcript; absent
        # when the pass was disabled, unconfigured, or found nothing.
        if voice_note:
            payload["voice_note"] = voice_note
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})

    async def image_recognize(request: Request) -> Response:
        # Same rule as voice_transcribe: the caller's own Mastodon web session
        # bearer, verified against the instance and then dropped (never
        # stored, never logged). The caller already holds the image bytes —
        # they uploaded them — so there is no separate "may this caller see
        # this image" check to add, and the server never fetches anything
        # from Mastodon on this path.
        # A same-machine caller (e.g. cyberboss on 127.0.0.1) may skip that
        # bearer entirely when CMX_LOCAL_TRUSTED_MEDIA is on; see
        # _local_trusted_media_enabled.
        if _is_loopback_host(
            request.headers.get("host", ""), settings.port
        ) and _local_trusted_media_enabled():
            verified = True
        else:
            bearer = _BEARER_RE.fullmatch(request.headers.get("authorization", "").strip())
            verified = bool(bearer) and await run_in_threadpool(
                _verify_mastodon_bearer, instance_settings.public_base_url, bearer.group(1)
            )
        if not verified:
            return JSONResponse(
                {"error": "unauthorized"}, status_code=401, headers={"Cache-Control": "no-store"}
            )

        form = await request.form()
        upload = form.get("file")
        if upload is None or not hasattr(upload, "filename"):
            return JSONResponse(
                {"error": "multipart field 'file' is required"},
                status_code=400,
                headers={"Cache-Control": "no-store"},
            )
        status_id = str(form.get("status_id") or "").strip()
        media_id = str(form.get("media_id") or "").strip()

        stream = upload.file
        stream.seek(0, 2)
        size = stream.tell()
        stream.seek(0)
        if size < 1:
            return JSONResponse(
                {"error": "empty_file"}, status_code=400, headers={"Cache-Control": "no-store"}
            )
        if size > MAX_IMAGE_BYTES:
            return JSONResponse(
                {"error": "file_too_large", "max_bytes": MAX_IMAGE_BYTES},
                status_code=413,
                headers={"Cache-Control": "no-store"},
            )
        image_bytes = stream.read()
        sha256 = hashlib.sha256(image_bytes).hexdigest()

        # Content-hash cache: the owner's requirement is that the same image
        # is recognised once and the result is shared by every resident, so a
        # hit here skips both the local model and Gemini entirely.
        existing = database.get_image_recognition(sha256)
        if existing is not None:
            alt_error: str | None = None
            if status_id and media_id:
                database.link_status_media(status_id, media_id, sha256)
                alt_error = await run_in_threadpool(
                    _write_recognition_alt,
                    instance_settings.public_base_url,
                    bearer.group(1),
                    status_id,
                    media_id,
                    existing,
                )
            response_body = _recognition_response(existing, cache_hit=True, cloud_error=None)
            if alt_error:
                response_body["alt_error"] = alt_error
            return JSONResponse(
                response_body, headers={"Cache-Control": "no-store"}
            )

        model_dir = resolve_ocr_model_dir()
        tier = resolve_ocr_tier()
        if not ocr_model_dir_ready(model_dir, tier):
            return JSONResponse(
                {"error": "recognizer_unavailable"},
                status_code=503,
                headers={"Cache-Control": "no-store"},
            )

        temp_dir = paths.runtime / "recognize-tmp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        temp_path = temp_dir / f"{secrets.token_urlsafe(12)}{_image_suffix(upload.filename)}"
        try:
            temp_path.write_bytes(image_bytes)
            # RapidOCR is CPU-bound and synchronous: keep it off the event
            # loop for the same reason voice_transcribe threads Whisper.
            local_result = await run_in_threadpool(
                ocr_image, temp_path, model_dir=model_dir, tier=tier
            )
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass

        if local_result.get("error"):
            return JSONResponse(
                {"error": "ocr_failed", "detail": str(local_result["error"])},
                status_code=502,
                headers={"Cache-Control": "no-store"},
            )

        local_text = str(local_result.get("text") or "")
        database.record_local_ocr(
            sha256,
            text=local_text,
            line_count=int(local_result.get("line_count") or 0),
            mean_confidence=local_result.get("mean_confidence"),
        )

        cloud_error: str | None = None
        if gemini_key_configured(paths):
            claimed = database.claim_gemini_daily_attempt(
                instance_settings.gemini_daily_limit
            )
            if not claimed:
                cloud_error = "daily_limit_reached"
            else:
                cloud_result = await run_in_threadpool(
                    recognize_image,
                    image_bytes,
                    local_ocr_text=local_text,
                    paths=paths,
                    mime_type=str(upload.content_type or "image/jpeg"),
                )
                if cloud_result.get("error"):
                    # Recognition must never block or fail a post: the row stays
                    # pending and the caller learns why via cloud_error in the
                    # 200 body, never via an HTTP error status.
                    cloud_error = str(cloud_result["error"])
                else:
                    database.record_cloud_recognition(
                        sha256,
                        corrected_text=cloud_result.get("corrected_text"),
                        description=cloud_result.get("description"),
                        keywords=cloud_result.get("keywords"),
                        uncertain_text=cloud_result.get("uncertain_text"),
                    )

        if status_id and media_id:
            database.link_status_media(status_id, media_id, sha256)

        row = database.get_image_recognition(sha256)
        alt_error: str | None = None
        if status_id and media_id:
            alt_error = await run_in_threadpool(
                _write_recognition_alt,
                instance_settings.public_base_url,
                bearer.group(1),
                status_id,
                media_id,
                row,
            )
        response_body = _recognition_response(row, cache_hit=False, cloud_error=cloud_error)
        if alt_error:
            response_body["alt_error"] = alt_error
        return JSONResponse(
            response_body,
            headers={"Cache-Control": "no-store"},
        )

    async def image_ask(request: Request) -> Response:
        # Same trust rule as image_recognize above: loopback caller with
        # CMX_LOCAL_TRUSTED_MEDIA on, or a verified Mastodon web session
        # bearer that is checked and then dropped.
        if _is_loopback_host(
            request.headers.get("host", ""), settings.port
        ) and _local_trusted_media_enabled():
            verified = True
        else:
            bearer = _BEARER_RE.fullmatch(request.headers.get("authorization", "").strip())
            verified = bool(bearer) and await run_in_threadpool(
                _verify_mastodon_bearer, instance_settings.public_base_url, bearer.group(1)
            )
        if not verified:
            return JSONResponse(
                {"error": "unauthorized"}, status_code=401, headers={"Cache-Control": "no-store"}
            )

        form = await request.form()
        upload = form.get("file")
        if upload is None or not hasattr(upload, "filename"):
            return JSONResponse(
                {"error": "multipart field 'file' is required"},
                status_code=400,
                headers={"Cache-Control": "no-store"},
            )
        question = str(form.get("question") or "").strip()
        if not question:
            return JSONResponse(
                {"error": "multipart field 'question' is required"},
                status_code=400,
                headers={"Cache-Control": "no-store"},
            )
        if len(question) > MAX_ASK_QUESTION_CHARS:
            return JSONResponse(
                {"error": "question_too_long", "max_chars": MAX_ASK_QUESTION_CHARS},
                status_code=400,
                headers={"Cache-Control": "no-store"},
            )

        stream = upload.file
        stream.seek(0, 2)
        size = stream.tell()
        stream.seek(0)
        if size < 1:
            return JSONResponse(
                {"error": "empty_file"}, status_code=400, headers={"Cache-Control": "no-store"}
            )
        if size > MAX_IMAGE_BYTES:
            return JSONResponse(
                {"error": "file_too_large", "max_bytes": MAX_IMAGE_BYTES},
                status_code=413,
                headers={"Cache-Control": "no-store"},
            )
        image_bytes = stream.read()
        sha256 = hashlib.sha256(image_bytes).hexdigest()

        if not gemini_key_configured(paths):
            return JSONResponse(
                {"error": "not_configured"}, status_code=503, headers={"Cache-Control": "no-store"}
            )
        # Q&A spends from the same daily Gemini pool as recognition; a burst
        # of questions must not starve the recognition path silently, so the
        # refusal is explicit here rather than a pending row.
        if not database.claim_gemini_daily_attempt(instance_settings.gemini_daily_limit):
            return JSONResponse(
                {"error": "daily_limit_reached"},
                status_code=429,
                headers={"Cache-Control": "no-store"},
            )

        # A bare multipart part arrives as application/octet-stream, which
        # Gemini rejects outright; fall back to the filename suffix then jpeg.
        mime_type = str(upload.content_type or "").strip().lower()
        if not mime_type.startswith("image/"):
            suffix = Path(str(upload.filename or "")).suffix.lower()
            mime_type = {
                ".png": "image/png",
                ".gif": "image/gif",
                ".webp": "image/webp",
            }.get(suffix, "image/jpeg")

        result = await run_in_threadpool(
            ask_image,
            image_bytes,
            question=question,
            paths=paths,
            mime_type=mime_type,
        )
        _append_vision_qa_log(paths, sha256=sha256, question=question, result=result)
        if result.get("error"):
            return JSONResponse(
                {"error": str(result["error"]), "detail": str(result.get("detail") or "")},
                status_code=502,
                headers={"Cache-Control": "no-store"},
            )
        return JSONResponse(
            {"answer": str(result.get("answer") or ""), "sha256": sha256},
            headers={"Cache-Control": "no-store"},
        )

    async def site_search_endpoint(request: Request) -> Response:
        # This search is deliberately driven by the browser's own Mastodon token:
        # REST decides the caller's current visibility, the local SQLite cache
        # indexes only those returned statuses, and every hit is revalidated below.
        bearer = _BEARER_RE.fullmatch(request.headers.get("authorization", "").strip())
        identity = await run_in_threadpool(
            verify_web_identity, instance_settings.public_base_url, bearer.group(1)
        ) if bearer else None
        if identity is None:
            return JSONResponse(
                {"error": "unauthorized"}, status_code=401, headers={"Cache-Control": "no-store"}
            )
        query = str(request.query_params.get("q") or "").strip()
        if not query:
            return JSONResponse(
                {"error": "query 'q' is required"},
                status_code=400,
                headers={"Cache-Control": "no-store"},
            )
        try:
            limit = int(request.query_params.get("limit") or 30)
        except ValueError:
            limit = 30
        limit = max(1, min(limit, 100))
        cache_key = f"web:{identity.account_id}"
        client = MastodonClient(
            base_url=instance_settings.base_url,
            host_header=instance_settings.host_header,
            token=bearer.group(1),
            timeout=instance_settings.timeout_seconds,
        )
        try:
            timings: dict[str, float] = {}
            self_author_id = await run_in_threadpool(
                refresh_search_cache, database, cache_key, client, timings=timings
            )
            started = time.perf_counter()
            candidates = await run_in_threadpool(
                database.search_statuses,
                cache_key,
                query,
                limit,
                self_author_id=self_author_id,
            )
            timings["sqlite_search_ms"] = (time.perf_counter() - started) * 1000
            hits: list[dict[str, Any]] = []
            mastodon_statuses: list[dict[str, Any]] = []
            started = time.perf_counter()
            for cached in candidates:
                status_id = str(cached.get("id") or "")
                if not status_id:
                    continue
                try:
                    raw = await run_in_threadpool(client.get_status, status_id)
                except MastodonApiError as exc:
                    if _visibility_failure(exc):
                        await run_in_threadpool(database.invalidate_status, cache_key, status_id)
                        continue
                    raise
                compact = compact_status(raw)
                await run_in_threadpool(database.cache_statuses, cache_key, [compact])
                mastodon_statuses.append(raw)
                hits.append({
                    "id": compact["id"],
                    "at": compact.get("created_at"),
                    "author": (compact.get("author") or {}).get("acct") or "",
                    "visibility": compact.get("visibility"),
                    "text": compact.get("text") or "",
                })
            timings["revalidate_ms"] = (time.perf_counter() - started) * 1000
            _LOGGER.info(
                "web_search timing refresh_home_ms=%.1f refresh_own_ms=%.1f "
                "sqlite_search_ms=%.1f revalidate_ms=%.1f",
                timings.get("refresh_home_ms", 0.0),
                timings.get("refresh_own_ms", 0.0),
                timings["sqlite_search_ms"],
                timings["revalidate_ms"],
            )
        except (MastodonApiError, RuntimeError) as exc:
            return JSONResponse(
                {"error": "search_unavailable", "detail": str(exc)[:200]},
                status_code=503,
                headers={"Cache-Control": "no-store"},
            )
        finally:
            client.close()
        if request.query_params.get("format") == "mastodon":
            # Mastodon's current web client uses Axios/XHR rather than
            # window.fetch. Nginx sends only its v2 status-search request here;
            # return the native result shape so the existing UI renders the
            # same revalidated, token-visible statuses.
            return JSONResponse(
                {"accounts": [], "hashtags": [], "collections": [], "statuses": mastodon_statuses},
                headers={"Cache-Control": "no-store"},
            )
        return JSONResponse(
            {"query": query, "count": len(hits), "items": hits},
            headers={"Cache-Control": "no-store"},
        )

    async def voice_font(request: Request) -> Response:
        # Kai ships with Windows and macOS but not with iOS or Android, so the
        # transcript would silently fall back to a serif on exactly the devices
        # the recordings are made on. The file name carries the version: it is
        # immutable, so a new subset means a new name, never an overwrite.
        name = str(request.path_params.get("name") or "")
        if not _FONT_NAME_RE.fullmatch(name):
            return JSONResponse({"error": "not_found"}, status_code=404)
        path = ASSETS_DIR / "fonts" / name
        if not path.is_file():
            return JSONResponse({"error": "not_found"}, status_code=404)
        return FileResponse(
            path,
            media_type="font/woff2",
            headers={
                "Cache-Control": "public, max-age=31536000, immutable",
                "X-Content-Type-Options": "nosniff",
            },
        )

    async def voice_remux(request: Request) -> Response:
        # MediaRecorder can only emit WebM or MP4, which Mastodon reads as video,
        # and Ogg — the obvious fix — will not play on iOS. MP3 is the one format
        # both ends accept; see voice_media. Same credential rule as transcribe:
        # the caller's own page token, verified then dropped.
        bearer = _BEARER_RE.fullmatch(request.headers.get("authorization", "").strip())
        verified = bool(bearer) and await run_in_threadpool(
            _verify_mastodon_bearer, instance_settings.public_base_url, bearer.group(1)
        )
        if not verified:
            return JSONResponse(
                {"error": "unauthorized"}, status_code=401, headers={"Cache-Control": "no-store"}
            )

        form = await request.form()
        upload = form.get("file")
        if upload is None or not hasattr(upload, "filename"):
            return JSONResponse(
                {"error": "multipart field 'file' is required"},
                status_code=400, headers={"Cache-Control": "no-store"},
            )
        stream = upload.file
        stream.seek(0, 2)
        size = stream.tell()
        stream.seek(0)
        if size < 1:
            return JSONResponse(
                {"error": "empty_file"}, status_code=400, headers={"Cache-Control": "no-store"}
            )
        try:
            max_bytes = WorkerConfig.load().max_audio_bytes
        except RuntimeError:
            max_bytes = 32 * 1024 * 1024
        if size > max_bytes:
            return JSONResponse(
                {"error": "file_too_large", "max_bytes": max_bytes},
                status_code=413, headers={"Cache-Control": "no-store"},
            )

        temp_dir = paths.runtime / "voice-tmp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        stem = secrets.token_urlsafe(12)
        source = temp_dir / f"{stem}{_audio_suffix(upload.filename)}"
        target = temp_dir / f"{stem}{MP3_SUFFIX}"
        try:
            with open(source, "wb") as out:
                shutil.copyfileobj(stream, out)
            await run_in_threadpool(to_mp3, source, target)
            payload = target.read_bytes()
        except VoiceMediaError as exc:
            return JSONResponse(
                {"error": "convert_failed", "detail": str(exc)[:120]},
                status_code=422, headers={"Cache-Control": "no-store"},
            )
        finally:
            for path in (source, target):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
        return Response(
            payload,
            media_type=MP3_MIME,
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    async def health(_request: Request) -> Response:
        social_enabled = any(
            bot.enabled and bot.remote_profile in {"social", "social_plus"}
            for bot in database.list_bots()
        )
        return JSONResponse(
            {
                "ok": True,
                "transport": "streamable-http",
                "mode": "profiled",
                "social_enabled": social_enabled,
            },
            headers={"Cache-Control": "no-store"},
        )

    oauth_routes = create_auth_routes(
        provider=provider,
        issuer_url=AnyHttpUrl(settings.oauth_issuer),
        client_registration_options=ClientRegistrationOptions(
            enabled=True,
            # None means client secrets never expire. The SDK reads an integer
            # literally: 0 would stamp every confidential client (ChatGPT
            # registers as one) with a secret that expires at issuance, killing
            # its /token exchange with "Client secret has expired".
            client_secret_expiry_seconds=None,
            valid_scopes=[READ_SCOPE, SOCIAL_SCOPE],
            # ChatGPT registers without asking for a scope and then replays
            # whatever scope our registration response handed back on every
            # /authorize. Defaulting to read-only therefore pinned its
            # connector to a read-only token forever, even when the owner
            # redeemed a cmx:social invite. Advertise both; the real grant is
            # still capped by the invite, the resident's remote_profile and
            # the resident's own Mastodon token.
            default_scopes=[READ_SCOPE, SOCIAL_SCOPE],
        ),
        revocation_options=RevocationOptions(enabled=True),
    )
    for route in oauth_routes:
        if route.path == "/.well-known/oauth-authorization-server":
            route.app = _NoStoreResponse(route.app)
            break

    # Clipboard is a same-origin browser feature, not an MCP surface: it
    # authenticates with the caller's own Mastodon web session and keeps its
    # own SQLite file and object directory.
    clipboard_routes, clipboard = build_clipboard_routes(
        runtime=paths.runtime,
        base_url=instance_settings.public_base_url,
        allowed_origins={settings.public_origin, *_loopback_origins(settings.port)},
    )

    routes = [
        *oauth_routes,
        *clipboard_routes,
        Route(
            "/.well-known/oauth-protected-resource/mcp/{bot_id}",
            protected_resource,
            methods=["GET", "OPTIONS"],
        ),
        Route("/oauth/approve", approve, methods=["GET", "POST"]),
        Route("/oauth/invite", invite, methods=["GET", "POST"]),
        Route("/files/upload", filebox_upload, methods=["POST"]),
        Route("/files/up", filebox_owner, methods=["GET", "POST"]),
        # Must stay ahead of the templated download route, which would otherwise
        # never match "voice.js" but would shadow future single-segment files.
        Route("/files/voice.js", voice_widget, methods=["GET"]),
        Route("/files/search.js", search_widget, methods=["GET"]),
        Route("/files/transcribe", voice_transcribe, methods=["POST"]),
        Route("/files/recognize", image_recognize, methods=["POST"]),
        Route("/files/ask", image_ask, methods=["POST"]),
        Route("/files/search", site_search_endpoint, methods=["GET"]),
        Route("/files/voice-remux", voice_remux, methods=["POST"]),
        Route("/files/fonts/{name}", voice_font, methods=["GET"]),
        Route("/files/{bot_id}/{file_id}/{name}", filebox_download, methods=["GET"]),
        Route("/_cmx/mcp-health", health, methods=["GET"]),
        *mcp_routes,
    ]

    async def _clipboard_sweep() -> None:
        while True:
            try:
                await run_in_threadpool(clipboard.sweep)
            except Exception:
                # A failed sweep must never kill the loop: the next tick retries,
                # and expired rows stay invisible to readers meanwhile.
                pass
            await asyncio.sleep(CLIPBOARD_SWEEP_SECONDS)

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        async with AsyncExitStack() as stack:
            for server in servers:
                await stack.enter_async_context(server.session_manager.run())
            sweeper = asyncio.create_task(_clipboard_sweep())
            try:
                yield
            finally:
                sweeper.cancel()
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
        # The filebox and Clipboard enforce their own (much larger) size and
        # quota limits, so the blanket 1 MiB cap must not apply to them.
        path = str(scope.get("path", ""))
        exempt = path.startswith("/files") or path.startswith("/clipboard-api")
        if scope.get("type") == "http" and not exempt:
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


def _loopback_origins(port: int) -> set[str]:
    """Origins matching every host form _is_loopback_host accepts for GET."""
    return {
        f"http://127.0.0.1:{port}",
        f"http://localhost:{port}",
        f"http://[::1]:{port}",
    }


def _local_trusted_media_enabled() -> bool:
    """Explicit opt-in for same-machine callers to skip the page bearer on
    recognize/transcribe. Off by default; when on it still only applies to
    requests whose Host header is loopback, the same trust boundary
    RemoteBoundaryMiddleware and /oauth/approve already rely on.
    """
    return os.getenv("CMX_LOCAL_TRUSTED_MEDIA", "").strip() == "1"


def _audio_suffix(filename: str | None) -> str:
    suffix = Path(_safe_filename(str(filename or ""))).suffix
    return suffix if _UPLOAD_SUFFIX_RE.fullmatch(suffix) else ".audio"


def _image_suffix(filename: str | None) -> str:
    suffix = Path(_safe_filename(str(filename or ""))).suffix
    return suffix if _UPLOAD_SUFFIX_RE.fullmatch(suffix) else ".img"


def _recognition_response(
    row: dict[str, Any], *, cache_hit: bool, cloud_error: str | None
) -> dict[str, Any]:
    """Shape one `image_recognition` row into the /files/recognize JSON body.

    `local` is always present (local OCR always runs, cache hit or not);
    `cloud` only appears once the state is 'done', and `cloud_error` only
    when this call's own Gemini attempt failed -- it is never reconstructed
    from history, so a cache hit never reports an error nobody just saw.
    """
    response: dict[str, Any] = {
        "sha256": row["image_sha256"],
        "cache_hit": cache_hit,
        "state": row["state"],
        "local": {
            "text": row["local_ocr_text"],
            "line_count": row["local_line_count"],
            "mean_confidence": row["local_mean_confidence"],
        },
    }
    if row["state"] == "done":
        response["cloud"] = {
            "description": row["cloud_description"],
            "keywords": row["search_keywords"],
            "corrected_text": row["cloud_corrected_text"],
            "uncertain_text": row["uncertain_text"],
        }
    if cloud_error:
        response["cloud_error"] = cloud_error
    return response


def _recognition_alt_text(row: dict[str, Any], original: str = "") -> str:
    parts: list[str] = []
    description = str(row.get("cloud_description") or "").strip()
    corrected = str(row.get("cloud_corrected_text") or "").strip()
    keywords = str(row.get("search_keywords") or "").strip()
    local_text = str(row.get("local_ocr_text") or "").strip()
    if description:
        parts.append(description)
    if corrected:
        parts.append(f"文字：{corrected}")
    elif local_text:
        parts.append(f"OCR：{local_text}")
    if keywords:
        parts.append(f"关键词：{keywords}")
    if not parts:
        return original.strip()
    marker = "AI识图："
    base = original.split(marker, 1)[0].strip()
    generated = marker + "；".join(parts)
    return (((base + "\n\n") if base else "") + generated)[:1500]


def _write_recognition_alt(
    base_url: str,
    token: str,
    status_id: str,
    media_id: str,
    row: dict[str, Any],
) -> str | None:
    """Update an attached image through the owning page session's status edit.

    Mastodon intentionally returns 404 from ``PUT /api/v1/media/:id`` once a
    media attachment belongs to a status. Its supported post-publication path
    is ``PUT /api/v1/statuses/:id`` with ``media_attributes``. Read the source
    first so adding generated alt text cannot blank or HTML-escape the post.
    """
    headers = {"Authorization": f"Bearer {token}"}
    try:
        with httpx.Client(base_url=base_url, headers=headers, timeout=VERIFY_TIMEOUT_SECONDS) as client:
            status_response = client.get(f"/api/v1/statuses/{quote(status_id, safe='')}")
            source_response = client.get(f"/api/v1/statuses/{quote(status_id, safe='')}/source")
            if status_response.status_code != 200 or source_response.status_code != 200:
                return f"status_read_{status_response.status_code}_{source_response.status_code}"
            status = status_response.json()
            source = source_response.json()
            attachments = status.get("media_attachments") or []
            target = next((item for item in attachments if str(item.get("id")) == media_id), None)
            if target is None:
                return "media_not_found"
            description = _recognition_alt_text(row, str(target.get("description") or ""))
            if not description:
                return None
            payload = {
                "status": str(source.get("text") or ""),
                "spoiler_text": str(source.get("spoiler_text") or ""),
                "media_ids": [str(item.get("id")) for item in attachments],
                "media_attributes": [{"id": media_id, "description": description}],
                "sensitive": bool(status.get("sensitive")),
                "language": status.get("language"),
            }
            update_response = client.put(
                f"/api/v1/statuses/{quote(status_id, safe='')}", json=payload
            )
            if update_response.status_code != 200:
                return f"status_update_{update_response.status_code}"
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        return f"status_update_unavailable:{type(exc).__name__}"
    return None


def _safe_filename(value: str) -> str:
    name = value.replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"[\x00-\x1f]", "", name).strip().lstrip(".")
    name = name[:120]
    return name or "file"


def hash_passphrase(passphrase: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt, 200_000)
    return f"pbkdf2${salt.hex()}${digest.hex()}"


def _verify_passphrase(passphrase: str, stored: str) -> bool:
    try:
        scheme, salt_hex, digest_hex = stored.split("$", 2)
        if scheme != "pbkdf2":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", passphrase.encode("utf-8"), bytes.fromhex(salt_hex), 200_000
        )
        import hmac as _hmac

        return _hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def _approval_error(message: str) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><meta charset='utf-8'><title>CMX MCP</title>"
        f"<main style='font-family:system-ui;padding:40px'><h1>无法授权</h1><p>{html.escape(message)}</p></main>",
        status_code=400,
        headers={"Cache-Control": "no-store"},
    )


def _consent_copy(scopes: list[str] | tuple[str, ...], bot: Any) -> tuple[str, str]:
    has_social = SOCIAL_SCOPE in scopes
    has_notifications = bot.remote_profile == "social_plus" and bot.remote_notifications
    has_polls = has_social and bool(getattr(bot, "remote_polls", False))
    has_boosts = has_social and bool(getattr(bot, "remote_boosts", False))
    title = "允许 CMX 社交 MCP 连接？" if has_social else "允许只读 CMX MCP 连接？"
    if not has_social:
        body = "该客户端可以读取该居民有权查看的 CMX 内容。不能执行社交写操作。"
    else:
        capabilities = ["发帖和回复", "安全纯文本编辑", "点赞和取消点赞", "收藏和取消收藏"]
        if has_polls:
            capabilities.append("创建投票和参与投票")
        if has_boosts:
            capabilities.append("转发和取消转发")
        body = "该客户端可以读取内容，并代表该居民：" + "、".join(capabilities) + "。"
    if has_notifications:
        body += "另可只读查看通知；不会执行清除、标记已读或其他通知写操作。"
    return title, body


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the profiled CMX remote MCP")
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
