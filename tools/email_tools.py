"""Email capability tool factories (search / read / draft_reply).

External email tools are disabled by default. When ``external_tools.email`` is
not enabled, ``build_email_capabilities`` returns an empty list (tools hidden).
When enabled but no real client is wired in, the null provider surfaces a stable
``AUTH_MISSING`` error.
"""

import uuid
from typing import TYPE_CHECKING

from core.pending_actions import PendingActionStore
from provider.email import get_email_provider
from tools.base import BaseTool, ToolErrorCode, ToolResult, tool
from tools.capabilities import CapabilityDef, ToolRiskLevel
from tools.confirmed_executors import ConfirmedExecutor
from tools.external_support import provider_exception_to_result

if TYPE_CHECKING:
    from core.agent import AgentSession
    from utils.config import Config


SEND_CAPABILITY_ID = "email.send"
DELETE_CAPABILITY_ID = "email.delete"


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

    @tool(
        name="email_send",
        description=(
            "Propose sending a reply to a message. This requires user "
            "confirmation and does not send the email directly."
        ),
        parameters={
            "type": "object",
            "properties": {
                "message_id": {
                    "type": "string",
                    "description": "Message id to reply to.",
                },
                "body": {"type": "string", "description": "Reply body to send."},
            },
            "required": ["message_id", "body"],
        },
    )
    async def email_send(
        message_id: str, body: str, session: "AgentSession"
    ) -> str:
        # Self-gate: record a pending action; never call the provider here.
        if not message_id.strip() or not body.strip():
            return ToolResult.error(
                ToolErrorCode.INVALID_ARGS,
                "Both message_id and a non-empty body are required.",
                user_action="Provide the reply target message_id and body.",
            ).to_tool_content()

        action_id = str(uuid.uuid4())
        summary = f"Send reply to message {message_id}"
        payload = {"message_id": message_id, "body": body}
        PendingActionStore(session.shared_context.config).create(
            action_id=action_id,
            capability_id=SEND_CAPABILITY_ID,
            summary=summary,
            payload=payload,
        )
        return ToolResult.requires_confirmation(
            action_id=action_id,
            capability_id=SEND_CAPABILITY_ID,
            summary=summary,
            payload=payload,
        ).to_tool_content()

    @tool(
        name="email_delete",
        description=(
            "Propose deleting (trashing) a message. This requires user "
            "confirmation and does not delete the message directly."
        ),
        parameters={
            "type": "object",
            "properties": {
                "message_id": {
                    "type": "string",
                    "description": "Message id to trash.",
                },
            },
            "required": ["message_id"],
        },
    )
    async def email_delete(message_id: str, session: "AgentSession") -> str:
        # Self-gate: record a pending action; never call the provider here.
        if not message_id.strip():
            return ToolResult.error(
                ToolErrorCode.INVALID_ARGS,
                "A non-empty message_id is required.",
                user_action="Provide the message_id to trash.",
            ).to_tool_content()

        action_id = str(uuid.uuid4())
        summary = f"Trash message {message_id}"
        payload = {"message_id": message_id}
        PendingActionStore(session.shared_context.config).create(
            action_id=action_id,
            capability_id=DELETE_CAPABILITY_ID,
            summary=summary,
            payload=payload,
        )
        return ToolResult.requires_confirmation(
            action_id=action_id,
            capability_id=DELETE_CAPABILITY_ID,
            summary=summary,
            payload=payload,
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
        (
            CapabilityDef(
                id=SEND_CAPABILITY_ID,
                tool_name="email_send",
                domain="email",
                operation="send",
                description="Send a reply after explicit confirmation.",
                risk_level=ToolRiskLevel.CONFIRM_REQUIRED,
                required_config=["external_tools.email"],
            ),
            email_send,
        ),
        (
            CapabilityDef(
                id=DELETE_CAPABILITY_ID,
                tool_name="email_delete",
                domain="email",
                operation="delete",
                description="Trash a message after explicit confirmation.",
                risk_level=ToolRiskLevel.CONFIRM_REQUIRED,
                required_config=["external_tools.email"],
            ),
            email_delete,
        ),
    ]


def build_email_confirmed_executors(
    config: "Config",
) -> dict[str, ConfirmedExecutor]:
    """Confirmed executors for email mutations, or {} when email is disabled.

    Mutating email capabilities (e.g. ``email.send`` / ``email.delete``) register
    their real provider execution here. ``/confirm`` routes a stored pending
    action to the matching executor exactly once instead of re-invoking the
    proposal tool, so confirming never creates another pending action.
    """
    if not config.external_tools.email.enabled:
        return {}

    provider = get_email_provider(config)

    async def send_confirmed(session: "AgentSession", payload: dict) -> str:
        message_id = payload.get("message_id")
        body = payload.get("body")
        if not isinstance(message_id, str) or not isinstance(body, str):
            return ToolResult.error(
                ToolErrorCode.INVALID_ARGS,
                "Stored send request is invalid.",
            ).to_tool_content()
        try:
            sent = await provider.send_reply(message_id, body)
        except Exception as e:  # noqa: BLE001 - mapped to stable error codes
            return provider_exception_to_result(e).to_tool_content()
        return ToolResult.success(
            f"Reply sent (id={sent.get('id', '')})."
        ).to_tool_content()

    async def delete_confirmed(session: "AgentSession", payload: dict) -> str:
        message_id = payload.get("message_id")
        if not isinstance(message_id, str):
            return ToolResult.error(
                ToolErrorCode.INVALID_ARGS,
                "Stored delete request is invalid.",
            ).to_tool_content()
        try:
            await provider.delete(message_id)
        except Exception as e:  # noqa: BLE001 - mapped to stable error codes
            return provider_exception_to_result(e).to_tool_content()
        return ToolResult.success(
            f"Message {message_id} moved to trash."
        ).to_tool_content()

    return {
        SEND_CAPABILITY_ID: send_confirmed,
        DELETE_CAPABILITY_ID: delete_confirmed,
    }
