"""Integration/regression tests for memory tools, subagent dispatch discovery,
and per-agent capability narrowing (Cookie).

These cover the final workstream behaviors:
- subagent_dispatch agent discovery + self-exclusion (points 2, 3)
- memory tools hidden unless registered AND policy-enabled (point 4)
- Cookie memory-only capability narrowing without filesystem write (point 5)
- external email/calendar provider visibility unchanged; memory independent of
  external_tools toggles (point 6)
"""

import tempfile
import unittest
from pathlib import Path

from tests.helpers import make_context, make_workspace, write_definition
from tools.capabilities import ToolPolicy
from tools.capability_catalog import build_capability_registry
from tools.registry import ToolRegistry
from tools.subagent_tool import create_subagent_dispatch_tool
from utils.config import (
    ExternalProviderConfig,
    ExternalToolsConfig,
    ToolsConfig,
)

MEMORY_TOOL_NAMES = {
    "memory_search",
    "memory_store_fact",
    "memory_store_preference",
    "memory_update_user_profile",
    "memory_update_assistant_preferences",
    "memory_store_project_context",
    "memory_store_decision",
    "memory_append_daily_note",
}

MEMORY_CAPABILITY_IDS = [
    "memory.search",
    "memory.store_fact",
    "memory.store_preference",
    "memory.update_user_profile",
    "memory.update_assistant_preferences",
    "memory.store_project_context",
    "memory.store_decision",
    "memory.append_daily_note",
]


def _tool_names(registry: ToolRegistry) -> set[str]:
    return {schema["function"]["name"] for schema in registry.get_tool_schemas()}


def _write_specialist_agents(workspace: Path) -> None:
    """Add the specialist agents that ship in the real default_workspace."""
    for agent_id, name in (
        ("mail-assistant", "Mail Assistant"),
        ("calendar-assistant", "Calendar Assistant"),
        ("researcher", "Researcher"),
    ):
        write_definition(
            workspace / "agents",
            agent_id,
            "AGENT.md",
            {"name": name, "description": f"{name} specialist."},
            f"You are {name}.",
        )


def _dispatch_enum(tool) -> list[str]:
    return tool.parameters["properties"]["agent_id"]["enum"]


class SubagentDispatchDiscoveryTests(unittest.TestCase):
    """Points 2 and 3: dispatch schema exposes peers and excludes self."""

    def test_pickle_dispatch_sees_specialists_and_cookie_excludes_self(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))  # writes pickle + cookie
            _write_specialist_agents(workspace)
            context = make_context(workspace)

            tool = create_subagent_dispatch_tool("pickle", context)
            self.assertIsNotNone(tool)
            enum = set(_dispatch_enum(tool))

            # Point 2: the four peers are dispatchable.
            self.assertEqual(
                enum,
                {"cookie", "mail-assistant", "calendar-assistant", "researcher"},
            )
            # Point 3: Pickle excludes itself.
            self.assertNotIn("pickle", enum)

    def test_each_agent_excludes_itself_from_its_own_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            _write_specialist_agents(workspace)
            context = make_context(workspace)

            for agent_id in (
                "pickle",
                "cookie",
                "mail-assistant",
                "calendar-assistant",
                "researcher",
            ):
                tool = create_subagent_dispatch_tool(agent_id, context)
                self.assertIsNotNone(tool)
                self.assertNotIn(agent_id, _dispatch_enum(tool))


class MemoryToolVisibilityTests(unittest.TestCase):
    """Point 4: memory tools are gated by policy even though always registered."""

    def test_memory_tools_present_in_permissive_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            context = make_context(workspace)  # no tools config => permissive
            agent_def = context.agent_loader.load("pickle")
            caps = build_capability_registry(
                agent_def, context, include_post_message=False
            )
            names = _tool_names(
                caps.build_tool_registry(ToolPolicy.from_config(context.config))
            )
            self.assertTrue(MEMORY_TOOL_NAMES.issubset(names))

    def test_memory_tools_hidden_when_policy_excludes_them(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            context = make_context(workspace)
            # Strict policy lists no memory.* capability.
            context.config.tools = ToolsConfig(
                enabled_capabilities=["filesystem.read"]
            )
            agent_def = context.agent_loader.load("pickle")
            caps = build_capability_registry(
                agent_def, context, include_post_message=False
            )
            names = _tool_names(
                caps.build_tool_registry(ToolPolicy.from_config(context.config))
            )
            self.assertFalse(any(n.startswith("memory_") for n in names))

    def test_policy_enables_only_listed_memory_tool(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            context = make_context(workspace)
            context.config.tools = ToolsConfig(
                enabled_capabilities=["memory.search"]
            )
            agent_def = context.agent_loader.load("pickle")
            caps = build_capability_registry(
                agent_def, context, include_post_message=False
            )
            names = _tool_names(
                caps.build_tool_registry(ToolPolicy.from_config(context.config))
            )
            self.assertIn("memory_search", names)
            other_memory = MEMORY_TOOL_NAMES - {"memory_search"}
            self.assertFalse(any(n in names for n in other_memory))


class CookieCapabilityNarrowingTests(unittest.TestCase):
    """Point 5: Cookie gets memory.* tools but no raw filesystem write/edit/bash."""

    def test_cookie_has_memory_without_filesystem_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            context = make_context(workspace)
            cookie_def = context.agent_loader.load("cookie").model_copy(
                update={"allowed_capabilities": list(MEMORY_CAPABILITY_IDS)}
            )
            caps = build_capability_registry(
                cookie_def, context, include_post_message=False
            )
            names = _tool_names(
                caps.build_tool_registry(
                    ToolPolicy.from_config(
                        context.config, cookie_def.allowed_capabilities
                    )
                )
            )
            # Memory tools available.
            self.assertTrue(MEMORY_TOOL_NAMES.issubset(names))
            # Raw filesystem mutation / shell excluded.
            self.assertNotIn("write", names)
            self.assertNotIn("edit", names)
            self.assertNotIn("bash", names)
            # read is also outside the allowed list -> absent.
            self.assertNotIn("read", names)

    def test_real_default_workspace_cookie_def_is_memory_only(self) -> None:
        # The shipped cookie agent definition is narrowed to the six memory.*
        # capability ids (and nothing filesystem/shell related).
        from core.context import SharedContext
        from utils.config import Config

        config = Config.load(Path("default_workspace"))
        context = SharedContext(config, channels=[])
        cookie_def = context.agent_loader.load("cookie")
        self.assertIsNotNone(cookie_def.allowed_capabilities)
        self.assertEqual(
            set(cookie_def.allowed_capabilities), set(MEMORY_CAPABILITY_IDS)
        )
        # The agent never lists raw filesystem/shell capabilities.
        for forbidden in (
            "filesystem.write",
            "filesystem.edit",
            "filesystem.read",
            "shell.bash",
        ):
            self.assertNotIn(forbidden, cookie_def.allowed_capabilities)


class ExternalAndMemoryIndependenceTests(unittest.TestCase):
    """Point 6: external email/calendar gating unchanged; memory independent."""

    def _context(self, workspace, *, email=False, calendar=False):
        context = make_context(workspace)
        context.config.external_tools = ExternalToolsConfig(
            email=ExternalProviderConfig(enabled=email),
            calendar=ExternalProviderConfig(enabled=calendar),
        )
        return context

    def test_disabled_external_tools_absent_memory_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            context = self._context(workspace)  # both external disabled
            agent_def = context.agent_loader.load("pickle")
            caps = build_capability_registry(
                agent_def, context, include_post_message=False
            )
            names = _tool_names(
                caps.build_tool_registry(ToolPolicy.from_config(context.config))
            )
            self.assertFalse(any(n.startswith("email_") for n in names))
            self.assertFalse(any(n.startswith("calendar_") for n in names))
            # Memory unaffected by external_tools toggle.
            self.assertTrue(MEMORY_TOOL_NAMES.issubset(names))

    def test_memory_present_regardless_of_external_enabled_state(self) -> None:
        for email, calendar in ((False, False), (True, True), (True, False)):
            with tempfile.TemporaryDirectory() as tmp:
                workspace = make_workspace(Path(tmp))
                context = self._context(
                    workspace, email=email, calendar=calendar
                )
                agent_def = context.agent_loader.load("pickle")
                caps = build_capability_registry(
                    agent_def, context, include_post_message=False
                )
                names = _tool_names(
                    caps.build_tool_registry(
                        ToolPolicy.from_config(context.config)
                    )
                )
                self.assertTrue(
                    MEMORY_TOOL_NAMES.issubset(names),
                    f"memory tools missing for email={email} calendar={calendar}",
                )


if __name__ == "__main__":
    unittest.main()
