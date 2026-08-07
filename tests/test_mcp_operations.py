"""Operations tests: /mcp diagnostics, reconnect backoff, and hot reload."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.commands.handlers import McpCommand
from provider.mcp.errors import McpConnectionError, McpNotConnectedError
from provider.mcp.hub import McpHub, McpServerState
from provider.mcp.models import McpServerInfo
from tests.helpers import make_context, make_workspace
from tests.mcp_fakes import FakeMcpClient, fake_tool
from utils.config import Config
from utils.mcp_config import McpConfig

TOKEN = "operations-secret-token-1234"


def _server(**overrides) -> dict:
    base = {
        "url": "http://movies-db:8765/mcp",
        "health_path": "/health",
        "allowed_tools": ["alpha"],
        "reconnect_initial_seconds": 1.0,
        "reconnect_max_seconds": 30.0,
    }
    base.update(overrides)
    return base


class _FakeClock:
    """Records how long each reconnect waited without actually sleeping."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.delays.append(seconds)
        await asyncio.sleep(0)


def _session(context):
    return SimpleNamespace(shared_context=context)


async def _hub(
    workspace: Path,
    servers: dict,
    clients: dict,
    **hub_kwargs,
):
    context = make_context(workspace)
    context.config.mcp = McpConfig.model_validate({"servers": servers})
    context.mcp_hub = McpHub(
        context.config,
        client_factory=lambda sid, cfg: clients[sid],
        **hub_kwargs,
    )
    await context.mcp_hub.start()
    return context


class McpCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_servers_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            context = make_context(workspace)
            await context.mcp_hub.start()
            out = await McpCommand().execute("", _session(context))
            self.assertEqual(out, "No MCP servers configured.")
            await context.mcp_hub.stop()

    async def test_healthy_server_summary(self) -> None:
        client = FakeMcpClient(
            "movies_db",
            tools=(fake_tool("movies_db", "alpha"), fake_tool("movies_db", "hidden")),
            info=McpServerInfo(server_id="movies_db", name="movies", version="3.1"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            context = await _hub(workspace, {"movies_db": _server()}, {"movies_db": client})
            out = await McpCommand().execute("", _session(context))
            self.assertIn("`movies_db`", out)
            self.assertIn("connected", out)
            self.assertIn("optional", out)
            self.assertIn("1/2 enabled", out)
            self.assertIn("1 quarantined", out)
            await context.mcp_hub.stop()

    async def test_failed_server_reports_a_safe_error_summary(self) -> None:
        client = FakeMcpClient(
            "movies_db",
            health_error=McpConnectionError("[movies_db] health probe failed: HTTP 503"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            context = await _hub(
                workspace,
                {"movies_db": _server(token_env="MOVIES_DB_TOKEN")},
                {"movies_db": client},
            )
            out = await McpCommand().execute("", _session(context))
            self.assertIn("last error", out)
            self.assertIn("503", out)
            await context.mcp_hub.stop()

    async def test_detail_view_lists_enabled_and_quarantined_tools(self) -> None:
        client = FakeMcpClient(
            "movies_db",
            tools=(
                fake_tool("movies_db", "alpha"),
                fake_tool("movies_db", "delete_everything"),
            ),
            info=McpServerInfo(server_id="movies_db", name="movies", version="3.1"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            context = await _hub(workspace, {"movies_db": _server()}, {"movies_db": client})
            out = await McpCommand().execute("movies_db", _session(context))
            self.assertIn("**State:** connected", out)
            self.assertIn("movies 3.1", out)
            self.assertIn("**Enabled tools:** `alpha`", out)
            self.assertIn("`delete_everything`", out.split("**Quarantined:**")[1])
            self.assertIn("**Last discovery:**", out)
            await context.mcp_hub.stop()

    async def test_unknown_server_id(self) -> None:
        client = FakeMcpClient("movies_db", tools=(fake_tool("movies_db", "alpha"),))
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            context = await _hub(workspace, {"movies_db": _server()}, {"movies_db": client})
            out = await McpCommand().execute("nope", _session(context))
            self.assertIn("not found", out)
            self.assertIn("`movies_db`", out)
            await context.mcp_hub.stop()

    async def test_output_never_contains_credentials_or_urls(self) -> None:
        client = FakeMcpClient(
            "movies_db",
            tools=(fake_tool("movies_db", "alpha"),),
            info=McpServerInfo(server_id="movies_db", name="movies", version="3.1"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            context = await _hub(
                workspace,
                {"movies_db": _server(token_env="MOVIES_DB_TOKEN")},
                {"movies_db": client},
            )
            for args in ("", "movies_db"):
                out = await McpCommand().execute(args, _session(context))
                self.assertNotIn(TOKEN, out)
                self.assertNotIn("Bearer", out)
                self.assertNotIn("http://", out)
                self.assertNotIn("MOVIES_DB_TOKEN", out)
            await context.mcp_hub.stop()

    def test_command_is_registered(self) -> None:
        from core.commands.registry import CommandRegistry

        resolved = CommandRegistry.with_builtins().resolve("/mcp movies_db")
        self.assertIsNotNone(resolved)
        command, args = resolved
        self.assertEqual(command.name, "mcp")
        self.assertEqual(args, "movies_db")


class ReconnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_backoff_is_bounded_and_jittered(self) -> None:
        clock = _FakeClock()
        client = FakeMcpClient("movies_db", health_error=McpConnectionError("[movies_db] down"))
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            context = await _hub(
                workspace,
                {"movies_db": _server(reconnect_initial_seconds=1, reconnect_max_seconds=8)},
                {"movies_db": client},
                sleep=clock.sleep,
                jitter=lambda: 1.0,  # deterministic upper bound of the jitter window
            )
            # Let several attempts run, then stop.
            for _ in range(12):
                await asyncio.sleep(0)
            await context.mcp_hub.stop()

            self.assertGreaterEqual(len(clock.delays), 3)
            # Exponential, then capped.
            self.assertEqual(clock.delays[0], 1.0)
            self.assertEqual(clock.delays[1], 2.0)
            self.assertEqual(clock.delays[2], 4.0)
            for delay in clock.delays:
                self.assertLessEqual(delay, 8.0)

    async def test_jitter_shrinks_the_delay_window(self) -> None:
        clock = _FakeClock()
        client = FakeMcpClient("movies_db", health_error=McpConnectionError("[movies_db] down"))
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            context = await _hub(
                workspace,
                {"movies_db": _server(reconnect_initial_seconds=4)},
                {"movies_db": client},
                sleep=clock.sleep,
                jitter=lambda: 0.0,  # lower bound
            )
            for _ in range(4):
                await asyncio.sleep(0)
            await context.mcp_hub.stop()
            self.assertEqual(clock.delays[0], 2.0)  # 4 * (0.5 + 0)

    async def test_reconnect_repeats_health_then_initialize_then_list(self) -> None:
        order: list[str] = []

        class OrderedClient(FakeMcpClient):
            async def probe_health(self):
                order.append("health")
                if self.health_error is not None:
                    error, self.health_error = self.health_error, None
                    raise error

            async def connect(self):
                order.append("initialize")
                return await super().connect()

            async def list_tools(self):
                order.append("list_tools")
                return await super().list_tools()

        clock = _FakeClock()
        client = OrderedClient(
            "movies_db",
            tools=(fake_tool("movies_db", "alpha"),),
            health_error=McpConnectionError("[movies_db] first probe fails"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            context = await _hub(workspace, {"movies_db": _server()}, {"movies_db": client}, sleep=clock.sleep)
            for _ in range(6):
                await asyncio.sleep(0)

            self.assertEqual(order, ["health", "health", "initialize", "list_tools"])
            self.assertEqual(context.mcp_hub.state("movies_db"), McpServerState.CONNECTED)
            await context.mcp_hub.stop()

    async def test_backoff_resets_after_a_stable_connection(self) -> None:
        clock = _FakeClock()

        class FlakyClient(FakeMcpClient):
            failures = 2

            async def probe_health(self):
                self.health_probes += 1
                if self.failures > 0:
                    self.failures -= 1
                    raise McpConnectionError("[movies_db] flaky")

        client = FlakyClient("movies_db", tools=(fake_tool("movies_db", "alpha"),))
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            context = await _hub(
                workspace,
                {"movies_db": _server(reconnect_initial_seconds=1)},
                {"movies_db": client},
                sleep=clock.sleep,
                jitter=lambda: 1.0,
            )
            for _ in range(8):
                await asyncio.sleep(0)
            runtime = context.mcp_hub._servers["movies_db"]
            self.assertEqual(runtime.state, McpServerState.CONNECTED)
            self.assertEqual(runtime.reconnect_attempt, 0)
            await context.mcp_hub.stop()

    async def test_reconnect_never_replays_a_failed_call(self) -> None:
        clock = _FakeClock()
        client = FakeMcpClient(
            "movies_db",
            tools=(fake_tool("movies_db", "alpha"),),
            call_error=McpNotConnectedError("[movies_db] stream lost"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            context = await _hub(workspace, {"movies_db": _server()}, {"movies_db": client}, sleep=clock.sleep)
            with self.assertRaises(McpNotConnectedError):
                await context.mcp_hub.call_tool("movies_db", "alpha", {"q": "x"})

            # A reconnect may run, but the failed call is never repeated.
            for _ in range(6):
                await asyncio.sleep(0)
            self.assertEqual(client.calls, [("alpha", {"q": "x"})])
            await context.mcp_hub.stop()

    async def test_shutdown_cancels_reconnect_work(self) -> None:
        clock = _FakeClock()
        client = FakeMcpClient("movies_db", health_error=McpConnectionError("[movies_db] down"))
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            context = await _hub(workspace, {"movies_db": _server()}, {"movies_db": client}, sleep=clock.sleep)
            await asyncio.sleep(0)
            runtime = context.mcp_hub._servers["movies_db"]
            self.assertIsNotNone(runtime.reconnect_task)

            await context.mcp_hub.stop()
            self.assertIsNone(runtime.reconnect_task)
            before = len(clock.delays)
            for _ in range(5):
                await asyncio.sleep(0)
            self.assertEqual(len(clock.delays), before)

    async def test_optional_outage_stays_isolated(self) -> None:
        clock = _FakeClock()
        broken = FakeMcpClient("notes_db", health_error=McpConnectionError("[notes_db] down"))
        good = FakeMcpClient("movies_db", tools=(fake_tool("movies_db", "alpha"),))
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            context = await _hub(
                workspace,
                {
                    "movies_db": _server(),
                    "notes_db": _server(url="https://notes/mcp"),
                },
                {"movies_db": good, "notes_db": broken},
                sleep=clock.sleep,
            )
            for _ in range(4):
                await asyncio.sleep(0)
            self.assertEqual(context.mcp_hub.state("movies_db"), McpServerState.CONNECTED)
            result = await context.mcp_hub.call_tool("movies_db", "alpha")
            self.assertFalse(result.is_error)
            await context.mcp_hub.stop()


class HotReloadTests(unittest.IsolatedAsyncioTestCase):
    async def _reload_with(self, context, servers: dict) -> None:
        """Simulate a config file change reaching the hub through the watchdog."""
        context.config.mcp = McpConfig.model_validate({"servers": servers}) if servers is not None else None
        context.mcp_hub.request_reconcile()
        # `request_reconcile` only signals; let the loop run the task.
        await asyncio.sleep(0)
        await context.mcp_hub.wait_for_reconcile()

    async def test_added_server_connects_before_publishing_capabilities(self) -> None:
        movies = FakeMcpClient("movies_db", tools=(fake_tool("movies_db", "alpha"),))
        notes = FakeMcpClient("notes_db", tools=(fake_tool("notes_db", "alpha"),))
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            context = await _hub(
                workspace,
                {"movies_db": _server()},
                {"movies_db": movies, "notes_db": notes},
            )
            self.assertNotIn("notes_db", context.mcp_hub.snapshots())

            await self._reload_with(
                context,
                {"movies_db": _server(), "notes_db": _server(url="https://notes/mcp")},
            )
            self.assertEqual(context.mcp_hub.state("notes_db"), McpServerState.CONNECTED)
            self.assertIn("notes_db", context.mcp_hub.snapshots())
            self.assertTrue(context.mcp_hub.is_tool_available("notes_db", "alpha"))
            await context.mcp_hub.stop()

    async def test_disabled_server_denies_calls_then_closes(self) -> None:
        client = FakeMcpClient("movies_db", tools=(fake_tool("movies_db", "alpha"),))
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            context = await _hub(workspace, {"movies_db": _server()}, {"movies_db": client})
            await self._reload_with(context, {"movies_db": _server(enabled=False)})

            self.assertFalse(context.mcp_hub.is_tool_available("movies_db", "alpha"))
            with self.assertRaises(McpNotConnectedError):
                await context.mcp_hub.call_tool("movies_db", "alpha")
            self.assertEqual(client.close_count, 1)
            self.assertEqual(client.calls, [])
            await context.mcp_hub.stop()

    async def test_removed_server_is_torn_down(self) -> None:
        client = FakeMcpClient("movies_db", tools=(fake_tool("movies_db", "alpha"),))
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            context = await _hub(workspace, {"movies_db": _server()}, {"movies_db": client})
            await self._reload_with(context, {})

            self.assertEqual(context.mcp_hub.server_ids(), [])
            self.assertEqual(context.mcp_hub.snapshots(), {})
            self.assertEqual(client.close_count, 1)
            with self.assertRaises(McpNotConnectedError):
                await context.mcp_hub.call_tool("movies_db", "alpha")
            await context.mcp_hub.stop()

    async def test_endpoint_change_replaces_the_connection(self) -> None:
        first = FakeMcpClient("movies_db", tools=(fake_tool("movies_db", "alpha"),))
        second = FakeMcpClient("movies_db", tools=(fake_tool("movies_db", "alpha"),))
        built: list[FakeMcpClient] = []

        def factory(server_id, config):
            client = first if not built else second
            built.append(client)
            return client

        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            context = make_context(workspace)
            context.config.mcp = McpConfig.model_validate({"servers": {"movies_db": _server()}})
            context.mcp_hub = McpHub(context.config, client_factory=factory)
            await context.mcp_hub.start()

            await self._reload_with(context, {"movies_db": _server(url="https://movies-db.internal/mcp")})
            self.assertEqual(first.close_count, 1)
            self.assertTrue(second.connected)
            self.assertEqual(context.mcp_hub.state("movies_db"), McpServerState.CONNECTED)
            await context.mcp_hub.stop()

    async def test_policy_only_change_keeps_the_connection(self) -> None:
        client = FakeMcpClient(
            "movies_db",
            tools=(fake_tool("movies_db", "alpha"), fake_tool("movies_db", "beta")),
        )
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            context = await _hub(
                workspace,
                {"movies_db": _server(allowed_tools=["alpha"])},
                {"movies_db": client},
            )
            self.assertFalse(context.mcp_hub.is_tool_available("movies_db", "beta"))

            await self._reload_with(context, {"movies_db": _server(allowed_tools=["alpha", "beta"])})
            # No reconnect: the same client, still connected.
            self.assertEqual(client.close_count, 0)
            self.assertEqual(client.connect_calls, 1)
            self.assertTrue(context.mcp_hub.is_tool_available("movies_db", "beta"))
            await context.mcp_hub.stop()

    async def test_newly_discovered_tool_stays_quarantined_after_reload(self) -> None:
        client = FakeMcpClient("movies_db", tools=(fake_tool("movies_db", "alpha"),))
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            context = await _hub(workspace, {"movies_db": _server()}, {"movies_db": client})
            client.tools = (
                fake_tool("movies_db", "alpha"),
                fake_tool("movies_db", "server_added"),
            )
            client.emit_tools_changed()
            await context.mcp_hub.wait_for_refresh("movies_db")
            await self._reload_with(context, {"movies_db": _server()})

            self.assertTrue(context.mcp_hub.is_tool_available("movies_db", "alpha"))
            self.assertFalse(context.mcp_hub.is_tool_available("movies_db", "server_added"))
            await context.mcp_hub.stop()

    async def test_watchdog_thread_handoff_does_no_async_work_inline(self) -> None:
        """`request_reconcile` must only signal; it may be called off-loop."""
        client = FakeMcpClient("movies_db", tools=(fake_tool("movies_db", "alpha"),))
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            context = await _hub(workspace, {"movies_db": _server()}, {"movies_db": client})
            context.config.mcp = McpConfig.model_validate({"servers": {}})

            done = asyncio.Event()
            loop = asyncio.get_running_loop()

            def from_other_thread() -> None:
                context.mcp_hub.request_reconcile()
                loop.call_soon_threadsafe(done.set)

            await asyncio.to_thread(from_other_thread)
            await done.wait()
            await asyncio.sleep(0)
            await context.mcp_hub.wait_for_reconcile()

            self.assertEqual(context.mcp_hub.server_ids(), [])
            await context.mcp_hub.stop()

    async def test_config_reload_notifies_registered_listeners(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            config = Config.load(workspace)
            fired: list[int] = []
            config.add_reload_listener(lambda: fired.append(1))
            self.assertTrue(config.reload())
            self.assertEqual(fired, [1])

    async def test_a_failing_listener_does_not_break_reload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            config = Config.load(workspace)

            def boom() -> None:
                raise RuntimeError("listener exploded")

            fired: list[int] = []
            config.add_reload_listener(boom)
            config.add_reload_listener(lambda: fired.append(1))
            self.assertTrue(config.reload())
            self.assertEqual(fired, [1])

    async def test_reconcile_signal_is_ignored_after_shutdown(self) -> None:
        client = FakeMcpClient("movies_db", tools=(fake_tool("movies_db", "alpha"),))
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            context = await _hub(workspace, {"movies_db": _server()}, {"movies_db": client})
            await context.mcp_hub.stop()
            context.mcp_hub.request_reconcile()  # must not raise or start work
            await asyncio.sleep(0)
            self.assertIsNone(context.mcp_hub._reconcile_task)


if __name__ == "__main__":
    unittest.main()
