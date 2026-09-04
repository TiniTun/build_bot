"""Google Calendar provider implementation.

Wraps the synchronous ``googleapiclient`` calendar service behind the async
``CalendarProvider`` protocol. Google client libraries are imported lazily so
the rest of the app (and tests injecting a fake service) do not require them
installed. Raw API payloads and tokens are never returned or logged; Google
``HttpError`` failures map to stable external-provider exceptions.
"""

import asyncio
from datetime import datetime
from typing import TYPE_CHECKING, Any

from provider.calendar.base import (
    AvailabilityResult,
    CalendarEvent,
    CreateEventRequest,
)
from provider.external_errors import (
    ProviderInvalidRequestError,
    ProviderNotFoundError,
    ProviderPermissionError,
)

if TYPE_CHECKING:
    from utils.config import Config, ExternalProviderConfig


def _map_http_error(exc: Exception) -> Exception:
    """Translate a Google ``HttpError`` into a stable provider exception.

    401/403 -> permission, 404 -> not-found, otherwise propagate unchanged so
    the tool layer maps it to a generic ``provider_error`` without leaking the
    raw payload.
    """
    try:
        from googleapiclient.errors import HttpError
    except ImportError:
        return exc
    if isinstance(exc, HttpError):
        status = getattr(getattr(exc, "resp", None), "status", None)
        if status in (401, 403):
            return ProviderPermissionError("google calendar denied the request")
        if status == 404:
            return ProviderNotFoundError("google calendar resource not found")
        if status == 400:
            return ProviderInvalidRequestError("google calendar rejected the request")
    return exc


def _day_bounds(day: str, timezone: str) -> tuple[str, str]:
    """RFC3339 bounds covering one local calendar day, [start, next-day-start).

    Built from the zone's own offset for that date so a DST boundary cannot
    silently widen or narrow the window.
    """
    from datetime import date, datetime, time, timedelta
    from zoneinfo import ZoneInfo

    zone = ZoneInfo(timezone)
    parsed = date.fromisoformat(day)
    start = datetime.combine(parsed, time.min, tzinfo=zone)
    end = datetime.combine(parsed + timedelta(days=1), time.min, tzinfo=zone)
    return start.isoformat(), end.isoformat()


def _has_utc_offset(value: str) -> bool:
    """Return True when an ISO datetime string carries a UTC offset.

    Google Calendar requires either an offset-bearing RFC3339 ``dateTime`` or an
    accompanying ``timeZone``; a naive datetime with neither is rejected with a
    400. We detect the offset so callers can attach a ``timeZone`` only when one
    is actually missing.
    """
    try:
        return datetime.fromisoformat(value).utcoffset() is not None
    except ValueError:
        return False


def _event_datetime(value: str, timezone: str | None) -> dict[str, Any]:
    """Build a Google ``start``/``end`` block, attaching ``timeZone`` if naive."""
    block: dict[str, Any] = {"dateTime": value}
    if timezone and not _has_utc_offset(value):
        block["timeZone"] = timezone
    return block


def _event_from_item(item: dict[str, Any]) -> CalendarEvent:
    """Map a Google Calendar event resource into a ``CalendarEvent``."""
    start = item.get("start", {})
    end = item.get("end", {})
    attendees = [
        a for a in item.get("attendees", []) if isinstance(a, dict)
    ]
    extended = item.get("extendedProperties") or {}
    private = extended.get("private") or {}
    return CalendarEvent(
        id=item.get("id"),
        title=item.get("summary", ""),
        start=start.get("dateTime") or start.get("date", ""),
        end=end.get("dateTime") or end.get("date", ""),
        attendees=[a["email"] for a in attendees if a.get("email")],
        location=item.get("location"),
        description=item.get("description"),
        # A date-only start is Google's all-day representation.
        all_day="dateTime" not in start and "date" in start,
        transparent=item.get("transparency") == "transparent",
        self_declined=any(
            a.get("self") and a.get("responseStatus") == "declined" for a in attendees
        ),
        private_properties={str(k): str(v) for k, v in private.items()},
    )


def _event_body(
    request: CreateEventRequest, timezone: str | None = None
) -> dict[str, Any]:
    """Build a Google Calendar event body from a ``CreateEventRequest``.

    Naive datetimes (no UTC offset) get ``timeZone`` attached from ``timezone``
    so Google can resolve them; offset-bearing datetimes are sent unchanged.
    """
    body: dict[str, Any] = {
        "summary": request.title,
        "start": _event_datetime(request.start, timezone),
        "end": _event_datetime(request.end, timezone),
    }
    if request.attendees:
        body["attendees"] = [{"email": email} for email in request.attendees]
    if request.location:
        body["location"] = request.location
    if request.description:
        body["description"] = request.description
    if request.private_properties:
        body["extendedProperties"] = {"private": dict(request.private_properties)}
    return body


class GoogleCalendarProvider:
    """Async ``CalendarProvider`` backed by the Google Calendar v3 API."""

    def __init__(
        self,
        config: "Config",
        provider_cfg: "ExternalProviderConfig",
        service: Any | None = None,
    ) -> None:
        self._config = config
        self._provider_cfg = provider_cfg
        self._service = service  # injected fake in tests
        self._calendar_id = provider_cfg.calendar_id or "primary"

    def _timezone(self) -> str | None:
        """Configured IANA timezone used to qualify naive event datetimes."""
        return getattr(self._config, "timezone", None)

    def _svc(self) -> Any:
        if self._service is None:
            from provider.google_auth import build_service

            self._service = build_service(
                self._config, self._provider_cfg, api="calendar", version="v3"
            )
        return self._service

    async def search(
        self,
        query: str,
        time_min: str | None,
        time_max: str | None,
    ) -> list[CalendarEvent]:
        kwargs: dict[str, Any] = {
            "calendarId": self._calendar_id,
            "q": query,
            "singleEvents": True,
            "orderBy": "startTime",
        }
        if time_min is not None:
            kwargs["timeMin"] = time_min
        if time_max is not None:
            kwargs["timeMax"] = time_max
        try:
            response = await asyncio.to_thread(
                self._svc().events().list(**kwargs).execute
            )
        except Exception as e:  # noqa: BLE001 - mapped to stable error codes
            raise _map_http_error(e) from e
        return [_event_from_item(item) for item in response.get("items", [])]

    async def list_day(
        self, day: str, timezone: str, calendar_id: str | None = None
    ) -> list[CalendarEvent]:
        """Every event overlapping one local day, ordered by start.

        Deliberately sends no ``q``: the planner needs the complete day, and a
        text search would silently omit events whose titles do not match.
        ``singleEvents`` expands recurrences so a weekly meeting appears as the
        instance that actually occupies today.
        """
        time_min, time_max = _day_bounds(day, timezone)
        try:
            response = await asyncio.to_thread(
                self._svc()
                .events()
                .list(
                    calendarId=calendar_id or self._calendar_id,
                    timeMin=time_min,
                    timeMax=time_max,
                    singleEvents=True,
                    orderBy="startTime",
                )
                .execute
            )
        except Exception as e:  # noqa: BLE001 - mapped to stable error codes
            raise _map_http_error(e) from e
        return [_event_from_item(item) for item in response.get("items", [])]

    async def availability(
        self,
        attendees: list[str],
        time_min: str,
        time_max: str,
    ) -> AvailabilityResult:
        body = {
            "timeMin": time_min,
            "timeMax": time_max,
            "items": [{"id": a} for a in attendees],
        }
        try:
            response = await asyncio.to_thread(
                self._svc().freebusy().query(body=body).execute
            )
        except Exception as e:  # noqa: BLE001 - mapped to stable error codes
            raise _map_http_error(e) from e
        busy: list[dict] = []
        for cal in response.get("calendars", {}).values():
            busy.extend(cal.get("busy", []))
        return AvailabilityResult(busy=busy, available=not busy)

    async def create_event(
        self, request: CreateEventRequest, calendar_id: str | None = None
    ) -> CalendarEvent:
        try:
            response = await asyncio.to_thread(
                self._svc()
                .events()
                .insert(
                    calendarId=calendar_id or self._calendar_id,
                    body=_event_body(request, self._timezone()),
                    sendUpdates="none",
                )
                .execute
            )
        except Exception as e:  # noqa: BLE001 - mapped to stable error codes
            raise _map_http_error(e) from e
        return _event_from_item(response)

    async def update_event(
        self,
        event_id: str,
        request: CreateEventRequest,
        calendar_id: str | None = None,
    ) -> CalendarEvent:
        try:
            response = await asyncio.to_thread(
                self._svc()
                .events()
                .patch(
                    calendarId=calendar_id or self._calendar_id,
                    eventId=event_id,
                    body=_event_body(request, self._timezone()),
                    sendUpdates="none",
                )
                .execute
            )
        except Exception as e:  # noqa: BLE001 - mapped to stable error codes
            raise _map_http_error(e) from e
        return _event_from_item(response)

    async def delete_event(
        self, event_id: str, calendar_id: str | None = None
    ) -> None:
        try:
            await asyncio.to_thread(
                self._svc()
                .events()
                .delete(
                    calendarId=calendar_id or self._calendar_id,
                    eventId=event_id,
                    sendUpdates="none",
                )
                .execute
            )
        except Exception as e:  # noqa: BLE001 - mapped to stable error codes
            raise _map_http_error(e) from e
