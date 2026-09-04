"""Sync diffing: idempotence, and the refusal to touch anything not ours."""

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from core.planning.models import PlannedEvent
from core.planning.sync import OWNER, diff, is_owned, marker
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

    def test_owned_event_without_slot_key_is_ignored_not_matched_to_everything(self):
        # Mutation 6 target: a marker missing slot_key must not be treated as
        # matching every desired slot -- it should simply be excluded from the
        # ours-by-slot_key map (and thus neither patched nor left unchanged).
        desired = [_planned("pattern:mon:walk", "Walk", "12:00", "12:30")]
        broken = CalendarEvent(
            id="broken1", title="Walk",
            start=f"{DAY}T12:00:00+10:00", end=f"{DAY}T12:30:00+10:00",
            private_properties={"owner": OWNER, "plan_date": DAY},
        )
        plan = diff(desired, [broken], plan_date=DAY)
        self.assertEqual(plan.unchanged, [])
        self.assertEqual([a.slot_key for a in plan.creates], ["pattern:mon:walk"])
        self.assertEqual(plan.patches, [])


if __name__ == "__main__":
    unittest.main()
