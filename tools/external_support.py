"""Shared helpers for external (email/calendar) tool factories.

Maps provider exceptions to stable ``ToolResult`` errors without leaking secrets
or raw provider messages to the LLM.
"""

from provider.external_errors import (
    AuthMissingError,
    ProviderInvalidRequestError,
    ProviderNotFoundError,
    ProviderPermissionError,
)
from tools.base import ToolErrorCode, ToolResult


def provider_exception_to_result(exc: Exception) -> ToolResult:
    """Translate a provider exception into a safe, stable ToolResult error."""
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
    # Unknown failure: do not surface the raw message (may contain secrets).
    return ToolResult.error(
        ToolErrorCode.PROVIDER_ERROR,
        "The external provider failed to handle the request.",
        retryable=True,
    )
