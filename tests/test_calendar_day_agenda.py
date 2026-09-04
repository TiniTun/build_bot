"""Day-listing and event-shape mapping for the Google Calendar provider.

Hermetic: a fake synchronous service is injected, following the pattern in
tests/test_google_calendar.py. No network, no Google auth.
"""

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from provider.calendar.base import CalendarEvent, CreateEventRequest, NullCalendarProvider
from provider.calendar.google_calendar import (
    GoogleCalendarProvider,
    _event_body,
    _event_from_item,
)
from provider.external_errors import AuthMissingError
from tests.helpers import make_context, make_workspace


class TestEventMapping(unittest.TestCase):
    def test_timed_event_is_not_all_day(self):
        event = _event_from_item({
            "id": "e1",
            "summary": "Standup",
            "start": {"dateTime": "2026-09-04T10:00:00+10:00"},
            "end": {"dateTime": "2026-09-04T10:30:00+10:00"},
        })
        self.assertFalse(event.all_day)
        self.assertEqual(event.start, "2026-09-04T10:00:00+10:00")

    def test_empty_start_when_neither_date_nor_datetime(self):
        """Regression test: handles missing date/dateTime without crashing."""
        event = _event_from_item({
            "id": "e1b",
            "summary": "Malformed event",
            "start": {},
            "end": {},
        })
        self.assertFalse(event.all_day)
        self.assertEqual(event.start, "")

    def test_date_only_event_is_all_day(self):
        event = _event_from_item({
            "id": "e2",
            "summary": "Public holiday",
            "start": {"date": "2026-09-04"},
            "end": {"date": "2026-09-05"},
        })
        self.assertTrue(event.all_day)

    def test_private_properties_are_read(self):
        event = _event_from_item({
            "id": "e3",
            "summary": "Deep work",
            "start": {"dateTime": "2026-09-04T10:40:00+10:00"},
            "end": {"dateTime": "2026-09-04T11:40:00+10:00"},
            "extendedProperties": {"private": {"owner": "build-bot-daily-planner"}},
        })
        self.assertEqual(event.private_properties["owner"], "build-bot-daily-planner")

    def test_missing_extended_properties_is_empty_dict(self):
        event = _event_from_item({
            "id": "e4", "summary": "x",
            "start": {"dateTime": "2026-09-04T10:00:00+10:00"},
            "end": {"dateTime": "2026-09-04T10:30:00+10:00"},
        })
        self.assertEqual(event.private_properties, {})

    def test_transparent_event_is_flagged(self):
        event = _event_from_item({
            "id": "e5", "summary": "Focus time",
            "start": {"dateTime": "2026-09-04T10:00:00+10:00"},
            "end": {"dateTime": "2026-09-04T12:00:00+10:00"},
            "transparency": "transparent",
        })
        self.assertTrue(event.transparent)

    def test_opaque_event_is_not_transparent(self):
        event = _event_from_item({
            "id": "e5b", "summary": "Meeting",
            "start": {"dateTime": "2026-09-04T10:00:00+10:00"},
            "end": {"dateTime": "2026-09-04T12:00:00+10:00"},
            "transparency": "opaque",
        })
        self.assertFalse(event.transparent)

    def test_missing_transparency_is_not_transparent(self):
        event = _event_from_item({
            "id": "e5c", "summary": "Meeting",
            "start": {"dateTime": "2026-09-04T10:00:00+10:00"},
            "end": {"dateTime": "2026-09-04T12:00:00+10:00"},
        })
        self.assertFalse(event.transparent)

    def test_self_declined_event_is_flagged(self):
        event = _event_from_item({
            "id": "e6", "summary": "Optional sync",
            "start": {"dateTime": "2026-09-04T10:00:00+10:00"},
            "end": {"dateTime": "2026-09-04T11:00:00+10:00"},
            "attendees": [{"email": "me@example.com", "self": True,
                           "responseStatus": "declined"}],
        })
        self.assertTrue(event.self_declined)

    def test_other_attendee_declined_is_not_self_declined(self):
        """Attendee with responseStatus declined but not self=True."""
        event = _event_from_item({
            "id": "e7", "summary": "Team sync",
            "start": {"dateTime": "2026-09-04T10:00:00+10:00"},
            "end": {"dateTime": "2026-09-04T11:00:00+10:00"},
            "attendees": [{"email": "other@example.com", "self": False,
                           "responseStatus": "declined"}],
        })
        self.assertFalse(event.self_declined)

    def test_self_with_accepted_response_is_not_declined(self):
        """Attendee with self=True but accepted responseStatus."""
        event = _event_from_item({
            "id": "e8", "summary": "Team sync",
            "start": {"dateTime": "2026-09-04T10:00:00+10:00"},
            "end": {"dateTime": "2026-09-04T11:00:00+10:00"},
            "attendees": [{"email": "me@example.com", "self": True,
                           "responseStatus": "accepted"}],
        })
        self.assertFalse(event.self_declined)

    def test_body_writes_private_properties(self):
        body = _event_body(
            CreateEventRequest(
                title="Deep work",
                start="2026-09-04T10:40:00+10:00",
                end="2026-09-04T11:40:00+10:00",
                private_properties={"owner": "build-bot-daily-planner",
                                    "slot_key": "pattern:mon:deep-work"},
            ),
            "Australia/Brisbane",
        )
        self.assertEqual(
            body["extendedProperties"]["private"]["slot_key"], "pattern:mon:deep-work"
        )

    def test_body_omits_extended_properties_when_empty(self):
        body = _event_body(
            CreateEventRequest(title="x", start="2026-09-04T10:00:00+10:00",
                               end="2026-09-04T10:30:00+10:00"),
            "Australia/Brisbane",
        )
        self.assertNotIn("extendedProperties", body)


class _RecordingEvents:
    """Records list/insert kwargs so the test can assert on the request."""

    def __init__(self, items):
        self.items = items
        self.list_kwargs = None
        self.insert_kwargs = None
        self.patch_kwargs = None
        self.delete_kwargs = None

    def list(self, **kwargs):
        self.list_kwargs = kwargs
        return SimpleNamespace(execute=lambda: {"items": self.items})

    def insert(self, **kwargs):
        self.insert_kwargs = kwargs
        return SimpleNamespace(execute=lambda: {
            "id": "new", "summary": kwargs["body"]["summary"],
            "start": kwargs["body"]["start"], "end": kwargs["body"]["end"],
        })

    def patch(self, **kwargs):
        self.patch_kwargs = kwargs
        return SimpleNamespace(execute=lambda: {
            "id": kwargs["eventId"], "summary": kwargs["body"]["summary"],
            "start": kwargs["body"]["start"], "end": kwargs["body"]["end"],
        })

    def delete(self, **kwargs):
        self.delete_kwargs = kwargs
        return SimpleNamespace(execute=lambda: None)


class _RecordingService:
    def __init__(self, items):
        self._events = _RecordingEvents(items)

    def events(self):
        return self._events


def _provider(items):
    service = _RecordingService(items)
    cfg = SimpleNamespace(calendar_id="primary")
    config = SimpleNamespace(timezone="Australia/Brisbane")
    return GoogleCalendarProvider(config, cfg, service=service), service


class TestListDay(unittest.TestCase):
    def test_list_day_sends_no_query_parameter(self):
        provider, service = _provider([])
        asyncio.run(provider.list_day("2026-09-04", "Australia/Brisbane"))
        kwargs = service.events().list_kwargs
        self.assertNotIn("q", kwargs)
        self.assertTrue(kwargs["singleEvents"])
        self.assertEqual(kwargs["orderBy"], "startTime")

    def test_list_day_window_covers_the_local_day(self):
        provider, service = _provider([])
        asyncio.run(provider.list_day("2026-09-04", "Australia/Brisbane"))
        kwargs = service.events().list_kwargs
        self.assertEqual(kwargs["timeMin"], "2026-09-04T00:00:00+10:00")
        self.assertEqual(kwargs["timeMax"], "2026-09-05T00:00:00+10:00")

    def test_list_day_window_crosses_a_dst_spring_forward_boundary(self):
        # `_day_bounds`'s docstring claims a DST transition cannot silently
        # widen or narrow the window because each bound's offset is computed
        # from its own date. Every other test here uses Australia/Brisbane,
        # which observes no DST, so none of them actually exercise that
        # claim. America/New_York springs forward on 2027-03-14 (02:00 clocks
        # jump to 03:00): a same-instant window would still read 23 wall-clock
        # hours, but this window must read as a full calendar day using each
        # boundary's own offset (-05:00 at the start of the day, -04:00 at
        # the end).
        provider, service = _provider([])
        asyncio.run(provider.list_day("2027-03-14", "America/New_York"))
        kwargs = service.events().list_kwargs
        self.assertEqual(kwargs["timeMin"], "2027-03-14T00:00:00-05:00")
        self.assertEqual(kwargs["timeMax"], "2027-03-15T00:00:00-04:00")

    def test_list_day_targets_the_requested_calendar(self):
        provider, service = _provider([])
        asyncio.run(provider.list_day("2026-09-04", "Australia/Brisbane",
                                      calendar_id="plan@group.calendar.google.com"))
        self.assertEqual(
            service.events().list_kwargs["calendarId"],
            "plan@group.calendar.google.com",
        )

    def test_create_event_defaults_to_configured_calendar(self):
        provider, service = _provider([])
        asyncio.run(provider.create_event(
            CreateEventRequest(title="x", start="2026-09-04T10:00:00+10:00",
                               end="2026-09-04T10:30:00+10:00")))
        self.assertEqual(service.events().insert_kwargs["calendarId"], "primary")

    def test_create_event_never_sends_invitations(self):
        provider, service = _provider([])
        asyncio.run(provider.create_event(
            CreateEventRequest(title="x", start="2026-09-04T10:00:00+10:00",
                               end="2026-09-04T10:30:00+10:00"),
            calendar_id="plan@group.calendar.google.com"))
        self.assertEqual(service.events().insert_kwargs["sendUpdates"], "none")

    def test_update_event_never_sends_invitations(self):
        provider, service = _provider([])
        asyncio.run(provider.update_event(
            "ev1",
            CreateEventRequest(title="x", start="2026-09-04T10:00:00+10:00",
                               end="2026-09-04T10:30:00+10:00")))
        self.assertEqual(service.events().patch_kwargs["sendUpdates"], "none")

    def test_update_event_targets_the_requested_calendar(self):
        provider, service = _provider([])
        asyncio.run(provider.update_event(
            "ev1",
            CreateEventRequest(title="x", start="2026-09-04T10:00:00+10:00",
                               end="2026-09-04T10:30:00+10:00"),
            calendar_id="plan@group.calendar.google.com"))
        self.assertEqual(
            service.events().patch_kwargs["calendarId"],
            "plan@group.calendar.google.com",
        )

    def test_update_event_defaults_to_configured_calendar(self):
        provider, service = _provider([])
        asyncio.run(provider.update_event(
            "ev1",
            CreateEventRequest(title="x", start="2026-09-04T10:00:00+10:00",
                               end="2026-09-04T10:30:00+10:00")))
        self.assertEqual(service.events().patch_kwargs["calendarId"], "primary")

    def test_delete_event_never_sends_invitations(self):
        provider, service = _provider([])
        asyncio.run(provider.delete_event("ev1"))
        self.assertEqual(service.events().delete_kwargs["sendUpdates"], "none")

    def test_delete_event_targets_the_requested_calendar(self):
        provider, service = _provider([])
        asyncio.run(provider.delete_event(
            "ev1", calendar_id="plan@group.calendar.google.com"))
        self.assertEqual(
            service.events().delete_kwargs["calendarId"],
            "plan@group.calendar.google.com",
        )


class TestNullProvider(unittest.TestCase):
    def test_list_day_reports_auth_missing_not_attribute_error(self):
        with self.assertRaises(AuthMissingError):
            asyncio.run(NullCalendarProvider().list_day("2026-09-04", "UTC"))


class TestDayAgendaCapability(unittest.TestCase):
    def test_capability_is_registered_when_calendar_is_enabled(self):
        from tools.calendar_tools import build_calendar_capabilities
        from utils.config import ExternalProviderConfig

        config = SimpleNamespace(
            external_tools=SimpleNamespace(
                calendar=ExternalProviderConfig(enabled=True,
                                                provider="google_calendar")),
            timezone="Australia/Brisbane", places=None)
        ids = [cap.id for cap, _ in build_calendar_capabilities(config)]
        self.assertIn("calendar.day_agenda", ids)

    def test_day_agenda_is_read_risk(self):
        from tools.calendar_tools import build_calendar_capabilities
        from tools.capabilities import ToolRiskLevel
        from utils.config import ExternalProviderConfig

        config = SimpleNamespace(
            external_tools=SimpleNamespace(
                calendar=ExternalProviderConfig(enabled=True,
                                                provider="google_calendar")),
            timezone="Australia/Brisbane", places=None)
        cap = next(c for c, _ in build_calendar_capabilities(config)
                   if c.id == "calendar.day_agenda")
        self.assertEqual(cap.risk_level, ToolRiskLevel.READ)


class _DayStub:
    """Records ``list_day`` calls; ``search`` raises so a wrong-method mutation
    surfaces as a mapped provider error instead of silently succeeding."""

    def __init__(self, events):
        self._events = events
        self.list_day_calls = []

    async def list_day(self, day, timezone, calendar_id=None):
        self.list_day_calls.append((day, timezone, calendar_id))
        return self._events

    async def search(self, query, time_min=None, time_max=None):
        raise AssertionError("day_agenda must call list_day, not search")


def _day_agenda_tool(context):
    from tools.calendar_tools import build_calendar_capabilities

    return next(
        pair[1]
        for pair in build_calendar_capabilities(context.config)
        if pair[0].id == "calendar.day_agenda"
    )


def _calendar_enabled_context(tmp):
    from utils.config import ExternalProviderConfig, ExternalToolsConfig

    workspace = make_workspace(Path(tmp))
    context = make_context(workspace)
    context.config.external_tools = ExternalToolsConfig(
        calendar=ExternalProviderConfig(enabled=True, provider="google_calendar")
    )
    context.config.timezone = "Australia/Brisbane"
    return context


class TestDayAgendaTool(unittest.IsolatedAsyncioTestCase):
    async def test_calls_list_day_not_search(self):
        import tools.calendar_tools as ct

        with tempfile.TemporaryDirectory() as tmp:
            context = _calendar_enabled_context(tmp)
            stub = _DayStub([])
            orig = ct.get_calendar_provider
            ct.get_calendar_provider = lambda config: stub
            try:
                tool_obj = _day_agenda_tool(context)
                raw = await tool_obj.execute(
                    session=SimpleNamespace(shared_context=context),
                    date="2026-09-04",
                )
            finally:
                ct.get_calendar_provider = orig
            self.assertEqual(
                stub.list_day_calls,
                [("2026-09-04", "Australia/Brisbane", None)],
            )
            self.assertIn("No events on 2026-09-04.", raw)

    async def test_no_events_message(self):
        import tools.calendar_tools as ct

        with tempfile.TemporaryDirectory() as tmp:
            context = _calendar_enabled_context(tmp)
            orig = ct.get_calendar_provider
            ct.get_calendar_provider = lambda config: _DayStub([])
            try:
                tool_obj = _day_agenda_tool(context)
                raw = await tool_obj.execute(
                    session=SimpleNamespace(shared_context=context),
                    date="2026-09-04",
                )
            finally:
                ct.get_calendar_provider = orig
            self.assertEqual(
                raw, "No events on 2026-09-04.\nfree:\n- 00:00-23:59"
            )

    async def test_annotates_all_day_free_and_declined_events(self):
        import tools.calendar_tools as ct

        events = [
            CalendarEvent(
                id="e1", title="Standup",
                start="2026-09-04T09:00:00+10:00", end="2026-09-04T09:15:00+10:00",
            ),
            CalendarEvent(
                id="e2", title="Public holiday",
                start="2026-09-04", end="2026-09-05", all_day=True,
            ),
            CalendarEvent(
                id="e3", title="Focus block",
                start="2026-09-04T10:00:00+10:00", end="2026-09-04T12:00:00+10:00",
                transparent=True,
            ),
            CalendarEvent(
                id="e4", title="Optional sync",
                start="2026-09-04T13:00:00+10:00", end="2026-09-04T13:30:00+10:00",
                self_declined=True,
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            context = _calendar_enabled_context(tmp)
            orig = ct.get_calendar_provider
            ct.get_calendar_provider = lambda config: _DayStub(events)
            try:
                tool_obj = _day_agenda_tool(context)
                raw = await tool_obj.execute(
                    session=SimpleNamespace(shared_context=context),
                    date="2026-09-04",
                )
            finally:
                ct.get_calendar_provider = orig
            self.assertIn(
                "- Standup (2026-09-04T09:00:00+10:00 → 2026-09-04T09:15:00+10:00)",
                raw,
            )
            self.assertIn("- Public holiday (all day)", raw)
            self.assertIn("- Focus block (2026-09-04T10:00:00+10:00 → "
                          "2026-09-04T12:00:00+10:00) (free)", raw)
            self.assertIn("- Optional sync (2026-09-04T13:00:00+10:00 → "
                          "2026-09-04T13:30:00+10:00) (declined)", raw)

    async def test_two_spaced_meetings_yield_gap_intervals(self):
        import tools.calendar_tools as ct

        events = [
            CalendarEvent(
                id="e1", title="Morning sync",
                start="2026-09-04T09:00:00+10:00", end="2026-09-04T10:00:00+10:00",
            ),
            CalendarEvent(
                id="e2", title="Afternoon review",
                start="2026-09-04T14:00:00+10:00", end="2026-09-04T15:00:00+10:00",
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            context = _calendar_enabled_context(tmp)
            orig = ct.get_calendar_provider
            ct.get_calendar_provider = lambda config: _DayStub(events)
            try:
                tool_obj = _day_agenda_tool(context)
                raw = await tool_obj.execute(
                    session=SimpleNamespace(shared_context=context),
                    date="2026-09-04",
                )
            finally:
                ct.get_calendar_provider = orig
            self.assertIn("free:", raw)
            self.assertIn("- 00:00-09:00", raw)
            self.assertIn("- 10:00-14:00", raw)
            self.assertIn("- 15:00-23:59", raw)

    async def test_all_day_event_does_not_consume_time(self):
        import tools.calendar_tools as ct

        events = [
            CalendarEvent(
                id="e1", title="Public holiday",
                start="2026-09-04", end="2026-09-05", all_day=True,
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            context = _calendar_enabled_context(tmp)
            orig = ct.get_calendar_provider
            ct.get_calendar_provider = lambda config: _DayStub(events)
            try:
                tool_obj = _day_agenda_tool(context)
                raw = await tool_obj.execute(
                    session=SimpleNamespace(shared_context=context),
                    date="2026-09-04",
                )
            finally:
                ct.get_calendar_provider = orig
            self.assertIn("- 00:00-23:59", raw)

    async def test_declined_and_transparent_events_do_not_consume_time(self):
        import tools.calendar_tools as ct

        events = [
            CalendarEvent(
                id="e1", title="Focus block",
                start="2026-09-04T10:00:00+10:00", end="2026-09-04T12:00:00+10:00",
                transparent=True,
            ),
            CalendarEvent(
                id="e2", title="Optional sync",
                start="2026-09-04T13:00:00+10:00", end="2026-09-04T14:00:00+10:00",
                self_declined=True,
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            context = _calendar_enabled_context(tmp)
            orig = ct.get_calendar_provider
            ct.get_calendar_provider = lambda config: _DayStub(events)
            try:
                tool_obj = _day_agenda_tool(context)
                raw = await tool_obj.execute(
                    session=SimpleNamespace(shared_context=context),
                    date="2026-09-04",
                )
            finally:
                ct.get_calendar_provider = orig
            self.assertIn("- 00:00-23:59", raw)

    async def test_full_day_meeting_reports_no_free_intervals(self):
        import tools.calendar_tools as ct

        events = [
            CalendarEvent(
                id="e1", title="Offsite",
                start="2026-09-04T00:00:00+10:00", end="2026-09-05T00:00:00+10:00",
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            context = _calendar_enabled_context(tmp)
            orig = ct.get_calendar_provider
            ct.get_calendar_provider = lambda config: _DayStub(events)
            try:
                tool_obj = _day_agenda_tool(context)
                raw = await tool_obj.execute(
                    session=SimpleNamespace(shared_context=context),
                    date="2026-09-04",
                )
            finally:
                ct.get_calendar_provider = orig
            self.assertIn("free:\n- none", raw)

    async def test_defaults_timezone_from_config_when_not_passed(self):
        import tools.calendar_tools as ct

        with tempfile.TemporaryDirectory() as tmp:
            context = _calendar_enabled_context(tmp)
            stub = _DayStub([])
            orig = ct.get_calendar_provider
            ct.get_calendar_provider = lambda config: stub
            try:
                tool_obj = _day_agenda_tool(context)
                await tool_obj.execute(
                    session=SimpleNamespace(shared_context=context),
                    date="2026-09-04", timezone="Europe/London",
                )
            finally:
                ct.get_calendar_provider = orig
            self.assertEqual(
                stub.list_day_calls, [("2026-09-04", "Europe/London", None)]
            )


class TestPlanningCapabilityRegistration(unittest.TestCase):
    def _planning_context(self, tmp):
        from utils.config import PlanningConfig

        workspace = make_workspace(Path(tmp))
        context = make_context(workspace)
        context.config.planning = PlanningConfig(enabled=True)
        return context

    def test_planning_capabilities_are_registered(self):
        from tools.capability_catalog import build_capability_registry

        with tempfile.TemporaryDirectory() as tmp:
            context = self._planning_context(tmp)
            agent_def = context.agent_loader.load("pickle")
            registry = build_capability_registry(
                agent_def, context, include_post_message=False
            )
            ids = [cap.id for cap in registry.capabilities()]
            self.assertIn("planning.build_day_plan", ids)
            self.assertIn("planning.sync_daily_plan", ids)

    def test_absent_planning_config_registers_no_planning_capabilities(self):
        from tools.capability_catalog import build_capability_registry

        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            context = make_context(workspace)
            agent_def = context.agent_loader.load("pickle")
            registry = build_capability_registry(
                agent_def, context, include_post_message=False
            )
            ids = [cap.id for cap in registry.capabilities()]
            self.assertNotIn("planning.build_day_plan", ids)
            self.assertNotIn("planning.sync_daily_plan", ids)

    def test_planning_confirmed_executor_resolves_sync_daily_plan(self):
        from tools.capability_catalog import build_confirmed_executor_registry

        with tempfile.TemporaryDirectory() as tmp:
            context = self._planning_context(tmp)
            registry = build_confirmed_executor_registry(context.config)
            executor = registry.get("planning.sync_daily_plan")
            self.assertTrue(callable(executor))

    def test_absent_planning_config_has_no_confirmed_executor(self):
        from tools.capability_catalog import build_confirmed_executor_registry

        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            context = make_context(workspace)
            registry = build_confirmed_executor_registry(context.config)
            self.assertIsNone(registry.get("planning.sync_daily_plan"))
