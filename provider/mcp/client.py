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
import logging
import os
from typing import Any, Callable, Mapping, Protocol, Sequence
from urllib.parse import urlsplit, urlunsplit

import httpx2
import mcp_types
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

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
    """Scrubs known secret values out of any text that becomes an error message."""

    def __init__(self) -> None:
        self._secrets: list[str] = []

    def add(self, secret: str | None) -> None:
        # Very short values would redact harmless substrings; a real token is long.
        if secret and len(secret) >= 6:
            self._secrets.append(secret)

    def scrub(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "***")
        return text


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


class StreamableHttpMcpClient:
    """One connection to one Streamable HTTP MCP server.

    The Bearer token is attached to the ``httpx2.AsyncClient`` rather than to a
    single request, so every exchange the transport makes in the session — the
    initialize POST, tool listing, tool calls, the resumption/notification GET
    stream, and the terminating DELETE — carries ``Authorization``.
    """

    def __init__(
        self,
        server_id: str,
        config: McpServerConfig,
        *,
        env: Mapping[str, str] | None = None,
        transport_factory: Callable[[], httpx2.AsyncBaseTransport] | None = None,
    ) -> None:
        self.server_id = server_id
        self._config = config
        self._env: Mapping[str, str] = os.environ if env is None else env
        self._transport_factory = transport_factory

        self._redactor = _Redactor()
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

    def _new_http_client(self, headers: Mapping[str, str], timeout: httpx2.Timeout) -> httpx2.AsyncClient:
        inner = self._transport_factory() if self._transport_factory is not None else httpx2.AsyncHTTPTransport()
        return httpx2.AsyncClient(
            headers=dict(headers),
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
        if _any(ValueError, TypeError, KeyError):
            return McpProtocolError(f"{prefix}: {detail}")
        return McpConnectionError(f"{prefix}: {detail}")

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
            raise McpTimeoutError(
                f"[{self.server_id}] connect timed out after {self._config.connect_timeout_seconds}s"
            ) from exc
        except Exception:
            await self.close()
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
            async with self._new_http_client(headers, timeout) as http_client:
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
        texts: list[str] = []
        dropped: list[str] = []
        for block in _content_blocks(raw):
            if isinstance(block, mcp_types.TextContent):
                texts.append(block.text)
            else:
                dropped.append(getattr(block, "type", type(block).__name__))

        structured = raw.structured_content
        return McpCallResult(
            server_id=self.server_id,
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
