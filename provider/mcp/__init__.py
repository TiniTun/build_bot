"""MCP (Model Context Protocol) client layer.

``client`` owns the only import of the official MCP SDK; ``models`` and
``errors`` are the SDK-free vocabulary everything above uses. ``hub`` is the
process-level manager that owns connections and discovery snapshots.
"""

from provider.mcp.errors import (
    McpAuthError,
    McpConfigError,
    McpConnectionError,
    McpError,
    McpNotConnectedError,
    McpProtocolError,
    McpTimeoutError,
)
from provider.mcp.models import McpCallResult, McpServerInfo, McpToolDef

__all__ = [
    "McpAuthError",
    "McpCallResult",
    "McpConfigError",
    "McpConnectionError",
    "McpError",
    "McpNotConnectedError",
    "McpProtocolError",
    "McpServerInfo",
    "McpTimeoutError",
    "McpToolDef",
]
