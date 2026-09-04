"""The readiness policy: what each verdict allows to be scheduled at all."""

import unittest

from core.planning.engine import budget_minutes, select_candidates, task_priority
from core.planning.models import (
    DayPattern, PatternLimits, ReadinessSignal, TimeWindow,
)
from provider.tasks.base import Task

LIMITS = PatternLimits(schedulable_window=TimeWindow(start="10:00", end="14:45"))

DEEP_WORK = DayPattern(name="Deep work", duration_minutes=60, priority="high",
                       energy="high", category="work")
WALK = DayPattern(name="Walk", duration_minutes=30, fixed_time="12:00",
                  flexibility="fixed", priority="medium", energy="low",
                  category="recovery")
ADMIN = DayPattern(name="Admin", duration_minutes=30, priority="low",
                   energy="low", category="admin")


def _signal(verdict):
    return ReadinessSignal(verdict=verdict, source="whoop",
                           retry_eligible=False, note="n")


def _task(tid, priority, minutes=30):
    return Task(id=tid, content=f"Task {tid}", priority=priority,
                estimated_minutes=minutes)


def _names(candidates):
    return [c.title for c in candidates]


class TestVerdictPolicy(unittest.TestCase):
    def test_green_admits_everything(self):
        got = select_candidates([DEEP_WORK, WALK, ADMIN], [], _signal("green"),
                                LIMITS, weekday="mon", overdue_ids=set())
        self.assertEqual(_names(got), ["Deep work", "Walk", "Admin"])

    def test_yellow_drops_high_energy_and_low_priority(self):
        got = select_candidates([DEEP_WORK, WALK, ADMIN], [], _signal("yellow"),
                                LIMITS, weekday="mon", overdue_ids=set())
        self.assertEqual(_names(got), ["Walk"])

    def test_red_keeps_only_recovery_or_essential(self):
        got = select_candidates([DEEP_WORK, WALK, ADMIN], [], _signal("red"),
                                LIMITS, weekday="mon", overdue_ids=set())
        self.assertEqual(_names(got), ["Walk"])

    def test_red_keeps_an_essential_activity_even_if_it_is_work(self):
        review = DayPattern(name="Weekly review", duration_minutes=45,
                            priority="essential", energy="medium", category="admin")
        got = select_candidates([review], [], _signal("red"), LIMITS,
                                weekday="fri", overdue_ids=set())
        self.assertEqual(_names(got), ["Weekly review"])

    def test_unknown_behaves_exactly_like_yellow(self):
        activities = [DEEP_WORK, WALK, ADMIN]
        as_yellow = select_candidates(activities, [], _signal("yellow"), LIMITS,
                                      weekday="mon", overdue_ids=set())
        as_unknown = select_candidates(activities, [], _signal("unknown"), LIMITS,
                                       weekday="mon", overdue_ids=set())
        self.assertEqual(_names(as_yellow), _names(as_unknown))

    def test_unknown_is_not_as_permissive_as_green(self):
        got = select_candidates([DEEP_WORK, WALK, ADMIN], [], _signal("unknown"),
                                LIMITS, weekday="mon", overdue_ids=set())
        self.assertEqual(_names(got), ["Walk"])


class TestTaskPriorityMapping(unittest.TestCase):
    def test_todoist_priorities_map_onto_the_pattern_scale(self):
        self.assertEqual(task_priority(_task("a", 4), overdue=False), "high")
        self.assertEqual(task_priority(_task("a", 3), overdue=False), "medium")
        self.assertEqual(task_priority(_task("a", 2), overdue=False), "low")
        self.assertEqual(task_priority(_task("a", 1), overdue=False), "low")

    def test_overdue_p1_is_promoted_to_essential(self):
        self.assertEqual(task_priority(_task("a", 4), overdue=True), "essential")

    def test_overdue_p2_is_not_promoted(self):
        self.assertEqual(task_priority(_task("a", 3), overdue=True), "medium")

    def test_overdue_p3_is_not_promoted(self):
        self.assertEqual(task_priority(_task("a", 2), overdue=True), "low")

    def test_red_schedules_no_task_except_an_overdue_p1(self):
        tasks = [_task("1", 4), _task("2", 3)]
        got = select_candidates([], tasks, _signal("red"), LIMITS,
                                weekday="mon", overdue_ids={"1"})
        self.assertEqual([c.slot_key for c in got], ["task:1"])

    def test_red_admits_overdue_p1_as_essential_priority_on_the_candidate(self):
        tasks = [_task("1", 4)]
        got = select_candidates([], tasks, _signal("red"), LIMITS,
                                weekday="mon", overdue_ids={"1"})
        self.assertEqual(got[0].priority, "essential")

    def test_non_overdue_p1_task_is_dropped_on_red(self):
        tasks = [_task("1", 4)]
        got = select_candidates([], tasks, _signal("red"), LIMITS,
                                weekday="mon", overdue_ids=set())
        self.assertEqual(got, [])


class TestDeterministicOrdering(unittest.TestCase):
    def test_overdue_tasks_come_before_due_today(self):
        tasks = [_task("due", 4), _task("late", 3)]
        got = select_candidates([], tasks, _signal("green"), LIMITS,
                                weekday="mon", overdue_ids={"late"})
        self.assertEqual([c.slot_key for c in got], ["task:late", "task:due"])

    def test_equal_tasks_tie_break_on_slot_key(self):
        tasks = [_task("b", 3), _task("a", 3)]
        got = select_candidates([], tasks, _signal("green"), LIMITS,
                                weekday="mon", overdue_ids=set())
        self.assertEqual([c.slot_key for c in got], ["task:a", "task:b"])

    def test_pattern_activities_tie_break_on_file_order(self):
        first = DayPattern(name="Alpha", duration_minutes=30, priority="medium")
        second = DayPattern(name="Beta", duration_minutes=30, priority="medium")
        got = select_candidates([second, first], [], _signal("green"), LIMITS,
                                weekday="mon", overdue_ids=set())
        self.assertEqual(_names(got), ["Beta", "Alpha"])

    def test_slot_keys_are_stable_across_runs(self):
        a = select_candidates([WALK], [], _signal("green"), LIMITS,
                              weekday="mon", overdue_ids=set())
        b = select_candidates([WALK], [], _signal("green"), LIMITS,
                              weekday="mon", overdue_ids=set())
        self.assertEqual([c.slot_key for c in a], [c.slot_key for c in b])
        self.assertEqual(a[0].slot_key, "pattern:mon:walk")

    def test_patterns_are_placed_before_tasks(self):
        tasks = [_task("t", 4)]
        got = select_candidates([ADMIN], tasks, _signal("green"), LIMITS,
                                weekday="mon", overdue_ids=set())
        self.assertEqual([c.slot_key for c in got],
                         ["pattern:mon:admin", "task:t"])


class TestBudgetMinutes(unittest.TestCase):
    def test_green_budget_is_full(self):
        self.assertEqual(budget_minutes("green", LIMITS), LIMITS.max_scheduled_minutes)

    def test_yellow_budget_is_60_percent(self):
        self.assertEqual(
            budget_minutes("yellow", LIMITS), int(LIMITS.max_scheduled_minutes * 0.6)
        )

    def test_red_budget_is_25_percent(self):
        self.assertEqual(
            budget_minutes("red", LIMITS), int(LIMITS.max_scheduled_minutes * 0.25)
        )

    def test_unknown_budget_matches_yellow(self):
        self.assertEqual(budget_minutes("unknown", LIMITS), budget_minutes("yellow", LIMITS))


if __name__ == "__main__":
    unittest.main()
