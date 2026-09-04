"""Pattern-file loading, validation and per-weekday overrides."""

import tempfile
import unittest
from pathlib import Path

from core.planning.models import DayPatternSet


GOOD = """
version: 1
defaults:
  schedulable_window: {start: "10:00", end: "14:45"}
  buffer_minutes: 10
  max_scheduled_minutes: 180
  min_free_minutes: 60
  min_block_minutes: 20
weekdays:
  mon:
    - name: Deep work
      duration_minutes: 60
      priority: high
      flexibility: flexible
      energy: high
      category: work
    - name: Walk
      duration_minutes: 30
      fixed_time: "12:00"
      priority: medium
      flexibility: fixed
      energy: low
      category: recovery
  sat:
    schedulable_window: {start: "09:00", end: "11:00"}
    activities:
      - {name: Long run, duration_minutes: 90, priority: high,
         energy: high, category: personal}
"""


def _load(text):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "day_patterns.yaml"
        path.write_text(text)
        return DayPatternSet.load(path)


class TestPatternLoading(unittest.TestCase):
    def test_loads_activities_for_a_weekday(self):
        patterns = _load(GOOD)
        names = [a.name for a in patterns.activities_for("mon")]
        self.assertEqual(names, ["Deep work", "Walk"])

    def test_unlisted_weekday_is_empty_not_an_error(self):
        self.assertEqual(_load(GOOD).activities_for("wed"), [])

    def test_weekday_may_override_the_window(self):
        patterns = _load(GOOD)
        self.assertEqual(patterns.limits_for("sat").schedulable_window.start, "09:00")
        self.assertEqual(patterns.limits_for("mon").schedulable_window.start, "10:00")

    def test_weekday_inherits_unoverridden_defaults(self):
        self.assertEqual(_load(GOOD).limits_for("sat").buffer_minutes, 10)

    def test_missing_version_is_rejected(self):
        with self.assertRaises(ValueError):
            _load(GOOD.replace("version: 1\n", ""))

    def test_unknown_major_version_is_rejected_not_guessed(self):
        with self.assertRaisesRegex(ValueError, "version"):
            _load(GOOD.replace("version: 1", "version: 2"))

    def test_fixed_time_and_window_are_mutually_exclusive(self):
        bad = GOOD.replace(
            '      fixed_time: "12:00"',
            '      fixed_time: "12:00"\n      window: {start: "10:00", end: "11:00"}',
        )
        with self.assertRaises(ValueError):
            _load(bad)

    def test_unknown_activity_key_is_rejected(self):
        with self.assertRaises(ValueError):
            _load(GOOD.replace("      category: work", "      colour: blue"))

    def test_shipped_workspace_pattern_file_is_valid(self):
        # The file we ship must load; a broken default is a broken feature.
        DayPatternSet.load(
            Path(__file__).resolve().parents[1]
            / "default_workspace" / "planning" / "day_patterns.yaml"
        )
