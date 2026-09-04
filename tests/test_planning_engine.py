"""Interval maths: what time is actually free, and what must never be touched."""

import unittest

from core.planning.engine import free_intervals
from core.planning.models import TimeWindow
from provider.calendar.base import CalendarEvent

TZ = "Australia/Brisbane"
DAY = "2026-09-04"
WINDOW = TimeWindow(start="10:00", end="14:45")


def _event(start, end, **over):
    data = dict(
        id=start, title="busy",
        start=f"{DAY}T{start}:00+10:00", end=f"{DAY}T{end}:00+10:00",
    )
    data.update(over)
    return CalendarEvent(**data)


def _hhmm(intervals):
    return [(i.start.strftime("%H:%M"), i.end.strftime("%H:%M")) for i in intervals]


class TestFreeIntervals(unittest.TestCase):
    def test_empty_day_is_one_interval(self):
        got = free_intervals([], WINDOW, 10, DAY, TZ)
        self.assertEqual(_hhmm(got), [("10:00", "14:45")])

    def test_meetings_subtract_with_buffers(self):
        events = [_event("10:00", "10:30"), _event("13:00", "14:00")]
        got = free_intervals(events, WINDOW, 10, DAY, TZ)
        self.assertEqual(_hhmm(got), [("10:40", "12:50"), ("14:10", "14:45")])

    def test_total_free_matches_the_spec_worked_example(self):
        events = [_event("10:00", "10:30"), _event("13:00", "14:00")]
        self.assertEqual(sum(i.minutes for i in free_intervals(events, WINDOW, 10, DAY, TZ)), 165)

    def test_all_day_event_consumes_no_time(self):
        events = [CalendarEvent(id="h", title="Public holiday",
                                start=DAY, end="2026-09-05", all_day=True)]
        self.assertEqual(_hhmm(free_intervals(events, WINDOW, 10, DAY, TZ)),
                         [("10:00", "14:45")])

    def test_all_day_event_alongside_a_real_meeting(self):
        # An all-day marker sharing the day with a real meeting must still
        # let the meeting's buffer subtract normally: the all-day exclusion
        # must not accidentally suppress the *other* event's busy span.
        events = [
            CalendarEvent(id="h", title="Public holiday",
                          start=DAY, end="2026-09-05", all_day=True),
            _event("12:00", "12:30"),
        ]
        self.assertEqual(_hhmm(free_intervals(events, WINDOW, 10, DAY, TZ)),
                         [("10:00", "11:50"), ("12:40", "14:45")])

    def test_transparent_event_consumes_no_time(self):
        events = [_event("10:00", "12:00", transparent=True)]
        self.assertEqual(_hhmm(free_intervals(events, WINDOW, 10, DAY, TZ)),
                         [("10:00", "14:45")])

    def test_declined_event_consumes_no_time(self):
        events = [_event("10:00", "12:00", self_declined=True)]
        self.assertEqual(_hhmm(free_intervals(events, WINDOW, 10, DAY, TZ)),
                         [("10:00", "14:45")])

    def test_events_outside_the_window_are_ignored(self):
        events = [_event("07:00", "08:00"), _event("16:00", "17:00")]
        self.assertEqual(_hhmm(free_intervals(events, WINDOW, 10, DAY, TZ)),
                         [("10:00", "14:45")])

    def test_overlapping_meetings_merge(self):
        events = [_event("11:00", "12:00"), _event("11:30", "12:30")]
        self.assertEqual(_hhmm(free_intervals(events, WINDOW, 0, DAY, TZ)),
                         [("10:00", "11:00"), ("12:30", "14:45")])

    def test_overlapping_meetings_merge_out_of_chronological_order(self):
        # Calendar APIs do not guarantee event order. If the events arrive
        # later-first, an unsorted merge would leave a false-free gap where
        # the earlier (but later-listed) meeting actually runs.
        events = [_event("11:30", "12:30"), _event("11:00", "12:00")]
        got = free_intervals(events, WINDOW, 0, DAY, TZ)
        self.assertEqual(_hhmm(got), [("10:00", "11:00"), ("12:30", "14:45")])

    def test_unparseable_event_is_skipped_and_logged(self):
        # We cannot mark it busy (its span is precisely what failed to
        # parse), so it is dropped and the window comes back fully free.
        # That is a real gap in safety if it happened silently, so pin the
        # warning as a tested property, not just a claim in a comment.
        events = [CalendarEvent(id="bad", title="broken",
                                start="not-a-date", end="also-not-a-date")]
        with self.assertLogs("core.planning.engine", level="WARNING") as logs:
            got = free_intervals(events, WINDOW, 10, DAY, TZ)
        self.assertEqual(_hhmm(got), [("10:00", "14:45")])
        self.assertTrue(any("bad" in message for message in logs.output))

    def test_meeting_covering_the_window_leaves_nothing(self):
        self.assertEqual(free_intervals([_event("09:00", "15:00")], WINDOW, 10, DAY, TZ), [])

    def test_zero_buffer_leaves_meeting_edges_untouched(self):
        # With buffer_minutes=0 the free edges must sit exactly on the
        # meeting boundary, not one minute inside or outside it.
        events = [_event("11:00", "12:00")]
        self.assertEqual(_hhmm(free_intervals(events, WINDOW, 0, DAY, TZ)),
                         [("10:00", "11:00"), ("12:00", "14:45")])


if __name__ == "__main__":
    unittest.main()
