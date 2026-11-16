"""Map Google API ``HttpError`` failures to stable provider exceptions.

Shared by the Gmail and Google Calendar providers. Google answers 403 both for
missing scopes and for rate/quota limits, so the error ``reason`` decides which
one it is. Every mapped failure is logged with its status and reason codes only
(never the request URL or response body) so a refusal can be diagnosed later.
"""

import logging

from provider.external_errors import (
    ProviderNotFoundError,
    ProviderPermissionError,
    ProviderRateLimitError,
)

logger = logging.getLogger(__name__)

_RATE_LIMIT_REASONS = frozenset(
    {
        "rateLimitExceeded",
        "userRateLimitExceeded",
        "quotaExceeded",
        "dailyLimitExceeded",
    }
)


def _error_reasons(exc: object) -> list[str]:
    """Google's machine-readable reason codes, e.g. ``rateLimitExceeded``."""
    details = getattr(exc, "error_details", None)
    if not isinstance(details, list):
        return []
    return [str(d["reason"]) for d in details if isinstance(d, dict) and d.get("reason")]


def map_google_http_error(exc: Exception, service: str) -> Exception:
    """Translate a Google ``HttpError`` into a provider exception.

    429, or 403 with a rate/quota reason -> rate limited; other 401/403 ->
    permission; 404 -> not found. Anything else is returned unchanged for the
    caller to map further or let the tool layer report as a provider error.
    """
    try:
        from googleapiclient.errors import HttpError
    except ImportError:  # pragma: no cover - libs absent means build_service raised
        return exc
    if not isinstance(exc, HttpError):
        return exc

    status = getattr(getattr(exc, "resp", None), "status", None)
    reasons = _error_reasons(exc)
    logger.warning(
        "google %s request failed: status=%s reasons=%s",
        service,
        status,
        ",".join(reasons) or "-",
    )
    if status == 429 or (status == 403 and _RATE_LIMIT_REASONS.intersection(reasons)):
        return ProviderRateLimitError(f"google {service} rate limit exceeded")
    if status in (401, 403):
        return ProviderPermissionError(f"google {service} denied the request")
    if status == 404:
        return ProviderNotFoundError(f"google {service} resource not found")
    return exc
