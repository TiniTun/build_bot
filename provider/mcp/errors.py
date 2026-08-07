"""Stable internal exceptions for the MCP client layer.

The rest of the application never sees MCP SDK or httpx exception types. Every
failure is mapped onto one of these so the hub and the capability bridge can
make policy decisions without importing the SDK.

Messages are redacted before they reach an exception: a Bearer token must never
appear in a message, ``repr``, log record, or traceback line we produce.
"""


class McpError(Exception):
    """Base class for all MCP client failures."""


class McpConfigError(McpError):
    """Configuration is unusable (missing URL reference, bad transport, ...)."""


class McpAuthError(McpError):
    """The Bearer token is missing, or the server rejected authentication."""


class McpConnectionError(McpError):
    """Transport-level failure: DNS, refused connection, health probe, stream loss."""


class McpTimeoutError(McpError):
    """A connect, discovery, or call deadline elapsed.

    A timeout is ambiguous: the server may or may not have applied the request.
    Callers must never automatically replay a mutation after this.
    """


class McpProtocolError(McpError):
    """The peer spoke MCP badly: malformed result, unexpected shape, bad schema."""


class McpNotConnectedError(McpError):
    """A request was made against a client that is not currently connected."""
