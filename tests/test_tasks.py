"""Tests for the tasks domain: config, provider, tools, visibility, confirmation."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx

from core.pending_actions import PendingActionStore
from provider.external_errors import (
    AuthMissingError,
    ProviderNotFoundError,
    ProviderPermissionError,
)
from provider.tasks import (
    BulkTaskUpdateRequest,
    NullTaskProvider,
    TaskUpdateRequest,
    get_task_provider,
)
from provider.tasks.todoist import TodoistTaskProvider
from tests.helpers import make_context, make_workspace
from tools.base import ToolErrorCode
from tools.capabilities import ToolPolicy
from tools.capability_catalog import (
    build_capability_registry,
    build_confirmed_executor_registry,
)
from tools.registry import ToolRegistry
from tools.task_tools import (
    build_task_capabilities,
    build_task_confirmed_executors,
)
from utils.config import (
    Config,
    ExternalToolsConfig,
    TasksProviderConfig,
    ToolsConfig,
)


def _agent_def(context, *, allow_skills: bool = False):
    agent_def = context.agent_loader.load("pickle")
    return agent_def.model_copy(update={"allow_skills": allow_skills})


def _session(context):
    return SimpleNamespace(shared_context=context)


def _tool_names(registry: ToolRegistry) -> set[str]:
    return {schema["function"]["name"] for schema in registry.get_tool_schemas()}


def _enable_tasks(context, *, token_env: str = "TODOIST_API_TOKEN"):
    context.config.external_tools = ExternalToolsConfig(
        tasks=TasksProviderConfig(enabled=True, api_token_env=token_env)
    )


class _FakeProvider:
    """Records provider calls for tool-layer tests."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def quick_add(self, text):
        self.calls.append(("quick_add", text))
        from provider.tasks.base import Task

        return Task(id="42", content=text)

    async def list_inbox(self, limit):
        self.calls.append(("list_inbox", limit))
        from provider.tasks.base import Task

        return [Task(id="1", content="A task", priority=2)]

    async def delete(self, task_id):
        self.calls.append(("delete", task_id))

    async def bulk_update(self, request):
        self.calls.append(("bulk_update", request.operation, tuple(request.task_ids)))
        return len(request.task_ids)


def _todoist(handler, *, token: str = "tok") -> TodoistTaskProvider:
    """Build a Todoist provider backed by an httpx MockTransport handler."""
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = TodoistTaskProvider(
        config=None, provider_cfg=TasksProviderConfig(), client=client
    )
    provider._token = token
    return provider


class TasksConfigTests(unittest.TestCase):
    def test_defaults_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            config = Config.load(workspace)
            tasks = config.external_tools.tasks
            self.assertFalse(tasks.enabled)
            self.assertEqual(tasks.provider, "todoist")
            self.assertEqual(tasks.api_token_env, "TODOIST_API_TOKEN")

    def test_enabled_with_custom_env(self) -> None:
        cfg = TasksProviderConfig(enabled=True, api_token_env="MY_TOKEN")
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.api_token_env, "MY_TOKEN")


class NullProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_null_provider_raises_auth_missing(self) -> None:
        provider = NullTaskProvider()
        with self.assertRaises(AuthMissingError):
            await provider.list_today(10)
        with self.assertRaises(AuthMissingError):
            await provider.quick_add("x")

    async def test_get_task_provider_disabled_returns_null(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config.load(make_workspace(Path(tmp)))
            self.assertIsInstance(get_task_provider(config), NullTaskProvider)

    async def test_todoist_without_token_raises_auth_missing(self) -> None:
        provider = TodoistTaskProvider(
            config=None, provider_cfg=TasksProviderConfig(api_token_env="UNSET_X")
        )
        provider._token = None
        with self.assertRaises(AuthMissingError):
            await provider.list_today(10)


class TodoistProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_quick_add_uses_api_v1_json_endpoint(self) -> None:
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("authorization")
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={})

        provider = _todoist(handler)
        task = await provider.quick_add("Buy milk tomorrow")
        self.assertEqual(task.id, "")
        self.assertEqual(task.content, "Buy milk tomorrow")
        self.assertEqual(seen["url"], "https://api.todoist.com/api/v1/tasks/quick")
        self.assertEqual(seen["auth"], "Bearer tok")
        self.assertEqual(seen["body"], {"text": "Buy milk tomorrow"})

    async def test_list_today_maps_tasks_and_limits(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.params.get("filter"), "today")
            return httpx.Response(
                200,
                json=[
                    {"id": 1, "content": "One", "priority": 4, "labels": ["work"],
                     "due": {"string": "today", "date": "2026-06-25"}},
                    {"id": 2, "content": "Two"},
                    {"id": 3, "content": "Three"},
                ],
            )

        provider = _todoist(handler)
        tasks = await provider.list_today(2)
        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[0].id, "1")
        self.assertEqual(tasks[0].due_string, "today")
        self.assertEqual(tasks[0].labels, ["work"])

    async def test_update_sends_only_changed_fields(self) -> None:
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"id": 9, "content": "Renamed"})

        provider = _todoist(handler)
        task = await provider.update(
            TaskUpdateRequest(task_id="9", content="Renamed", priority=3)
        )
        self.assertEqual(task.content, "Renamed")
        self.assertEqual(seen["body"], {"content": "Renamed", "priority": 3})

    async def test_complete_and_delete(self) -> None:
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((request.method, str(request.url)))
            return httpx.Response(204)

        provider = _todoist(handler)
        await provider.complete("5")
        await provider.delete("5")
        self.assertIn(("POST", "https://api.todoist.com/rest/v2/tasks/5/close"), seen)
        self.assertIn(("DELETE", "https://api.todoist.com/rest/v2/tasks/5"), seen)

    async def test_bulk_update_complete_applies_each(self) -> None:
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(204)

        provider = _todoist(handler)
        count = await provider.bulk_update(
            BulkTaskUpdateRequest(task_ids=["1", "2", "3"], operation="complete")
        )
        self.assertEqual(count, 3)
        self.assertEqual(len(seen), 3)

    async def test_status_errors_mapped(self) -> None:
        provider = _todoist(lambda r: httpx.Response(403))
        with self.assertRaises(ProviderPermissionError):
            await provider.list_today(5)
        provider = _todoist(lambda r: httpx.Response(404))
        with self.assertRaises(ProviderNotFoundError):
            await provider.delete("nope")


class TaskCapabilityVisibilityTests(unittest.TestCase):
    def test_disabled_hides_task_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = make_context(make_workspace(Path(tmp)))
            self.assertEqual(build_task_capabilities(context.config), [])
            caps = build_capability_registry(
                _agent_def(context), context, include_post_message=False
            )
            names = _tool_names(
                caps.build_tool_registry(ToolPolicy.from_config(context.config))
            )
            self.assertFalse(any(n.startswith("tasks_") for n in names))

    def test_enabled_shows_all_task_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = make_context(make_workspace(Path(tmp)))
            _enable_tasks(context)
            caps = build_capability_registry(
                _agent_def(context), context, include_post_message=False
            )
            names = _tool_names(
                caps.build_tool_registry(ToolPolicy.from_config(context.config))
            )
            self.assertTrue(
                {
                    "tasks_quick_add",
                    "tasks_inbox",
                    "tasks_today",
                    "tasks_overdue",
                    "tasks_search",
                    "tasks_update",
                    "tasks_complete",
                    "tasks_delete",
                    "tasks_bulk_update",
                }.issubset(names)
            )

    def test_read_only_policy_hides_mutations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = make_context(make_workspace(Path(tmp)))
            _enable_tasks(context)
            context.config.tools = ToolsConfig(
                enabled_capabilities=["tasks.inbox", "tasks.today"]
            )
            caps = build_capability_registry(
                _agent_def(context), context, include_post_message=False
            )
            names = _tool_names(
                caps.build_tool_registry(ToolPolicy.from_config(context.config))
            )
            self.assertIn("tasks_inbox", names)
            self.assertNotIn("tasks_delete", names)
            self.assertNotIn("tasks_quick_add", names)

    def test_capability_for_tool_name_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = make_context(make_workspace(Path(tmp)))
            _enable_tasks(context)
            caps = build_capability_registry(
                _agent_def(context), context, include_post_message=False
            )
            self.assertEqual(
                caps.capability_for_tool_name("tasks_bulk_update"),
                "tasks.bulk_update",
            )

    def test_task_assistant_exposes_exactly_its_task_toolset(self) -> None:
        # Use the real default workspace so we validate the shipped task-assistant
        # AGENT.md (its allowed_capabilities), not a synthetic test agent.
        workspace = Path(__file__).resolve().parents[1] / "default_workspace"
        from core.context import SharedContext

        config = Config.load(workspace)
        config.external_tools = ExternalToolsConfig(
            tasks=TasksProviderConfig(enabled=True)
        )
        context = SharedContext(config, channels=[])

        agent_def = context.agent_loader.load("task-assistant")
        caps = build_capability_registry(
            agent_def, context, include_post_message=False
        )
        registry = caps.build_tool_registry(
            ToolPolicy.from_config(config, agent_def.allowed_capabilities)
        )
        names = _tool_names(registry)

        task_tool_names = {
            "tasks_quick_add",
            "tasks_inbox",
            "tasks_today",
            "tasks_overdue",
            "tasks_search",
            "tasks_update",
            "tasks_complete",
            "tasks_delete",
            "tasks_bulk_update",
        }
        # The agent allows exactly the task tools plus subagent_dispatch; the
        # allowed_capabilities narrowing keeps builtins (read/write/bash/...) out.
        self.assertEqual(names, task_tool_names | {"subagent_dispatch"})
        # And nothing from other external domains leaks in.
        self.assertFalse(any(n.startswith(("email_", "calendar_")) for n in names))


class TaskToolExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_quick_add_executes_directly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = make_context(make_workspace(Path(tmp)))
            _enable_tasks(context)
            fake = _FakeProvider()
            import tools.task_tools as tt

            orig = tt.get_task_provider
            tt.get_task_provider = lambda config: fake
            try:
                pairs = build_task_capabilities(context.config)
            finally:
                tt.get_task_provider = orig
            quick_add = next(t for cap, t in pairs if cap.id == "tasks.quick_add")
            raw = await quick_add.execute(session=_session(context), text="Buy milk")
            self.assertIn("Task added", raw)
            self.assertEqual(fake.calls, [("quick_add", "Buy milk")])

    async def test_inbox_renders_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = make_context(make_workspace(Path(tmp)))
            _enable_tasks(context)
            fake = _FakeProvider()
            import tools.task_tools as tt

            orig = tt.get_task_provider
            tt.get_task_provider = lambda config: fake
            try:
                pairs = build_task_capabilities(context.config)
            finally:
                tt.get_task_provider = orig
            inbox = next(t for cap, t in pairs if cap.id == "tasks.inbox")
            raw = await inbox.execute(session=_session(context))
            self.assertIn("[1]", raw)
            self.assertIn("A task", raw)

    async def test_enabled_without_token_surfaces_auth_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = make_context(make_workspace(Path(tmp)))
            _enable_tasks(context, token_env="DEFINITELY_UNSET_TOKEN_ENV")
            pairs = build_task_capabilities(context.config)
            today = next(t for cap, t in pairs if cap.id == "tasks.today")
            raw = await today.execute(session=_session(context))
            payload = json.loads(raw)
            self.assertFalse(payload["ok"])
            self.assertEqual(
                payload["error"]["code"], ToolErrorCode.AUTH_MISSING.value
            )

    async def test_update_with_no_fields_is_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = make_context(make_workspace(Path(tmp)))
            _enable_tasks(context)
            pairs = build_task_capabilities(context.config)
            update = next(t for cap, t in pairs if cap.id == "tasks.update")
            raw = await update.execute(session=_session(context), task_id="1")
            payload = json.loads(raw)
            self.assertEqual(
                payload["error"]["code"], ToolErrorCode.INVALID_ARGS.value
            )


class TaskConfirmationFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_delete_records_pending_without_executing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = make_context(make_workspace(Path(tmp)))
            _enable_tasks(context, token_env="UNSET_TOKEN")
            pairs = build_task_capabilities(context.config)
            delete = next(t for cap, t in pairs if cap.id == "tasks.delete")
            raw = await delete.execute(session=_session(context), task_id="99")
            payload = json.loads(raw)
            self.assertTrue(payload["requires_confirmation"])
            self.assertEqual(payload["action"]["capability_id"], "tasks.delete")
            stored = PendingActionStore(context.config).get(payload["action"]["id"])
            self.assertIsNotNone(stored)

    async def test_bulk_update_records_pending_without_executing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = make_context(make_workspace(Path(tmp)))
            _enable_tasks(context, token_env="UNSET_TOKEN")
            pairs = build_task_capabilities(context.config)
            bulk = next(t for cap, t in pairs if cap.id == "tasks.bulk_update")
            raw = await bulk.execute(
                session=_session(context),
                task_ids=["1", "2"],
                operation="complete",
            )
            payload = json.loads(raw)
            self.assertTrue(payload["requires_confirmation"])
            self.assertEqual(
                payload["action"]["capability_id"], "tasks.bulk_update"
            )
            self.assertEqual(payload["action"]["payload"]["operation"], "complete")

    async def test_confirmed_executors_run_provider_exactly_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = make_context(make_workspace(Path(tmp)))
            _enable_tasks(context)
            fake = _FakeProvider()
            import tools.task_tools as tt

            orig = tt.get_task_provider
            tt.get_task_provider = lambda config: fake
            try:
                executors = build_task_confirmed_executors(context.config)
            finally:
                tt.get_task_provider = orig

            session = _session(context)
            out = await executors["tasks.delete"](session, {"task_id": "5"})
            self.assertIn("deleted", out)
            out = await executors["tasks.bulk_update"](
                session, {"task_ids": ["1", "2", "3"], "operation": "complete"}
            )
            self.assertIn("3 task", out)
            self.assertEqual(
                fake.calls,
                [
                    ("delete", "5"),
                    ("bulk_update", "complete", ("1", "2", "3")),
                ],
            )

    async def test_confirmed_registry_includes_task_executors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = make_context(make_workspace(Path(tmp)))
            _enable_tasks(context)
            registry = build_confirmed_executor_registry(context.config)
            self.assertIsNotNone(registry.get("tasks.delete"))
            self.assertIsNotNone(registry.get("tasks.bulk_update"))


if __name__ == "__main__":
    unittest.main()
