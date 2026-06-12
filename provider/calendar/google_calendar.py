"""Google Calendar provider implementation.

Wraps the synchronous ``googleapiclient`` calendar service behind the async
``CalendarProvider`` protocol. Google client libraries are imported lazily so
the rest of the app (and tests injecting a fake service) do not require them
installed. Raw API payloads and tokens are never returned or logged; Google
``HttpError`` failures map to stable external-provider exceptions.
"""

import asyncio
from typing import TYPE_CHECKING, Any

from provider.calendar.base import (
    AvailabilityResult,
    CalendarEvent,
    CreateEventRequest,
)
from provider.external_errors import ProviderNotFoundError, ProviderPermissionError

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
    return exc


def _event_from_item(item: dict[str, Any]) -> CalendarEvent:
    """Map a Google Calendar event resource into a ``CalendarEvent``."""
    start = item.get("start", {})
    end = item.get("end", {})
    return CalendarEvent(
        id=item.get("id"),
        title=item.get("summary", ""),
        start=start.get("dateTime") or start.get("date", ""),
        end=end.get("dateTime") or end.get("date", ""),
        attendees=[
            a["email"]
            for a in item.get("attendees", [])
            if isinstance(a, dict) and a.get("email")
        ],
        location=item.get("location"),
        description=item.get("description"),
    )


def _event_body(request: CreateEventRequest) -> dict[str, Any]:
    """Build a Google Calendar event body from a ``CreateEventRequest``."""
    body: dict[str, Any] = {
        "summary": request.title,
        "start": {"dateTime": request.start},
        "end": {"dateTime": request.end},
    }
    if request.attendees:
        body["attendees"] = [{"email": email} for email in request.attendees]
    if request.location:
        body["location"] = request.location
    if request.description:
        body["description"] = request.description
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

    async def create_event(self, request: CreateEventRequest) -> CalendarEvent:
        try:
            response = await asyncio.to_thread(
                self._svc()
                .events()
                .insert(calendarId=self._calendar_id, body=_event_body(request))
                .execute
            )
        except Exception as e:  # noqa: BLE001 - mapped to stable error codes
            raise _map_http_error(e) from e
        return _event_from_item(response)

    async def update_event(
        self, event_id: str, request: CreateEventRequest
    ) -> CalendarEvent:
        try:
            response = await asyncio.to_thread(
                self._svc()
                .events()
                .patch(
                    calendarId=self._calendar_id,
                    eventId=event_id,
                    body=_event_body(request),
                )
                .execute
            )
        except Exception as e:  # noqa: BLE001 - mapped to stable error codes
            raise _map_http_error(e) from e
        return _event_from_item(response)

    async def delete_event(self, event_id: str) -> None:
        try:
            await asyncio.to_thread(
                self._svc()
                .events()
                .delete(calendarId=self._calendar_id, eventId=event_id)
                .execute
            )
        except Exception as e:  # noqa: BLE001 - mapped to stable error codes
            raise _map_http_error(e) from e
