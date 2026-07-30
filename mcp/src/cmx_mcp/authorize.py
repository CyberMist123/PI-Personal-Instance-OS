from __future__ import annotations

import argparse
import base64
import hashlib
import json
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from .compact import compact_account
from .config import InstanceSettings, Paths, validate_remote_profile
from .db import Database
from .mastodon_client import MastodonClient
from .secrets import write_secret

DEFAULT_SCOPES = (
    "read:accounts",
    "read:statuses",
    "read:notifications",
    "write:statuses",
    "write:favourites",
    "write:bookmarks",
    "write:media",
    "write:notifications",
    "write:accounts",
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Authorize a CMX resident in the browser and save its token with DPAPI"
    )
    parser.add_argument("--id", required=True)
    parser.add_argument("--display-name", required=True)
    parser.add_argument("--profile", choices=["reader", "resident", "personal"], default="resident")
    parser.add_argument("--media-root", required=True)
    parser.add_argument(
        "--default-audience",
        choices=["residents", "direct", "public_explicit"],
        default="residents",
    )
    parser.add_argument("--allow-public", action="store_true")
    parser.add_argument("--remote-profile", choices=["disabled", "reader", "social", "social_plus"], default="reader")
    parser.add_argument("--remote-polls", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--remote-boosts", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--remote-notifications", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()

    paths = Paths.discover()
    paths.ensure()
    settings = InstanceSettings.load(paths)
    bot_id = _bot_id(args.id)
    media_root = Path(args.media_root).expanduser().resolve()
    media_root.mkdir(parents=True, exist_ok=True)

    scopes = DEFAULT_SCOPES[:3] if args.profile == "reader" else DEFAULT_SCOPES
    token, approved_scopes = _authorize_in_browser(
        public_base_url=settings.public_base_url,
        bot_id=bot_id,
        display_name=args.display_name.strip(),
        timeout_seconds=max(30, min(args.timeout, 900)),
        scopes=scopes,
    )

    client = MastodonClient(
        base_url=settings.base_url,
        host_header=settings.host_header,
        token=token,
        timeout=settings.timeout_seconds,
    )
    try:
        account = compact_account(client.verify_credentials())
    finally:
        client.close()
    authorized_username = str(account.get("acct") or "").split("@", 1)[0].lower()
    if authorized_username != bot_id:
        raise RuntimeError(
            f"授权页上点「同意」的是 @{authorized_username}，不是 @{bot_id}。"
            "没有保存任何凭据。请重来一次，并用【无痕窗口】打开授权链接，"
            f"或先在授权页右上角登出，改用 @{bot_id} 登录。"
        )

    token_ref = f"{bot_id}.token.dpapi"
    write_secret(paths.secrets / token_ref, token)

    database = Database(paths.database)
    database.initialize()
    database.upsert_bot(
        bot_id=bot_id,
        display_name=args.display_name.strip(),
        profile=args.profile,
        media_root=media_root,
        token_ref=token_ref,
        default_audience=args.default_audience,
        allow_public=bool(args.allow_public),
        remote_profile=validate_remote_profile(args.remote_profile)[0],
        remote_polls=bool(args.remote_polls),
        remote_boosts=bool(args.remote_boosts),
        remote_notifications=bool(args.remote_notifications),
    )

    executable = paths.home / ".venv" / "Scripts" / "cmx-mcp.exe"
    config = {
        "mcpServers": {
            f"cmx-{bot_id}": {
                "command": str(executable),
                "args": ["--bot", bot_id],
                "env": {"CMX_MCP_HOME": str(paths.home)},
            }
        }
    }
    print(
        json.dumps(
            {
                "ok": True,
                "bot": bot_id,
                "account": account,
                "approved_scopes": approved_scopes,
                "token_storage": str(paths.secrets / token_ref),
                "mcp_config": config,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def _authorize_in_browser(
    *,
    public_base_url: str,
    bot_id: str,
    display_name: str,
    timeout_seconds: int,
    scopes: tuple[str, ...] = DEFAULT_SCOPES,
) -> tuple[str, list[str]]:
    result: dict[str, str] = {}
    ready = threading.Event()
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")

    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_factory(result, ready, state))
    callback_url = f"http://127.0.0.1:{server.server_port}/callback"
    scope_text = " ".join(scopes)

    with httpx.Client(timeout=30.0, follow_redirects=False) as http:
        app_response = http.post(
            f"{public_base_url}/api/v1/apps",
            data={
                "client_name": f"CMX MCP - {display_name or bot_id}",
                "redirect_uris": callback_url,
                "scopes": scope_text,
                "website": public_base_url,
            },
        )
        app_response.raise_for_status()
        app = app_response.json()
        client_id = str(app["client_id"])
        client_secret = str(app["client_secret"])

        query = urlencode(
            {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": callback_url,
                "scope": scope_text,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "force_login": "true",
            }
        )
        authorize_url = f"{public_base_url}/oauth/authorize?{query}"

        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        _announce(authorize_url, bot_id=bot_id)

        try:
            if not _wait_with_progress(ready, timeout_seconds):
                raise RuntimeError(
                    "浏览器授权超时。重新运行一次即可；这次的链接已经作废。"
                )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        if result.get("error"):
            raise RuntimeError(f"Authorization failed: {result['error']}")
        code = result.get("code", "")
        if not code:
            raise RuntimeError("Authorization callback did not contain a code")

        token_response = http.post(
            f"{public_base_url}/oauth/token",
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": callback_url,
                "code": code,
                "code_verifier": verifier,
            },
        )
        token_response.raise_for_status()
        token_data: dict[str, Any] = token_response.json()
        token = str(token_data.get("access_token", "")).strip()
        if not token:
            raise RuntimeError("Mastodon did not return an access token")
        approved = token_data.get("scope", scope_text)
        approved_scopes = approved.split() if isinstance(approved, str) else list(approved)
        return token, approved_scopes


def _announce(authorize_url: str, *, bot_id: str) -> None:
    """Show the link no matter what, and say which account has to click it.

    webbrowser.open returns True on Windows even when nothing visibly opened,
    so the old "only print the URL if opening failed" branch almost never
    fired and the owner was left staring at a silent prompt. The far more
    common failure is subtler: the browser already holds the owner's session,
    so the consent page appears logged in as the wrong account and the grant
    would be rejected afterwards for a name mismatch.
    """
    print()
    print(f"这一步要授权的账号是：@{bot_id}")
    print("请确认授权页顶部显示的就是这个账号。如果显示的是 @owner 或别的账号：")
    print("  · 用【无痕 / 隐私窗口】打开下面的链接（最省事），或")
    print("  · 点授权页右上角的登出图标，改用这个 AI 的账号登录。")
    print()
    print("这条链接必须在【运行本脚本的这台电脑上】打开：授权后浏览器要跳回")
    print("http://127.0.0.1 上的一个临时端口，在手机或别的机器上点，回调回不来。")
    print()
    print("授权链接（可复制）：")
    print(authorize_url)
    if _copy_to_clipboard(authorize_url):
        print("（已复制到剪贴板）")
    print()
    try:
        opened = webbrowser.open(authorize_url)
    except Exception:  # noqa: BLE001 - any browser failure falls back to the link
        opened = False
    if opened:
        print("已尝试打开浏览器；没弹出来就手动粘贴上面的链接。")
    else:
        print("打不开浏览器，请手动粘贴上面的链接。")


def _copy_to_clipboard(text: str) -> bool:
    """Best effort only: clip.exe exists on Windows and nowhere else."""
    try:
        completed = subprocess.run(
            ["clip"],
            input=text.encode("utf-16-le"),
            shell=False,
            check=False,
            capture_output=True,
        )
        return completed.returncode == 0
    except (OSError, ValueError):
        return False


def _wait_with_progress(ready: threading.Event, timeout_seconds: int) -> bool:
    """Wait for the callback while showing that we are still waiting.

    A silent five-minute block is indistinguishable from a hang, which is
    exactly how it was read the first time it happened.
    """
    interactive = False
    try:
        interactive = bool(sys.stdout.isatty())
    except (AttributeError, ValueError):
        interactive = False
    deadline = time.monotonic() + timeout_seconds
    last_report = 0.0
    while True:
        if ready.wait(0.5):
            if interactive:
                print("\r" + " " * 78 + "\r", end="", flush=True)
            print("已收到授权回调，正在换取 Token...")
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if interactive:
                print("\r" + " " * 78 + "\r", end="", flush=True)
            return False
        if interactive:
            minutes, seconds = divmod(int(remaining), 60)
            print(
                f"\r等待浏览器里点「同意授权」... 还剩 {minutes}:{seconds:02d}  (Ctrl+C 可以中止)",
                end="",
                flush=True,
            )
        elif remaining and (last_report == 0.0 or last_report - remaining >= 30):
            last_report = remaining
            print(f"仍在等待浏览器授权，还剩 {int(remaining)} 秒...", flush=True)


def _handler_factory(result: dict[str, str], ready: threading.Event, expected_state: str):
    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
            parsed = urlparse(self.path)
            if parsed.path != "/callback":
                self.send_error(404)
                return
            params = parse_qs(parsed.query)
            returned_state = params.get("state", [""])[0]
            if returned_state != expected_state:
                result["error"] = "state mismatch"
                ok = False
            elif params.get("error"):
                result["error"] = params.get("error_description", params["error"])[0]
                ok = False
            else:
                result["code"] = params.get("code", [""])[0]
                ok = bool(result["code"])
                if not ok:
                    result["error"] = "missing authorization code"

            title = "CMX authorization complete" if ok else "CMX authorization failed"
            message = (
                "Authorization succeeded. You can close this page and return to PowerShell."
                if ok
                else "Authorization failed. Return to PowerShell for details."
            )
            body = (
                "<!doctype html><html><head><meta charset='utf-8'><title>"
                + title
                + "</title></head><body style='font-family:system-ui;padding:40px'>"
                + f"<h1>{title}</h1><p>{message}</p></body></html>"
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            ready.set()

        def log_message(self, _format: str, *_args: object) -> None:
            return

    return CallbackHandler


def _bot_id(value: str) -> str:
    cleaned = value.strip().lower()
    if not cleaned or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for ch in cleaned):
        raise SystemExit("Bot ID may only contain a-z, 0-9, hyphen, and underscore")
    return cleaned


if __name__ == "__main__":
    main()
