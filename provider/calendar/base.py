"""Calendar provider protocol, data models, and a null provider.

Real provider integration (e.g. Google Calendar) is out of scope for this
iteration. ``get_calendar_provider`` returns a ``NullCalendarProvider`` that
raises ``AuthMissingError`` until a real client is wired in.

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
    """Read/availability/create calendar operations. No delete/update here."""

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


def get_calendar_provider(config: "Config") -> CalendarProvider:
    """Return the configured calendar provider, or a null provider if unavailable."""
    external = config.external_tools.calendar
    if not external.enabled:
        return NullCalendarProvider()
    # No real client is bundled yet; a real provider would be selected here based
    # on ``external.provider``. Until then, behave as auth-missing.
    return NullCalendarProvider()