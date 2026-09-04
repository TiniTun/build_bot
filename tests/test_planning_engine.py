"""Interval maths: what time is actually free, and what must never be touched."""

import unittest
from datetime import timedelta

from core.planning.engine import _at, _place, build_day_plan, free_intervals
from core.planning.models import (
    DayPattern, Interval, PatternLimits, ReadinessSignal, TimeWindow,
)
from provider.calendar.base import CalendarEvent
from provider.tasks.base import Task

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


LIMITS = PatternLimits(schedulable_window=WINDOW)
DEEP_WORK = DayPattern(name="Deep work", duration_minutes=60, priority="high",
                       energy="high", category="work")
WALK = DayPattern(name="Walk", duration_minutes=30, fixed_time="12:00",
                  flexibility="fixed", priority="medium", energy="low",
                  category="recovery")
AGENDA = [_event("10:00", "10:30"), _event("13:00", "14:00")]


def _sig(v):
    return ReadinessSignal(verdict=v, source="whoop", retry_eligible=False, note="n")


def _plan(verdict, patterns=(DEEP_WORK, WALK), tasks=(), limits=LIMITS):
    return build_day_plan(
        day=DAY, timezone=TZ, agenda=AGENDA, tasks=list(tasks),
        patterns=list(patterns), limits=limits, signal=_sig(verdict),
        weekday="mon", overdue_ids=set(),
    )


def _placed(plan):
    return [(e.title, e.start.strftime("%H:%M"), e.end.strftime("%H:%M"))
            for e in plan.events]


class TestBuildDayPlanWorkedExample(unittest.TestCase):
    """The spec's worked example, asserted exactly."""

    def test_green_places_walk_at_its_fixed_time_and_deep_work_first_thing(self):
        self.assertEqual(
            _placed(_plan("green")),
            [("Walk", "12:00", "12:30"), ("Deep work", "10:40", "11:40")],
        )

    def test_green_refuses_a_third_item_that_would_break_min_free(self):
        task = Task(id="9", content="Draft invoice", priority=3,
                    estimated_minutes=30)
        plan = _plan("green", tasks=[task])
        self.assertNotIn("Draft invoice", [e.title for e in plan.events])
        reasons = {u.slot_key: u.reason for u in plan.unscheduled}
        self.assertEqual(reasons["task:9"], "min_free_minutes")

    def test_yellow_drops_deep_work_for_energy(self):
        plan = _plan("yellow")
        self.assertEqual([e.title for e in plan.events], ["Walk"])
        reasons = {u.slot_key: u.reason for u in plan.unscheduled}
        self.assertEqual(reasons["pattern:mon:deep-work"], "energy_above_yellow")

    def test_red_keeps_only_the_recovery_walk(self):
        plan = _plan("red")
        self.assertEqual([e.title for e in plan.events], ["Walk"])
        reasons = {u.slot_key: u.reason for u in plan.unscheduled}
        self.assertEqual(reasons["pattern:mon:deep-work"], "not_recovery_on_red")

    def test_unknown_matches_yellow(self):
        self.assertEqual(_placed(_plan("unknown")), _placed(_plan("yellow")))


class TestPlacementRules(unittest.TestCase):
    def test_fixed_time_colliding_with_a_meeting_is_never_moved(self):
        clashing = DayPattern(name="Walk", duration_minutes=30, fixed_time="13:15",
                              flexibility="fixed", priority="medium",
                              energy="low", category="recovery")
        plan = _plan("green", patterns=[clashing])
        self.assertEqual(plan.events, [])
        self.assertEqual(plan.unscheduled[0].reason, "fixed_time_unavailable")

    def test_block_shorter_than_min_block_is_refused(self):
        tiny = DayPattern(name="Tiny", duration_minutes=5, priority="high",
                          energy="low", category="work")
        plan = _plan("green", patterns=[tiny])
        self.assertEqual(plan.unscheduled[0].reason, "below_min_block_minutes")

    def test_existing_events_are_reported_but_never_planned_over(self):
        plan = _plan("green")
        self.assertEqual(len(plan.mandatory), 2)
        for event in plan.events:
            self.assertFalse(
                event.start.strftime("%H:%M") < "10:30"
                and event.end.strftime("%H:%M") > "10:00"
            )

    def test_plan_is_reproducible(self):
        self.assertEqual(_plan("green").plan_hash, _plan("green").plan_hash)

    def test_plan_hash_changes_when_placement_changes(self):
        self.assertNotEqual(_plan("green").plan_hash, _plan("red").plan_hash)

    def test_place_splits_the_interval_it_lands_in(self):
        # `_place` is the mechanism that keeps two blocks from landing on top
        # of each other; pin its splitting behavior directly.
        day_10 = _at(DAY, "10:00", TZ)
        day_11 = _at(DAY, "11:00", TZ)
        ivs = [Interval(start=day_10, end=day_11)]
        _place(ivs, day_10 + timedelta(minutes=20), day_10 + timedelta(minutes=40))
        self.assertEqual(
            [(i.start, i.end) for i in ivs],
            [
                (day_10, day_10 + timedelta(minutes=20)),
                (day_10 + timedelta(minutes=40), day_11),
            ],
        )

    def test_second_flexible_block_never_overlaps_the_first(self):
        # Without `_place` consuming the interval a placed block claims, a
        # second flexible candidate would be free to start at the same time
        # as the first. Deep work (60 min) must claim 10:40-11:40 before
        # Study (30 min) looks for room, landing it at 11:40, not 10:40.
        study = DayPattern(name="Study", duration_minutes=30, priority="medium",
                           energy="low", category="personal")
        plan = _plan("green", patterns=[DEEP_WORK, study])
        self.assertEqual(
            _placed(plan),
            [("Deep work", "10:40", "11:40"), ("Study", "11:40", "12:10")],
        )

    def test_verdict_budget_binds_even_when_min_free_would_allow_more(self):
        # A tight max_scheduled_minutes must refuse a task on its own, even
        # when total_free - min_free_minutes is generous enough to admit it.
        # This isolates the verdict-budget term of `budget` from the
        # min_free term, which the worked example's numbers happen to make
        # equal for green/yellow.
        tight = PatternLimits(schedulable_window=WINDOW, max_scheduled_minutes=40,
                              min_free_minutes=0)
        task = Task(id="1", content="Quick task", priority=3,
                    estimated_minutes=50)
        plan = _plan("green", patterns=[], tasks=[task], limits=tight)
        self.assertEqual(plan.events, [])
        reasons = {u.slot_key: u.reason for u in plan.unscheduled}
        self.assertEqual(reasons["task:1"], "max_scheduled_minutes")


if __name__ == "__main__":
    unittest.main()
