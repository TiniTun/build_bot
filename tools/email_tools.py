"""Email capability tool factories (search / read / draft_reply).

External email tools are disabled by default. When ``external_tools.email`` is
not enabled, ``build_email_capabilities`` returns an empty list (tools hidden).
When enabled but no real client is wired in, the null provider surfaces a stable
``AUTH_MISSING`` error.
"""

from typing import TYPE_CHECKING

from provider.email import get_email_provider
from tools.base import BaseTool, ToolErrorCode, ToolResult, tool
from tools.capabilities import CapabilityDef, ToolRiskLevel
from tools.external_support import provider_exception_to_result

if TYPE_CHECKING:
    from core.agent import AgentSession
    from utils.config import Config


def build_email_capabilities(
    config: "Config",
) -> list[tuple[CapabilityDef, BaseTool]]:
    """Build email capability/tool pairs, or [] when email is disabled."""
    if not config.external_tools.email.enabled:
        return []

    provider = get_email_provider(config)

    @tool(
        name="email_search",
        description="Search the user's mailbox and return matching message summaries.",
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
    async def email_search(
        query: str, session: "AgentSession", limit: int = 10
    ) -> str:
        try:
            results = await provider.search(query, limit)
        except Exception as e:  # noqa: BLE001 - mapped to stable error codes
            return provider_exception_to_result(e).to_tool_content()
        if not results:
            return ToolResult.success("No matching emails found.").to_tool_content()
        lines = [
            f"- [{r.id}] {r.subject} — {r.sender}: {r.snippet}" for r in results
        ]
        return ToolResult.success("\n".join(lines)).to_tool_content()

    @tool(
        name="email_read",
        description="Read a single email message by id.",
        parameters={
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "description": "Email message id."},
            },
            "required": ["message_id"],
        },
    )
    async def email_read(message_id: str, session: "AgentSession") -> str:
        try:
            msg = await provider.read(message_id)
        except Exception as e:  # noqa: BLE001 - mapped to stable error codes
            return provider_exception_to_result(e).to_tool_content()
        body = (
            f"Subject: {msg.subject}\nFrom: {msg.sender}\n"
            f"To: {', '.join(msg.recipients)}\n\n{msg.body}"
        )
        return ToolResult.success(body).to_tool_content()

    @tool(
        name="email_draft_reply",
        description="Create a draft reply to a message. Does not send the email.",
        parameters={
            "type": "object",
            "properties": {
                "message_id": {
                    "type": "string",
                    "description": "Message id to reply to.",
                },
                "body": {"type": "string", "description": "Draft reply body."},
            },
            "required": ["message_id", "body"],
        },
    )
    async def email_draft_reply(
        message_id: str, body: str, session: "AgentSession"
    ) -> str:
        if not body.strip():
            return ToolResult.error(
                ToolErrorCode.INVALID_ARGS, "Draft body must not be empty."
            ).to_tool_content()
        try:
            draft = await provider.draft_reply(message_id, body)
        except Exception as e:  # noqa: BLE001 - mapped to stable error codes
            return provider_exception_to_result(e).to_tool_content()
        return ToolResult.success(
            f"Draft created (draft_id={draft.draft_id}). Not sent."
        ).to_tool_content()

    return [
        (
            CapabilityDef(
                id="email.search",
                tool_name="email_search",
                domain="email",
                operation="search",
                description="Search the mailbox for messages.",
                risk_level=ToolRiskLevel.READ,
                required_config=["external_tools.email"],
            ),
            email_search,
        ),
        (
            CapabilityDef(
                id="email.read",
                tool_name="email_read",
                domain="email",
                operation="read",
                description="Read a single email message.",
                risk_level=ToolRiskLevel.READ,
                required_config=["external_tools.email"],
            ),
            email_read,
        ),
        (
            CapabilityDef(
                id="email.draft_reply",
                tool_name="email_draft_reply",
                domain="email",
                operation="draft_reply",
                description="Draft a reply without sending it.",
                risk_level=ToolRiskLevel.DRAFT,
                required_config=["external_tools.email"],
            ),
            email_draft_reply,
        ),
    ]
