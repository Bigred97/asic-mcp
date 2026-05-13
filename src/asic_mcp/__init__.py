"""asic-mcp — MCP server for Australian Securities and Investments Commission statistics."""
from __future__ import annotations

try:
    from importlib.metadata import version as _v
    __version__ = _v("asic-mcp")
except Exception:
    __version__ = "0.0.0+unknown"
