"""Lifecycle tests for the process-level MCP hub."""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path

from provider.mcp.errors import (
    McpAuthError,
    McpConnectionError,
    McpLoginRequiredError,
    McpNotConnectedError,
    McpTimeoutError,
)
from provider.mcp.hub import McpHub, McpServerState
from provider.mcp.models import McpServerInfo
from provider.mcp.oauth_store import OAuthGrant, OAuthStore
from tests.helpers import make_workspace
from tests.mcp_fakes import FakeMcpClient, fake_tool
from utils.config import Config
from utils.mcp_config import McpConfig


def _config(workspace: Path, servers: dict) -> Config:
    config = Config.load(workspace)
    config.mcp = McpConfig.model_validate({"servers": servers})
    return config


def _server(**overrides) -> dict:
    base = {
        "url": "http://movies-db:8765/mcp",
        "health_path": "/health",
        "allowed_tools": ["alpha"],
    }
    base.update(overrides)
    return base


class _FactoryRecorder:
    """Client factory returning pre-seeded fakes and recording construction."""

    def __init__(self, clients: dict[str, FakeMcpClient]) -> None:
        self.clients = clients
        self.built: list[str] = []

    def __call__(self, server_id: str, config) -> FakeMcpClient:
        self.built.append(server_id)
        return self.clients[server_id]


class HubStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_mcp_config_is_a_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            hub = McpHub(Config.load(workspace))
            self.assertFalse(hub.is_configured)
            await hub.start()
            self.assertEqual(hub.server_ids(), [])
            self.assertEqual(hub.snapshots(), {})
            await hub.stop()

    async def test_health_probe_precedes_initialize_and_list_tools(self) -> None:
        order: list[str] = []

        class OrderedClient(FakeMcpClient):
            async def probe_health(self):
                order.append("health")
                await super().probe_health()

            async def connect(self):
                order.append("initialize")
                return await super().connect()

            async def list_tools(self):
                order.append("list_tools")
                return await super().list_tools()

        client = OrderedClient("movies_db", tools=(fake_tool("movies_db", "alpha"),))
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            hub = McpHub(
                _config(workspace, {"movies_db": _server()}),
                client_factory=_FactoryRecorder({"movies_db": client}),
            )
            await hub.start()
            self.assertEqual(order, ["health", "initialize", "list_tools"])
            await hub.stop()

    async def test_successful_startup_publishes_immutable_snapshot(self) -> None:
        client = FakeMcpClient(
            "movies_db",
            tools=(fake_tool("movies_db", "alpha"), fake_tool("movies_db", "hidden")),
            info=McpServerInfo(server_id="movies_db", name="movies", version="2.0"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            hub = McpHub(
                _config(workspace, {"movies_db": _server()}),
                client_factory=_FactoryRecorder({"movies_db": client}),
            )
            await hub.start()
            snapshot = hub.snapshot("movies_db")
            self.assertEqual(snapshot.version, 1)
            self.assertEqual([t.name for t in snapshot.tools], ["alpha", "hidden"])
            self.assertEqual(snapshot.server_info.version, "2.0")
            self.assertIsInstance(snapshot.tools, tuple)
            self.assertEqual(hub.state("movies_db"), McpServerState.CONNECTED)

            status = hub.status("movies_db")
            self.assertEqual(status.discovered_tools, 2)
            self.assertEqual(status.enabled_tools, 1)
            self.assertEqual(status.quarantined_tools, 1)
            await hub.stop()

    async def test_disabled_server_is_never_connected(self) -> None:
        client = FakeMcpClient("movies_db")
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            hub = McpHub(
                _config(workspace, {"movies_db": _server(enabled=False)}),
                client_factory=_FactoryRecorder({"movies_db": client}),
            )
            await hub.start()
            self.assertEqual(hub.state("movies_db"), McpServerState.DISABLED)
            self.assertEqual(client.connect_calls, 0)
            self.assertIsNone(hub.snapshot("movies_db"))
            await hub.stop()

    async def test_failed_health_probe_skips_discovery_and_degrades(self) -> None:
        client = FakeMcpClient("movies_db", health_error=McpConnectionError("[movies_db] health probe failed"))
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            hub = McpHub(
                _config(workspace, {"movies_db": _server()}),
                client_factory=_FactoryRecorder({"movies_db": client}),
            )
            await hub.start()
            self.assertEqual(hub.state("movies_db"), McpServerState.DEGRADED)
            self.assertEqual(client.connect_calls, 0)
            self.assertEqual(client.list_calls, 0)
            self.assertIsNone(hub.snapshot("movies_db"))
            await hub.stop()

    async def test_optional_server_failure_leaves_others_healthy(self) -> None:
        broken = FakeMcpClient("notes_db", connect_error=McpAuthError("[notes_db] bad token"))
        good = FakeMcpClient("movies_db", tools=(fake_tool("movies_db", "alpha"),))
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            hub = McpHub(
                _config(
                    workspace,
                    {"movies_db": _server(), "notes_db": _server(url="https://notes/mcp")},
                ),
                client_factory=_FactoryRecorder({"movies_db": good, "notes_db": broken}),
            )
            await hub.start()  # must not raise
            self.assertEqual(hub.state("movies_db"), McpServerState.CONNECTED)
            self.assertEqual(hub.state("notes_db"), McpServerState.DEGRADED)
            self.assertIn("bad token", hub.status("notes_db").last_error)
            await hub.stop()

    async def test_required_server_failure_fails_startup_and_closes_clients(self) -> None:
        good = FakeMcpClient("movies_db", tools=(fake_tool("movies_db", "alpha"),))
        broken = FakeMcpClient("notes_db", connect_error=McpConnectionError("[notes_db] refused"))
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            hub = McpHub(
                _config(
                    workspace,
                    {
                        "movies_db": _server(),
                        "notes_db": _server(url="https://notes/mcp", required=True),
                    },
                ),
                client_factory=_FactoryRecorder({"movies_db": good, "notes_db": broken}),
            )
            with self.assertRaises(McpConnectionError):
                await hub.start()
            # The healthy server was closed as part of the failed startup.
            self.assertEqual(good.close_count, 1)
            self.assertFalse(good.connected)

    async def test_start_is_idempotent(self) -> None:
        client = FakeMcpClient("movies_db", tools=(fake_tool("movies_db", "alpha"),))
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            hub = McpHub(
                _config(workspace, {"movies_db": _server()}),
                client_factory=_FactoryRecorder({"movies_db": client}),
            )
            await hub.start()
            await hub.start()
            self.assertEqual(client.connect_calls, 1)
            await hub.stop()


class HubShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_called_exactly_once_per_started_client(self) -> None:
        a = FakeMcpClient("movies_db", tools=(fake_tool("movies_db", "alpha"),))
        b = FakeMcpClient("notes_db", tools=(fake_tool("notes_db", "alpha"),))
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            hub = McpHub(
                _config(
                    workspace,
                    {"movies_db": _server(), "notes_db": _server(url="https://notes/mcp")},
                ),
                client_factory=_FactoryRecorder({"movies_db": a, "notes_db": b}),
            )
            await hub.start()
            await hub.stop()
            self.assertEqual((a.close_count, b.close_count), (1, 1))
            # A second stop must not double-close.
            await hub.stop()
            self.assertEqual((a.close_count, b.close_count), (1, 1))

    async def test_stop_before_start_is_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            hub = McpHub(_config(workspace, {"movies_db": _server()}))
            await hub.stop()

    async def test_shutdown_cancels_in_flight_refresh(self) -> None:
        client = FakeMcpClient("movies_db", tools=(fake_tool("movies_db", "alpha"),))
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            hub = McpHub(
                _config(workspace, {"movies_db": _server()}),
                client_factory=_FactoryRecorder({"movies_db": client}),
            )
            await hub.start()

            slow = asyncio.Event()

            async def slow_list():
                await slow.wait()
                return client.tools

            client.list_tools = slow_list  # type: ignore[assignment]
            client.emit_tools_changed()
            await asyncio.sleep(0)
            await hub.stop()
            self.assertEqual(client.close_count, 1)


class HubInvocationTests(unittest.IsolatedAsyncioTestCase):
    async def _hub(self, workspace: Path, client: FakeMcpClient, **server_overrides):
        hub = McpHub(
            _config(workspace, {"movies_db": _server(**server_overrides)}),
            client_factory=_FactoryRecorder({"movies_db": client}),
        )
        await hub.start()
        return hub

    async def test_call_routes_by_server_and_exact_name(self) -> None:
        client = FakeMcpClient("movies_db", tools=(fake_tool("movies_db", "alpha"),))
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            hub = await self._hub(workspace, client)
            result = await hub.call_tool("movies_db", "alpha", {"q": "x"})
            self.assertEqual(result.text_blocks, ("alpha ok",))
            self.assertEqual(client.calls, [("alpha", {"q": "x"})])
            await hub.stop()

    async def test_unknown_server_and_tool_are_rejected(self) -> None:
        client = FakeMcpClient("movies_db", tools=(fake_tool("movies_db", "alpha"),))
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            hub = await self._hub(workspace, client)
            with self.assertRaises(McpNotConnectedError):
                await hub.call_tool("nope", "alpha")
            with self.assertRaises(McpNotConnectedError):
                await hub.call_tool("movies_db", "not_allowed")
            self.assertEqual(client.calls, [])
            await hub.stop()

    async def test_disabling_a_server_denies_calls_immediately(self) -> None:
        client = FakeMcpClient("movies_db", tools=(fake_tool("movies_db", "alpha"),))
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            hub = await self._hub(workspace, client)
            self.assertTrue(hub.is_tool_available("movies_db", "alpha"))

            hub._servers["movies_db"].config = hub._servers["movies_db"].config.model_copy(update={"enabled": False})

            self.assertFalse(hub.is_tool_available("movies_db", "alpha"))
            with self.assertRaises(McpNotConnectedError):
                await hub.call_tool("movies_db", "alpha")
            await hub.stop()

    async def test_degraded_server_denies_calls(self) -> None:
        client = FakeMcpClient("movies_db", health_error=McpConnectionError("down"))
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            hub = await self._hub(workspace, client)
            self.assertFalse(hub.is_tool_available("movies_db", "alpha"))
            with self.assertRaises(McpNotConnectedError):
                await hub.call_tool("movies_db", "alpha")
            await hub.stop()

    async def test_per_server_concurrency_limit_is_enforced(self) -> None:
        client = FakeMcpClient("movies_db", tools=(fake_tool("movies_db", "alpha"),), call_delay=0.02)
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            hub = await self._hub(workspace, client, max_concurrent_calls=2)
            await asyncio.gather(*(hub.call_tool("movies_db", "alpha") for _ in range(6)))
            self.assertEqual(len(client.calls), 6)
            self.assertLessEqual(client.max_observed_concurrency, 2)
            await hub.stop()

    async def test_timeout_is_surfaced_and_never_replayed(self) -> None:
        client = FakeMcpClient(
            "movies_db",
            tools=(fake_tool("movies_db", "alpha"),),
            call_error=McpTimeoutError("[movies_db] call to alpha timed out"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            hub = await self._hub(workspace, client)
            with self.assertRaises(McpTimeoutError):
                await hub.call_tool("movies_db", "alpha")
            self.assertEqual(len(client.calls), 1)
            await hub.stop()


class HubRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_changed_swaps_snapshot_atomically(self) -> None:
        client = FakeMcpClient("movies_db", tools=(fake_tool("movies_db", "alpha"),))
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            hub = McpHub(
                _config(workspace, {"movies_db": _server()}),
                client_factory=_FactoryRecorder({"movies_db": client}),
            )
            await hub.start()
            first = hub.snapshot("movies_db")

            client.tools = (
                fake_tool("movies_db", "alpha"),
                fake_tool("movies_db", "brand_new"),
            )
            client.emit_tools_changed()
            await hub.wait_for_refresh("movies_db")

            second = hub.snapshot("movies_db")
            self.assertEqual(second.version, first.version + 1)
            self.assertEqual([t.name for t in second.tools], ["alpha", "brand_new"])
            # The earlier snapshot object is untouched.
            self.assertEqual([t.name for t in first.tools], ["alpha"])
            # A newly discovered tool is not allowed by local policy.
            self.assertFalse(hub.is_tool_available("movies_db", "brand_new"))
            await hub.stop()

    async def test_failed_rediscovery_keeps_previous_snapshot(self) -> None:
        client = FakeMcpClient("movies_db", tools=(fake_tool("movies_db", "alpha"),))
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            hub = McpHub(
                _config(workspace, {"movies_db": _server()}),
                client_factory=_FactoryRecorder({"movies_db": client}),
            )
            await hub.start()
            client.list_error = McpConnectionError("[movies_db] stream lost")
            client.emit_tools_changed()
            await hub.wait_for_refresh("movies_db")
            snapshot = hub.snapshot("movies_db")
            self.assertEqual(snapshot.version, 1)
            self.assertEqual([t.name for t in snapshot.tools], ["alpha"])
            await hub.stop()


class _RecordingHub:
    """Stands in for the real hub to assert lifecycle ordering."""

    def __init__(self, order: list[str], *, fail: Exception | None = None) -> None:
        self.order = order
        self.fail = fail

    async def start(self) -> None:
        self.order.append("hub.start")
        if self.fail is not None:
            raise self.fail

    async def stop(self) -> None:
        self.order.append("hub.stop")

    def set_notice_callback(self, callback) -> None:
        self.order.append("hub.set_notice_callback" if callback is not None else "hub.clear_notice_callback")

    def snapshots(self) -> dict:
        return {}

    def server_config(self, server_id: str):
        return None


class _StubReloader:
    def __init__(self, order: list[str]) -> None:
        self.order = order

    def start(self) -> None:
        self.order.append("reloader.start")

    def stop(self) -> None:
        self.order.append("reloader.stop")


class LifecycleOrderingTests(unittest.IsolatedAsyncioTestCase):
    async def test_server_starts_mcp_before_workers_and_stops_it_after(self) -> None:
        from server.server import Server
        from tests.helpers import make_context

        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            context = make_context(workspace)
            # Keep the test off the network: no uvicorn API task.
            context.config.api = None
            order: list[str] = []
            context.mcp_hub = _RecordingHub(order)

            server = Server(context)
            server.config_reloader = _StubReloader(order)
            server._setup_workers = lambda: order.append("workers.setup")
            server._start_workers = lambda: order.append("workers.start")

            async def monitor() -> None:
                order.append("monitor")
                raise asyncio.CancelledError

            server._monitor_workers = monitor

            with self.assertRaises(asyncio.CancelledError):
                await server.run()

            self.assertEqual(
                order,
                [
                    "workers.setup",
                    "hub.set_notice_callback",
                    "hub.start",
                    "workers.start",
                    "monitor",
                    "hub.stop",
                    "reloader.stop",
                ],
            )

    async def test_required_server_failure_aborts_server_startup(self) -> None:
        from server.server import Server
        from tests.helpers import make_context

        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            context = make_context(workspace)
            context.config.api = None
            order: list[str] = []
            context.mcp_hub = _RecordingHub(order, fail=McpConnectionError("[notes_db] refused"))

            server = Server(context)
            server.config_reloader = _StubReloader(order)
            server._setup_workers = lambda: order.append("workers.setup")
            server._start_workers = lambda: order.append("workers.start")

            with self.assertRaises(McpConnectionError):
                await server.run()

            # Workers never started, and the reloader thread was released.
            self.assertNotIn("workers.start", order)
            self.assertIn("reloader.stop", order)

    async def test_chat_loop_starts_mcp_before_workers_and_stops_it_after(self) -> None:
        from cli.chat import ChatLoop

        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            chat = ChatLoop(Config.load(workspace))
            order: list[str] = []
            chat.context.mcp_hub = _RecordingHub(order)
            chat.config_reloader = _StubReloader(order)
            chat.get_user_input = lambda: "exit"
            chat.console.print = lambda *a, **k: None

            started: list[str] = []
            for worker in chat.workers:
                worker.start = lambda w=worker: order.append("workers.start")

                async def _stop(w=worker):
                    started.append("stopped")

                worker.stop = _stop

            await chat.run()

            self.assertLess(order.index("hub.start"), order.index("workers.start"))
            self.assertEqual(order[-1], "hub.stop")


class _NoticeRecorder:
    """Async notice callback that records ``(server_id, kind)`` pairs."""

    def __init__(self) -> None:
        self.notices: list[tuple[str, str]] = []

    async def __call__(self, server_id: str, kind: str) -> None:
        self.notices.append((server_id, kind))


class _CountingSleep:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.delays.append(seconds)
        await asyncio.sleep(0)


async def _spin(times: int = 20) -> None:
    for _ in range(times):
        await asyncio.sleep(0)


def _oauth_server(store: Path, **overrides) -> dict:
    base = {
        "url": "https://notion.example/mcp",
        "auth": {"type": "oauth", "token_store": str(store)},
        "allowed_tools": ["alpha"],
        "reconnect_initial_seconds": 1.0,
        "reconnect_max_seconds": 60.0,
    }
    base.update(overrides)
    return base


def _login_error() -> McpLoginRequiredError:
    return McpLoginRequiredError("[notion] login required")


class HubLoginRequiredTests(unittest.IsolatedAsyncioTestCase):
    async def test_login_required_state_and_single_notice(self) -> None:
        client = FakeMcpClient("notion", connect_error=_login_error())
        sleeper = _CountingSleep()
        notices = _NoticeRecorder()
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            store = Path(tmp) / "notion.json"
            factory = _FactoryRecorder({"notion": client})
            hub = McpHub(
                _config(workspace, {"notion": _oauth_server(store)}),
                client_factory=factory,
                sleep=sleeper.sleep,
                jitter=lambda: 1.0,
            )
            hub.set_notice_callback(notices)
            await hub.start()
            await _spin()
            # A second reconnect cycle: the store changed, the grant is still dead.
            store.write_text("{}")
            os.utime(store, (1000.0, 1000.0))
            await _spin()

            self.assertEqual(factory.built, ["notion", "notion"])
            self.assertEqual(hub.state("notion"), McpServerState.LOGIN_REQUIRED)
            self.assertIsNone(hub.snapshot("notion"))
            self.assertEqual(notices.notices, [("notion", "login_required")])
            await hub.stop()

    async def test_login_required_no_bring_up_until_store_changes(self) -> None:
        client = FakeMcpClient("notion", tools=(fake_tool("notion", "alpha"),), connect_error=_login_error())
        sleeper = _CountingSleep()
        notices = _NoticeRecorder()
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            store = Path(tmp) / "notion.json"
            factory = _FactoryRecorder({"notion": client})
            hub = McpHub(
                _config(workspace, {"notion": _oauth_server(store)}),
                client_factory=factory,
                sleep=sleeper.sleep,
                jitter=lambda: 1.0,
            )
            hub.set_notice_callback(notices)
            await hub.start()
            await _spin(40)

            # Many poll cycles at max backoff, but no new client and no network.
            self.assertEqual(factory.built, ["notion"])
            self.assertEqual(client.connect_calls, 1)
            self.assertGreaterEqual(len(sleeper.delays), 3)
            self.assertTrue(all(d == 60.0 for d in sleeper.delays))
            self.assertEqual(hub.state("notion"), McpServerState.LOGIN_REQUIRED)

            # mcp-login writes the store: the next poll brings the server up.
            client.connect_error = None
            store.write_text("{}")
            await _spin()
            self.assertEqual(factory.built, ["notion", "notion"])
            self.assertEqual(hub.state("notion"), McpServerState.CONNECTED)
            self.assertTrue(hub.is_tool_available("notion", "alpha"))

            # A later, new incident (a call hits a dead grant) notifies again.
            client.call_error = _login_error()
            with self.assertRaises(McpLoginRequiredError):
                await hub.call_tool("notion", "alpha", {})
            await _spin()
            self.assertEqual(hub.state("notion"), McpServerState.LOGIN_REQUIRED)
            self.assertEqual(notices.notices, [("notion", "login_required")] * 2)
            await hub.stop()

    async def test_refresh_cap_warning_once(self) -> None:
        now = 1_800_000_000.0
        day = 86400.0
        client = FakeMcpClient("notion", tools=(fake_tool("notion", "alpha"),))
        sleeper = _CountingSleep()
        notices = _NoticeRecorder()
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            store = Path(tmp) / "notion.json"
            OAuthStore(store).save(_grant(granted_at=now - 167 * day))
            hub = McpHub(
                _config(workspace, {"notion": _oauth_server(store)}),
                client_factory=_FactoryRecorder({"notion": client}),
                clock=lambda: now,
                sleep=sleeper.sleep,
            )
            hub.set_notice_callback(notices)
            await hub.start()
            await _spin()
            self.assertEqual(notices.notices, [("notion", "refresh_cap_soon")])

            # A second bring-up for the same grant does not warn again.
            client.call_error = McpNotConnectedError("[notion] stream lost")
            with self.assertRaises(McpNotConnectedError):
                await hub.call_tool("notion", "alpha", {})
            client.call_error = None
            await _spin()
            self.assertEqual(hub.state("notion"), McpServerState.CONNECTED)
            self.assertEqual(client.connect_calls, 2)
            self.assertEqual(notices.notices, [("notion", "refresh_cap_soon")])
            await hub.stop()

    async def test_refresh_cap_not_warned_for_recent_grant(self) -> None:
        now = 1_800_000_000.0
        client = FakeMcpClient("notion", tools=(fake_tool("notion", "alpha"),))
        notices = _NoticeRecorder()
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            store = Path(tmp) / "notion.json"
            OAuthStore(store).save(_grant(granted_at=now - 165 * 86400.0))
            hub = McpHub(
                _config(workspace, {"notion": _oauth_server(store)}),
                client_factory=_FactoryRecorder({"notion": client}),
                clock=lambda: now,
            )
            hub.set_notice_callback(notices)
            await hub.start()
            await _spin()
            self.assertEqual(notices.notices, [])
            await hub.stop()

    async def test_other_servers_unaffected(self) -> None:
        notion = FakeMcpClient("notion", connect_error=_login_error())
        movies = FakeMcpClient("movies_db", tools=(fake_tool("movies_db", "alpha"),))
        sleeper = _CountingSleep()
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            store = Path(tmp) / "notion.json"
            hub = McpHub(
                _config(workspace, {"notion": _oauth_server(store), "movies_db": _server()}),
                client_factory=_FactoryRecorder({"notion": notion, "movies_db": movies}),
                sleep=sleeper.sleep,
            )
            hub.set_notice_callback(_NoticeRecorder())
            await hub.start()
            await _spin()
            self.assertEqual(hub.state("notion"), McpServerState.LOGIN_REQUIRED)
            self.assertEqual(hub.state("movies_db"), McpServerState.CONNECTED)
            self.assertTrue(hub.is_tool_available("movies_db", "alpha"))
            result = await hub.call_tool("movies_db", "alpha", {})
            self.assertFalse(result.is_error)
            await hub.stop()

    @unittest.skipIf(os.geteuid() == 0, "root ignores directory permissions")
    async def test_unreadable_token_store_does_not_abort_startup(self) -> None:
        from provider.mcp.client import StreamableHttpMcpClient
        from tests.mcp_fakes import FakeMcpHttpServer, tool_spec

        movies = FakeMcpClient("movies_db", tools=(fake_tool("movies_db", "alpha"),))
        notion_server = FakeMcpHttpServer(tools=[tool_spec("alpha")])

        def factory(server_id: str, config):
            if server_id == "movies_db":
                return movies
            return StreamableHttpMcpClient(server_id, config, env={}, transport_factory=notion_server.transport)

        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            store_dir = Path(tmp) / "secrets"
            store_dir.mkdir()
            (store_dir / "notion.json").write_text("{}")
            os.chmod(store_dir, 0)
            try:
                hub = McpHub(
                    _config(workspace, {"notion": _oauth_server(store_dir / "notion.json"), "movies_db": _server()}),
                    client_factory=factory,
                    sleep=_CountingSleep().sleep,
                )
                hub.set_notice_callback(_NoticeRecorder())
                with self.assertLogs("provider.mcp.oauth_store", level="WARNING") as logs:
                    await hub.start()
                    await _spin()
                self.assertIn(hub.state("notion"), (McpServerState.LOGIN_REQUIRED, McpServerState.DEGRADED))
                self.assertEqual(hub.state("movies_db"), McpServerState.CONNECTED)
                reconnect = hub._servers["notion"].reconnect_task
                self.assertIsNotNone(reconnect)
                self.assertFalse(reconnect.done())
                self.assertTrue(all("{}" not in line for line in logs.output))
                self.assertEqual(notion_server.requests, [])
                await hub.stop()
            finally:
                os.chmod(store_dir, 0o700)

    async def test_failing_notice_callback_is_logged_not_raised(self) -> None:
        client = FakeMcpClient("notion", connect_error=_login_error())

        async def broken(server_id: str, kind: str) -> None:
            raise RuntimeError("telegram down")

        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            hub = McpHub(
                _config(workspace, {"notion": _oauth_server(Path(tmp) / "notion.json")}),
                client_factory=_FactoryRecorder({"notion": client}),
                sleep=_CountingSleep().sleep,
            )
            hub.set_notice_callback(broken)
            with self.assertLogs("provider.mcp.hub", level="ERROR"):
                await hub.start()
                await _spin()
            self.assertEqual(hub.state("notion"), McpServerState.LOGIN_REQUIRED)
            await hub.stop()

    async def test_mcp_command_shows_login_hint(self) -> None:
        from types import SimpleNamespace

        from core.commands.handlers import McpCommand
        from tests.helpers import make_context

        client = FakeMcpClient("notion", connect_error=_login_error())
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            context = make_context(workspace)
            context.config.mcp = McpConfig.model_validate(
                {"servers": {"notion": _oauth_server(Path(tmp) / "notion.json")}}
            )
            context.mcp_hub = McpHub(
                context.config,
                client_factory=_FactoryRecorder({"notion": client}),
                sleep=_CountingSleep().sleep,
            )
            await context.mcp_hub.start()
            session = SimpleNamespace(shared_context=context)
            for args in ("", "notion"):
                out = await McpCommand().execute(args, session)
                self.assertIn("login_required", out)
                self.assertIn("build-bot mcp-login notion", out)
            await context.mcp_hub.stop()


def _grant(*, granted_at: float) -> OAuthGrant:
    return OAuthGrant(
        client_id="client",
        client_secret=None,
        redirect_uri="http://127.0.0.1:33418/callback",
        token_endpoint="https://notion.example/token",
        resource="https://notion.example/mcp",
        access_token="access",
        refresh_token="refresh",
        expires_at=granted_at + 3600,
        granted_at=granted_at,
    )


if __name__ == "__main__":
    unittest.main()
