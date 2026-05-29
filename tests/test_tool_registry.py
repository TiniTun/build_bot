import json
import unittest

from tools.base import ToolErrorCode, ToolResult, tool
from tools.registry import ToolRegistry


class ToolRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_execute_tool_allows_tool_argument_named_name(self) -> None:
        @tool(
            name="echo_name",
            description="Echo a provided name.",
            parameters={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        )
        async def echo_name(session: object, name: str) -> str:
            return name

        registry = ToolRegistry()
        registry.register(echo_name)

        result = await registry.execute_tool(
            "echo_name",
            session=object(),
            name="Daily reminder",
        )

        self.assertEqual(result, "Daily reminder")

    def test_create_cron_job_schema_requires_name(self) -> None:
        schema = ToolRegistry.with_builtins().get("create_cron_job").get_tool_schema()

        required = schema["function"]["parameters"]["required"]

        self.assertIn("name", required)

    def test_with_builtins_registers_core_tools(self) -> None:
        registry = ToolRegistry.with_builtins()

        tool_names = {tool.name for tool in registry.list_all()}

        self.assertEqual(
            tool_names,
            {"read", "write", "edit", "create_cron_job", "bash"},
        )

    def test_tool_result_formats_success_as_plain_text(self) -> None:
        result = ToolResult.success("done")

        self.assertEqual(result.to_tool_content(), "done")

    def test_tool_result_formats_error_as_structured_json(self) -> None:
        result = ToolResult.error(
            ToolErrorCode.INVALID_ARGS,
            "Missing required path.",
            retryable=False,
            user_action="Provide a path.",
        )

        payload = json.loads(result.to_tool_content())

        self.assertEqual(
            payload,
            {
                "ok": False,
                "error": {
                    "code": "invalid_args",
                    "message": "Missing required path.",
                    "retryable": False,
                    "user_action": "Provide a path.",
                },
            },
        )

    async def test_execute_tool_normalizes_tool_result_errors(self) -> None:
        @tool(
            name="fail_cleanly",
            description="Return a structured error.",
            parameters={"type": "object", "properties": {}},
        )
        async def fail_cleanly(session: object) -> ToolResult:
            return ToolResult.error(
                ToolErrorCode.AUTH_MISSING,
                "API key is not configured.",
                user_action="Configure the API key.",
            )

        registry = ToolRegistry()
        registry.register(fail_cleanly)

        result = await registry.execute_tool("fail_cleanly", session=object())
        payload = json.loads(result)

        self.assertEqual(payload["error"]["code"], "auth_missing")
        self.assertEqual(payload["error"]["message"], "API key is not configured.")

    async def test_execute_tool_returns_structured_error_for_missing_tool(self) -> None:
        result = await ToolRegistry().execute_tool("missing_tool", session=object())

        payload = json.loads(result)

        self.assertEqual(payload["error"]["code"], "not_found")
        self.assertEqual(payload["error"]["message"], "Tool not found: missing_tool")
