import tempfile
import unittest
from pathlib import Path

from core.skill_loader import SkillLoader
from tests.helpers import make_workspace, write_definition
from utils.config import Config
from utils.def_loader import DefNotFoundError


class SkillLoaderTests(unittest.TestCase):
    def test_discovers_valid_skills_and_skips_invalid_definitions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            write_definition(
                workspace / "skills",
                "weather",
                "SKILL.md",
                {
                    "name": "weather-information",
                    "description": "Look up weather information.",
                },
                "Use the weather provider before answering.",
            )
            write_definition(
                workspace / "skills",
                "broken",
                "SKILL.md",
                {"name": "broken-skill"},
                "Missing description.",
            )

            loader = SkillLoader.from_config(Config.load(workspace))

            with self.assertLogs("core.skill_loader", level="WARNING"):
                skills = loader.discover_skills()

            self.assertEqual([skill.id for skill in skills], ["weather"])
            self.assertEqual(skills[0].name, "weather-information")
            self.assertEqual(
                skills[0].content,
                "Use the weather provider before answering.",
            )

    def test_load_skill_raises_for_missing_skill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            loader = SkillLoader.from_config(Config.load(workspace))

            with self.assertRaises(DefNotFoundError):
                loader.load_skill("missing")
