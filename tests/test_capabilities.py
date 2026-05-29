import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.commands.handlers import ConfirmCommand
from core.events import CliEventSource
from core.pending_actions import PendingActionStore
from tests.helpers import make_context, make_workspace, write_file
from tools.base import ToolErrorCode, ToolResult, tool
from tools.calendar_tools import build_calendar_capabilities
from tools.capabilities import (
    CapabilityDef,
    ConfirmationRequiredTool,
    ToolPolicy,
    ToolRiskLevel,
)
from tools.capability_catalog import build_capability_registry
from tools.email_tools import build_email_capabilities
from tools.registry import ToolRegistry
from utils.config import (
    Config,
    ExternalProviderConfig,
    ExternalToolsConfig,
    ToolsConfig,
)


def _context(workspace: Path):
    return make_context(workspace)


def _session(context):
    return SimpleNamespace(shared_context=context)


def _agent_def(context, *, allow_skills: bool = False):
    agent_def = context.agent_loader.load("pickle")
    return agent_def.model_copy(update={"allow_skills": allow_skills})


def _tool_names(registry: ToolRegistry) -> set[str]:
    return {schema["function"]["name"] for schema in registry.get_tool_schemas()}


class CapabilityCatalogTests(unittest.TestCase):
    def test_registers_builtin_capabilities(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            context = _context(workspace)
            caps = build_capability_registry(
                _agent_def(context), context, include_post_message=False
            )
            ids = {c.id for c in caps.capabilities()}
            self.assertTrue(
                {
                    "filesystem.read",
                    "filesystem.write",
                    "filesystem.edit",
                    "cron.create_job",
                    "shell.bash",
                }.issubset(ids)
            )

    def test_with_builtins_still_works(self) -> None:
        registry = ToolRegistry.with_builtins()
        names = {t.name for t in registry.list_all()}
        self.assertEqual(
            names, {"read", "write", "edit", "create_cron_job", "bash"}
        )

    def test_permissive_policy_matches_legacy_tool_set(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            context = _context(workspace)  # no tools config => permissive
            caps = build_capability_registry(
                _agent_def(context), context, include_post_message=False
            )
            registry = caps.build_tool_registry(ToolPolicy.from_config(context.config))
            names = _tool_names(registry)
            # Builtins always present; subagent present (cookie agent dispatchable).
            self.assertEqual(
                names,
                {
                    "read",
                    "write",
                    "edit",
                    "create_cron_job",
                    "bash",
                    "subagent_dispatch",
                },
            )

    def test_dotted_capability_ids_preserved_with_safe_tool_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            context = _context(workspace)
            context.config.external_tools = ExternalToolsConfig(
                email=ExternalProviderConfig(enabled=True),
                calendar=ExternalProviderConfig(enabled=True),
            )
            caps = build_capability_registry(
                _agent_def(context), context, include_post_message=False
            )
            # internal dotted id <-> safe tool name mapping
            self.assertEqual(
                caps.capability_for_tool_name("email_search"), "email.search"
            )
            self.assertEqual(
                caps.capability_for_tool_name("calendar_create_event"),
                "calendar.create_event",
            )


class ExternalCapabilityVisibilityTests(unittest.TestCase):
    def _context_with(self, workspace, *, email=False, calendar=False, tools=None):
        context = _context(workspace)
        context.config.external_tools = ExternalToolsConfig(
            email=ExternalProviderConfig(enabled=email),
            calendar=ExternalProviderConfig(enabled=calendar),
        )
        context.config.tools = tools
        return context

    def test_disabled_external_capabilities_absent_from_schemas(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            context = self._context_with(workspace)  # both disabled
            caps = build_capability_registry(
                _agent_def(context), context, include_post_message=False
            )
            names = _tool_names(
                caps.build_tool_registry(ToolPolicy.from_config(context.config))
            )
            self.assertFalse(any(n.startswith("email_") for n in names))
            self.assertFalse(any(n.startswith("calendar_") for n in names))

    def test_disabled_email_hides_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            context = self._context_with(workspace, email=False)
            self.assertEqual(build_email_capabilities(context.config), [])

    def test_read_only_policy_allows_email_search_blocks_create_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            tools = ToolsConfig(
                enabled_capabilities=[
                    "filesystem.read",
                    "email.search",
                    "email.read",
                    "calendar.search",
                    "calendar.availability",
                ]
            )
            context = self._context_with(
                workspace, email=True, calendar=True, tools=tools
            )
            caps = build_capability_registry(
                _agent_def(context), context, include_post_message=False
            )
            names = _tool_names(
                caps.build_tool_registry(ToolPolicy.from_config(context.config))
            )
            self.assertIn("email_search", names)
            self.assertNotIn("calendar_create_event", names)

    def test_draft_policy_allows_draft_reply_and_has_no_send_tool(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            tools = ToolsConfig(
                enabled_capabilities=["email.search", "email.read", "email.draft_reply"]
            )
            context = self._context_with(workspace, email=True, tools=tools)
            caps = build_capability_registry(
                _agent_def(context), context, include_post_message=False
            )
            names = _tool_names(
                caps.build_tool_registry(ToolPolicy.from_config(context.config))
            )
            self.assertIn("email_draft_reply", names)
            self.assertFalse(any("send" in n for n in names))


class ConfirmationFlowTests(unittest.TestCase):
    def test_confirm_executes_stored_cron_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            context = _context(workspace)
            action_id = "11111111-1111-4111-8111-111111111111"
            PendingActionStore(context.config).create(
                action_id=action_id,
                capability_id="cron.create_job",
                summary="Create a scheduled cron job.",
                payload={
                    "name": "Morning digest",
                    "description": "Send a digest.",
                    "agent": "pickle",
                    "schedule": "0 8 * * *",
                    "prompt": "Summarize overnight activity.",
                },
            )
            session = SimpleNamespace(
                agent=SimpleNamespace(agent_def=_agent_def(context)),
                state=SimpleNamespace(source=CliEventSource()),
                shared_context=context,
                session_id="test-session",
            )

            output = asyncio.run(ConfirmCommand().execute(action_id, session))

            self.assertIn("Confirmed `cron.create_job`", output)
            self.assertIn("Created cron job `morning-digest`", output)
            self.assertTrue(
                (workspace / "crons" / "morning-digest" / "CRON.md").is_file()
            )
            self.assertIsNone(PendingActionStore(context.config).get(action_id))

    def test_pending_action_store_rejects_traversal_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            context = _context(workspace)
            outside = context.config.event_path / "secret.json"
            write_file(outside, '{"id": "secret"}')
            store = PendingActionStore(context.config)

            self.assertIsNone(store.get("../secret"))
            self.assertFalse(store.delete("../secret"))
            self.assertTrue(outside.is_file())

    def test_create_event_returns_pending_action_without_executing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            context = _context(workspace)
            context.config.external_tools = ExternalToolsConfig(
                calendar=ExternalProviderConfig(enabled=True)
            )
            (cap, tool_obj) = next(
                pair
                for pair in build_calendar_capabilities(context.config)
                if pair[0].id == "calendar.create_event"
            )
            session = _session(context)
            raw = asyncio.run(
                tool_obj.execute(
                    session=session,
                    title="Team sync",
                    start="2026-06-01T10:00:00+00:00",
                    end="2026-06-01T10:30:00+00:00",
                )
            )
            payload = json.loads(raw)
            self.assertFalse(payload["ok"])
            self.assertTrue(payload["requires_confirmation"])
            self.assertEqual(
                payload["action"]["capability_id"], "calendar.create_event"
            )
            # Pending action persisted under the workspace.
            action_id = payload["action"]["id"]
            stored = PendingActionStore(context.config).get(action_id)
            self.assertIsNotNone(stored)
            self.assertEqual(stored["capability_id"], "calendar.create_event")

    def test_confirmation_wrapper_does_not_execute_wrapped_tool(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            context = _context(workspace)
            executed = {"called": False}

            @tool(
                name="danger",
                description="A mutating tool.",
                parameters={"type": "object", "properties": {}},
            )
            async def danger(session: object) -> str:
                executed["called"] = True
                return "mutated"

            cap = CapabilityDef(
                id="danger.do",
                tool_name="danger",
                domain="danger",
                operation="do",
                description="Dangerous op.",
                risk_level=ToolRiskLevel.CONFIRM_REQUIRED,
            )
            wrapper = ConfirmationRequiredTool(cap, danger)
            raw = asyncio.run(wrapper.execute(session=_session(context)))
            payload = json.loads(raw)
            self.assertTrue(payload["requires_confirmation"])
            self.assertFalse(executed["called"])

    def test_confirm_required_capability_is_gated_by_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            context = _context(workspace)
            context.config.external_tools = ExternalToolsConfig(
                calendar=ExternalProviderConfig(enabled=True)
            )
            context.config.tools = ToolsConfig(
                enabled_capabilities=["calendar.create_event"]
            )
            caps = build_capability_registry(
                _agent_def(context), context, include_post_message=False
            )
            registry = caps.build_tool_registry(ToolPolicy.from_config(context.config))
            tool_obj = registry.get("calendar_create_event")
            self.assertIsInstance(tool_obj, ConfirmationRequiredTool)
            raw = asyncio.run(
                tool_obj.execute(
                    session=_session(context),
                    title="X",
                    start="2026-06-01T10:00:00+00:00",
                    end="2026-06-01T11:00:00+00:00",
                )
            )
            self.assertTrue(json.loads(raw)["requires_confirmation"])


class ExternalAuthMissingTests(unittest.IsolatedAsyncioTestCase):
    async def test_enabled_email_without_client_returns_auth_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            config = Config.load(workspace)
            config.external_tools = ExternalToolsConfig(
                email=ExternalProviderConfig(enabled=True)
            )
            pairs = build_email_capabilities(config)
            search = next(t for cap, t in pairs if cap.id == "email.search")
            raw = await search.execute(session=object(), query="hello")
            payload = json.loads(raw)
            self.assertFalse(payload["ok"])
            self.assertEqual(
                payload["error"]["code"], ToolErrorCode.AUTH_MISSING.value
            )


class ToolResultSerializationTests(unittest.TestCase):
    def test_structured_error_still_serializes(self) -> None:
        result = ToolResult.error(
            ToolErrorCode.INVALID_ARGS,
            "Missing required path.",
            user_action="Provide a path.",
        )
        payload = json.loads(result.to_tool_content())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "invalid_args")

    def test_requires_confirmation_serializes(self) -> None:
        result = ToolResult.requires_confirmation(
            action_id="abc",
            capability_id="calendar.create_event",
            summary="Create calendar event",
            payload={"title": "X"},
        )
        payload = json.loads(result.to_tool_content())
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["requires_confirmation"])
        self.assertEqual(payload["action"]["id"], "abc")
        self.assertEqual(payload["action"]["payload"], {"title": "X"})


if __name__ == "__main__":
    unittest.main()
