"""Day-listing and event-shape mapping for the Google Calendar provider.

Hermetic: a fake synchronous service is injected, following the pattern in
tests/test_google_calendar.py. No network, no Google auth.
"""

import unittest
from types import SimpleNamespace

from provider.calendar.base import CreateEventRequest
from provider.calendar.google_calendar import _event_body, _event_from_item


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
