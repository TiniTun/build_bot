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
