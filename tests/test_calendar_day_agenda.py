"""Day-listing and event-shape mapping for the Google Calendar provider.

Hermetic: a fake synchronous service is injected, following the pattern in
tests/test_google_calendar.py. No network, no Google auth.
"""

import asyncio
import unittest
from types import SimpleNamespace

from provider.calendar.base import CreateEventRequest, NullCalendarProvider
from provider.calendar.google_calendar import (
    GoogleCalendarProvider,
    _event_body,
    _event_from_item,
)
from provider.external_errors import AuthMissingError


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
