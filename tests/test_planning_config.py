"""Planning configuration: defaults, path resolution, and mode validation."""

import tempfile
import unittest
from pathlib import Path

from utils.config import Config, PlanningConfig


class TestPlanningConfig(unittest.TestCase):
    def test_absent_block_leaves_planning_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "config.user.yaml").write_text(
                "llm: {provider: openai, model: m, api_key: k}\ndefault_agent: pickle\n"
            )
            config = Config.load(Path(tmp))
        self.assertIsNone(config.planning)

    def test_mode_defaults_to_shadow(self):
        self.assertEqual(PlanningConfig().mode, "shadow")

    def test_invalid_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            PlanningConfig(mode="apply-everything")

    def test_patterns_path_resolves_against_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "config.user.yaml").write_text(
                "llm: {provider: openai, model: m, api_key: k}\n"
                "default_agent: pickle\n"
                "planning: {enabled: true, patterns_path: planning/day_patterns.yaml}\n"
            )
            config = Config.load(Path(tmp))
        assert config.planning is not None
        self.assertTrue(config.planning.patterns_path.is_absolute())
        # Compare against the config's OWN workspace, not a separately resolved
        # temp path: this asserts the real invariant (the nested path is resolved
        # against the workspace root) without depending on whether the platform
        # hands back a symlinked temp directory.
        self.assertEqual(
            config.planning.patterns_path,
            config.workspace / "planning" / "day_patterns.yaml",
        )

    def test_planning_calendar_id_may_not_be_primary(self):
        with self.assertRaises(ValueError):
            PlanningConfig(planning_calendar_id="primary")

    def test_planning_calendar_id_rejects_primary_case_insensitive(self):
        with self.assertRaises(ValueError):
            PlanningConfig(planning_calendar_id="Primary")

    def test_planning_calendar_id_rejects_primary_uppercase(self):
        with self.assertRaises(ValueError):
            PlanningConfig(planning_calendar_id="PRIMARY")

    def test_planning_calendar_id_rejects_primary_with_whitespace(self):
        with self.assertRaises(ValueError):
            PlanningConfig(planning_calendar_id=" primary ")

    def test_plan_deadline_invalid_format_rejected(self):
        with self.assertRaises(ValueError):
            PlanningConfig(plan_deadline="25:99")

    def test_plan_deadline_invalid_format_with_am_pm(self):
        with self.assertRaises(ValueError):
            PlanningConfig(plan_deadline="8:30am")
