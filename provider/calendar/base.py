"""Calendar provider protocol, data models, and a null provider.

``get_calendar_provider`` selects ``GoogleCalendarProvider`` when the calendar
domain is enabled with ``provider == "google_calendar"``; otherwise it returns a
``NullCalendarProvider`` that raises ``AuthMissingError``.

Note: ``calendar.create_event`` is confirmation-gated at the tool layer and
never reaches a provider without an explicit confirmation step.
"""

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from provider.external_errors import AuthMissingError

if TYPE_CHECKING:
    from utils.config import Config


class CalendarEvent(BaseModel):
    """A calendar event."""

    id: str | None = None
    title: str
    start: str
    end: str
    attendees: list[str] = Field(default_factory=list)
    location: str | None = None
    description: str | None = None


class AvailabilityResult(BaseModel):
    """Free/busy availability over a window."""

    busy: list[dict] = Field(default_factory=list)
    available: bool = True


class CreateEventRequest(BaseModel):
    """Validated request to create an event."""

    title: str
    start: str
    end: str
    attendees: list[str] = Field(default_factory=list)
    location: str | None = None
    description: str | None = None


@runtime_checkable
class CalendarProvider(Protocol):
    """Read/availability/create/update/delete calendar operations."""

    async def search(
        self,
        query: str,
        time_min: str | None,
        time_max: str | None,
    ) -> list[CalendarEvent]: ...

    async def availability(
        self,
        attendees: list[str],
        time_min: str,
        time_max: str,
    ) -> AvailabilityResult: ...

    async def create_event(self, request: CreateEventRequest) -> CalendarEvent: ...

    async def update_event(
        self, event_id: str, request: CreateEventRequest
    ) -> CalendarEvent: ...

    async def delete_event(self, event_id: str) -> None: ...


class NullCalendarProvider:
    """Placeholder provider that reports missing authentication."""

    async def search(
        self, query: str, time_min: str | None, time_max: str | None
    ) -> list[CalendarEvent]:
        raise AuthMissingError("calendar provider is not configured")

    async def availability(
        self, attendees: list[str], time_min: str, time_max: str
    ) -> AvailabilityResult:
        raise AuthMissingError("calendar provider is not configured")

    async def create_event(self, request: CreateEventRequest) -> CalendarEvent:
        raise AuthMissingError("calendar provider is not configured")

    async def update_event(
        self, event_id: str, request: CreateEventRequest
    ) -> CalendarEvent:
        raise AuthMissingError("calendar provider is not configured")

    async def delete_event(self, event_id: str) -> None:
        raise AuthMissingError("calendar provider is not configured")


def get_calendar_provider(config: "Config") -> CalendarProvider:
    """Return the configured calendar provider, or a null provider if unavailable."""
    external = config.external_tools.calendar
    if not external.enabled:
        return NullCalendarProvider()
    if external.provider == "google_calendar":
        from provider.calendar.google_calendar import GoogleCalendarProvider

        return GoogleCalendarProvider(config, external)
    # Unknown provider name: behave as auth-missing rather than raising.
    return NullCalendarProvider()