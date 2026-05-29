"""Tool registry for managing available tools."""

from typing import TYPE_CHECKING, Any

from tools.base import BaseTool, ToolErrorCode, ToolResult
from tools.builtin_tools import read_file, write_file, edit_file, create_cron_job, bash

if TYPE_CHECKING:
    from core.agent import AgentSession


class ToolRegistry:
    """Registry for all available tools."""

    def __init__(self) -> None:
        """Initialize an empty tool registry."""
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        """Register a tool."""
        self._tools[tool.name] = tool

    def get(self, name: str) -> BaseTool | None:
        """Get a tool by name."""
        return self._tools.get(name)
    
    def list_all(self) -> list[BaseTool]:
        """List all registered tools."""
        return list(self._tools.values())
    
    def get_tool_schemas(self) -> list[dict[str,Any]]:
        """Get tool schemas for all registered tools."""
        return [tool.get_tool_schema() for tool in self._tools.values()]
    
    async def execute_tool(
            self, tool_name: str, session: "AgentSession", **kwargs: Any
    ) -> str:
        """Execute a tool by name."""
        tool = self.get(tool_name)
        if tool is None:
            return ToolResult.error(
                ToolErrorCode.NOT_FOUND,
                f"Tool not found: {tool_name}",
            ).to_tool_content()
        
        try:
            return await tool.execute(session=session, **kwargs)
        except TypeError as e:
            return ToolResult.error(
                ToolErrorCode.INVALID_ARGS,
                f"Invalid arguments for tool {tool_name}: {e}",
                user_action="Retry the tool call with arguments matching its schema.",
            ).to_tool_content()
        except Exception as e:
            return ToolResult.error(
                ToolErrorCode.PROVIDER_ERROR,
                f"Tool {tool_name} failed: {e}",
                retryable=True,
            ).to_tool_content()
    
    @classmethod
    def with_builtins(cls) -> "ToolRegistry":
        """Create a ToolRegistry with builtin tools already registered."""
        
        registry = cls()

        registry.register(read_file)
        registry.register(write_file)
        registry.register(edit_file)
        registry.register(create_cron_job)
        registry.register(bash)

        return registry
