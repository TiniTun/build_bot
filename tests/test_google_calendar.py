"""Tests for the Google Calendar provider and approval-gated mutation tools.

All tests are hermetic: no network or Google auth. A fake synchronous service is
injected into ``GoogleCalendarProvider``, and ``get_calendar_provider`` is
monkeypatched where the tool/executor layer needs a working provider.
"""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.pending_actions import PendingActionStore
from provider.calendar.base import (
    AvailabilityResult,
    CalendarEvent,
    CreateEventRequest,
)
from provider.calendar.google_calendar import GoogleCalendarProvider
from provider.external_errors import ProviderInvalidRequestError
from tests.helpers import make_context, make_workspace
from tools.base import ToolErrorCode
from tools.calendar_tools import (
    build_calendar_capabilities,
    build_calendar_confirmed_executors,
)
from utils.config import ExternalProviderConfig, ExternalToolsConfig


# --- Fake Google service -------------------------------------------------


class _FakeExecutable:
    def __init__(self, result, exc=None):
        self._result = result
        self._exc = exc

    def execute(self):
        if self._exc is not None:
            raise self._exc
        return self._result


class _FakeHttpResp:
    def __init__(self, status):
        self.status = status
        self.reason = "error"


def _http_error(status):
    """Build a real googleapiclient HttpError with the given status."""
    from googleapiclient.errors import HttpError

    return HttpError(_FakeHttpResp(status), b"{}")


class _FakeEvents:
    def __init__(self, parent):
        self._parent = parent

    def list(self, **kwargs):
        self._parent.calls.append(("list", kwargs))
        return _FakeExecutable(self._parent.list_result, self._parent.exc)

    def insert(self, **kwargs):
        self._parent.calls.append(("insert", kwargs))
        return _FakeExecutable(self._parent.insert_result, self._parent.exc)

    def patch(self, **kwargs):
        self._parent.calls.append(("patch", kwargs))
        return _FakeExecutable(self._parent.patch_result, self._parent.exc)

    def delete(self, **kwargs):
        self._parent.calls.append(("delete", kwargs))
        return _FakeExecutable(self._parent.delete_result, self._parent.exc)


class _FakeFreebusy:
    def __init__(self, parent):
        self._parent = parent

    def query(self, body):
        self._parent.calls.append(("freebusy", body))
        return _FakeExecutable(self._parent.freebusy_result, self._parent.exc)


class FakeService:
    def __init__(
        self,
        *,
        list_result=None,
        insert_result=None,
        patch_result=None,
        delete_result=None,
        freebusy_result=None,
        exc=None,
    ):
        self.list_result = list_result or {"items": []}
        self.insert_result = insert_result or {}
        self.patch_result = patch_result or {}
        self.delete_result = delete_result
        self.freebusy_result = freebusy_result or {"calendars": {}}
        self.exc = exc
        self.calls = []

    def events(self):
        return _FakeEvents(self)

    def freebusy(self):
        return _FakeFreebusy(self)


def _provider_cfg():
    return ExternalProviderConfig(
        enabled=True, provider="google_calendar", calendar_id="primary"
    )


# --- Provider mapping tests ---------------------------------------------


class GoogleCalendarProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_maps_events(self) -> None:
        service = FakeService(
            list_result={
                "items": [
                    {
                        "id": "ev1",
                        "summary": "Team sync",
                        "start": {"dateTime": "2026-06-01T10:00:00Z"},
                        "end": {"dateTime": "2026-06-01T10:30:00Z"},
                        "attendees": [{"email": "a@x.com"}, {"email": "b@x.com"}],
                        "location": "Room 1",
                        "description": "Weekly",
                    }
                ]
            }
        )
        provider = GoogleCalendarProvider(None, _provider_cfg(), service=service)
        events = await provider.search("sync", None, None)
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertEqual(ev.id, "ev1")
        self.assertEqual(ev.title, "Team sync")
        self.assertEqual(ev.start, "2026-06-01T10:00:00Z")
        self.assertEqual(ev.attendees, ["a@x.com", "b@x.com"])
        self.assertEqual(ev.location, "Room 1")
        # timeMin/timeMax omitted when None.
        _, kwargs = service.calls[0]
        self.assertNotIn("timeMin", kwargs)
        self.assertNotIn("timeMax", kwargs)

    async def test_search_all_day_event_uses_date(self) -> None:
        service = FakeService(
            list_result={
                "items": [
                    {
                        "id": "ev2",
                        "summary": "Holiday",
                        "start": {"date": "2026-06-02"},
                        "end": {"date": "2026-06-03"},
                    }
                ]
            }
        )
        provider = GoogleCalendarProvider(None, _provider_cfg(), service=service)
        events = await provider.search("holiday", "2026-06-01T00:00:00Z", None)
        self.assertEqual(events[0].start, "2026-06-02")
        _, kwargs = service.calls[0]
        self.assertEqual(kwargs["timeMin"], "2026-06-01T00:00:00Z")

    async def test_availability_busy_and_available_flag(self) -> None:
        busy_service = FakeService(
            freebusy_result={
                "calendars": {
                    "a@x.com": {
                        "busy": [
                            {
                                "start": "2026-06-01T10:00:00Z",
                                "end": "2026-06-01T11:00:00Z",
                            }
                        ]
                    }
                }
            }
        )
        provider = GoogleCalendarProvider(None, _provider_cfg(), service=busy_service)
        result = await provider.availability(
            ["a@x.com"], "2026-06-01T00:00:00Z", "2026-06-02T00:00:00Z"
        )
        self.assertIsInstance(result, AvailabilityResult)
        self.assertEqual(len(result.busy), 1)
        self.assertFalse(result.available)

        free_service = FakeService(
            freebusy_result={"calendars": {"a@x.com": {"busy": []}}}
        )
        provider2 = GoogleCalendarProvider(None, _provider_cfg(), service=free_service)
        result2 = await provider2.availability(
            ["a@x.com"], "2026-06-01T00:00:00Z", "2026-06-02T00:00:00Z"
        )
        self.assertTrue(result2.available)

    async def test_create_event_inserts_and_maps(self) -> None:
        service = FakeService(
            insert_result={
                "id": "new1",
                "summary": "Lunch",
                "start": {"dateTime": "2026-06-01T12:00:00Z"},
                "end": {"dateTime": "2026-06-01T13:00:00Z"},
            }
        )
        provider = GoogleCalendarProvider(None, _provider_cfg(), service=service)
        event = await provider.create_event(
            CreateEventRequest(
                title="Lunch",
                start="2026-06-01T12:00:00Z",
                end="2026-06-01T13:00:00Z",
            )
        )
        self.assertEqual(event.id, "new1")
        self.assertEqual(service.calls[0][0], "insert")

    async def test_naive_datetime_gets_config_timezone(self) -> None:
        service = FakeService(
            insert_result={
                "id": "new1",
                "summary": "Test Smoke Meeting",
                "start": {"dateTime": "2026-06-16T10:00:00"},
                "end": {"dateTime": "2026-06-16T10:30:00"},
            }
        )
        config = SimpleNamespace(timezone="Australia/Brisbane")
        provider = GoogleCalendarProvider(config, _provider_cfg(), service=service)
        await provider.create_event(
            CreateEventRequest(
                title="Test Smoke Meeting",
                start="2026-06-16T10:00:00",
                end="2026-06-16T10:30:00",
            )
        )
        _, kwargs = service.calls[0]
        body = kwargs["body"]
        self.assertEqual(body["start"]["timeZone"], "Australia/Brisbane")
        self.assertEqual(body["end"]["timeZone"], "Australia/Brisbane")
        self.assertEqual(body["start"]["dateTime"], "2026-06-16T10:00:00")

    async def test_offset_datetime_keeps_no_timezone(self) -> None:
        service = FakeService(
            insert_result={
                "id": "new1",
                "summary": "Lunch",
                "start": {"dateTime": "2026-06-16T10:00:00+10:00"},
                "end": {"dateTime": "2026-06-16T10:30:00+10:00"},
            }
        )
        config = SimpleNamespace(timezone="Australia/Brisbane")
        provider = GoogleCalendarProvider(config, _provider_cfg(), service=service)
        await provider.create_event(
            CreateEventRequest(
                title="Lunch",
                start="2026-06-16T10:00:00+10:00",
                end="2026-06-16T10:30:00+10:00",
            )
        )
        _, kwargs = service.calls[0]
        body = kwargs["body"]
        # Offset already present: do not attach a (possibly conflicting) timeZone.
        self.assertNotIn("timeZone", body["start"])
        self.assertNotIn("timeZone", body["end"])

    async def test_invalid_request_error_maps(self) -> None:
        service = FakeService(exc=_http_error(400))
        provider = GoogleCalendarProvider(None, _provider_cfg(), service=service)
        with self.assertRaises(ProviderInvalidRequestError):
            await provider.create_event(
                CreateEventRequest(
                    title="x", start="2026-06-16T10:00:00", end="2026-06-16T10:30:00"
                )
            )

    async def test_update_and_delete(self) -> None:
        service = FakeService(
            patch_result={
                "id": "ev1",
                "summary": "Renamed",
                "start": {"dateTime": "2026-06-01T12:00:00Z"},
                "end": {"dateTime": "2026-06-01T13:00:00Z"},
            }
        )
        provider = GoogleCalendarProvider(None, _provider_cfg(), service=service)
        event = await provider.update_event(
            "ev1",
            CreateEventRequest(
                title="Renamed",
                start="2026-06-01T12:00:00Z",
                end="2026-06-01T13:00:00Z",
            ),
        )
        self.assertEqual(event.title, "Renamed")
        self.assertEqual(service.calls[0][0], "patch")

        result = await provider.delete_event("ev1")
        self.assertIsNone(result)
        self.assertEqual(service.calls[1][0], "delete")

    async def test_permission_error_maps(self) -> None:
        from provider.external_errors import ProviderPermissionError

        service = FakeService(exc=_http_error(403))
        provider = GoogleCalendarProvider(None, _provider_cfg(), service=service)
        with self.assertRaises(ProviderPermissionError):
            await provider.search("x", None, None)

    async def test_not_found_error_maps(self) -> None:
        from provider.external_errors import ProviderNotFoundError

        service = FakeService(exc=_http_error(404))
        provider = GoogleCalendarProvider(None, _provider_cfg(), service=service)
        with self.assertRaises(ProviderNotFoundError):
            await provider.delete_event("missing")


# --- Tool / executor integration tests ----------------------------------


class _CountingProvider:
    """Wraps a fake-backed provider and counts mutating calls."""

    def __init__(self, service):
        self._p = GoogleCalendarProvider(None, _provider_cfg(), service=service)
        self.inserts = 0
        self.patches = 0
        self.deletes = 0

    async def search(self, query, time_min, time_max):
        return await self._p.search(query, time_min, time_max)

    async def availability(self, attendees, time_min, time_max):
        return await self._p.availability(attendees, time_min, time_max)

    async def create_event(self, request):
        self.inserts += 1
        return await self._p.create_event(request)

    async def update_event(self, event_id, request):
        self.patches += 1
        return await self._p.update_event(event_id, request)

    async def delete_event(self, event_id):
        self.deletes += 1
        return await self._p.delete_event(event_id)


def _calendar_context(tmp):
    workspace = make_workspace(Path(tmp))
    context = make_context(workspace)
    context.config.external_tools = ExternalToolsConfig(
        calendar=ExternalProviderConfig(
            enabled=True, provider="google_calendar", calendar_id="primary"
        )
    )
    return context


def _session(context):
    return SimpleNamespace(shared_context=context)


def _cap(context, cap_id):
    return next(
        pair
        for pair in build_calendar_capabilities(context.config)
        if pair[0].id == cap_id
    )


class CalendarToolApprovalTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_event_proposal_does_not_call_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = _calendar_context(tmp)
            counting = _CountingProvider(FakeService())
            import tools.calendar_tools as ct

            orig = ct.get_calendar_provider
            ct.get_calendar_provider = lambda config: counting
            try:
                _, tool_obj = _cap(context, "calendar.create_event")
                raw = await tool_obj.execute(
                    session=_session(context),
                    title="Team sync",
                    start="2026-06-01T10:00:00+00:00",
                    end="2026-06-01T10:30:00+00:00",
                )
            finally:
                ct.get_calendar_provider = orig
            payload = json.loads(raw)
            self.assertTrue(payload["requires_confirmation"])
            self.assertEqual(counting.inserts, 0)

    async def test_confirm_create_calls_insert_once_reject_calls_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = _calendar_context(tmp)
            insert_service = FakeService(
                insert_result={
                    "id": "new1",
                    "summary": "Team sync",
                    "start": {"dateTime": "2026-06-01T10:00:00+00:00"},
                    "end": {"dateTime": "2026-06-01T10:30:00+00:00"},
                }
            )
            counting = _CountingProvider(insert_service)
            import tools.calendar_tools as ct

            orig = ct.get_calendar_provider
            ct.get_calendar_provider = lambda config: counting
            try:
                executors = build_calendar_confirmed_executors(context.config)
                executor = executors["calendar.create_event"]
                payload = {
                    "title": "Team sync",
                    "start": "2026-06-01T10:00:00+00:00",
                    "end": "2026-06-01T10:30:00+00:00",
                    "attendees": [],
                    "location": None,
                    "description": None,
                }
                raw = await executor(_session(context), payload)
            finally:
                ct.get_calendar_provider = orig
            # Success results return plain text, not JSON.
            self.assertIn("Event created", raw)
            self.assertEqual(counting.inserts, 1)

            # Reject path: deleting a pending action invokes no executor/provider.
            action_id = "22222222-2222-4222-8222-222222222222"
            store = PendingActionStore(context.config)
            store.create(
                action_id=action_id,
                capability_id="calendar.create_event",
                summary="Create",
                payload=payload,
            )
            self.assertTrue(store.delete(action_id))
            self.assertEqual(counting.inserts, 1)  # unchanged by reject

    async def test_confirmed_create_naive_datetime_succeeds(self) -> None:
        # Smoke case: naive datetime + config.timezone must NOT become a
        # generic provider_error; the body carries timeZone and Google succeeds.
        with tempfile.TemporaryDirectory() as tmp:
            context = _calendar_context(tmp)
            context.config.timezone = "Australia/Brisbane"
            insert_service = FakeService(
                insert_result={
                    "id": "smoke1",
                    "summary": "Test Smoke Meeting",
                    "start": {"dateTime": "2026-06-16T10:00:00"},
                    "end": {"dateTime": "2026-06-16T10:30:00"},
                }
            )
            provider = GoogleCalendarProvider(
                context.config, _provider_cfg(), service=insert_service
            )
            import tools.calendar_tools as ct

            orig = ct.get_calendar_provider
            ct.get_calendar_provider = lambda config: provider
            try:
                executor = build_calendar_confirmed_executors(context.config)[
                    "calendar.create_event"
                ]
                raw = await executor(
                    _session(context),
                    {
                        "title": "Test Smoke Meeting",
                        "start": "2026-06-16T10:00:00",
                        "end": "2026-06-16T10:30:00",
                        "attendees": [],
                        "location": None,
                        "description": None,
                    },
                )
            finally:
                ct.get_calendar_provider = orig
            self.assertIn("Event created", raw)
            self.assertNotIn("provider_error", raw)
            _, kwargs = insert_service.calls[0]
            self.assertEqual(
                kwargs["body"]["start"]["timeZone"], "Australia/Brisbane"
            )

    async def test_update_proposal_then_confirmed_patch_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = _calendar_context(tmp)
            patch_service = FakeService(
                patch_result={
                    "id": "ev1",
                    "summary": "Renamed",
                    "start": {"dateTime": "2026-06-01T12:00:00+00:00"},
                    "end": {"dateTime": "2026-06-01T13:00:00+00:00"},
                }
            )
            counting = _CountingProvider(patch_service)
            import tools.calendar_tools as ct

            orig = ct.get_calendar_provider
            ct.get_calendar_provider = lambda config: counting
            try:
                _, tool_obj = _cap(context, "calendar.update_event")
                raw = await tool_obj.execute(
                    session=_session(context),
                    event_id="ev1",
                    title="Renamed",
                    start="2026-06-01T12:00:00+00:00",
                    end="2026-06-01T13:00:00+00:00",
                )
                proposal = json.loads(raw)
                self.assertTrue(proposal["requires_confirmation"])
                self.assertEqual(counting.patches, 0)

                executor = build_calendar_confirmed_executors(context.config)[
                    "calendar.update_event"
                ]
                result = await executor(
                    _session(context), proposal["action"]["payload"]
                )
            finally:
                ct.get_calendar_provider = orig
            self.assertIn("Event updated", result)
            self.assertEqual(counting.patches, 1)

    async def test_delete_proposal_then_confirmed_delete_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = _calendar_context(tmp)
            counting = _CountingProvider(FakeService(delete_result=None))
            import tools.calendar_tools as ct

            orig = ct.get_calendar_provider
            ct.get_calendar_provider = lambda config: counting
            try:
                _, tool_obj = _cap(context, "calendar.delete_event")
                raw = await tool_obj.execute(
                    session=_session(context), event_id="ev1"
                )
                proposal = json.loads(raw)
                self.assertTrue(proposal["requires_confirmation"])
                self.assertEqual(counting.deletes, 0)

                executor = build_calendar_confirmed_executors(context.config)[
                    "calendar.delete_event"
                ]
                result = await executor(
                    _session(context), proposal["action"]["payload"]
                )
            finally:
                ct.get_calendar_provider = orig
            self.assertIn("Event deleted", result)
            self.assertEqual(counting.deletes, 1)

    async def test_confirmed_executor_maps_permission_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = _calendar_context(tmp)
            counting = _CountingProvider(FakeService(exc=_http_error(403)))
            import tools.calendar_tools as ct

            orig = ct.get_calendar_provider
            ct.get_calendar_provider = lambda config: counting
            try:
                executor = build_calendar_confirmed_executors(context.config)[
                    "calendar.delete_event"
                ]
                raw = await executor(_session(context), {"event_id": "ev1"})
            finally:
                ct.get_calendar_provider = orig
            payload = json.loads(raw)
            self.assertFalse(payload["ok"])
            self.assertEqual(
                payload["error"]["code"], ToolErrorCode.PERMISSION_DENIED.value
            )

    async def test_confirmed_executor_maps_not_found_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = _calendar_context(tmp)
            counting = _CountingProvider(FakeService(exc=_http_error(404)))
            import tools.calendar_tools as ct

            orig = ct.get_calendar_provider
            ct.get_calendar_provider = lambda config: counting
            try:
                executor = build_calendar_confirmed_executors(context.config)[
                    "calendar.delete_event"
                ]
                raw = await executor(_session(context), {"event_id": "ev1"})
            finally:
                ct.get_calendar_provider = orig
            payload = json.loads(raw)
            self.assertFalse(payload["ok"])
            self.assertEqual(
                payload["error"]["code"], ToolErrorCode.NOT_FOUND.value
            )


if __name__ == "__main__":
    unittest.main()
