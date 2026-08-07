"""Process-level manager for every configured MCP server.

Exactly one hub lives on ``SharedContext``. It owns client connections,
discovery snapshots, invocation routing, per-server concurrency, health state,
and shutdown. Agent sessions never create connections of their own; they read a
snapshot and call back through :meth:`McpHub.call_tool`.

Startup order for a server is fixed and observable: ``/health`` (when
configured) must succeed *before* the MCP session is initialized, which must
happen before ``list_tools``, which must happen before any capability is
published. Optional servers that fail anywhere in that chain degrade on their
own; a server marked ``required`` fails application startup.

Nothing here retries a tool call. A reconnect may restore *future* calls, but an
in-flight mutation that failed ambiguously is never replayed.
"""

import asyncio
import logging
import random
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Mapping

from provider.mcp.client import McpClient, StreamableHttpMcpClient
from provider.mcp.errors import (
    McpError,
    McpNotConnectedError,
    McpTimeoutError,
)
from provider.mcp.models import McpCallResult, McpServerInfo, McpToolDef
from utils.mcp_config import McpServerConfig

if TYPE_CHECKING:
    from utils.config import Config

logger = logging.getLogger(__name__)

ClientFactory = Callable[[str, McpServerConfig], McpClient]


class McpServerState(str, Enum):
    """Coarse lifecycle state, reported by ``/mcp`` and used for gating calls."""

    DISABLED = "disabled"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    # Reachable configuration, unusable server: health, auth, or discovery failed.
    DEGRADED = "degraded"
    RECONNECTING = "reconnecting"
    # A required server that could not be brought up; startup fails on this.
    FAILED = "failed"


@dataclass(frozen=True)
class McpDiscoverySnapshot:
    """An immutable view of one server's advertised tools at a point in time.

    ``version`` increases on every successful refresh so a session can hold a
    stable schema while the hub moves on.
    """

    server_id: str
    version: int
    tools: tuple[McpToolDef, ...]
    server_info: McpServerInfo | None
    discovered_at: float

    def tool(self, name: str) -> McpToolDef | None:
        for tool in self.tools:
            if tool.name == name:
                return tool
        return None


@dataclass(frozen=True)
class McpServerStatus:
    """Operator-facing status. Never contains credentials or raw payloads."""

    server_id: str
    enabled: bool
    required: bool
    state: McpServerState
    server_name: str | None = None
    server_version: str | None = None
    last_discovery_at: float | None = None
    discovered_tools: int = 0
    enabled_tools: int = 0
    quarantined_tools: int = 0
    last_error: str | None = None


@dataclass
class _ServerRuntime:
    """Mutable per-server runtime state. Deliberately not in the config models."""

    server_id: str
    config: McpServerConfig
    client: McpClient | None = None
    state: McpServerState = McpServerState.DISABLED
    snapshot: McpDiscoverySnapshot | None = None
    last_error: str | None = None
    version: int = 0
    semaphore: asyncio.Semaphore | None = None
    refresh_task: asyncio.Task[None] | None = None
    pending_refresh: bool = False
    reconnect_task: asyncio.Task[None] | None = None
    reconnect_attempt: int = 0


def _default_client_factory(server_id: str, config: McpServerConfig) -> McpClient:
    return StreamableHttpMcpClient(server_id, config)


class McpHub:
    """Owns MCP connections for one application context.

    Construction performs no I/O so ``SharedContext.__init__`` stays synchronous;
    :meth:`start` is the async lifecycle entry point used by both the CLI chat
    loop and the long-running server.
    """

    def __init__(
        self,
        config: "Config",
        *,
        client_factory: ClientFactory | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self._config = config
        self._client_factory = client_factory or _default_client_factory
        self._clock = clock
        self._sleep = sleep or asyncio.sleep
        self._jitter = jitter
        self._servers: dict[str, _ServerRuntime] = {}
        self._started = False
        self._shutting_down = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._reconcile_task: asyncio.Task[None] | None = None
        self._reconcile_pending = False

    # ---- introspection -------------------------------------------------

    @property
    def is_configured(self) -> bool:
        """True when a non-empty ``mcp:`` block exists in config."""
        mcp = self._config.mcp
        return mcp is not None and bool(mcp.servers)

    @property
    def is_started(self) -> bool:
        return self._started

    def server_ids(self) -> list[str]:
        return list(self._servers)

    def snapshot(self, server_id: str) -> McpDiscoverySnapshot | None:
        runtime = self._servers.get(server_id)
        return runtime.snapshot if runtime else None

    def snapshots(self) -> dict[str, McpDiscoverySnapshot]:
        """Every published snapshot, keyed by server id."""
        return {sid: runtime.snapshot for sid, runtime in self._servers.items() if runtime.snapshot is not None}

    def state(self, server_id: str) -> McpServerState:
        runtime = self._servers.get(server_id)
        return runtime.state if runtime else McpServerState.DISABLED

    def server_config(self, server_id: str) -> McpServerConfig | None:
        runtime = self._servers.get(server_id)
        return runtime.config if runtime else None

    def statuses(self) -> list[McpServerStatus]:
        """Compact status for every configured server, in config order."""
        return [self._status_for(runtime) for runtime in self._servers.values()]

    def status(self, server_id: str) -> McpServerStatus | None:
        runtime = self._servers.get(server_id)
        return self._status_for(runtime) if runtime else None

    def _status_for(self, runtime: _ServerRuntime) -> McpServerStatus:
        snapshot = runtime.snapshot
        discovered = list(snapshot.tools) if snapshot else []
        allowed = [t for t in discovered if runtime.config.is_tool_allowed(t.name)]
        info = snapshot.server_info if snapshot else None
        return McpServerStatus(
            server_id=runtime.server_id,
            enabled=runtime.config.enabled,
            required=runtime.config.required,
            state=runtime.state,
            server_name=info.name if info else None,
            server_version=info.version if info else None,
            last_discovery_at=snapshot.discovered_at if snapshot else None,
            discovered_tools=len(discovered),
            enabled_tools=len(allowed),
            quarantined_tools=len(discovered) - len(allowed),
            last_error=runtime.last_error,
        )

    # ---- lifecycle -----------------------------------------------------

    async def start(self) -> None:
        """Bring up every enabled server before agent sessions can be created.

        Raises the underlying ``McpError`` if a server marked ``required`` cannot
        be brought up; every already-started client is closed first.
        """
        if self._started:
            return
        self._started = True
        self._shutting_down = False
        self._loop = asyncio.get_running_loop()
        # The watchdog observer thread signals through this; it never performs
        # async connection work itself.
        self._config.add_reload_listener(self.request_reconcile)

        mcp = self._config.mcp
        if mcp is None:
            return

        for server_id, server_config in mcp.servers.items():
            runtime = _ServerRuntime(
                server_id=server_id,
                config=server_config,
                semaphore=asyncio.Semaphore(server_config.max_concurrent_calls),
            )
            self._servers[server_id] = runtime
            if not server_config.enabled:
                runtime.state = McpServerState.DISABLED
                logger.info("MCP server %s is disabled by config", server_id)
                continue
            try:
                await self._bring_up(runtime)
            except McpError as exc:
                self._mark_failure(runtime, exc)
                if server_config.required:
                    logger.error("Required MCP server %s failed to start: %s", server_id, exc)
                    await self.stop()
                    raise
                logger.warning("Optional MCP server %s is degraded: %s", server_id, exc)
                self._schedule_reconnect(runtime)

    async def _bring_up(self, runtime: _ServerRuntime) -> None:
        """health -> connect/initialize -> list_tools -> publish snapshot."""
        runtime.state = McpServerState.CONNECTING
        client = runtime.client
        if client is None:
            client = self._client_factory(runtime.server_id, runtime.config)
            client.set_tools_changed_callback(self._on_tools_changed)
            runtime.client = client

        await client.probe_health()
        info = await client.connect()
        tools = await client.list_tools()
        self._publish(runtime, info, tools)
        runtime.state = McpServerState.CONNECTED
        runtime.last_error = None
        # A stable connection resets the backoff ladder.
        runtime.reconnect_attempt = 0
        logger.info(
            "MCP server %s connected (%s tools discovered, %s enabled)",
            runtime.server_id,
            len(tools),
            sum(1 for t in tools if runtime.config.is_tool_allowed(t.name)),
        )

    def _publish(
        self,
        runtime: _ServerRuntime,
        info: McpServerInfo | None,
        tools: tuple[McpToolDef, ...],
    ) -> None:
        """Swap in a new immutable snapshot in one assignment."""
        runtime.version += 1
        runtime.snapshot = McpDiscoverySnapshot(
            server_id=runtime.server_id,
            version=runtime.version,
            tools=tuple(tools),
            server_info=info,
            discovered_at=self._clock(),
        )

    def _mark_failure(self, runtime: _ServerRuntime, exc: BaseException) -> None:
        runtime.state = McpServerState.FAILED if runtime.config.required else McpServerState.DEGRADED
        runtime.last_error = str(exc)
        # A degraded server publishes nothing: its capabilities disappear rather
        # than lingering as tools that would fail on every call.
        runtime.snapshot = None

    async def stop(self) -> None:
        """Close every client. Safe to call repeatedly and before ``start()``."""
        self._shutting_down = True
        await _cancel(self._reconcile_task)
        self._reconcile_task = None
        for runtime in list(self._servers.values()):
            await self._tear_down(runtime)
        self._started = False
        self._loop = None

    async def _tear_down(self, runtime: _ServerRuntime) -> None:
        """Stop all background work for one server and close its client."""
        await _cancel(runtime.refresh_task)
        runtime.refresh_task = None
        await _cancel(runtime.reconnect_task)
        runtime.reconnect_task = None

        client = runtime.client
        runtime.client = None
        runtime.snapshot = None
        runtime.state = McpServerState.DISABLED
        if client is not None:
            try:
                await client.close()
            except Exception:  # pragma: no cover - shutdown must not raise
                logger.exception("Error closing MCP client for %s", runtime.server_id)

    # ---- reconnect -----------------------------------------------------

    def _schedule_reconnect(self, runtime: _ServerRuntime) -> None:
        """Start the backoff loop for a server that is down."""
        if self._shutting_down or not runtime.config.enabled:
            return
        if runtime.reconnect_task is not None and not runtime.reconnect_task.done():
            return
        runtime.reconnect_task = asyncio.create_task(
            self._reconnect(runtime), name=f"mcp-reconnect-{runtime.server_id}"
        )

    def _backoff_delay(self, runtime: _ServerRuntime) -> float:
        """Bounded exponential backoff with jitter.

        Jitter keeps several servers that went down together from retrying in
        lockstep.
        """
        config = runtime.config
        raw = config.reconnect_initial_seconds * (2**runtime.reconnect_attempt)
        capped = min(raw, config.reconnect_max_seconds)
        # Full jitter over the lower half of the window: [0.5x, 1.0x].
        return capped * (0.5 + 0.5 * self._jitter())

    async def _reconnect(self, runtime: _ServerRuntime) -> None:
        """Retry ``health -> initialize -> list_tools`` until it succeeds.

        This restores *future* availability only. No previously failed call is
        ever replayed here.
        """
        while not self._shutting_down and runtime.config.enabled:
            delay = self._backoff_delay(runtime)
            runtime.state = McpServerState.RECONNECTING
            await self._sleep(delay)
            if self._shutting_down or not runtime.config.enabled:
                return

            client = runtime.client
            runtime.client = None
            if client is not None:
                try:
                    await client.close()
                except Exception:  # pragma: no cover - best effort
                    logger.debug("Error closing stale MCP client", exc_info=True)

            try:
                await self._bring_up(runtime)
            except McpError as exc:
                runtime.reconnect_attempt += 1
                self._mark_failure(runtime, exc)
                logger.info(
                    "MCP reconnect attempt %s for %s failed: %s",
                    runtime.reconnect_attempt,
                    runtime.server_id,
                    exc,
                )
                continue
            logger.info("MCP server %s reconnected", runtime.server_id)
            return

    # ---- configuration reconciliation ----------------------------------

    def request_reconcile(self) -> None:
        """Signal that configuration changed. Safe to call from any thread.

        The config watchdog runs outside the event loop, so this only hands a
        wake-up to the loop; all connection work happens in the loop's task.
        """
        loop = self._loop
        if loop is None or self._shutting_down:
            return
        try:
            loop.call_soon_threadsafe(self._start_reconcile)
        except RuntimeError:  # pragma: no cover - loop already closed
            logger.debug("Ignoring MCP reconcile signal: event loop is closed")

    def _start_reconcile(self) -> None:
        if self._shutting_down:
            return
        if self._reconcile_task is not None and not self._reconcile_task.done():
            self._reconcile_pending = True
            return
        self._reconcile_task = asyncio.create_task(self._reconcile(), name="mcp-reconcile")

    async def _reconcile(self) -> None:
        """Apply a configuration change to the running set of servers.

        A tool call can never race into an unconfigured endpoint: the runtime's
        config (which gates ``call_tool``) is replaced before any connection work
        starts, so a removed, disabled, or re-pointed server denies immediately
        and only then closes.
        """
        while True:
            self._reconcile_pending = False
            desired = self._config.mcp.servers if self._config.mcp else {}

            for server_id in list(self._servers):
                if server_id not in desired:
                    runtime = self._servers.pop(server_id)
                    # Deny first, close second.
                    runtime.config = runtime.config.model_copy(update={"enabled": False})
                    logger.info("MCP server %s removed from config", server_id)
                    await self._tear_down(runtime)

            for server_id, server_config in desired.items():
                await self._reconcile_one(server_id, server_config)

            if not self._reconcile_pending or self._shutting_down:
                return

    async def _reconcile_one(self, server_id: str, server_config: McpServerConfig) -> None:
        runtime = self._servers.get(server_id)

        if runtime is None:
            runtime = _ServerRuntime(
                server_id=server_id,
                config=server_config,
                semaphore=asyncio.Semaphore(server_config.max_concurrent_calls),
            )
            self._servers[server_id] = runtime
            if not server_config.enabled:
                return
            logger.info("MCP server %s added by config reload", server_id)
            # Capabilities become visible only once discovery has published a
            # snapshot, so nothing is exposed before the server is usable.
            await self._connect_or_degrade(runtime)
            return

        previous = runtime.config
        runtime.config = server_config

        if not server_config.enabled:
            if previous.enabled:
                logger.info("MCP server %s disabled by config reload", server_id)
                await self._tear_down(runtime)
                runtime.config = server_config
            return

        if _connection_identity(previous) != _connection_identity(server_config):
            logger.info("MCP server %s endpoint or auth changed; reconnecting", server_id)
            await self._tear_down(runtime)
            runtime.config = server_config
            runtime.semaphore = asyncio.Semaphore(server_config.max_concurrent_calls)
            await self._connect_or_degrade(runtime)
            return

        if not previous.enabled:
            await self._connect_or_degrade(runtime)
            return

        # Policy-only change: the live connection is fine. New sessions rebuild
        # their catalog from the current config; existing sessions keep their
        # schemas but are re-checked at invocation time.
        if previous.max_concurrent_calls != server_config.max_concurrent_calls:
            runtime.semaphore = asyncio.Semaphore(server_config.max_concurrent_calls)

    async def _connect_or_degrade(self, runtime: _ServerRuntime) -> None:
        try:
            await self._bring_up(runtime)
        except McpError as exc:
            self._mark_failure(runtime, exc)
            logger.warning("MCP server %s is degraded after reload: %s", runtime.server_id, exc)
            self._schedule_reconnect(runtime)

    async def wait_for_reconcile(self) -> None:
        """Test/ops helper: await an in-flight reconciliation, if any."""
        task = self._reconcile_task
        if task is not None and not task.done():
            await task

    # ---- tool-list change refresh --------------------------------------

    def _on_tools_changed(self, server_id: str) -> None:
        """Called from the client's notification path; must not block."""
        runtime = self._servers.get(server_id)
        if runtime is None or self._shutting_down:
            return
        if runtime.refresh_task is not None and not runtime.refresh_task.done():
            # Coalesce: one refresh in flight is enough, but do not lose an event
            # that arrived while it was running.
            runtime.pending_refresh = True
            return
        runtime.refresh_task = asyncio.create_task(self._refresh(runtime), name=f"mcp-refresh-{server_id}")

    async def _refresh(self, runtime: _ServerRuntime) -> None:
        """Re-discover and swap the snapshot atomically."""
        while True:
            runtime.pending_refresh = False
            client = runtime.client
            if client is None or not client.is_connected:
                return
            try:
                tools = await client.list_tools()
            except McpError as exc:
                logger.warning("MCP rediscovery for %s failed: %s", runtime.server_id, exc)
                runtime.last_error = str(exc)
                return
            info = runtime.snapshot.server_info if runtime.snapshot else None
            self._publish(runtime, info, tools)
            logger.info(
                "MCP server %s refreshed tool list (version %s)",
                runtime.server_id,
                runtime.version,
            )
            if not runtime.pending_refresh:
                return

    async def wait_for_refresh(self, server_id: str) -> None:
        """Test/ops helper: await an in-flight rediscovery, if any."""
        runtime = self._servers.get(server_id)
        if runtime is None:
            return
        task = runtime.refresh_task
        if task is not None and not task.done():
            await task

    # ---- invocation ----------------------------------------------------

    def is_tool_available(self, server_id: str, tool_name: str) -> bool:
        """Whether this exact tool may be invoked right now.

        Checked at call time, not only when a session's schema was built, so
        disabling a server or a tool takes effect immediately.
        """
        runtime = self._servers.get(server_id)
        if runtime is None or not runtime.config.enabled:
            return False
        if runtime.state is not McpServerState.CONNECTED:
            return False
        if not runtime.config.is_tool_allowed(tool_name):
            return False
        snapshot = runtime.snapshot
        return snapshot is not None and snapshot.tool(tool_name) is not None

    async def call_tool(
        self,
        server_id: str,
        tool_name: str,
        arguments: Mapping[str, Any] | None = None,
    ) -> McpCallResult:
        """Route one call by server id and exact remote tool name.

        Raises ``McpError`` subclasses only. Never retries.
        """
        runtime = self._servers.get(server_id)
        if runtime is None:
            raise McpNotConnectedError(f"[{server_id}] is not a configured MCP server")
        if not runtime.config.enabled:
            raise McpNotConnectedError(f"[{server_id}] is disabled")
        if not runtime.config.is_tool_allowed(tool_name):
            raise McpNotConnectedError(f"[{server_id}] tool {tool_name} is not enabled by local policy")
        client = runtime.client
        if client is None or runtime.state is not McpServerState.CONNECTED:
            raise McpNotConnectedError(f"[{server_id}] is not connected (state: {runtime.state.value})")

        semaphore = runtime.semaphore
        assert semaphore is not None
        correlation_id = uuid.uuid4().hex[:12]
        started = time.monotonic()
        async with semaphore:
            try:
                result = await client.call_tool(tool_name, arguments)
            except McpTimeoutError as exc:
                # Ambiguous: the server may have applied it. Surface, never replay.
                self._log_call(runtime, tool_name, "timeout", started, correlation_id, exc)
                raise
            except McpError as exc:
                runtime.last_error = str(exc)
                self._log_call(runtime, tool_name, "error", started, correlation_id, exc)
                self._on_call_failure(runtime, exc)
                raise
        self._log_call(
            runtime,
            tool_name,
            "remote_error" if result.is_error else "ok",
            started,
            correlation_id,
        )
        return result

    def _log_call(
        self,
        runtime: _ServerRuntime,
        tool_name: str,
        outcome: str,
        started: float,
        correlation_id: str,
        exc: BaseException | None = None,
    ) -> None:
        """Operational record for one call.

        Arguments and results are deliberately absent: they routinely carry user
        content, and the error text is already redacted upstream.
        """
        logger.info(
            "mcp call %s/%s %s in %dms",
            runtime.server_id,
            tool_name,
            outcome,
            int((time.monotonic() - started) * 1000),
            extra={
                "mcp_server_id": runtime.server_id,
                "mcp_tool": tool_name,
                "mcp_outcome": outcome,
                "mcp_latency_ms": int((time.monotonic() - started) * 1000),
                "mcp_correlation_id": correlation_id,
                "mcp_error": str(exc) if exc is not None else None,
            },
        )

    def _on_call_failure(self, runtime: _ServerRuntime, exc: McpError) -> None:
        """A lost connection should start restoring future availability."""
        client = runtime.client
        if isinstance(exc, McpNotConnectedError) or (client is not None and not client.is_connected):
            runtime.state = McpServerState.DEGRADED
            self._schedule_reconnect(runtime)


def _connection_identity(config: McpServerConfig) -> tuple:
    """The parts of a server config that require replacing the connection."""
    return (
        config.transport,
        config.url,
        config.url_env,
        config.token_env,
        config.health_url,
        config.health_path,
    )


async def _cancel(task: asyncio.Task[None] | None) -> None:
    """Cancel a background task and wait for it to unwind."""
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
