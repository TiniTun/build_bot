import unittest

from tools.base import tool
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
