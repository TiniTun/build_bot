"""Host-only MCP servers: reachable by the engine, invisible to every agent.

``expose_to_agents: False`` makes the health_planner privacy guarantee
structural rather than conventional: ``core/planning/engine.py`` already
projects the readiness document through a strict allowlist before it reaches
a transcript, but that projection can be bypassed entirely if the raw MCP
tool ever becomes an agent-visible capability. This flag stops that at the
bridge, before any capability/tool pair is built — see
``tools/mcp_tools.py::build_mcp_capabilities``.
"""

import tempfile
import unittest
from pathlib import Path

from provider.mcp.hub import McpHub
from tests.helpers import make_context, make_workspace
from tests.mcp_fakes import FakeMcpClient, fake_tool
from tools.mcp_tools import build_mcp_capabilities
from utils.mcp_config import McpConfig, McpServerConfig


async def _context_with_server(workspace: Path, server_id: str, config: McpServerConfig, client: FakeMcpClient):
    """Build a connected ``SharedContext`` with one MCP server, already started.

    Narrows ``_context_with_mcp`` in ``tests/test_mcp_bridge.py`` to a single
    server so the ``expose_to_agents`` tests below stay short. Callers are
    responsible for the surrounding ``tempfile.TemporaryDirectory`` and for
    calling ``context.mcp_hub.stop()``, matching the convention already used
    throughout ``tests/test_mcp_bridge.py``.
    """
    context = make_context(workspace)
    context.config.mcp = McpConfig(servers={server_id: config})
    context.mcp_hub = McpHub(context.config, client_factory=lambda sid, cfg: client)
    await context.mcp_hub.start()
    return context


class ExposeToAgentsFieldTests(unittest.TestCase):
    def test_defaults_to_true_preserving_existing_behaviour(self):
        config = McpServerConfig(url="http://127.0.0.1:8765/mcp", allowed_tools=["a_tool"])
        self.assertTrue(config.expose_to_agents)

    def test_can_be_turned_off(self):
        config = McpServerConfig(
            url="http://127.0.0.1:8767/mcp",
            allowed_tools=["get_daily_readiness"],
            expose_to_agents=False,
        )
        self.assertFalse(config.expose_to_agents)


class HostOnlyCapabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_host_only_server_registers_no_capabilities(self):
        client = FakeMcpClient(
            "health_planner",
            tools=(
                fake_tool("health_planner", "get_daily_readiness"),
                fake_tool("health_planner", "whoop_status"),
            ),
        )
        config = McpServerConfig(
            url="http://127.0.0.1:8767/mcp",
            allowed_tools=["get_daily_readiness", "whoop_status"],
            expose_to_agents=False,
        )
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            context = await _context_with_server(workspace, "health_planner", config, client)
            self.assertEqual([cap.id for cap, _ in build_mcp_capabilities(context)], [])
            await context.mcp_hub.stop()

    async def test_exposed_server_still_registers_capabilities(self):
        client = FakeMcpClient("movies_db", tools=(fake_tool("movies_db", "movies_recommend"),))
        config = McpServerConfig(url="http://127.0.0.1:8765/mcp", allowed_tools=["movies_recommend"])
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            context = await _context_with_server(workspace, "movies_db", config, client)
            self.assertEqual(
                [cap.id for cap, _ in build_mcp_capabilities(context)],
                ["mcp.movies_db.movies_recommend"],
            )
            await context.mcp_hub.stop()

    async def test_host_only_tool_is_still_callable_through_the_hub(self):
        """The engine must still reach it; only the agent-facing bridge is cut."""
        client = FakeMcpClient("health_planner", tools=(fake_tool("health_planner", "get_daily_readiness"),))
        config = McpServerConfig(
            url="http://127.0.0.1:8767/mcp",
            allowed_tools=["get_daily_readiness"],
            expose_to_agents=False,
        )
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            context = await _context_with_server(workspace, "health_planner", config, client)

            self.assertEqual(build_mcp_capabilities(context), [])
            self.assertTrue(context.mcp_hub.is_tool_available("health_planner", "get_daily_readiness"))

            result = await context.mcp_hub.call_tool("health_planner", "get_daily_readiness", {})
            self.assertFalse(result.is_error)
            self.assertEqual(client.calls, [("get_daily_readiness", {})])

            await context.mcp_hub.stop()


if __name__ == "__main__":
    unittest.main()
