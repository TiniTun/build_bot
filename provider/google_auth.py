"""Shared Google OAuth boundary for the email/calendar providers.

Loads OAuth client secrets and cached user tokens from workspace-relative
paths, refreshes tokens when possible, and builds authenticated Google API
service clients. Google client libraries are imported lazily so the rest of the
app (and tests that mock the clients) do not require them installed.

Token values and raw credential file contents are never returned or logged.
Failures surface as ``AuthMissingError`` / ``ProviderPermissionError`` so tool
factories map them to stable error codes instead of leaking secrets.
"""

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from provider.external_errors import AuthMissingError, ProviderPermissionError

if TYPE_CHECKING:
    from utils.config import Config, ExternalProviderConfig

logger = logging.getLogger(__name__)


def _resolve(config: "Config", path_str: str) -> Path:
    """Resolve a possibly workspace-relative path against the workspace root."""
    path = Path(path_str)
    if not path.is_absolute():
        path = config.workspace / path
    return path


def load_credentials(config: "Config", provider_cfg: "ExternalProviderConfig") -> Any:
    """Load and refresh Google OAuth credentials for one provider domain.

    Returns a ``google.oauth2.credentials.Credentials`` instance. Raises
    ``AuthMissingError`` when configuration or token/secret files are missing or
    unusable, and ``ProviderPermissionError`` when Google rejects the refresh.
    """
    if not provider_cfg.credentials_path or not provider_cfg.token_path:
        raise AuthMissingError("google credentials_path/token_path not configured")
    if not provider_cfg.scopes:
        raise AuthMissingError("google scopes not configured")

    try:
        from google.auth.exceptions import GoogleAuthError, RefreshError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError as e:
        raise AuthMissingError("google auth libraries are not installed") from e

    creds_path = _resolve(config, provider_cfg.credentials_path)
    token_path = _resolve(config, provider_cfg.token_path)
    if not creds_path.is_file():
        raise AuthMissingError("google client secret file not found")
    if not token_path.is_file():
        raise AuthMissingError("google token file not found; run OAuth flow first")

    try:
        creds = Credentials.from_authorized_user_file(
            str(token_path), provider_cfg.scopes
        )
    except (ValueError, OSError) as e:
        raise AuthMissingError("google token file is invalid") from e

    if creds.valid:
        return creds

    if not (creds.expired and creds.refresh_token):
        raise AuthMissingError("google token is invalid and cannot be refreshed")

    try:
        creds.refresh(Request())
    except RefreshError as e:
        raise AuthMissingError("google token refresh failed") from e
    except GoogleAuthError as e:
        raise ProviderPermissionError("google rejected the token refresh") from e

    try:
        token_path.write_text(creds.to_json())
    except OSError as e:
        logger.warning("failed to persist refreshed google token: %s", e)

    return creds


def build_service(
    config: "Config",
    provider_cfg: "ExternalProviderConfig",
    *,
    api: str,
    version: str,
) -> Any:
    """Build an authenticated ``googleapiclient`` service for ``api``/``version``.

    Credentials are loaded/refreshed first. ``ImportError`` for the Google client
    library maps to ``AuthMissingError``; other build failures propagate and are
    mapped to a generic ``provider_error`` by the tool layer (no secrets leak).
    """
    creds = load_credentials(config, provider_cfg)
    try:
        from googleapiclient.discovery import build
    except ImportError as e:
        raise AuthMissingError("google api client library is not installed") from e

    return build(api, version, credentials=creds, cache_discovery=False)
