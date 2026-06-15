"""Memory capability tool factories (search / store_* / append_daily_note).

Memory tools are local and always available (no external provider). They are
backed by :class:`core.memory_store.MemoryStore`, a small Markdown-backed store
rooted at ``config.memories_path``. ``ValueError`` from the store is converted
into a stable ``INVALID_ARGS`` tool error.
"""

from typing import TYPE_CHECKING

from core.memory_store import MemoryStore
from tools.base import BaseTool, ToolErrorCode, ToolResult, tool
from tools.capabilities import CapabilityDef, ToolRiskLevel

if TYPE_CHECKING:
    from core.agent import AgentSession
    from utils.config import Config


def build_memory_capabilities(
    config: "Config",
) -> list[tuple[CapabilityDef, BaseTool]]:
    """Build memory capability/tool pairs. Always available (local store)."""
    store = MemoryStore(config)

    @tool(
        name="memory_search",
        description=(
            "Search stored memories (facts, preferences, projects, decisions, "
            "daily notes) and return matching snippets."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query."},
                "limit": {
                    "type": "integer",
                    "description": "Max results to return.",
                    "default": 10,
                },
            },
            "required": ["query"],
        },
    )
    async def memory_search(
        query: str, session: "AgentSession", limit: int = 10
    ) -> str:
        try:
            hits = store.search(query, limit)
        except ValueError as e:
            return ToolResult.error(
                ToolErrorCode.INVALID_ARGS, str(e)
            ).to_tool_content()
        if not hits:
            return ToolResult.success(
                "No matching memories found."
            ).to_tool_content()
        lines = [
            f"- [{h.category}] {h.path}: {h.snippet}" for h in hits
        ]
        return ToolResult.success("\n".join(lines)).to_tool_content()

    @tool(
        name="memory_store_fact",
        description="Store a durable fact in long-term memory.",
        parameters={
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "The fact to store."},
            },
            "required": ["content"],
        },
    )
    async def memory_store_fact(content: str, session: "AgentSession") -> str:
        try:
            path = store.store_fact(content)
        except ValueError as e:
            return ToolResult.error(
                ToolErrorCode.INVALID_ARGS, str(e)
            ).to_tool_content()
        return ToolResult.success(
            f"Fact stored in {path.name}."
        ).to_tool_content()

    @tool(
        name="memory_store_preference",
        description="Store a user preference in long-term memory.",
        parameters={
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The preference to store.",
                },
            },
            "required": ["content"],
        },
    )
    async def memory_store_preference(
        content: str, session: "AgentSession"
    ) -> str:
        try:
            path = store.store_preference(content)
        except ValueError as e:
            return ToolResult.error(
                ToolErrorCode.INVALID_ARGS, str(e)
            ).to_tool_content()
        return ToolResult.success(
            f"Preference stored in {path.name}."
        ).to_tool_content()

    @tool(
        name="memory_store_project_context",
        description=(
            "Store project-specific context. Requires a project identifier."
        ),
        parameters={
            "type": "object",
            "properties": {
                "project": {
                    "type": "string",
                    "description": "Project identifier (normalized to a filename).",
                },
                "content": {
                    "type": "string",
                    "description": "The project context to store.",
                },
            },
            "required": ["project", "content"],
        },
    )
    async def memory_store_project_context(
        project: str, content: str, session: "AgentSession"
    ) -> str:
        if not project or not project.strip():
            return ToolResult.error(
                ToolErrorCode.INVALID_ARGS,
                "A non-blank project identifier is required.",
            ).to_tool_content()
        try:
            path = store.store_project_context(project, content)
        except ValueError as e:
            return ToolResult.error(
                ToolErrorCode.INVALID_ARGS, str(e)
            ).to_tool_content()
        return ToolResult.success(
            f"Project context stored in {path.name}."
        ).to_tool_content()

    @tool(
        name="memory_store_decision",
        description="Store a decision with an optional rationale.",
        parameters={
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The decision to record.",
                },
                "rationale": {
                    "type": "string",
                    "description": "Optional rationale for the decision.",
                    "default": "",
                },
            },
            "required": ["content"],
        },
    )
    async def memory_store_decision(
        content: str, session: "AgentSession", rationale: str = ""
    ) -> str:
        try:
            path = store.store_decision(content, rationale)
        except ValueError as e:
            return ToolResult.error(
                ToolErrorCode.INVALID_ARGS, str(e)
            ).to_tool_content()
        return ToolResult.success(
            f"Decision stored in {path.name}."
        ).to_tool_content()

    @tool(
        name="memory_append_daily_note",
        description="Append a note to today's daily notes file.",
        parameters={
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The note to append.",
                },
            },
            "required": ["content"],
        },
    )
    async def memory_append_daily_note(
        content: str, session: "AgentSession"
    ) -> str:
        try:
            path = store.append_daily_note(content)
        except ValueError as e:
            return ToolResult.error(
                ToolErrorCode.INVALID_ARGS, str(e)
            ).to_tool_content()
        return ToolResult.success(
            f"Daily note appended to {path.name}."
        ).to_tool_content()

    return [
        (
            CapabilityDef(
                id="memory.search",
                tool_name="memory_search",
                domain="memory",
                operation="search",
                description="Search stored memories for matching snippets.",
                risk_level=ToolRiskLevel.READ,
            ),
            memory_search,
        ),
        (
            CapabilityDef(
                id="memory.store_fact",
                tool_name="memory_store_fact",
                domain="memory",
                operation="store_fact",
                description="Store a durable fact.",
                risk_level=ToolRiskLevel.WRITE,
            ),
            memory_store_fact,
        ),
        (
            CapabilityDef(
                id="memory.store_preference",
                tool_name="memory_store_preference",
                domain="memory",
                operation="store_preference",
                description="Store a user preference.",
                risk_level=ToolRiskLevel.WRITE,
            ),
            memory_store_preference,
        ),
        (
            CapabilityDef(
                id="memory.store_project_context",
                tool_name="memory_store_project_context",
                domain="memory",
                operation="store_project_context",
                description="Store project-specific context.",
                risk_level=ToolRiskLevel.WRITE,
            ),
            memory_store_project_context,
        ),
        (
            CapabilityDef(
                id="memory.store_decision",
                tool_name="memory_store_decision",
                domain="memory",
                operation="store_decision",
                description="Store a decision with optional rationale.",
                risk_level=ToolRiskLevel.WRITE,
            ),
            memory_store_decision,
        ),
        (
            CapabilityDef(
                id="memory.append_daily_note",
                tool_name="memory_append_daily_note",
                domain="memory",
                operation="append_daily_note",
                description="Append a note to today's daily notes.",
                risk_level=ToolRiskLevel.WRITE,
            ),
            memory_append_daily_note,
        ),
    ]
