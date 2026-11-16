"""NotionTaskProvider: the tasks domain over the hosted Notion MCP (host calls).

A fake hub stands in for ``McpHub.call_tool`` and returns either the recorded
``notion-query-data-sources`` payload (``tests/fixtures/notion_query_tasks.json``)
or crafted payloads. No network, no real workspace.
"""

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from provider.external_errors import (
    ProviderInvalidRequestError,
    ProviderInvalidResponseError,
)
from provider.mcp.models import McpCallResult
from provider.tasks import (
    BulkTaskUpdateRequest,
    NullTaskProvider,
    TaskUpdateRequest,
    get_task_provider,
)
from provider.tasks.notion import NotionTaskProvider
from tests.helpers import make_workspace
from tools.base import ToolErrorCode
from tools.external_support import provider_exception_to_result
from tools.task_tools import build_task_capabilities
from utils.config import Config, ExternalToolsConfig, TasksProviderConfig

FIXTURE = Path(__file__).parent / "fixtures" / "notion_query_tasks.json"
DATA_SOURCE_ID = "0b2c30e0-3aa2-47bb-85b7-69a1032cca73"
COLLECTION = f"collection://{DATA_SOURCE_ID}"
QUERY_TOOL = "notion-query-data-sources"


def _fixture_text() -> str:
    return FIXTURE.read_text()


class _FakeHub:
    """Records every call; answers from a per-tool queue of results."""

    def __init__(self, responses: dict[str, McpCallResult] | None = None) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self._responses = responses or {}

    async def call_tool(self, server_id, tool_name, arguments=None):
        self.calls.append((server_id, tool_name, arguments))
        if tool_name in self._responses:
            return self._responses[tool_name]
        return McpCallResult(server_id=server_id, tool_name=tool_name,
                             text_blocks=("{}",))


def _result(tool: str, text: str, *, is_error: bool = False) -> McpCallResult:
    return McpCallResult(server_id="notion", tool_name=tool,
                         text_blocks=(text,), is_error=is_error)


def _query_hub(text: str | None = None) -> _FakeHub:
    return _FakeHub({QUERY_TOOL: _result(QUERY_TOOL, text or _fixture_text())})


# 2026-11-15 20:00 UTC is 2026-11-16 06:00 in Brisbane (UTC+10, no DST): the
# UTC date and the local date differ, so a provider using UTC would be caught.
def _clock() -> datetime:
    return datetime(2026, 11, 15, 20, 0, tzinfo=timezone.utc)


def _provider(hub: _FakeHub, *, default_minutes: int = 30) -> NotionTaskProvider:
    config = SimpleNamespace(timezone="Australia/Brisbane")
    cfg = TasksProviderConfig(
        enabled=True, provider="notion", mcp_server="notion",
        data_source_id=DATA_SOURCE_ID, default_duration_minutes=default_minutes,
    )
    return NotionTaskProvider(config, cfg, hub, clock=_clock)


def _query_args(hub: _FakeHub) -> dict:
    server, tool, args = hub.calls[-1]
    assert server == "notion" and tool == QUERY_TOOL, (server, tool)
    return args["data"]


class NotionQueryTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_inbox_queries_status_inbox(self) -> None:
        hub = _query_hub()
        tasks = await _provider(hub).list_inbox(10)
        data = _query_args(hub)
        self.assertEqual(data["data_source_urls"], [COLLECTION])
        self.assertIn(f'FROM "{COLLECTION}"', data["query"])
        self.assertIn('"Status" = ?', data["query"])
        self.assertEqual(data["params"][0], "Inbox")
        self.assertNotIn("Inbox", data["query"])  # bound, never interpolated
        self.assertEqual(len(tasks), 3)
        first = tasks[0]
        self.assertEqual(first.id, "40121f6ce43981129943fd9117c4f9c3")
        self.assertEqual(first.content, "Write the provider")
        self.assertEqual(
            first.url, "https://app.notion.com/40121f6ce43981129943fd9117c4f9c3"
        )
        self.assertEqual(first.labels, [])

    async def test_list_today_includes_scheduled_due_and_doing(self) -> None:
        hub = _query_hub()
        await _provider(hub).list_today(10)
        data = _query_args(hub)
        query = data["query"]
        self.assertIn('"Status" = ?', query)
        self.assertIn('"date:Scheduled:start" < ?', query)
        self.assertIn('"date:Due:start" < ?', query)
        self.assertIn('"Status" NOT IN (?, ?)', query)
        params = data["params"]
        self.assertIn("Doing", params)
        self.assertIn("Done", params)
        self.assertIn("Dropped", params)
        # "Today" is Brisbane's 2026-11-16; "<= today" is "< tomorrow" so a
        # datetime due later today still counts.
        self.assertIn("2026-11-17", params)
        self.assertNotIn("2026-11-16", params)
        self.assertNotIn("2026-11-17", query)

    async def test_list_overdue_excludes_done_and_dropped(self) -> None:
        hub = _query_hub()
        await _provider(hub).list_overdue(10)
        data = _query_args(hub)
        self.assertIn('"Status" NOT IN (?, ?)', data["query"])
        self.assertIn('"date:Due:start" < ?', data["query"])
        self.assertEqual(data["params"][:3], ["Done", "Dropped", "2026-11-16"])

    async def test_limit_is_bound(self) -> None:
        hub = _query_hub()
        await _provider(hub).list_inbox(7)
        data = _query_args(hub)
        self.assertIn("LIMIT ?", data["query"])
        self.assertEqual(data["params"][-1], 7)

    async def test_priority_mapping(self) -> None:
        tasks = await _provider(_query_hub()).list_inbox(10)
        # Fixture: P2, P1, P3.
        self.assertEqual([t.priority for t in tasks], [3, 4, 2])

    async def test_priority_mapping_p4_and_absent(self) -> None:
        payload = json.loads(_fixture_text())
        payload["results"][0]["Priority"] = "P4"
        payload["results"][1]["Priority"] = None
        tasks = await _provider(_query_hub(json.dumps(payload))).list_inbox(10)
        self.assertEqual(tasks[0].priority, 1)
        self.assertEqual(tasks[1].priority, 1)

    async def test_estimate_and_assumed_duration(self) -> None:
        tasks = await _provider(_query_hub(), default_minutes=25).list_inbox(10)
        self.assertEqual(tasks[0].estimated_minutes, 120)
        self.assertFalse(tasks[0].duration_assumed)
        self.assertEqual(tasks[2].estimated_minutes, 25)
        self.assertTrue(tasks[2].duration_assumed)

    async def test_due_fields_map_from_due_start(self) -> None:
        payload = json.loads(_fixture_text())
        payload["results"][0]["date:Due:start"] = "2026-11-20T09:00:00.000+10:00"
        payload["results"][0]["date:Due:is_datetime"] = 1
        payload["results"][0]["Status"] = "Done"
        tasks = await _provider(_query_hub(json.dumps(payload))).list_inbox(10)
        self.assertEqual(tasks[0].due_date, "2026-11-20")
        self.assertEqual(tasks[0].due_string, "2026-11-20T09:00:00.000+10:00")
        self.assertTrue(tasks[0].is_completed)
        self.assertIsNone(tasks[1].due_date)

    async def test_search_uses_name_contains_param(self) -> None:
        hub = _query_hub()
        await _provider(hub).search("Rent 50%", 5)
        data = _query_args(hub)
        self.assertIn('LOWER("Name") LIKE ?', data["query"])
        self.assertIn('"Status" NOT IN (?, ?)', data["query"])
        self.assertIn("%rent 50\\%%", data["params"])
        self.assertNotIn("rent", data["query"].lower())


class NotionResponseStrictnessTests(unittest.IsolatedAsyncioTestCase):
    async def _assert_invalid(self, text: str, *, is_error: bool = False) -> None:
        hub = _FakeHub({QUERY_TOOL: _result(QUERY_TOOL, text, is_error=is_error)})
        with self.assertRaises(ProviderInvalidResponseError) as ctx:
            await _provider(hub).list_inbox(10)
        # The message never carries row data.
        self.assertNotIn("Write the provider", str(ctx.exception))
        self.assertNotIn("app.notion.com", str(ctx.exception))

    async def test_unexpected_payload_raises(self) -> None:
        await self._assert_invalid("not json")
        await self._assert_invalid(json.dumps({"rows": []}))
        await self._assert_invalid(json.dumps({"results": {}}))
        bad = json.loads(_fixture_text())
        del bad["results"][0]["Status"]
        await self._assert_invalid(json.dumps(bad))
        bad = json.loads(_fixture_text())
        bad["results"][0]["Priority"] = "urgent"
        await self._assert_invalid(json.dumps(bad))
        bad = json.loads(_fixture_text())
        bad["results"][0]["Estimate (min)"] = "120"
        await self._assert_invalid(json.dumps(bad))
        bad = json.loads(_fixture_text())
        bad["results"][0]["url"] = "https://app.notion.com/no-id-here"
        await self._assert_invalid(json.dumps(bad))

    async def test_error_result_raises(self) -> None:
        await self._assert_invalid(_fixture_text(), is_error=True)

    async def test_missing_text_block_raises(self) -> None:
        hub = _FakeHub({QUERY_TOOL: McpCallResult(server_id="notion",
                                                  tool_name=QUERY_TOOL)})
        with self.assertRaises(ProviderInvalidResponseError):
            await _provider(hub).list_inbox(10)

    def test_invalid_response_maps_to_provider_error(self) -> None:
        result = provider_exception_to_result(ProviderInvalidResponseError("x"))
        self.assertEqual(result.error_value.code, ToolErrorCode.PROVIDER_ERROR)


class NotionWriteTests(unittest.IsolatedAsyncioTestCase):
    async def test_quick_add_creates_inbox_page(self) -> None:
        hub = _FakeHub({"notion-create-pages": _result(
            "notion-create-pages",
            json.dumps({"pages": [{
                "id": "40121f6c-e439-8112-9943-fd9117c4f9c3",
                "url": "https://www.notion.so/40121f6ce43981129943fd9117c4f9c3",
            }]}),
        )})
        task = await _provider(hub).quick_add("Pay rent tomorrow #Finance")
        server, tool, args = hub.calls[-1]
        self.assertEqual((server, tool), ("notion", "notion-create-pages"))
        self.assertEqual(
            args,
            {
                "parent": {"type": "data_source_id", "data_source_id": DATA_SOURCE_ID},
                "pages": [{"properties": {
                    "Name": "Pay rent tomorrow #Finance",
                    "Status": "Inbox",
                    "Source": "Telegram",
                    "Created by agent": "__YES__",
                }}],
            },
        )
        self.assertEqual(task.id, "40121f6ce43981129943fd9117c4f9c3")
        self.assertEqual(task.content, "Pay rent tomorrow #Finance")
        self.assertEqual(
            task.url, "https://www.notion.so/40121f6ce43981129943fd9117c4f9c3"
        )

    async def test_quick_add_unknown_response_does_not_raise(self) -> None:
        hub = _FakeHub({"notion-create-pages": _result(
            "notion-create-pages", "Created 1 page.")})
        with self.assertLogs("provider.tasks.notion", level="WARNING"):
            task = await _provider(hub).quick_add("Buy milk")
        self.assertEqual(task.id, "")
        self.assertEqual(task.content, "Buy milk")

    async def test_quick_add_error_result_raises(self) -> None:
        hub = _FakeHub({"notion-create-pages": _result(
            "notion-create-pages", "denied", is_error=True)})
        with self.assertRaises(ProviderInvalidResponseError):
            await _provider(hub).quick_add("Buy milk")

    async def test_update_maps_fields(self) -> None:
        hub = _FakeHub()
        task = await _provider(hub).update(TaskUpdateRequest(
            task_id="abc", content="Renamed", due_string="2026-11-20", priority=4,
        ))
        server, tool, args = hub.calls[-1]
        self.assertEqual((server, tool), ("notion", "notion-update-page"))
        self.assertEqual(args, {
            "page_id": "abc",
            "command": "update_properties",
            "properties": {
                "Name": "Renamed",
                "date:Due:start": "2026-11-20",
                "date:Due:is_datetime": 0,
                "Priority": "P1",
            },
        })
        self.assertEqual(task.id, "abc")
        self.assertEqual(task.content, "Renamed")

    async def test_priority_mapping_in_update(self) -> None:
        for priority, label in ((4, "P1"), (3, "P2"), (2, "P3"), (1, "P4")):
            hub = _FakeHub()
            await _provider(hub).update(
                TaskUpdateRequest(task_id="abc", priority=priority))
            self.assertEqual(hub.calls[-1][2]["properties"], {"Priority": label})

    async def test_update_due_datetime(self) -> None:
        hub = _FakeHub()
        await _provider(hub).update(TaskUpdateRequest(
            task_id="abc", due_string="2026-11-20T09:30:00+10:00"))
        props = hub.calls[-1][2]["properties"]
        self.assertEqual(props["date:Due:start"], "2026-11-20T09:30:00+10:00")
        self.assertEqual(props["date:Due:is_datetime"], 1)

    async def test_update_rejects_labels_project_section(self) -> None:
        for field in ({"labels": ["x"]}, {"project_id": "p"}, {"section_id": "s"}):
            hub = _FakeHub()
            with self.assertRaises(ProviderInvalidRequestError) as ctx:
                await _provider(hub).update(
                    TaskUpdateRequest(task_id="abc", content="x", **field))
            self.assertIn("not supported by the notion provider", str(ctx.exception))
            self.assertEqual(hub.calls, [])

    async def test_update_due_string_requires_iso(self) -> None:
        for bad in ("tomorrow 9am", "next monday", "20/11/2026"):
            hub = _FakeHub()
            with self.assertRaises(ProviderInvalidRequestError):
                await _provider(hub).update(
                    TaskUpdateRequest(task_id="abc", due_string=bad))
            self.assertEqual(hub.calls, [])

    async def test_update_error_result_raises(self) -> None:
        hub = _FakeHub({"notion-update-page": _result(
            "notion-update-page", "nope", is_error=True)})
        with self.assertRaises(ProviderInvalidResponseError):
            await _provider(hub).update(TaskUpdateRequest(task_id="a", content="b"))

    async def test_complete_sets_done(self) -> None:
        hub = _FakeHub()
        await _provider(hub).complete("abc")
        self.assertEqual(hub.calls[-1], ("notion", "notion-update-page", {
            "page_id": "abc", "command": "update_properties",
            "properties": {"Status": "Done"},
        }))

    async def test_delete_sets_dropped(self) -> None:
        hub = _FakeHub()
        await _provider(hub).delete("abc")
        self.assertEqual(hub.calls[-1], ("notion", "notion-update-page", {
            "page_id": "abc", "command": "update_properties",
            "properties": {"Status": "Dropped"},
        }))

    async def test_bulk_update_applies_per_item(self) -> None:
        hub = _FakeHub()
        count = await _provider(hub).bulk_update(BulkTaskUpdateRequest(
            task_ids=["a", "b"], operation="complete"))
        self.assertEqual(count, 2)
        self.assertEqual([c[2]["page_id"] for c in hub.calls], ["a", "b"])


class NotionSelectionTests(unittest.TestCase):
    def _config(self, tmp: str, **tasks) -> Config:
        config = Config.load(make_workspace(Path(tmp)))
        config.external_tools = ExternalToolsConfig(
            tasks=TasksProviderConfig(enabled=True, provider="notion", **tasks)
        )
        return config

    def test_get_task_provider_selects_notion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(
                tmp, mcp_server="notion", data_source_id=DATA_SOURCE_ID)
            provider = get_task_provider(config, _FakeHub())
            self.assertIsInstance(provider, NotionTaskProvider)

    def test_notion_without_hub_or_ids_is_null(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(
                tmp, mcp_server="notion", data_source_id=DATA_SOURCE_ID)
            self.assertIsInstance(get_task_provider(config), NullTaskProvider)
            config.external_tools.tasks.data_source_id = None
            self.assertIsInstance(
                get_task_provider(config, _FakeHub()), NullTaskProvider)

    def test_data_source_id_must_be_a_bare_id(self) -> None:
        with self.assertRaises(ValueError):
            TasksProviderConfig(data_source_id='x" UNION SELECT 1 --')
        with self.assertRaises(ValueError):
            TasksProviderConfig(data_source_id=COLLECTION)


class NotionToolDescriptionTests(unittest.TestCase):
    def _descriptions(self, provider: str) -> dict[str, str]:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config.load(make_workspace(Path(tmp)))
            config.external_tools = ExternalToolsConfig(tasks=TasksProviderConfig(
                enabled=True, provider=provider, mcp_server="notion",
                data_source_id=DATA_SOURCE_ID,
            ))
            pairs = build_task_capabilities(config, _FakeHub())
        return {cap.tool_name: tool.description for cap, tool in pairs}

    def test_notion_descriptions_drop_todoist_syntax(self) -> None:
        notion = self._descriptions("notion")
        self.assertNotIn("Todoist", notion["tasks_quick_add"])
        self.assertNotIn("Todoist", notion["tasks_search"])
        self.assertIn("Inbox", notion["tasks_quick_add"])
        self.assertIn("name", notion["tasks_search"].lower())

    def test_todoist_descriptions_unchanged(self) -> None:
        todoist = self._descriptions("todoist")
        self.assertIn("Todoist quick-add syntax", todoist["tasks_quick_add"])
        self.assertIn("Todoist filter query", todoist["tasks_search"])


if __name__ == "__main__":
    unittest.main()
