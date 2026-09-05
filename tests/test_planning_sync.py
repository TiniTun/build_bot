"""Sync diffing: idempotence, and the refusal to touch anything not ours."""

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from core.planning.models import (
    DayPlan,
    PatternLimits,
    PlannedEvent,
    ReadinessSignal,
    TimeWindow,
)
from core.planning.sync import (
    GuardError,
    OWNER,
    SyncAction,
    SyncPlan,
    check_guards,
    diff,
    is_owned,
    marker,
)
from provider.calendar.base import CalendarEvent

TZ = ZoneInfo("Australia/Brisbane")
DAY = "2026-09-04"


def _planned(slot_key, title, start, end):
    return PlannedEvent(
        slot_key=slot_key, title=title,
        start=datetime.fromisoformat(f"{DAY}T{start}:00+10:00"),
        end=datetime.fromisoformat(f"{DAY}T{end}:00+10:00"),
    )


def _owned(slot_key, title, start, end, event_id="ev1", offset="+10:00"):
    return CalendarEvent(
        id=event_id, title=title,
        start=f"{DAY}T{start}:00{offset}", end=f"{DAY}T{end}:00{offset}",
        private_properties=marker(DAY, slot_key),
    )


def _foreign(title, start, end, event_id="foreign1", **props):
    return CalendarEvent(
        id=event_id, title=title,
        start=f"{DAY}T{start}:00+10:00", end=f"{DAY}T{end}:00+10:00",
        private_properties=props,
    )


class TestOwnership(unittest.TestCase):
    def test_marker_carries_owner_date_and_slot(self):
        m = marker(DAY, "pattern:mon:walk")
        self.assertEqual(m["owner"], OWNER)
        self.assertEqual(m["plan_date"], DAY)
        self.assertEqual(m["slot_key"], "pattern:mon:walk")

    def test_event_without_a_marker_is_not_owned(self):
        self.assertFalse(is_owned(_foreign("Lunch", "12:00", "13:00")))

    def test_event_with_a_different_owner_is_not_owned(self):
        self.assertFalse(is_owned(_foreign("X", "12:00", "13:00", owner="someone-else")))

    def test_our_own_event_is_owned(self):
        self.assertTrue(is_owned(_owned("pattern:mon:walk", "Walk", "12:00", "12:30")))


class TestIdempotence(unittest.TestCase):
    def test_rerunning_an_identical_plan_creates_nothing(self):
        desired = [_planned("pattern:mon:walk", "Walk", "12:00", "12:30")]
        existing = [_owned("pattern:mon:walk", "Walk", "12:00", "12:30")]
        plan = diff(desired, existing, plan_date=DAY)
        self.assertEqual(plan.creates, [])
        self.assertEqual(plan.patches, [])
        self.assertEqual(plan.deletes, [])
        self.assertEqual(plan.unchanged, ["pattern:mon:walk"])
        self.assertTrue(plan.is_empty)

    def test_equivalent_instants_in_a_different_offset_are_unchanged(self):
        # Google returns the calendar's offset; a raw string compare would call
        # this a patch on every single run and break idempotence silently.
        desired = [_planned("pattern:mon:walk", "Walk", "12:00", "12:30")]
        existing = [CalendarEvent(
            id="ev1", title="Walk",
            start="2026-09-04T02:00:00+00:00", end="2026-09-04T02:30:00+00:00",
            private_properties=marker(DAY, "pattern:mon:walk"),
        )]
        self.assertEqual(diff(desired, existing, plan_date=DAY).unchanged,
                         ["pattern:mon:walk"])

    def test_moved_event_is_patched_not_recreated(self):
        desired = [_planned("pattern:mon:walk", "Walk", "12:30", "13:00")]
        existing = [_owned("pattern:mon:walk", "Walk", "12:00", "12:30")]
        plan = diff(desired, existing, plan_date=DAY)
        self.assertEqual([a.slot_key for a in plan.patches], ["pattern:mon:walk"])
        self.assertEqual(plan.patches[0].event_id, "ev1")
        self.assertEqual(plan.creates, [])

    def test_retitled_event_is_patched(self):
        desired = [_planned("task:8241", "Draft invoice v2", "10:40", "11:10")]
        existing = [_owned("task:8241", "Draft invoice", "10:40", "11:10")]
        self.assertEqual(len(diff(desired, existing, plan_date=DAY).patches), 1)

    def test_owned_event_no_longer_wanted_is_deleted(self):
        existing = [_owned("pattern:mon:walk", "Walk", "12:00", "12:30")]
        plan = diff([], existing, plan_date=DAY)
        self.assertEqual([a.slot_key for a in plan.deletes], ["pattern:mon:walk"])

    def test_new_event_is_created(self):
        desired = [_planned("pattern:mon:deep-work", "Deep work", "10:40", "11:40")]
        plan = diff(desired, [], plan_date=DAY)
        self.assertEqual([a.slot_key for a in plan.creates], ["pattern:mon:deep-work"])


class TestForeignEventsAreUntouchable(unittest.TestCase):
    def test_foreign_event_is_never_deleted(self):
        existing = [_foreign("Lunch with Sam", "12:00", "13:00")]
        plan = diff([], existing, plan_date=DAY)
        self.assertEqual(plan.deletes, [])
        self.assertEqual(plan.refused_foreign, ["foreign1"])

    def test_foreign_event_is_never_patched_even_on_a_slot_collision(self):
        # Same time as something we want, but not ours: we create alongside it
        # and never touch theirs.
        desired = [_planned("pattern:mon:walk", "Walk", "12:00", "12:30")]
        existing = [_foreign("Their walk", "12:00", "12:30")]
        plan = diff(desired, existing, plan_date=DAY)
        self.assertEqual(plan.patches, [])
        self.assertEqual([a.slot_key for a in plan.creates], ["pattern:mon:walk"])
        self.assertEqual(plan.refused_foreign, ["foreign1"])

    def test_our_marker_for_a_different_date_is_left_alone(self):
        stale = CalendarEvent(
            id="old", title="Walk",
            start="2026-09-03T12:00:00+10:00", end="2026-09-03T12:30:00+10:00",
            private_properties=marker("2026-09-03", "pattern:mon:walk"),
        )
        plan = diff([], [stale], plan_date=DAY)
        self.assertEqual(plan.deletes, [],
                         "another day's plan is not this run's business")


class TestMutationCoverage(unittest.TestCase):
    """Guards for the specific mutations that would silently pass the brief's
    own suite. See task-3.1-report.md for the mapping from mutation to test.
    """

    def test_owned_event_with_different_offset_but_different_instant_is_patched(self):
        # Companion to the differing-offset idempotence test: this checks the
        # comparison actually discriminates, not just that it tolerates
        # cosmetic offset differences. If _instant() were replaced by raw
        # string compare this would still pass (both differ), so it alone
        # would not catch mutation 1 -- paired with the unchanged case above
        # it pins down real instant comparison in both directions.
        desired = [_planned("pattern:mon:walk", "Walk", "12:00", "12:30")]
        existing = [CalendarEvent(
            id="ev1", title="Walk",
            # +10:00 12:00 is 02:00 UTC; +00:00 03:00 UTC is a different, later
            # instant despite looking "close" as strings.
            start="2026-09-04T03:00:00+00:00", end="2026-09-04T03:30:00+00:00",
            private_properties=marker(DAY, "pattern:mon:walk"),
        )]
        plan = diff(desired, existing, plan_date=DAY)
        self.assertEqual([a.slot_key for a in plan.patches], ["pattern:mon:walk"])
        self.assertEqual(plan.unchanged, [])

    def test_owned_event_for_a_different_date_is_not_deleted_even_when_unwanted(self):
        # Mutation 2 target: dropping the plan_date check turns another day's
        # owned event into a deletion candidate for today's empty plan.
        stale = CalendarEvent(
            id="old", title="Walk",
            start="2026-09-03T12:00:00+10:00", end="2026-09-03T12:30:00+10:00",
            private_properties=marker("2026-09-05", "pattern:mon:walk"),
        )
        plan = diff([], [stale], plan_date=DAY)
        self.assertEqual(plan.deletes, [])
        self.assertEqual(plan.refused_foreign, [])
        self.assertEqual(plan.unchanged, [])

    def test_foreign_event_at_matching_time_and_title_is_never_patched(self):
        # Mutation 3 target: dropping the is_owned() check would let a foreign
        # event whose time+title happen to match our desired slot slip into
        # "unchanged" or worse "patch" territory instead of refused_foreign.
        desired = [_planned("pattern:mon:walk", "Walk", "12:00", "12:30")]
        existing = [_foreign("Walk", "12:00", "12:30")]
        plan = diff(desired, existing, plan_date=DAY)
        self.assertEqual(plan.patches, [])
        self.assertEqual(plan.unchanged, [])
        self.assertEqual([a.slot_key for a in plan.creates], ["pattern:mon:walk"])
        self.assertEqual(plan.refused_foreign, ["foreign1"])

    def test_owned_event_without_slot_key_is_deleted_and_logged(self):
        # Mutation 6 target: a marker missing slot_key must not be treated as
        # matching every desired slot. It is provably ours (owner + plan_date
        # match) and provably unmatchable, which is exactly the condition
        # "owned + not desired -> delete" covers -- so it must be routed to
        # plan.deletes with a signal, never silently dropped.
        desired = [_planned("pattern:mon:walk", "Walk", "12:00", "12:30")]
        broken = CalendarEvent(
            id="broken1", title="Walk",
            start=f"{DAY}T12:00:00+10:00", end=f"{DAY}T12:30:00+10:00",
            private_properties={"owner": OWNER, "plan_date": DAY},
        )
        with self.assertLogs("core.planning.sync", level="WARNING") as logs:
            plan = diff(desired, [broken], plan_date=DAY)
        self.assertEqual(plan.unchanged, [])
        self.assertEqual([a.slot_key for a in plan.creates], ["pattern:mon:walk"])
        self.assertEqual(plan.patches, [])
        self.assertEqual([a.event_id for a in plan.deletes], ["broken1"])
        self.assertTrue(any("broken1" in message for message in logs.output))

    def test_foreign_event_without_slot_key_is_refused_not_deleted(self):
        # Pins that the new delete-on-missing-slot_key path can only be
        # reached by something already proven ours -- a foreign event with no
        # slot_key at all must still land in refused_foreign, never deletes.
        foreign = CalendarEvent(
            id="foreign2", title="Someone else's thing",
            start=f"{DAY}T12:00:00+10:00", end=f"{DAY}T12:30:00+10:00",
            private_properties={},
        )
        plan = diff([], [foreign], plan_date=DAY)
        self.assertEqual(plan.deletes, [])
        self.assertEqual(plan.refused_foreign, ["foreign2"])


LIMITS = PatternLimits(schedulable_window=TimeWindow(start="10:00", end="14:45"))
PLAN_CAL = "plan@group.calendar.google.com"


def _day_plan(events):
    return DayPlan(
        date=DAY, timezone="Australia/Brisbane",
        readiness=ReadinessSignal(verdict="green", source="whoop",
                                  retry_eligible=False, note="n"),
        events=events,
    )


def _guard(day_plan, sync=None, *, planning_calendar_id=PLAN_CAL,
           read_calendar_id="primary", max_events=12):
    check_guards(day_plan, sync or SyncPlan(),
                 planning_calendar_id=planning_calendar_id,
                 read_calendar_id=read_calendar_id,
                 limits=LIMITS, max_events_per_day=max_events)


class TestGuards(unittest.TestCase):
    def test_a_valid_plan_passes(self):
        _guard(_day_plan([_planned("pattern:mon:walk", "Walk", "12:00", "12:30")]))

    def test_unset_planning_calendar_is_refused(self):
        with self.assertRaisesRegex(GuardError, "planning_calendar_id"):
            _guard(_day_plan([]), planning_calendar_id=None)

    def test_primary_as_planning_calendar_is_refused(self):
        with self.assertRaisesRegex(GuardError, "primary"):
            _guard(_day_plan([]), planning_calendar_id="primary")

    def test_planning_calendar_equal_to_the_read_calendar_is_refused(self):
        with self.assertRaisesRegex(GuardError, "same calendar"):
            _guard(_day_plan([]), planning_calendar_id="work@example.com",
                   read_calendar_id="work@example.com")

    def test_planning_calendar_matches_an_explicit_non_primary_read_calendar(self):
        # Guard 3 (planning calendar == read calendar) can only ever fire
        # when the two are equal -- and when the read calendar is left
        # *unset*, `_read_calendar_id` always resolves it to "primary", which
        # guard 2 (planning_calendar_id == "primary") refuses first. So guard
        # 3 only does real work when `external_tools.calendar.calendar_id` is
        # an explicit non-primary value that happens to collide with
        # planning_calendar_id -- exercise that case directly rather than the
        # unreachable "both resolve to primary" one.
        with self.assertRaisesRegex(GuardError, "same calendar"):
            _guard(_day_plan([]), planning_calendar_id="team@example.com",
                   read_calendar_id="team@example.com")

    def test_event_on_another_date_is_refused(self):
        stray = PlannedEvent(
            slot_key="x", title="Stray",
            start=datetime.fromisoformat("2026-09-05T12:00:00+10:00"),
            end=datetime.fromisoformat("2026-09-05T12:30:00+10:00"))
        with self.assertRaisesRegex(GuardError, "date"):
            _guard(_day_plan([stray]))

    def test_event_outside_the_schedulable_window_is_refused(self):
        with self.assertRaisesRegex(GuardError, "window"):
            _guard(_day_plan([_planned("x", "Late", "20:00", "20:30")]))

    def test_too_many_events_is_refused(self):
        events = [_planned(f"task:{i}", f"T{i}", "10:00", "10:15") for i in range(13)]
        with self.assertRaisesRegex(GuardError, "max_events_per_day"):
            _guard(_day_plan(events), max_events=12)

    def test_patching_a_foreign_event_id_is_refused(self):
        sync = SyncPlan(
            patches=[SyncAction(op="patch", slot_key="x", title="T",
                                start=None, end=None, event_id="foreign1")],
            refused_foreign=["foreign1"])
        with self.assertRaisesRegex(GuardError, "foreign"):
            _guard(_day_plan([]), sync)

    def test_deleting_a_foreign_event_id_is_refused(self):
        sync = SyncPlan(
            deletes=[SyncAction(op="delete", slot_key="x", title="T",
                                event_id="foreign1")],
            refused_foreign=["foreign1"])
        with self.assertRaisesRegex(GuardError, "foreign"):
            _guard(_day_plan([]), sync)


if __name__ == "__main__":
    unittest.main()
