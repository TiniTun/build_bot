"""Streamable HTTP MCP client built on the official Python MCP SDK.

Only this module imports the SDK. Everything above it works with the narrow
``McpClient`` interface and the immutable models in ``provider.mcp.models``, so
the SDK can be swapped or faked without touching the hub, the capability bridge,
or agent code.

Lifecycle note: the SDK exposes the transport and the client session as async
context managers whose ``anyio`` task groups must be entered and exited in the
same task. A long-lived host cannot honor that from ``connect()``/``close()``
call sites, so the whole ``async with`` chain runs inside one dedicated runner
task. ``connect()`` waits for it to become ready, ``close()`` asks it to unwind,
and all requests are issued against the session it publishes. Exiting normally
(rather than cancelling) is what lets the transport send its session-terminating
DELETE.
"""

import asyncio
import contextlib
import logging
import os
import threading
from typing import Any, AsyncGenerator, Awaitable, Callable, Mapping, Protocol, Sequence
from urllib.parse import urlsplit, urlunsplit

import httpx2
import mcp_types
from mcp.client.auth.exceptions import OAuthFlowError, OAuthTokenError
from mcp.client.auth.oauth2 import OAuthClientProvider
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import AuthorizationCodeResult, OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
from mcp.shared.exceptions import MCPError

from provider.mcp.errors import (
    McpAuthError,
    McpConfigError,
    McpConnectionError,
    McpError,
    McpLoginRequiredError,
    McpNotConnectedError,
    McpOAuthClientRejectedError,
    McpProtocolError,
    McpTimeoutError,
)
from provider.mcp.models import (
    McpCallResult,
    McpServerInfo,
    McpToolDef,
    OAuthCallback,
    OAuthClientRegistration,
    OAuthLoginResult,
)
from provider.mcp.oauth import McpOAuthAuth
from provider.mcp.oauth_store import OAuthStore
from utils.mcp_config import McpServerConfig

logger = logging.getLogger(__name__)

# Defensive bound on tools/list pagination so a misbehaving server cannot make
# discovery loop forever.
MAX_DISCOVERY_PAGES = 50

CLIENT_INFO = mcp_types.Implementation(name="build-bot", version="0.1.0")

ToolsChangedCallback = Callable[[str], None]


class McpClient(Protocol):
    """The only MCP surface the rest of the application depends on."""

    server_id: str

    @property
    def is_connected(self) -> bool: ...

    async def probe_health(self) -> None:
        """Raise ``McpError`` unless the configured health endpoint is reachable."""
        ...

    async def connect(self) -> McpServerInfo: ...

    async def list_tools(self) -> tuple[McpToolDef, ...]: ...

    async def call_tool(self, tool_name: str, arguments: Mapping[str, Any] | None = None) -> McpCallResult: ...

    async def close(self) -> None:
        """Idempotent. Safe to call when never connected."""
        ...

    def set_tools_changed_callback(self, callback: ToolsChangedCallback | None) -> None: ...


class _StatusWatchingTransport(httpx2.AsyncBaseTransport):
    """Notes HTTP status codes on their way past.

    The SDK converts a non-2xx POST into a generic JSON-RPC ``INTERNAL_ERROR``
    and discards the status, so a rejected token would otherwise be
    indistinguishable from a server crash. Watching the transport keeps
    "auth failed" separable from "transport failed" for operators.
    """

    def __init__(self, inner: httpx2.AsyncBaseTransport, observer: Callable[[int], None]) -> None:
        self._inner = inner
        self._observer = observer

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        response = await self._inner.handle_async_request(request)
        self._observer(response.status_code)
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()


class _Redactor:
    """Scrubs known secret values out of any text that becomes an error message.

    A redactor built with ``shared`` also feeds every secret it learns into that
    one, so the process-wide log filter can scrub secrets from every client.
    Logging can run on other threads, so mutation and iteration are locked.
    """

    def __init__(self, shared: "_Redactor | None" = None) -> None:
        # A dict keeps insertion order and drops duplicates (a refreshed token
        # is reported once per adoption).
        self._secrets: dict[str, None] = {}
        self._lock = threading.Lock()
        self._shared = shared

    def add(self, secret: str | None) -> None:
        # Very short values would redact harmless substrings; a real token is long.
        if secret and len(secret) >= 6:
            with self._lock:
                self._secrets[secret] = None
            if self._shared is not None:
                self._shared.add(secret)

    def scrub(self, text: str) -> str:
        with self._lock:
            secrets = tuple(self._secrets)
        for secret in secrets:
            text = text.replace(secret, "***")
        return text


# The one process-wide redactor: every client's redactor feeds it, and
# ``SecretScrubFilter`` reads it. It only ever accumulates secret values.
_SHARED_REDACTOR = _Redactor()

# Loggers that can see request data or token material. Filters on a logger do not
# apply to records propagated from its children, so module loggers are listed
# individually. This is belt-and-braces: ``utils.logging.setup_logging`` puts the
# same filter on every handler it creates, which covers every logger.
SCRUBBED_LOGGERS = (
    "mcp.client.auth",
    "mcp.client.auth.oauth2",
    "mcp.client.auth.utils",
    "mcp.client.streamable_http",
    "provider.mcp",
    "provider.mcp.client",
    "provider.mcp.hub",
    "provider.mcp.oauth",
    "provider.mcp.oauth_store",
)


class SecretScrubFilter(logging.Filter):
    """Replaces known secrets in a log record's message, args and traceback.

    The message is rendered with its args first, so a secret passed as a ``%s``
    argument is caught too. A traceback is formatted into ``exc_text`` and
    scrubbed; ``exc_info`` is then dropped so no formatter re-renders it raw.
    """

    def __init__(self, redactor: _Redactor) -> None:
        super().__init__()
        self._redactor = redactor

    def filter(self, record: logging.LogRecord) -> bool:
        scrub = self._redactor.scrub
        try:
            message = record.getMessage()
        except (TypeError, ValueError):
            # Malformed format/args: keep both visible, but scrubbed.
            message = f"{record.msg} {record.args!r}"
        record.msg = scrub(message)
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        record.exc_info = None
        if record.exc_text:
            record.exc_text = scrub(record.exc_text)
        if record.stack_info:
            record.stack_info = scrub(record.stack_info)
        return True


def make_secret_scrub_filter() -> SecretScrubFilter:
    """Return a ``SecretScrubFilter`` over the process-wide redactor (for handlers)."""
    return SecretScrubFilter(_SHARED_REDACTOR)


def install_secret_scrub_filter() -> None:
    """Attach the shared ``SecretScrubFilter`` to each ``SCRUBBED_LOGGERS`` entry once."""
    for name in SCRUBBED_LOGGERS:
        target = logging.getLogger(name)
        if not any(isinstance(f, SecretScrubFilter) for f in target.filters):
            target.addFilter(SecretScrubFilter(_SHARED_REDACTOR))


def _leaf_exceptions(exc: BaseException) -> list[BaseException]:
    """Flatten ``ExceptionGroup``s that anyio task groups raise into real causes.

    Without this, every transport failure would surface as the useless
    "unhandled errors in a TaskGroup" and no secret inside a nested message
    would ever be redacted.
    """
    if isinstance(exc, BaseExceptionGroup):
        leaves: list[BaseException] = []
        for sub in exc.exceptions:
            leaves.extend(_leaf_exceptions(sub))
        return leaves or [exc]
    return [exc]


def _causal_chain(exc: BaseException) -> list[BaseException]:
    """The exception plus its ``__cause__``/``__context__`` ancestry, flattened."""
    chain: list[BaseException] = []
    seen: set[int] = set()
    pending = _leaf_exceptions(exc)
    while pending:
        current = pending.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        chain.append(current)
        for nxt in (current.__cause__, current.__context__):
            if nxt is not None:
                pending.extend(_leaf_exceptions(nxt))
    return chain


def _status_code_in_chain(exc: BaseException) -> int | None:
    """Find an HTTP status code anywhere in an exception's cause chain."""
    for current in _causal_chain(exc):
        response = getattr(current, "response", None)
        status = getattr(response, "status_code", None)
        if isinstance(status, int):
            return status
        status = getattr(current, "status_code", None)
        if isinstance(status, int):
            return status
    return None


def _classify_rpc_error(error: MCPError, prefix: str, detail: str) -> McpError:
    """Map the SDK's JSON-RPC error onto lost-session vs. failed-request.

    Only a lost session may become ``McpConnectionError``: the hub answers that by
    taking the server offline and reconnecting. The SDK reports a lost session as
    ``CONNECTION_CLOSED`` (the stream ended or the POST raised) or, for a 404 on an
    established session, as ``INVALID_REQUEST`` "Session terminated". Any other
    code is the server answering this one request (invalid params, a tool-side
    failure, a 5xx stand-in): the call fails and the session stays up.
    """
    if error.code == mcp_types.CONNECTION_CLOSED:
        return McpConnectionError(f"{prefix}: {detail}")
    if error.code == mcp_types.INVALID_REQUEST and error.message == "Session terminated":
        return McpConnectionError(f"{prefix}: {detail}")
    if error.code == mcp_types.REQUEST_TIMEOUT:
        return McpTimeoutError(f"{prefix}: timed out")
    return McpProtocolError(f"{prefix}: {detail}")


class StreamableHttpMcpClient:
    """One connection to one Streamable HTTP MCP server.

    The Bearer token is attached to the ``httpx2.AsyncClient`` rather than to a
    single request, so every exchange the transport makes in the session — the
    initialize POST, tool listing, tool calls, the resumption/notification GET
    stream, and the terminating DELETE — carries ``Authorization``. With
    ``auth: oauth`` configured, an ``McpOAuthAuth`` supplies (and refreshes) that
    header instead of a static ``token_env`` value.
    """

    def __init__(
        self,
        server_id: str,
        config: McpServerConfig,
        *,
        env: Mapping[str, str] | None = None,
        transport_factory: Callable[[], httpx2.AsyncBaseTransport] | None = None,
        oauth_auth: McpOAuthAuth | None = None,
    ) -> None:
        self.server_id = server_id
        self._config = config
        self._env: Mapping[str, str] = os.environ if env is None else env
        self._transport_factory = transport_factory

        install_secret_scrub_filter()
        self._redactor = _Redactor(shared=_SHARED_REDACTOR)
        if oauth_auth is None and config.auth is not None:
            oauth_auth = McpOAuthAuth(OAuthStore(config.auth.token_store), on_secret=self._redactor.add)
        self._oauth_auth = oauth_auth
        self._session: ClientSession | None = None
        self._info: McpServerInfo | None = None
        self._runner: asyncio.Task[None] | None = None
        self._ready: asyncio.Future[McpServerInfo] | None = None
        self._stop: asyncio.Event | None = None
        self._closing = False
        self._tools_changed: ToolsChangedCallback | None = None
        self._last_auth_rejection: int | None = None

    # ---- introspection -------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self._session is not None and not self._closing

    @property
    def server_info(self) -> McpServerInfo | None:
        return self._info

    def set_tools_changed_callback(self, callback: ToolsChangedCallback | None) -> None:
        self._tools_changed = callback

    def __repr__(self) -> str:  # pragma: no cover - trivial, but must stay secret-free
        return f"StreamableHttpMcpClient(server_id={self.server_id!r}, connected={self.is_connected})"

    # ---- configuration resolution --------------------------------------

    def _resolve_url(self) -> str:
        if self._config.url is not None:
            return self._config.url
        assert self._config.url_env is not None  # enforced by config validation
        value = self._env.get(self._config.url_env)
        if not value:
            raise McpConfigError(f"[{self.server_id}] environment variable {self._config.url_env} is not set")
        if not value.startswith(("http://", "https://")):
            raise McpConfigError(f"[{self.server_id}] {self._config.url_env} must contain an http:// or https:// URL")
        return value

    def _resolve_token(self) -> str | None:
        if self._config.token_env is None:
            return None
        token = self._env.get(self._config.token_env)
        if not token:
            raise McpAuthError(f"[{self.server_id}] environment variable {self._config.token_env} is not set")
        self._redactor.add(token)
        return token

    def _resolve_health_url(self, mcp_url: str) -> str | None:
        if self._config.health_url is not None:
            return self._config.health_url
        if self._config.health_path is None:
            return None
        parts = urlsplit(mcp_url)
        return urlunsplit((parts.scheme, parts.netloc, self._config.health_path, "", ""))

    def _auth_headers(self, token: str | None) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"} if token else {}

    def _new_http_client(
        self,
        headers: Mapping[str, str],
        timeout: httpx2.Timeout,
        auth: httpx2.Auth | None = None,
    ) -> httpx2.AsyncClient:
        inner = self._transport_factory() if self._transport_factory is not None else httpx2.AsyncHTTPTransport()
        return httpx2.AsyncClient(
            headers=dict(headers),
            auth=auth,
            timeout=timeout,
            # Redirects are refused: following one could replay the Authorization
            # header to a host the operator never configured.
            follow_redirects=False,
            transport=_StatusWatchingTransport(inner, self._note_status),
        )

    def _note_status(self, status: int) -> None:
        if status in (401, 403):
            self._last_auth_rejection = status

    # ---- error mapping -------------------------------------------------

    def _wrap(self, exc: BaseException, action: str) -> McpError:
        """Map an SDK/HTTP/protocol failure onto a stable, redacted internal error.

        anyio task groups raise ``ExceptionGroup``, so classification looks at
        the flattened causes rather than the outermost wrapper.
        """
        chain = _causal_chain(exc)
        for candidate in chain:
            if isinstance(candidate, McpError):
                return candidate

        prefix = f"[{self.server_id}] {action} failed"
        login_error = self._login_required_error(action)
        if login_error is not None:
            return login_error
        detail = self._redactor.scrub("; ".join(f"{type(e).__name__}: {e}" for e in chain[:3]))

        status = _status_code_in_chain(exc) or self._last_auth_rejection
        if status in (401, 403):
            return McpAuthError(f"{prefix}: server rejected authentication (HTTP {status})")

        def _any(*types: type[BaseException]) -> bool:
            return any(isinstance(e, types) for e in chain)

        if _any(asyncio.TimeoutError, TimeoutError, httpx2.TimeoutException):
            return McpTimeoutError(f"{prefix}: timed out")
        if _any(httpx2.TransportError):
            return McpConnectionError(f"{prefix}: {detail}")
        rpc_error = next((e for e in chain if isinstance(e, MCPError)), None)
        if rpc_error is not None:
            return _classify_rpc_error(rpc_error, prefix, detail)
        if _any(ValueError, TypeError, KeyError):
            return McpProtocolError(f"{prefix}: {detail}")
        # Anything else (e.g. anyio's closed/broken stream) means the session is gone.
        return McpConnectionError(f"{prefix}: {detail}")

    def _login_required_error(self, action: str) -> McpLoginRequiredError | None:
        """An ``McpLoginRequiredError`` if the OAuth auth has flagged a dead grant.

        The SDK transport swallows exceptions raised by the auth flow, so the
        auth's flag is the reliable signal. ``last_error`` is secret-free.
        """
        auth = self._oauth_auth
        if auth is None or not auth.login_required:
            return None
        reason = auth.last_error or "OAuth login required"
        return McpLoginRequiredError(f"[{self.server_id}] {action} failed: login required: {reason}")

    # ---- health --------------------------------------------------------

    async def probe_health(self) -> None:
        """GET the configured health endpoint. No-op when none is configured.

        The probe is deliberately unauthenticated: it answers "is the service
        up", and must not depend on a token being valid.
        """
        url = self._resolve_health_url(self._resolve_url())
        if url is None:
            return
        timeout = httpx2.Timeout(self._config.health_timeout_seconds)
        try:
            async with self._new_http_client({}, timeout) as client:
                response = await client.get(url)
        except Exception as exc:
            raise self._wrap(exc, "health probe") from exc
        if response.status_code >= 400:
            raise McpConnectionError(f"[{self.server_id}] health probe failed: HTTP {response.status_code}")

    # ---- connection lifecycle ------------------------------------------

    async def connect(self) -> McpServerInfo:
        """Open the session and initialize. Returns cached info if already up."""
        if self._info is not None and self.is_connected:
            return self._info
        if self._runner is not None:
            raise McpError(f"[{self.server_id}] connect() called while a session is active")

        url = self._resolve_url()
        token = self._resolve_token()
        headers = self._auth_headers(token)

        loop = asyncio.get_running_loop()
        self._ready = loop.create_future()
        self._stop = asyncio.Event()
        self._closing = False
        self._last_auth_rejection = None
        self._runner = asyncio.create_task(self._run(url, headers), name=f"mcp-{self.server_id}")

        try:
            info = await asyncio.wait_for(asyncio.shield(self._ready), self._config.connect_timeout_seconds)
        except asyncio.TimeoutError as exc:
            await self.close()
            login_error = self._login_required_error("connect")
            if login_error is not None:
                raise login_error from None
            raise McpTimeoutError(
                f"[{self.server_id}] connect timed out after {self._config.connect_timeout_seconds}s"
            ) from exc
        except Exception as exc:
            await self.close()
            login_error = self._login_required_error("connect")
            if login_error is not None and not isinstance(exc, McpLoginRequiredError):
                raise login_error from None
            raise

        self._info = info
        return info

    async def _run(self, url: str, headers: Mapping[str, str]) -> None:
        """Own the SDK context managers for the whole life of the connection."""
        assert self._ready is not None and self._stop is not None
        ready, stop = self._ready, self._stop
        timeout = httpx2.Timeout(
            self._config.read_timeout_seconds,
            connect=self._config.connect_timeout_seconds,
        )
        try:
            async with self._new_http_client(headers, timeout, self._oauth_auth) as http_client:
                async with streamable_http_client(url, http_client=http_client) as (
                    read_stream,
                    write_stream,
                ):
                    async with ClientSession(
                        read_stream,
                        write_stream,
                        read_timeout_seconds=self._config.read_timeout_seconds,
                        client_info=CLIENT_INFO,
                        message_handler=self._on_message,
                    ) as session:
                        init = await session.initialize()
                        self._session = session
                        if not ready.done():
                            ready.set_result(self._to_server_info(init))
                        await stop.wait()
        except asyncio.CancelledError:
            if not ready.done():
                ready.set_exception(McpConnectionError(f"[{self.server_id}] connection cancelled"))
            raise
        except BaseException as exc:
            wrapped = self._wrap(exc, "connection")
            if not ready.done():
                ready.set_exception(wrapped)
            else:
                logger.warning("MCP session for %s ended: %s", self.server_id, wrapped)
        finally:
            self._session = None

    def _to_server_info(self, init: mcp_types.InitializeResult) -> McpServerInfo:
        tools_cap = getattr(init.capabilities, "tools", None)
        return McpServerInfo(
            server_id=self.server_id,
            name=getattr(init.server_info, "name", "") or "",
            version=getattr(init.server_info, "version", "") or "",
            protocol_version=init.protocol_version or "",
            instructions=init.instructions,
            supports_tools=tools_cap is not None,
            supports_tool_list_changed=bool(getattr(tools_cap, "list_changed", False) if tools_cap else False),
        )

    async def _on_message(self, message: Any) -> None:
        """Tee for server notifications; only tool-list changes are acted on."""
        if isinstance(message, mcp_types.ToolListChangedNotification):
            callback = self._tools_changed
            if callback is not None:
                try:
                    callback(self.server_id)
                except Exception:  # pragma: no cover - callback owns its errors
                    logger.exception("tools-changed callback for %s raised", self.server_id)

    async def close(self) -> None:
        """Unwind the session. Idempotent, and safe before any connect()."""
        self._closing = True
        runner, self._runner = self._runner, None
        stop = self._stop
        self._info = None
        if runner is None:
            return
        if stop is not None:
            stop.set()
        try:
            # Give the runner a bounded chance to unwind cleanly so the transport
            # can send its session-terminating DELETE, then cancel.
            await asyncio.wait_for(asyncio.shield(runner), self._config.connect_timeout_seconds)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            runner.cancel()
            try:
                await runner
            except (asyncio.CancelledError, Exception):
                pass
        except Exception as exc:  # pragma: no cover - runner already logs
            logger.debug("MCP runner for %s exited with %s", self.server_id, exc)
        finally:
            self._consume_ready()
            self._session = None
            self._stop = None
            self._ready = None

    def _consume_ready(self) -> None:
        """Settle the readiness future so its outcome is never left unretrieved.

        A connect() that timed out stops awaiting this future while the runner is
        still unwinding; without this, asyncio logs "exception was never
        retrieved" at garbage-collection time.
        """
        ready = self._ready
        if ready is None:
            return
        if not ready.done():
            ready.cancel()
        elif not ready.cancelled():
            ready.exception()

    # ---- requests ------------------------------------------------------

    def _require_session(self) -> ClientSession:
        session = self._session
        if session is None or self._closing:
            raise McpNotConnectedError(f"[{self.server_id}] is not connected")
        return session

    async def list_tools(self) -> tuple[McpToolDef, ...]:
        """Discover every advertised tool, following pagination cursors."""
        session = self._require_session()
        collected: list[McpToolDef] = []
        cursor: str | None = None
        try:
            async with asyncio.timeout(self._config.discovery_timeout_seconds):
                for _ in range(MAX_DISCOVERY_PAGES):
                    params = mcp_types.PaginatedRequestParams(cursor=cursor) if cursor is not None else None
                    page = await session.list_tools(params=params)
                    collected.extend(self._to_tool_def(t) for t in page.tools)
                    cursor = page.next_cursor
                    if cursor is None:
                        return tuple(collected)
        except asyncio.TimeoutError as exc:
            raise McpTimeoutError(
                f"[{self.server_id}] tool discovery timed out after {self._config.discovery_timeout_seconds}s"
            ) from exc
        except Exception as exc:
            raise self._wrap(exc, "tool discovery") from exc
        raise McpProtocolError(f"[{self.server_id}] tool discovery exceeded {MAX_DISCOVERY_PAGES} pages")

    def _to_tool_def(self, tool: mcp_types.Tool) -> McpToolDef:
        annotations = getattr(tool, "annotations", None)
        return McpToolDef(
            server_id=self.server_id,
            name=tool.name,
            description=tool.description or "",
            title=getattr(tool, "title", None),
            input_schema=dict(tool.input_schema or {}),
            output_schema=dict(tool.output_schema) if tool.output_schema else None,
            annotations=(
                annotations.model_dump(exclude_none=True)
                if hasattr(annotations, "model_dump")
                else (dict(annotations) if annotations else None)
            ),
        )

    async def call_tool(self, tool_name: str, arguments: Mapping[str, Any] | None = None) -> McpCallResult:
        """Invoke one remote tool. Never retries: a timeout is ambiguous.

        Per-server concurrency is enforced by the hub, which is the single
        invocation path; this method deliberately does not gate a second time.
        """
        session = self._require_session()
        try:
            async with asyncio.timeout(self._config.read_timeout_seconds):
                raw = await session.call_tool(tool_name, dict(arguments or {}))
        except asyncio.TimeoutError as exc:
            raise McpTimeoutError(
                f"[{self.server_id}] call to {tool_name} timed out after {self._config.read_timeout_seconds}s"
            ) from exc
        except Exception as exc:
            raise self._wrap(exc, f"call to {tool_name}") from exc

        if not isinstance(raw, mcp_types.CallToolResult):
            raise McpProtocolError(f"[{self.server_id}] call to {tool_name} returned an unsupported result type")
        return self._to_call_result(tool_name, raw)

    def _to_call_result(self, tool_name: str, raw: mcp_types.CallToolResult) -> McpCallResult:
        return _to_call_result(self.server_id, tool_name, raw)


def _to_call_result(server_id: str, tool_name: str, raw: mcp_types.CallToolResult) -> McpCallResult:
    texts: list[str] = []
    dropped: list[str] = []
    for block in _content_blocks(raw):
        if isinstance(block, mcp_types.TextContent):
            texts.append(block.text)
        else:
            dropped.append(getattr(block, "type", type(block).__name__))

    structured = raw.structured_content
    return McpCallResult(
        server_id=server_id,
        tool_name=tool_name,
        text_blocks=tuple(texts),
        structured_content=dict(structured) if structured else None,
        is_error=bool(raw.is_error),
        # `_meta` is host-side only; it never becomes model-visible content.
        private_meta=dict(raw.meta) if raw.meta else {},
        dropped_content_types=tuple(dict.fromkeys(dropped)),
    )


def _content_blocks(raw: mcp_types.CallToolResult) -> Sequence[Any]:
    content = raw.content
    if content is None:
        return ()
    return content


# ---- interactive OAuth login (the ``mcp-login`` command) -----------------

LOGIN_CLIENT_NAME = "build_bot"


class _MemoryTokenStorage:
    """SDK ``TokenStorage`` held in memory; the login command persists the outcome.

    Only a client registration is ever seeded: a login always runs the full
    authorization, so it starts with no tokens.
    """

    def __init__(self, client_info: OAuthClientInformationFull | None) -> None:
        self.tokens: OAuthToken | None = None
        self.client_info = client_info

    async def get_tokens(self) -> OAuthToken | None:
        return self.tokens

    async def set_tokens(self, tokens: OAuthToken) -> None:
        _SHARED_REDACTOR.add(tokens.access_token)
        _SHARED_REDACTOR.add(tokens.refresh_token)
        self.tokens = tokens

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        return self.client_info

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        _SHARED_REDACTOR.add(client_info.client_secret)
        self.client_info = client_info


class _LoginOAuthProvider(OAuthClientProvider):
    """``OAuthClientProvider`` that reports the first failure of its flow.

    The Streamable HTTP transport logs and swallows exceptions raised inside a
    POST, so a failed authorization would otherwise leave ``initialize`` waiting
    forever. The failure is published on ``failure`` instead, and re-raised.
    """

    def __init__(self, failure: "asyncio.Future[BaseException]", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.failure = failure

    async def async_auth_flow(self, request: httpx2.Request) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        flow = super().async_auth_flow(request)
        try:
            outgoing = await flow.__anext__()
            while True:
                incoming = yield outgoing
                outgoing = await flow.asend(incoming)
        except StopAsyncIteration:
            return
        except Exception as exc:
            if not self.failure.done():
                self.failure.set_result(exc)
            raise
        finally:
            await flow.aclose()


def _seed_client_info(
    registration: OAuthClientRegistration | None, redirect_uri: str
) -> OAuthClientInformationFull | None:
    if registration is None:
        return None
    _SHARED_REDACTOR.add(registration.client_secret)
    return OAuthClientInformationFull(
        client_id=registration.client_id,
        client_secret=registration.client_secret,
        redirect_uris=[redirect_uri],
        token_endpoint_auth_method="client_secret_post" if registration.client_secret else "none",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
    )


def _retrieve_outcome(task: "asyncio.Future[Any]") -> None:
    if not task.cancelled():
        task.exception()


def _login_session_error(server_id: str, exc: BaseException | None) -> McpConnectionError:
    leaves = _causal_chain(exc) if exc is not None else []
    detail = _SHARED_REDACTOR.scrub("; ".join(f"{type(e).__name__}: {e}" for e in leaves[:3]))
    return McpConnectionError(f"[{server_id}] login session failed: {detail}")


def _login_failure(server_id: str, exc: BaseException, reused_registration: bool) -> BaseException:
    """Map an SDK OAuth failure onto an internal error; pass anything else through.

    Non-SDK exceptions come from the caller's own redirect/callback handlers and
    already carry the caller's wording.
    """
    if not isinstance(exc, OAuthFlowError):
        return exc
    if isinstance(exc, OAuthTokenError) and str(exc).startswith("Invalid token response"):
        # The SDK appends a pydantic validation error whose input_value can echo a token.
        detail = "Invalid token response from the token endpoint"
    else:
        detail = _SHARED_REDACTOR.scrub(str(exc))
    if reused_registration and "invalid_client" in detail:
        return McpOAuthClientRejectedError(f"[{server_id}] the stored client registration was rejected")
    return McpAuthError(f"[{server_id}] OAuth login failed: {detail}")


async def authorize_interactively(
    server_id: str,
    url: str,
    *,
    redirect_uri: str,
    registration: OAuthClientRegistration | None,
    redirect_handler: Callable[[str], Awaitable[None]],
    callback_handler: Callable[[], Awaitable[OAuthCallback]],
    transport: httpx2.AsyncBaseTransport | None = None,
    identity_tool: str | None = None,
    request_timeout_seconds: float = 30.0,
) -> OAuthLoginResult:
    """Run the SDK's authorization-code + PKCE flow by opening one MCP session.

    The first ``initialize`` POST gets a 401, which makes the SDK discover the
    authorization server, register a client unless ``registration`` is given,
    hand the authorization URL to ``redirect_handler`` and wait on
    ``callback_handler``. With the new token the session initializes, and
    ``identity_tool`` (if any) is called once to show whom the grant belongs to.
    No overall deadline is applied here: the caller owns it.

    Raises ``McpOAuthClientRejectedError`` when a reused registration is rejected
    (``invalid_client``), ``McpAuthError`` for any other OAuth failure, and
    ``McpConnectionError`` when the session fails for another reason.
    """
    loop = asyncio.get_running_loop()
    failure: asyncio.Future[BaseException] = loop.create_future()
    storage = _MemoryTokenStorage(_seed_client_info(registration, redirect_uri))

    async def on_callback() -> AuthorizationCodeResult:
        result = await callback_handler()
        _SHARED_REDACTOR.add(result.code)
        return AuthorizationCodeResult(code=result.code, state=result.state, iss=result.iss)

    provider = _LoginOAuthProvider(
        failure,
        server_url=url,
        client_metadata=OAuthClientMetadata(
            client_name=LOGIN_CLIENT_NAME,
            redirect_uris=[redirect_uri],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
        ),
        storage=storage,
        redirect_handler=redirect_handler,
        callback_handler=on_callback,
    )

    install_secret_scrub_filter()
    error: BaseException | None = None
    identity: McpCallResult | None = None
    identity_error: str | None = None
    http_client = httpx2.AsyncClient(
        auth=provider,
        transport=transport if transport is not None else httpx2.AsyncHTTPTransport(),
        timeout=httpx2.Timeout(request_timeout_seconds),
        follow_redirects=False,
    )
    init: asyncio.Task[Any] | None = None
    try:
        async with http_client:
            async with streamable_http_client(url, http_client=http_client) as (read_stream, write_stream):
                # No session-level read timeout: initialize stays pending while the
                # operator logs in through the browser.
                async with ClientSession(read_stream, write_stream, client_info=CLIENT_INFO) as session:
                    init = asyncio.ensure_future(session.initialize())
                    await asyncio.wait({init, failure}, return_when=asyncio.FIRST_COMPLETED)
                    if failure.done():
                        error = _login_failure(server_id, failure.result(), registration is not None)
                    elif init.exception() is not None:
                        error = _login_session_error(server_id, init.exception())
                    elif identity_tool is not None:
                        try:
                            raw = await session.call_tool(
                                identity_tool, {}, read_timeout_seconds=request_timeout_seconds
                            )
                            identity = _to_call_result(server_id, identity_tool, raw)
                        except Exception as exc:
                            identity_error = _SHARED_REDACTOR.scrub(f"{type(exc).__name__}: {exc}")
    except Exception as exc:
        # A failed auth flow can also surface as the transport's task-group error.
        if failure.done() and not failure.cancelled():
            error = _login_failure(server_id, failure.result(), registration is not None)
        elif error is None:
            error = _login_session_error(server_id, exc)
    finally:
        if init is not None:
            # Not awaited: swallowing a CancelledError here would defeat the caller's deadline.
            init.cancel()
            init.add_done_callback(_retrieve_outcome)
        if not failure.done():
            failure.cancel()

    if error is not None:
        raise error

    tokens = storage.tokens
    client_info = provider.context.client_info
    metadata = provider.context.oauth_metadata
    if tokens is None or client_info is None:
        raise McpAuthError(f"[{server_id}] OAuth login finished without a token")
    if metadata is None or metadata.token_endpoint is None:
        raise McpAuthError(f"[{server_id}] authorization server metadata has no token endpoint")
    return OAuthLoginResult(
        registration=OAuthClientRegistration(client_id=client_info.client_id, client_secret=client_info.client_secret),
        token_endpoint=str(metadata.token_endpoint),
        resource=provider.context.get_resource_url(),
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        expires_in=float(tokens.expires_in) if tokens.expires_in is not None else None,
        identity=identity,
        identity_error=identity_error,
    )
