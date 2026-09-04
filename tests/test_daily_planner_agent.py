"""The planner's capability set — what it has, and what it must never have.

The frontmatter assertions below parse AGENT.md directly with ``yaml.safe_load``
rather than going through ``AgentLoader``/``Config.load`` against the real
``default_workspace``: that path needs ``config.user.yaml``, which is
gitignored and absent in a fresh worktree (see
``test_real_default_workspace_cookie_def_is_memory_only`` and
``test_task_assistant_exposes_exactly_its_task_toolset``, which fail here for
exactly that reason). Parsing the frontmatter needs no config at all.

The end-to-end registry test below still needs a working ``Config``, so it
builds one from an isolated synthetic workspace (``tests.helpers.make_workspace``)
rather than the real, config-less ``default_workspace`` — sidestepping the same
wall while still exercising the real assembly path in ``core/agent.py``.
"""

import tempfile
import unittest
from pathlib import Path

import yaml

from provider.mcp.hub import McpHub
from tests.helpers import make_context, make_workspace
from tests.mcp_fakes import FakeMcpClient, fake_tool
from tools.capabilities import ToolPolicy
from tools.capability_catalog import build_capability_registry
from utils.config import ExternalProviderConfig, PlanningConfig, TasksProviderConfig
from utils.mcp_config import McpConfig, McpServerConfig

AGENT = (Path(__file__).resolve().parents[1]
         / "default_workspace" / "agents" / "daily-planner" / "AGENT.md")

FORBIDDEN = {
    "shell.bash",
    "filesystem.read", "filesystem.write", "filesystem.edit",
    "calendar.search", "calendar.create_event", "calendar.update_event",
    "calendar.delete_event",
    "tasks.quick_add", "tasks.update", "tasks.complete", "tasks.delete",
    "tasks.bulk_update",
    "messaging.post_message",
    "agent.subagent_dispatch",
    "cron.create_job",
    "skills.invoke", "skills.run_script",
}


def _frontmatter():
    text = AGENT.read_text()
    return yaml.safe_load(text.split("---")[1])


class TestDailyPlannerAgent(unittest.TestCase):
    def test_declares_an_explicit_allowlist(self):
        self.assertIsInstance(_frontmatter()["allowed_capabilities"], list)

    def test_has_exactly_the_capabilities_it_needs(self):
        self.assertEqual(
            sorted(_frontmatter()["allowed_capabilities"]),
            sorted([
                "calendar.day_agenda",
                "planning.build_day_plan",
                "planning.sync_daily_plan",
                "tasks.overdue",
                "tasks.today",
            ]),
        )

    def test_grants_nothing_dangerous(self):
        granted = set(_frontmatter()["allowed_capabilities"])
        self.assertEqual(granted & FORBIDDEN, set())

    def test_never_grants_raw_whoop_access(self):
        granted = set(_frontmatter()["allowed_capabilities"])
        self.assertEqual([g for g in granted if g.startswith("mcp.")], [])

    def test_no_workspace_agent_grants_health_planner_mcp(self):
        """The privacy invariant, across every shipped agent."""
        agents_dir = AGENT.parents[1]
        for agent_md in agents_dir.glob("*/AGENT.md"):
            frontmatter = yaml.safe_load(agent_md.read_text().split("---")[1])
            granted = frontmatter.get("allowed_capabilities") or []
            for capability in granted:
                self.assertFalse(
                    capability.startswith("mcp.health_planner"),
                    f"{agent_md.parent.name} grants raw WHOOP access",
                )

    def test_skills_are_disabled(self):
        self.assertFalse(_frontmatter().get("allow_skills", False))


class TestDailyPlannerAssembledRegistry(unittest.IsolatedAsyncioTestCase):
    """One layer beyond ``test_mcp_host_only.py``.

    That suite asserts on ``build_mcp_capabilities`` — the bridge between
    discovered MCP tools and the capability catalog. It does not assert on the
    final ``ToolRegistry`` a session's LLM loop actually calls
    ``get_tool_schemas()`` against. Here we assemble that registry the same
    way ``core/agent.py::Agent._build_tools`` does —
    ``build_capability_registry(...)`` then
    ``CapabilityRegistry.build_tool_registry(ToolPolicy.from_config(...))`` —
    with a ``health_planner`` MCP server configured ``expose_to_agents:
    False``, and confirm no ``mcp_health_planner_*`` tool name survives into
    it. This is the daily-planner's own subject matter (an assembled
    toolset), so the guarantee belongs in this file even though it is a
    workspace-wide invariant, not specific to this one agent.
    """

    async def _build_registry(self, tmp_path: str):
        workspace = make_workspace(Path(tmp_path))

        # Ship the real, authored daily-planner AGENT.md into the synthetic
        # workspace, so this exercises the actual shipped capability set
        # rather than a hand-rolled approximation of it.
        agent_dir = workspace / "agents" / "daily-planner"
        agent_dir.mkdir(parents=True)
        (agent_dir / "AGENT.md").write_text(AGENT.read_text())

        context = make_context(workspace)
        # Enable the domains the planner's five capabilities live in, mirroring
        # what a real deployment running this agent would have configured.
        # `provider` is left unset on both, so `get_calendar_provider`/
        # `get_task_provider` fall back to their null providers — no real
        # credentials needed to build (not execute) the capability set.
        context.config.external_tools.calendar = ExternalProviderConfig(enabled=True)
        context.config.external_tools.tasks = TasksProviderConfig(enabled=True)
        context.config.planning = PlanningConfig(enabled=True, mode="shadow")

        client = FakeMcpClient(
            "health_planner",
            tools=(
                fake_tool("health_planner", "get_daily_readiness"),
                fake_tool("health_planner", "whoop_status"),
            ),
        )
        health_planner_config = McpServerConfig(
            url="http://127.0.0.1:8767/mcp",
            allowed_tools=["get_daily_readiness", "whoop_status"],
            expose_to_agents=False,
        )
        context.config.mcp = McpConfig(servers={"health_planner": health_planner_config})
        context.mcp_hub = McpHub(context.config, client_factory=lambda sid, cfg: client)
        await context.mcp_hub.start()
        try:
            agent_def = context.agent_loader.load("daily-planner")
            # include_post_message=True: this agent runs from cron in
            # production (source.is_cron), which is exactly the scenario the
            # allowlist must still exclude messaging.post_message under.
            capabilities = build_capability_registry(
                agent_def, context, include_post_message=True
            )
            policy = ToolPolicy.from_config(
                context.config, agent_def.allowed_capabilities
            )
            return capabilities.build_tool_registry(policy)
        finally:
            await context.mcp_hub.stop()

    async def test_host_only_mcp_tool_never_reaches_the_assembled_registry(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = await self._build_registry(tmp)
        names = {schema["function"]["name"] for schema in registry.get_tool_schemas()}
        leaked = [n for n in names if n.startswith("mcp_health_planner_")]
        self.assertEqual(
            leaked, [],
            f"host-only MCP tool reached the assembled registry: {leaked}",
        )

    async def test_assembled_registry_matches_the_declared_allowlist_exactly(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = await self._build_registry(tmp)
        names = {schema["function"]["name"] for schema in registry.get_tool_schemas()}
        self.assertEqual(
            names,
            {
                "calendar_day_agenda",
                "tasks_today",
                "tasks_overdue",
                "planning_build_day_plan",
                "planning_sync_daily_plan",
            },
        )


if __name__ == "__main__":
    unittest.main()
