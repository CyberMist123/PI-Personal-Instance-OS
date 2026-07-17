from __future__ import annotations

import argparse

from .extensions import register_extended_tools
from .server import Runtime, build_server


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one local CMX resident MCP over STDIO")
    parser.add_argument("--bot", required=True, help="Bot ID stored in local SQLite")
    args = parser.parse_args()

    runtime = Runtime(args.bot)
    try:
        server = build_server(runtime)
        register_extended_tools(server, runtime)
        server.run(transport="stdio")
    finally:
        runtime.client.close()


if __name__ == "__main__":
    main()
