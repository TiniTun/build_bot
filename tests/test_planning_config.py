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
        self.assertEqual(
            config.planning.patterns_path,
            Path(tmp).resolve() / "planning" / "day_patterns.yaml",
        )

    def test_planning_calendar_id_may_not_be_primary(self):
        with self.assertRaises(ValueError):
            PlanningConfig(planning_calendar_id="primary")
