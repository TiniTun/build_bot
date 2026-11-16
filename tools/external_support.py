"""Shared helpers for external (email/calendar/tasks) tool factories.

Maps provider exceptions to stable ``ToolResult`` errors without leaking secrets
or raw provider messages to the LLM. MCP errors reach here from MCP-backed
providers (the Notion task provider lets ``McpError`` propagate unchanged).
"""

from provider.external_errors import (
    AuthMissingError,
    ProviderInvalidRequestError,
    ProviderInvalidResponseError,
    ProviderNotFoundError,
    ProviderPermissionError,
    ProviderRateLimitError,
)
from provider.mcp.errors import McpAuthError, McpNotConnectedError, McpTimeoutError
from tools.base import ToolErrorCode, ToolResult


def provider_exception_to_result(exc: Exception) -> ToolResult:
    """Translate a provider exception into a safe, stable ToolResult error."""
    if isinstance(exc, McpTimeoutError):
        # Ambiguous: the server may have applied the change. Never retry blindly.
        return ToolResult.error(
            ToolErrorCode.PROVIDER_ERROR,
            "The request timed out and is ambiguous: it may have been applied; check before retrying.",
        )
    if isinstance(exc, McpAuthError):
        # Covers McpLoginRequiredError: only an operator login can fix it.
        return ToolResult.error(
            ToolErrorCode.AUTH_MISSING,
            "Notion is not authenticated.",
            user_action="Notion needs a re-login: on bakeryd run build-bot --workspace ./default_workspace mcp-login notion",
        )
    if isinstance(exc, McpNotConnectedError):
        return ToolResult.error(
            ToolErrorCode.PROVIDER_ERROR,
            "Notion is not connected right now; try again later.",
            retryable=True,
        )
    if isinstance(exc, AuthMissingError):
        return ToolResult.error(
            ToolErrorCode.AUTH_MISSING,
            "The external provider is not authenticated.",
            user_action="Enable and authenticate the provider in config before use.",
        )
    if isinstance(exc, ProviderPermissionError):
        return ToolResult.error(
            ToolErrorCode.PERMISSION_DENIED,
            "The provider denied this request due to insufficient permissions.",
            user_action="Grant the required scopes and try again.",
        )
    if isinstance(exc, ProviderRateLimitError):
        return ToolResult.error(
            ToolErrorCode.RATE_LIMITED,
            "The provider is temporarily rate limiting requests.",
            retryable=True,
        )
    if isinstance(exc, ProviderNotFoundError):
        return ToolResult.error(
            ToolErrorCode.NOT_FOUND,
            "The requested resource was not found.",
        )
    if isinstance(exc, ProviderInvalidRequestError):
        return ToolResult.error(
            ToolErrorCode.INVALID_ARGS,
            "The provider rejected the request as malformed.",
            user_action=(
                "Provide RFC3339 datetimes with a UTC offset (e.g. "
                "2026-06-16T10:00:00+10:00), or set a valid 'timezone' in config."
            ),
        )
    if isinstance(exc, ProviderInvalidResponseError):
        # Not retryable: the same call would return the same unexpected shape.
        return ToolResult.error(
            ToolErrorCode.PROVIDER_ERROR,
            "The external provider returned an unexpected response.",
        )
    # Unknown failure: do not surface the raw message (may contain secrets).
    return ToolResult.error(
        ToolErrorCode.PROVIDER_ERROR,
        "The external provider failed to handle the request.",
        retryable=True,
    )
