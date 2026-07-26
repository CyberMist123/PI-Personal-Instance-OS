"""CMX MCP package."""

from importlib import metadata as _metadata

try:
    __version__ = _metadata.version("cmx-mcp")
except _metadata.PackageNotFoundError:  # running from a plain source tree
    __version__ = "0.3.0rc2"
