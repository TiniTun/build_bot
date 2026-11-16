"""Server-mode OAuth for a hosted MCP server: bearer header plus refresh.

This auth never registers a client and never starts an interactive flow; that is
the login command's job. It reads the grant from ``OAuthStore``, attaches the
access token, and refreshes it with one form POST to the stored token endpoint.

The provider rotates the refresh token on every refresh and revokes the whole
grant if a rotated-out refresh token is replayed after a short grace period, so
retries follow fixed rules:

- request never sent (connect error) or a 5xx/429 answer → transient, keep tokens;
- request possibly sent but no answer (timeout/reset) → retry the same POST once,
  then require login rather than risk a revoking replay;
- ``invalid_grant``/``invalid_client`` or any other rejection → require login.

Once login is required, the refresh token that failed is never sent again; only a
grant with a different refresh token (a new login) clears the state.

Token material never appears in an exception, ``last_error`` or a log record. The
token endpoint's response body is never logged or quoted either.
"""

import logging
import re
import time
from dataclasses import replace
from typing import AsyncGenerator, Callable

import httpx2

from provider.mcp.errors import McpConfigError, McpConnectionError
from provider.mcp.oauth_store import OAuthGrant, OAuthStore

logger = logging.getLogger(__name__)

_TERMINAL_ERRORS = frozenset({"invalid_grant", "invalid_client"})
_TOKEN_TIMEOUT = httpx2.Timeout(30.0)
# Used only when a successful token response omits ``expires_in``.
_DEFAULT_EXPIRES_IN = 3600.0
_SAFE_ERROR_CODE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
# The request never left this machine, so the token cannot have been rotated.
_NOT_SENT_ERRORS = (httpx2.ConnectError, httpx2.ConnectTimeout, httpx2.PoolTimeout)


class LoginRequired(Exception):
    """Internal signal: the grant is missing or dead and a new login is needed.

    It never carries token material.
    """


class _AmbiguousSend(Exception):
    """The refresh POST may have reached the server, but no answer came back."""


class McpOAuthAuth(httpx2.Auth):
    """``httpx2.Auth`` that attaches and refreshes an OAuth bearer token."""

    def __init__(
        self,
        store: OAuthStore,
        *,
        on_secret: Callable[[str], None],
        clock: Callable[[], float] = time.time,
        refresh_margin: float = 600.0,
        token_transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._store = store
        self._on_secret = on_secret
        self._clock = clock
        self._refresh_margin = refresh_margin
        self._token_transport = token_transport
        self._grant: OAuthGrant | None = None
        self._login_required = False
        # Refresh token of the grant that required login; never sent again.
        self._dead_refresh_token: str | None = None
        self.last_error: str | None = None

    @property
    def login_required(self) -> bool:
        return self._login_required

    async def async_auth_flow(self, request: httpx2.Request) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        grant = await self._usable_grant()
        request.headers["Authorization"] = f"Bearer {grant.access_token}"
        response = yield request
        if response.status_code != 401:
            return

        # A token we believed valid was rejected. Another process may have rotated
        # it; otherwise force one refresh. Either way, retry exactly once.
        logger.warning("MCP OAuth: server rejected a non-expired access token (HTTP 401)")
        retry = await self._recover_from_401(grant.access_token)
        request.headers["Authorization"] = f"Bearer {retry.access_token}"
        response = yield request
        if response.status_code == 401:
            self._require_login(retry, "server rejected the access token after a refresh (HTTP 401)")
            raise LoginRequired(self.last_error)

    # -- grant selection ---------------------------------------------------

    def _is_valid(self, grant: OAuthGrant) -> bool:
        return self._clock() < grant.expires_at - self._refresh_margin

    async def _usable_grant(self) -> OAuthGrant:
        if self._grant is None or self._login_required:
            self._adopt(self._load())
        assert self._grant is not None
        if self._is_valid(self._grant):
            return self._grant
        return await self._refresh(stale_access_token=None)

    async def _recover_from_401(self, rejected_access_token: str) -> OAuthGrant:
        reloaded = self._load()
        if reloaded.access_token != rejected_access_token and self._is_valid(reloaded):
            self._adopt(reloaded)
            return reloaded
        return await self._refresh(stale_access_token=rejected_access_token)

    async def _refresh(self, *, stale_access_token: str | None) -> OAuthGrant:
        """Refresh under the cross-process lock, unless another writer already did."""
        async with self._store.locked():
            grant = self._load()
            if self._is_valid(grant) and grant.access_token != stale_access_token:
                self._adopt(grant)
                return grant
            self._grant = grant
            return await self._refresh_locked(grant)

    def _load(self) -> OAuthGrant:
        """Load the stored grant; a missing, malformed or dead one requires login."""
        try:
            grant = self._store.load()
        except McpConfigError as exc:
            self._require_login(None, str(exc))
            raise LoginRequired(self.last_error) from None
        if grant is None:
            self._require_login(None, "no OAuth grant stored; run the login command")
            raise LoginRequired(self.last_error)
        if self._login_required and grant.refresh_token == self._dead_refresh_token:
            raise LoginRequired(self.last_error)
        return grant

    def _adopt(self, grant: OAuthGrant) -> None:
        self._on_secret(grant.access_token)
        self._on_secret(grant.refresh_token)
        self._grant = grant
        if self._login_required:
            logger.info("MCP OAuth: a new grant was found; login is no longer required")
        self._login_required = False
        self._dead_refresh_token = None
        self.last_error = None

    def _require_login(self, grant: OAuthGrant | None, reason: str) -> None:
        self._login_required = True
        self._dead_refresh_token = grant.refresh_token if grant is not None else None
        self.last_error = reason
        logger.warning("MCP OAuth: login required: %s", reason)

    # -- the refresh POST --------------------------------------------------

    async def _refresh_locked(self, grant: OAuthGrant) -> OAuthGrant:
        form = {
            "grant_type": "refresh_token",
            "refresh_token": grant.refresh_token,
            "client_id": grant.client_id,
            "resource": grant.resource,
        }
        if grant.client_secret is not None:
            form["client_secret"] = grant.client_secret

        async with httpx2.AsyncClient(
            transport=self._token_transport, follow_redirects=False, timeout=_TOKEN_TIMEOUT
        ) as client:
            try:
                response = await self._post(client, grant.token_endpoint, form)
            except _AmbiguousSend:
                logger.warning("MCP OAuth: token request got no answer; retrying once with the same refresh token")
                try:
                    response = await self._post(client, grant.token_endpoint, form)
                except (_AmbiguousSend, McpConnectionError):
                    self._require_login(grant, "token request got no answer twice")
                    raise LoginRequired(self.last_error) from None
                # The first POST may already have rotated the token: anything but
                # success now must not leave us holding a possibly-dead refresh token.
                return self._handle_response(grant, response, after_ambiguous=True)
        return self._handle_response(grant, response, after_ambiguous=False)

    async def _post(self, client: httpx2.AsyncClient, url: str, form: dict[str, str]) -> httpx2.Response:
        try:
            return await client.post(url, data=form)
        except _NOT_SENT_ERRORS as exc:
            self.last_error = f"token endpoint unreachable ({type(exc).__name__})"
            logger.warning("MCP OAuth: %s", self.last_error)
            raise McpConnectionError(f"OAuth refresh failed: {self.last_error}") from None
        except httpx2.TransportError as exc:
            logger.warning("MCP OAuth: token request failed after send (%s)", type(exc).__name__)
            raise _AmbiguousSend() from None

    def _handle_response(self, grant: OAuthGrant, response: httpx2.Response, *, after_ambiguous: bool) -> OAuthGrant:
        status = response.status_code
        if status == 200:
            return self._accept_tokens(grant, response)

        error_code = _error_code(response)
        detail = f"HTTP {status}" + (f" ({error_code})" if error_code else "")
        transient = status >= 500 or status == 429
        if transient and not after_ambiguous:
            self.last_error = f"token endpoint unavailable: {detail}"
            logger.warning("MCP OAuth: refresh failed: %s; keeping current tokens", detail)
            raise McpConnectionError(f"OAuth refresh failed: {detail}")
        if error_code in _TERMINAL_ERRORS:
            self._require_login(grant, f"refresh rejected: {detail}")
        elif transient:
            self._require_login(grant, f"refresh failed after an unanswered attempt: {detail}")
        else:
            self._require_login(grant, f"refresh rejected: {detail}")
        raise LoginRequired(self.last_error)

    def _accept_tokens(self, grant: OAuthGrant, response: httpx2.Response) -> OAuthGrant:
        try:
            body = response.json()
            if not isinstance(body, dict):
                raise ValueError("not an object")
            access_token = body["access_token"]
            refresh_token = body.get("refresh_token", grant.refresh_token)
            expires_in = body.get("expires_in", _DEFAULT_EXPIRES_IN)
            if not isinstance(access_token, str) or not access_token:
                raise TypeError("access_token")
            if not isinstance(refresh_token, str) or not refresh_token:
                raise TypeError("refresh_token")
            if isinstance(expires_in, bool) or not isinstance(expires_in, (int, float)) or expires_in <= 0:
                raise TypeError("expires_in")
        except (ValueError, KeyError, TypeError):
            # The server answered 200, so it may have rotated the refresh token.
            self._require_login(grant, "token endpoint returned an unusable success response")
            raise LoginRequired(self.last_error) from None

        self._on_secret(access_token)
        self._on_secret(refresh_token)
        refreshed = replace(
            grant,
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=self._clock() + float(expires_in),
        )
        self._grant = refreshed
        self.last_error = None
        try:
            self._store.save(refreshed)
        except OSError as exc:
            raise McpConfigError(
                f"refreshed OAuth tokens could not be saved to {self._store.path}: {type(exc).__name__}"
            ) from None
        logger.info("MCP OAuth: access token refreshed")
        return refreshed


def _error_code(response: httpx2.Response) -> str | None:
    """Return the OAuth ``error`` code if it is a short, safe identifier."""
    try:
        body = response.json()
    except ValueError:
        return None
    code = body.get("error") if isinstance(body, dict) else None
    if isinstance(code, str) and _SAFE_ERROR_CODE.match(code):
        return code
    return None
