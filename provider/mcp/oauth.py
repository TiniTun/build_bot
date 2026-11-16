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

If saving a refreshed grant fails, the in-memory grant stays authoritative: a
store that still holds a refresh token this process already rotated out is stale,
so the in-memory grant is used (and its save retried) instead of replaying it.

That in-memory state lives in ``_GrantState``, one per token-store path for the
whole process, not on the auth instance. The hub builds a new client, and so a new
``McpOAuthAuth``, on every reconnect; per-instance state would forget the rotation
and replay the rotated-out refresh token from the stale store, which makes the
provider revoke the grant. A new instance still reads the store on first use, so
a new login (a refresh token this process never rotated out) is adopted.

Token material never appears in an exception, ``last_error`` or a log record. The
token endpoint's response body is never logged or quoted either.
"""

import logging
import re
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import AsyncGenerator, Callable

import httpx2

from provider.mcp.errors import McpConfigError, McpConnectionError
from provider.mcp.oauth_store import OAuthGrant, OAuthStore

logger = logging.getLogger(__name__)

_TERMINAL_ERRORS = frozenset({"invalid_grant", "invalid_client"})
_TOKEN_TIMEOUT = httpx2.Timeout(30.0)
# Used only when a successful token response omits ``expires_in``.
DEFAULT_EXPIRES_IN = 3600.0
_SAFE_ERROR_CODE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
# The request never left this machine, so the token cannot have been rotated.
_NOT_SENT_ERRORS = (httpx2.ConnectError, httpx2.ConnectTimeout, httpx2.PoolTimeout)


@dataclass
class _GrantState:
    """The authoritative grant state for one token store in this process."""

    grant: OAuthGrant | None = None
    login_required: bool = False
    # Refresh token of the grant that required login; never sent again.
    dead_refresh_token: str | None = None
    # Refresh tokens this process rotated out. A stored grant holding one is stale
    # (an earlier save failed) and must never be sent again.
    rotated_out: set[str] = field(default_factory=set)
    last_error: str | None = None


# Deliberate process-level state, like the shared secret redactor: every
# ``McpOAuthAuth`` for one store path shares one ``_GrantState`` so a reconnect
# (a new client and auth) keeps what the previous auth learned. Only the event
# loop touches it; across processes the store's file lock serializes refreshes.
_GRANT_STATES: dict[Path, _GrantState] = {}


def _grant_state_for(path: Path) -> _GrantState:
    return _GRANT_STATES.setdefault(path.expanduser().resolve(), _GrantState())


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
        self._state = _grant_state_for(store.path)
        # This instance has not read the store yet: a new auth (a reconnect) reads
        # it once, so a login written meanwhile is not hidden by the shared grant.
        self._synced = False

    @property
    def login_required(self) -> bool:
        return self._state.login_required

    @property
    def last_error(self) -> str | None:
        return self._state.last_error

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
        state = self._state
        if not self._synced or state.grant is None or state.login_required:
            self._adopt(self._load_current())
            self._synced = True
        assert state.grant is not None
        if self._is_valid(state.grant):
            return state.grant
        return await self._refresh(stale_access_token=None)

    def _load_current(self) -> OAuthGrant:
        """The stored grant, or the in-memory one when the store is stale."""
        grant = self._load()
        state = self._state
        if self._is_stale(grant) and state.grant is not None:
            if state.login_required and state.grant.refresh_token == state.dead_refresh_token:
                # The in-memory grant died and the store only holds an older,
                # rotated-out one: neither may be sent; only a new login helps.
                raise LoginRequired(self.last_error)
            logger.warning("MCP OAuth: token store holds a rotated-out grant; using the in-memory grant")
            return state.grant
        return grant

    async def _recover_from_401(self, rejected_access_token: str) -> OAuthGrant:
        reloaded = self._load()
        if self._is_stale(reloaded):
            return await self._refresh(stale_access_token=rejected_access_token)
        if reloaded.access_token != rejected_access_token and self._is_valid(reloaded):
            self._adopt(reloaded)
            return reloaded
        return await self._refresh(stale_access_token=rejected_access_token)

    async def _refresh(self, *, stale_access_token: str | None) -> OAuthGrant:
        """Refresh under the cross-process lock, unless another writer already did."""
        async with self._store.locked():
            grant = self._load()
            if self._is_stale(grant):
                # An earlier save failed: memory holds the newer grant.
                assert self._state.grant is not None
                logger.warning("MCP OAuth: token store holds a rotated-out grant; using the in-memory grant")
                grant = self._state.grant
                self._save(grant)
            if self._is_valid(grant) and grant.access_token != stale_access_token:
                self._adopt(grant)
                return grant
            self._state.grant = grant
            return await self._refresh_locked(grant)

    def _is_stale(self, grant: OAuthGrant) -> bool:
        return grant.refresh_token in self._state.rotated_out

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
        state = self._state
        if state.login_required and grant.refresh_token == state.dead_refresh_token:
            raise LoginRequired(self.last_error)
        return grant

    def _adopt(self, grant: OAuthGrant) -> None:
        self._on_secret(grant.access_token)
        self._on_secret(grant.refresh_token)
        state = self._state
        state.grant = grant
        if state.login_required:
            logger.info("MCP OAuth: a new grant was found; login is no longer required")
        state.login_required = False
        state.dead_refresh_token = None
        state.last_error = None

    def _require_login(self, grant: OAuthGrant | None, reason: str) -> None:
        state = self._state
        state.login_required = True
        state.dead_refresh_token = grant.refresh_token if grant is not None else None
        state.last_error = reason
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
            self._state.last_error = f"token endpoint unreachable ({type(exc).__name__})"
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
            self._state.last_error = f"token endpoint unavailable: {detail}"
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
            expires_in = body.get("expires_in", DEFAULT_EXPIRES_IN)
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
        if refresh_token != grant.refresh_token:
            self._state.rotated_out.add(grant.refresh_token)
        refreshed = replace(
            grant,
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=self._clock() + float(expires_in),
        )
        self._state.grant = refreshed
        self._state.last_error = None
        # A failed save must not fail this request: the tokens are valid, and the
        # in-memory grant stays authoritative until a later save succeeds.
        self._save(refreshed)
        logger.info("MCP OAuth: access token refreshed")
        return refreshed

    def _save(self, grant: OAuthGrant) -> bool:
        try:
            self._store.save(grant)
        except OSError as exc:
            logger.error(
                "MCP OAuth: refreshed tokens could not be saved to %s (%s); keeping them in memory",
                self._store.path,
                type(exc).__name__,
            )
            return False
        return True


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
