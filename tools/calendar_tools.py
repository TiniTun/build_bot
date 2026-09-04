"""Calendar capability tool factories (search / availability / create_event).

External calendar tools are disabled by default. ``calendar.create_event`` is
confirmation-gated: invoking it records a pending action and returns a
``requires_confirmation`` result; it never calls the provider's ``create_event``
in this iteration, so it cannot mutate external state without confirmation.
"""

import logging
import uuid
from typing import TYPE_CHECKING

from pydantic import ValidationError

from core.pending_actions import PendingActionStore
from provider.calendar import CreateEventRequest, get_calendar_provider
from provider.places import build_map_links
from tools.base import BaseTool, ToolErrorCode, ToolResult, tool
from tools.capabilities import CapabilityDef, ToolRiskLevel
from tools.confirmed_executors import ConfirmedExecutor
from tools.external_support import provider_exception_to_result

if TYPE_CHECKING:
    from core.agent import AgentSession
    from provider.places import PlacesProvider
    from utils.config import Config


logger = logging.getLogger(__name__)


CREATE_EVENT_CAPABILITY_ID = "calendar.create_event"
UPDATE_EVENT_CAPABILITY_ID = "calendar.update_event"
DELETE_EVENT_CAPABILITY_ID = "calendar.delete_event"


def build_calendar_capabilities(
    config: "Config",
    places_provider: "PlacesProvider | None" = None,
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
        geocode = config.external_tools.calendar.geocode_locations
        lines = []
        for ev in events:
            line = f"- {ev.title} ({ev.start} → {ev.end})"
            if geocode and places_provider is not None and ev.location:
                try:
                    matches = await places_provider.search(ev.location)
                    if matches:
                        p = matches[0]
                        links = build_map_links(p.lat, p.lon, p.name, p.place_id)
                        line += (
                            f"\n  Apple Maps: {links.apple_maps_url}"
                            f"\n  Google Maps: {links.google_maps_url}"
                        )
                except Exception:  # noqa: BLE001 - geocoding is best-effort
                    # Non-blocking: skip links for this event, never fail the call.
                    logger.debug(
                        "geocoding failed for event location %r", ev.location,
                        exc_info=True,
                    )
            lines.append(line)
        return ToolResult.success("\n".join(lines)).to_tool_content()

    @tool(
        name="calendar_day_agenda",
        description=(
            "Every event on a date plus the free intervals between them. "
            "Returns the whole day, not a search."
        ),
        parameters={
            "type": "object",
            "properties": {
                "date": {"type": "string",
                         "description": "The day, as YYYY-MM-DD."},
                "timezone": {"type": "string",
                             "description": "IANA timezone (optional; "
                                            "defaults to the configured one)."},
            },
            "required": ["date"],
        },
    )
    async def calendar_day_agenda(
        date: str, session: "AgentSession", timezone: str | None = None
    ) -> str:
        zone = timezone or getattr(config, "timezone", None) or "UTC"
        try:
            events = await provider.list_day(date, zone)
        except Exception as e:  # noqa: BLE001 - mapped to stable error codes
            return provider_exception_to_result(e).to_tool_content()
        if not events:
            return ToolResult.success(f"No events on {date}.").to_tool_content()
        lines = []
        for event in events:
            when = "all day" if event.all_day else f"{event.start} → {event.end}"
            note = " (free)" if event.transparent else ""
            note += " (declined)" if event.self_declined else ""
            lines.append(f"- {event.title} ({when}){note}")
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

    @tool(
        name="calendar_update_event",
        description=(
            "Propose updating an existing calendar event. This requires user "
            "confirmation and does not modify the event directly."
        ),
        parameters={
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "Event id to update."},
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
            "required": ["event_id", "title", "start", "end"],
        },
    )
    async def calendar_update_event(
        event_id: str,
        title: str,
        start: str,
        end: str,
        session: "AgentSession",
        attendees: list[str] | None = None,
        location: str | None = None,
        description: str | None = None,
    ) -> str:
        # Validate the request shape, but never call provider.update_event here.
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
                user_action="Provide event_id, title, ISO start, and ISO end.",
            ).to_tool_content()

        action_id = str(uuid.uuid4())
        summary = (
            f"Update calendar event {event_id}: {request.title} "
            f"({request.start} → {request.end})"
        )
        payload = {"event_id": event_id, **request.model_dump()}
        PendingActionStore(session.shared_context.config).create(
            action_id=action_id,
            capability_id=UPDATE_EVENT_CAPABILITY_ID,
            summary=summary,
            payload=payload,
        )
        return ToolResult.requires_confirmation(
            action_id=action_id,
            capability_id=UPDATE_EVENT_CAPABILITY_ID,
            summary=summary,
            payload=payload,
        ).to_tool_content()

    @tool(
        name="calendar_delete_event",
        description=(
            "Propose deleting a calendar event. This requires user confirmation "
            "and does not delete the event directly."
        ),
        parameters={
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "Event id to delete."},
            },
            "required": ["event_id"],
        },
    )
    async def calendar_delete_event(
        event_id: str,
        session: "AgentSession",
    ) -> str:
        if not event_id:
            return ToolResult.error(
                ToolErrorCode.INVALID_ARGS,
                "An event_id is required to delete an event.",
                user_action="Provide the event_id to delete.",
            ).to_tool_content()

        action_id = str(uuid.uuid4())
        summary = f"Delete calendar event {event_id}"
        payload = {"event_id": event_id}
        PendingActionStore(session.shared_context.config).create(
            action_id=action_id,
            capability_id=DELETE_EVENT_CAPABILITY_ID,
            summary=summary,
            payload=payload,
        )
        return ToolResult.requires_confirmation(
            action_id=action_id,
            capability_id=DELETE_EVENT_CAPABILITY_ID,
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
                id="calendar.day_agenda",
                tool_name="calendar_day_agenda",
                domain="calendar",
                operation="day_agenda",
                description="List every event and free interval for one date.",
                risk_level=ToolRiskLevel.READ,
                required_config=["external_tools.calendar"],
            ),
            calendar_day_agenda,
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
        (
            CapabilityDef(
                id=UPDATE_EVENT_CAPABILITY_ID,
                tool_name="calendar_update_event",
                domain="calendar",
                operation="update_event",
                description="Update a calendar event after explicit confirmation.",
                risk_level=ToolRiskLevel.CONFIRM_REQUIRED,
                required_config=["external_tools.calendar"],
            ),
            calendar_update_event,
        ),
        (
            CapabilityDef(
                id=DELETE_EVENT_CAPABILITY_ID,
                tool_name="calendar_delete_event",
                domain="calendar",
                operation="delete_event",
                description="Delete a calendar event after explicit confirmation.",
                risk_level=ToolRiskLevel.CONFIRM_REQUIRED,
                required_config=["external_tools.calendar"],
            ),
            calendar_delete_event,
        ),
    ]


def build_calendar_confirmed_executors(
    config: "Config",
) -> dict[str, ConfirmedExecutor]:
    """Confirmed executors for calendar mutations, or {} when calendar is disabled.

    Each executor performs the real provider mutation exactly once. ``/confirm``
    routes a stored pending action here instead of re-invoking the proposal tool,
    so confirming never creates another pending action.
    """
    if not config.external_tools.calendar.enabled:
        return {}

    provider = get_calendar_provider(config)

    async def create_event_confirmed(
        session: "AgentSession", payload: dict
    ) -> str:
        try:
            request = CreateEventRequest(**payload)
        except (ValidationError, TypeError):
            return ToolResult.error(
                ToolErrorCode.INVALID_ARGS,
                "Stored event request is invalid.",
            ).to_tool_content()
        try:
            event = await provider.create_event(request)
        except Exception as e:  # noqa: BLE001 - mapped to stable error codes
            return provider_exception_to_result(e).to_tool_content()
        return ToolResult.success(
            f"Event created (id={event.id}): {event.title} "
            f"({event.start} → {event.end})."
        ).to_tool_content()

    async def update_event_confirmed(
        session: "AgentSession", payload: dict
    ) -> str:
        event_id = payload.get("event_id")
        if not event_id:
            return ToolResult.error(
                ToolErrorCode.INVALID_ARGS,
                "Stored update request is missing an event_id.",
            ).to_tool_content()
        try:
            request = CreateEventRequest(
                **{k: v for k, v in payload.items() if k != "event_id"}
            )
        except (ValidationError, TypeError):
            return ToolResult.error(
                ToolErrorCode.INVALID_ARGS,
                "Stored event request is invalid.",
            ).to_tool_content()
        try:
            event = await provider.update_event(event_id, request)
        except Exception as e:  # noqa: BLE001 - mapped to stable error codes
            return provider_exception_to_result(e).to_tool_content()
        return ToolResult.success(
            f"Event updated (id={event.id}): {event.title} "
            f"({event.start} → {event.end})."
        ).to_tool_content()

    async def delete_event_confirmed(
        session: "AgentSession", payload: dict
    ) -> str:
        event_id = payload.get("event_id")
        if not event_id:
            return ToolResult.error(
                ToolErrorCode.INVALID_ARGS,
                "Stored delete request is missing an event_id.",
            ).to_tool_content()
        try:
            await provider.delete_event(event_id)
        except Exception as e:  # noqa: BLE001 - mapped to stable error codes
            return provider_exception_to_result(e).to_tool_content()
        return ToolResult.success(
            f"Event deleted (id={event_id})."
        ).to_tool_content()

    return {
        CREATE_EVENT_CAPABILITY_ID: create_event_confirmed,
        UPDATE_EVENT_CAPABILITY_ID: update_event_confirmed,
        DELETE_EVENT_CAPABILITY_ID: delete_event_confirmed,
    }
