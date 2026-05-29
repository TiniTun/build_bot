"""Calendar capability tool factories (search / availability / create_event).

External calendar tools are disabled by default. ``calendar.create_event`` is
confirmation-gated: invoking it records a pending action and returns a
``requires_confirmation`` result; it never calls the provider's ``create_event``
in this iteration, so it cannot mutate external state without confirmation.
"""

import uuid
from typing import TYPE_CHECKING

from pydantic import ValidationError

from core.pending_actions import PendingActionStore
from provider.calendar import CreateEventRequest, get_calendar_provider
from tools.base import BaseTool, ToolErrorCode, ToolResult, tool
from tools.capabilities import CapabilityDef, ToolRiskLevel
from tools.external_support import provider_exception_to_result

if TYPE_CHECKING:
    from core.agent import AgentSession
    from utils.config import Config


CREATE_EVENT_CAPABILITY_ID = "calendar.create_event"


def build_calendar_capabilities(
    config: "Config",
) -> list[tuple[CapabilityDef, BaseTool]]:
    """Build calendar capability/tool pairs, or [] when calendar is disabled."""
    if not config.external_tools.calendar.enabled:
        return []

    provider = get_calendar_provider(config)

    @tool(
        name="calendar_search",
        description="Search calendar events within an optional time window.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query."},
                "time_min": {
                    "type": "string",
                    "description": "ISO lower bound (optional).",
                },
                "time_max": {
                    "type": "string",
                    "description": "ISO upper bound (optional).",
                },
            },
            "required": ["query"],
        },
    )
    async def calendar_search(
        query: str,
        session: "AgentSession",
        time_min: str | None = None,
        time_max: str | None = None,
    ) -> str:
        try:
            events = await provider.search(query, time_min, time_max)
        except Exception as e:  # noqa: BLE001 - mapped to stable error codes
            return provider_exception_to_result(e).to_tool_content()
        if not events:
            return ToolResult.success("No matching events found.").to_tool_content()
        lines = [f"- {ev.title} ({ev.start} → {ev.end})" for ev in events]
        return ToolResult.success("\n".join(lines)).to_tool_content()

    @tool(
        name="calendar_availability",
        description="Check free/busy availability for attendees over a time window.",
        parameters={
            "type": "object",
            "properties": {
                "attendees": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Attendee emails.",
                },
                "time_min": {"type": "string", "description": "ISO window start."},
                "time_max": {"type": "string", "description": "ISO window end."},
            },
            "required": ["attendees", "time_min", "time_max"],
        },
    )
    async def calendar_availability(
        attendees: list[str],
        time_min: str,
        time_max: str,
        session: "AgentSession",
    ) -> str:
        try:
            result = await provider.availability(attendees, time_min, time_max)
        except Exception as e:  # noqa: BLE001 - mapped to stable error codes
            return provider_exception_to_result(e).to_tool_content()
        status = "available" if result.available else "busy"
        return ToolResult.success(
            f"Attendees are {status} ({len(result.busy)} busy blocks)."
        ).to_tool_content()

    @tool(
        name="calendar_create_event",
        description=(
            "Propose creating a calendar event. This requires user confirmation "
            "and does not create the event directly."
        ),
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Event title."},
                "start": {"type": "string", "description": "ISO start datetime."},
                "end": {"type": "string", "description": "ISO end datetime."},
                "attendees": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Attendee emails (optional).",
                },
                "location": {"type": "string", "description": "Location (optional)."},
                "description": {
                    "type": "string",
                    "description": "Event description (optional).",
                },
            },
            "required": ["title", "start", "end"],
        },
    )
    async def calendar_create_event(
        title: str,
        start: str,
        end: str,
        session: "AgentSession",
        attendees: list[str] | None = None,
        location: str | None = None,
        description: str | None = None,
    ) -> str:
        # Validate the request shape, but never call provider.create_event here.
        try:
            request = CreateEventRequest(
                title=title,
                start=start,
                end=end,
                attendees=attendees or [],
                location=location,
                description=description,
            )
        except ValidationError as e:
            return ToolResult.error(
                ToolErrorCode.INVALID_ARGS,
                f"Invalid event request: {e.error_count()} field error(s).",
                user_action="Provide title, ISO start, and ISO end.",
            ).to_tool_content()

        action_id = str(uuid.uuid4())
        summary = (
            f"Create calendar event: {request.title} "
            f"({request.start} → {request.end})"
        )
        payload = request.model_dump()
        PendingActionStore(session.shared_context.config).create(
            action_id=action_id,
            capability_id=CREATE_EVENT_CAPABILITY_ID,
            summary=summary,
            payload=payload,
        )
        return ToolResult.requires_confirmation(
            action_id=action_id,
            capability_id=CREATE_EVENT_CAPABILITY_ID,
            summary=summary,
            payload=payload,
        ).to_tool_content()

    return [
        (
            CapabilityDef(
                id="calendar.search",
                tool_name="calendar_search",
                domain="calendar",
                operation="search",
                description="Search calendar events.",
                risk_level=ToolRiskLevel.READ,
                required_config=["external_tools.calendar"],
            ),
            calendar_search,
        ),
        (
            CapabilityDef(
                id="calendar.availability",
                tool_name="calendar_availability",
                domain="calendar",
                operation="availability",
                description="Check attendee availability.",
                risk_level=ToolRiskLevel.READ,
                required_config=["external_tools.calendar"],
            ),
            calendar_availability,
        ),
        (
            CapabilityDef(
                id=CREATE_EVENT_CAPABILITY_ID,
                tool_name="calendar_create_event",
                domain="calendar",
                operation="create_event",
                description="Create a calendar event after explicit confirmation.",
                risk_level=ToolRiskLevel.CONFIRM_REQUIRED,
                required_config=["external_tools.calendar"],
            ),
            calendar_create_event,
        ),
    ]
