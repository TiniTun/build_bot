"""Duration-label parsing for Todoist tasks."""

import unittest

from provider.tasks.base import Task, parse_duration_label


class TestParseDurationLabel(unittest.TestCase):
    def test_minute_labels(self):
        self.assertEqual(parse_duration_label(["15m"]), 15)
        self.assertEqual(parse_duration_label(["30m"]), 30)
        self.assertEqual(parse_duration_label(["60m"]), 60)
        self.assertEqual(parse_duration_label(["90m"]), 90)

    def test_hour_labels(self):
        self.assertEqual(parse_duration_label(["1h"]), 60)
        self.assertEqual(parse_duration_label(["1.5h"]), 90)
        self.assertEqual(parse_duration_label(["2h"]), 120)

    def test_first_match_wins_in_label_order(self):
        self.assertEqual(parse_duration_label(["work", "30m", "60m"]), 30)

    def test_no_duration_label(self):
        self.assertIsNone(parse_duration_label(["work", "home"]))
        self.assertIsNone(parse_duration_label([]))

    def test_ignores_non_duration_lookalikes(self):
        # A label must be exactly a duration, not merely contain one.
        self.assertIsNone(parse_duration_label(["sprint-30max"]))
        self.assertIsNone(parse_duration_label(["0m"]))

    def test_task_defaults_carry_assumed_flag(self):
        task = Task(id="1", content="x")
        self.assertIsNone(task.estimated_minutes)
        self.assertFalse(task.duration_assumed)


def _raw(labels, **over):
    data = {"id": "8241", "content": "Draft invoice", "labels": labels, "priority": 1}
    data.update(over)
    return data


class TestTodoistDurationMapping(unittest.TestCase):
    def test_label_duration_is_not_assumed(self):
        from provider.tasks.todoist import _to_task

        task = _to_task(_raw(["45m"]), default_minutes=30)
        self.assertEqual(task.estimated_minutes, 45)
        self.assertFalse(task.duration_assumed)

    def test_missing_label_uses_configured_fallback_and_marks_assumed(self):
        from provider.tasks.todoist import _to_task

        task = _to_task(_raw(["work"]), default_minutes=25)
        self.assertEqual(task.estimated_minutes, 25)
        self.assertTrue(task.duration_assumed)

    def test_config_default_is_thirty_minutes(self):
        from utils.config import TasksProviderConfig

        self.assertEqual(TasksProviderConfig().default_duration_minutes, 30)
