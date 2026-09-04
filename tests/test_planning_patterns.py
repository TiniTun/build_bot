"""Pattern-file loading, validation and per-weekday overrides."""

import tempfile
import unittest
from pathlib import Path

from core.planning.models import DayPattern, DayPatternSet


GOOD = """
version: 1
defaults:
  schedulable_window: {start: "10:00", end: "14:45"}
  buffer_minutes: 25
  max_scheduled_minutes: 210
  min_free_minutes: 45
  min_block_minutes: 15
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
        # GOOD's defaults deliberately differ from PatternLimits' own field
        # defaults (25/210/45/15 vs 10/180/60/20). A `limits_for()` that
        # rebuilt PatternLimits from just the overrides (ignoring
        # self.defaults) would fall back to the schema defaults here instead
        # of inheriting these, so this pins the merge rather than merely
        # matching pydantic's own defaults.
        self.assertEqual(_load(GOOD).limits_for("sat").buffer_minutes, 25)

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

    def test_fixed_time_and_window_are_mutually_exclusive_in_mapping_weekday(self):
        # The mon variant above only corrupts the first, list-shaped
        # weekday. Corrupt sat too: it is mapping-shaped (schedulable_window
        # + activities) and not first, so this pins that validate_set
        # actually walks every weekday and both accepted shapes.
        bad = GOOD.replace(
            "      - {name: Long run, duration_minutes: 90, priority: high,\n"
            "         energy: high, category: personal}",
            '      - {name: Long run, duration_minutes: 90, priority: high,\n'
            '         energy: high, category: personal, fixed_time: "10:00",\n'
            '         window: {start: "10:00", end: "11:00"}}',
        )
        with self.assertRaises(ValueError):
            _load(bad)

    def test_unknown_activity_key_is_rejected(self):
        with self.assertRaises(ValueError):
            _load(GOOD.replace("      category: work", "      colour: blue"))

    def test_unknown_activity_key_is_rejected_in_mapping_weekday(self):
        # Same rationale as above: pin that sat's activities (reached via
        # the mapping shape) are validated too, not just mon's plain list.
        bad = GOOD.replace(
            "      - {name: Long run, duration_minutes: 90, priority: high,\n"
            "         energy: high, category: personal}",
            "      - {name: Long run, duration_minutes: 90, priority: high,\n"
            "         energy: high, colour: blue}",
        )
        with self.assertRaises(ValueError):
            _load(bad)

    def test_unknown_weekday_key_is_rejected(self):
        with self.assertRaises(ValueError):
            _load(GOOD.replace("weekdays:\n  mon:", "weekdays:\n  someday: []\n  mon:"))

    def test_shipped_workspace_pattern_file_is_valid(self):
        # The file we ship must load; a broken default is a broken feature.
        DayPatternSet.load(
            Path(__file__).resolve().parents[1]
            / "default_workspace" / "planning" / "day_patterns.yaml"
        )


class TestDayPatternSlug(unittest.TestCase):
    def _activity(self, **overrides):
        fields = {
            "name": "Deep Work: Focus!",
            "duration_minutes": 60,
            "priority": "medium",
            "energy": "medium",
            "category": "work",
        }
        fields.update(overrides)
        return DayPattern.model_validate(fields)

    def test_slug_strips_punctuation_and_joins_words(self):
        self.assertEqual(self._activity().slug(), "deep-work-focus")

    def test_slug_is_stable_across_repeat_calls(self):
        activity = self._activity()
        self.assertEqual(activity.slug(), activity.slug())

    def test_slug_is_unaffected_by_unrelated_fields(self):
        a = self._activity(duration_minutes=60, priority="high")
        b = self._activity(duration_minutes=90, priority="low")
        self.assertEqual(a.slug(), b.slug())
