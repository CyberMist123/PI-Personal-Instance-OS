from __future__ import annotations

import argparse
import getpass
import json
from pathlib import Path

from .compact import compact_account
from .config import InstanceSettings, Paths
from .db import Database
from .mastodon_client import MastodonClient
from .secrets import read_secret, write_secret


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage the local CMX MCP")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init")

    add = sub.add_parser("add-bot")
    add.add_argument("--id", required=True)
    add.add_argument("--display-name", required=True)
    add.add_argument("--profile", choices=["reader", "resident", "personal"], default="resident")
    add.add_argument("--media-root", required=True)
    add.add_argument(
        "--default-audience",
        choices=["residents", "direct", "public_explicit"],
        default="residents",
    )
    add.add_argument("--allow-public", action="store_true")

    sub.add_parser("list-bots")

    test = sub.add_parser("test")
    test.add_argument("--bot", required=True)

    disable = sub.add_parser("disable")
    disable.add_argument("--bot", required=True)

    enable = sub.add_parser("enable")
    enable.add_argument("--bot", required=True)

    config = sub.add_parser("print-config")
    config.add_argument("--bot", required=True)

    args = parser.parse_args()
    paths = Paths.discover()
    paths.ensure()
    db = Database(paths.database)
    db.initialize()

    if args.command == "init":
        print(f"CMX MCP initialized: {paths.database}")
        return

    if args.command == "add-bot":
        bot_id = _bot_id(args.id)
        media_root = Path(args.media_root).expanduser().resolve()
        media_root.mkdir(parents=True, exist_ok=True)
        token = getpass.getpass("Paste this resident's Mastodon access token: ").strip()
        if not token:
            raise SystemExit("Token cannot be empty")
        token_ref = f"{bot_id}.token.dpapi"
        write_secret(paths.secrets / token_ref, token)
        db.upsert_bot(
            bot_id=bot_id,
            display_name=args.display_name.strip(),
            profile=args.profile,
            media_root=media_root,
            token_ref=token_ref,
            default_audience=args.default_audience,
            allow_public=bool(args.allow_public),
        )
        print(f"Saved bot '{bot_id}'. Token encrypted with Windows DPAPI.")
        return

    if args.command == "list-bots":
        rows = [
            {
                "id": bot.bot_id,
                "display_name": bot.display_name,
                "profile": bot.profile,
                "enabled": bot.enabled,
                "media_root": str(bot.media_root),
                "default_audience": bot.default_audience,
                "allow_public": bot.allow_public,
            }
            for bot in db.list_bots()
        ]
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return

    if args.command in {"enable", "disable"}:
        db.set_enabled(args.bot, args.command == "enable")
        print(f"{args.bot}: {args.command}d")
        return

    if args.command == "test":
        bot = db.get_bot(args.bot)
        if not bot.enabled:
            raise SystemExit(f"Bot '{args.bot}' is disabled")
        settings = InstanceSettings.load(paths)
        token = read_secret(paths.secrets / bot.token_ref)
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
        print(json.dumps({"ok": True, "bot": bot.bot_id, "account": account}, ensure_ascii=False, indent=2))
        return

    if args.command == "print-config":
        executable = paths.home / ".venv" / "Scripts" / "cmx-mcp.exe"
        payload = {
            "mcpServers": {
                f"cmx-{args.bot}": {
                    "command": str(executable),
                    "args": ["--bot", args.bot],
                    "env": {"CMX_MCP_HOME": str(paths.home)},
                }
            }
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    raise SystemExit("Unknown command")


def _bot_id(value: str) -> str:
    cleaned = value.strip().lower()
    if not cleaned or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for ch in cleaned):
        raise SystemExit("Bot ID may only contain a-z, 0-9, hyphen, and underscore")
    return cleaned


if __name__ == "__main__":
    main()
