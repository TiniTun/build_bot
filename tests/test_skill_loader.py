import asyncio
import tempfile
import unittest
from pathlib import Path

from core.skill_loader import SkillLoader
from tests.helpers import make_workspace, write_definition, write_file
from tools.skill_tool import create_skill_tool
from utils.config import Config
from utils.def_loader import DefNotFoundError

VALID_WHEN = ["The user asks about the weather"]


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
                    "when_to_use": VALID_WHEN,
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
            self.assertEqual(skills[0].when_to_use, VALID_WHEN)
            self.assertEqual(skills[0].required_tools, [])
            self.assertEqual(skills[0].permissions, {})
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

    def test_parses_full_v2_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            write_definition(
                workspace / "skills",
                "full",
                "SKILL.md",
                {
                    "name": "full-skill",
                    "description": "A complete skill.",
                    "when_to_use": ["When testing"],
                    "required_tools": ["read", "bash"],
                    "permissions": {"network": True},
                    "references": [
                        {
                            "path": "docs/guide.md",
                            "description": "Detailed guide.",
                            "when_to_load": "before deep work",
                        }
                    ],
                    "scripts": [
                        {
                            "path": "run.sh",
                            "description": "Runner.",
                            "when_to_run": "to execute the task",
                        }
                    ],
                },
                "Body text.",
            )
            write_file(workspace / "skills" / "full" / "docs" / "guide.md", "guide")
            write_file(workspace / "skills" / "full" / "run.sh", "echo hi")

            loader = SkillLoader.from_config(Config.load(workspace))
            skill = loader.load_skill("full")

            self.assertEqual(skill.required_tools, ["read", "bash"])
            self.assertEqual(skill.permissions, {"network": True})
            self.assertEqual(skill.references[0].path, "docs/guide.md")
            self.assertEqual(skill.scripts[0].when_to_run, "to execute the task")


class SkillValidatorTests(unittest.TestCase):
    def _loader(self, workspace: Path) -> SkillLoader:
        return SkillLoader.from_config(Config.load(workspace))

    def _result(self, results, skill_id):
        return next(r for r in results if r.id == skill_id)

    def test_reports_missing_required_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            write_definition(
                workspace / "skills",
                "no-when",
                "SKILL.md",
                {"name": "x", "description": "y"},
                "Body.",
            )
            results = self._loader(workspace).validate_skills()
            result = self._result(results, "no-when")
            self.assertFalse(result.ok)
            self.assertTrue(any("when_to_use" in e for e in result.errors))

    def test_reports_empty_when_to_use(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            write_definition(
                workspace / "skills",
                "empty-when",
                "SKILL.md",
                {"name": "x", "description": "y", "when_to_use": []},
                "Body.",
            )
            result = self._result(
                self._loader(workspace).validate_skills(), "empty-when"
            )
            self.assertFalse(result.ok)

    def test_reports_unknown_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            write_definition(
                workspace / "skills",
                "extra",
                "SKILL.md",
                {
                    "name": "x",
                    "description": "y",
                    "when_to_use": VALID_WHEN,
                    "bogus": "field",
                },
                "Body.",
            )
            result = self._result(self._loader(workspace).validate_skills(), "extra")
            self.assertFalse(result.ok)
            self.assertTrue(any("bogus" in e for e in result.errors))

    def test_reports_invalid_type(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            write_definition(
                workspace / "skills",
                "bad-type",
                "SKILL.md",
                {"name": "x", "description": "y", "when_to_use": "not-a-list"},
                "Body.",
            )
            result = self._result(self._loader(workspace).validate_skills(), "bad-type")
            self.assertFalse(result.ok)

    def test_reports_empty_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            write_definition(
                workspace / "skills",
                "empty-body",
                "SKILL.md",
                {"name": "x", "description": "y", "when_to_use": VALID_WHEN},
                "",
            )
            result = self._result(
                self._loader(workspace).validate_skills(), "empty-body"
            )
            self.assertFalse(result.ok)
            self.assertTrue(any("body" in e for e in result.errors))

    def test_reports_unsafe_reference_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            write_definition(
                workspace / "skills",
                "unsafe",
                "SKILL.md",
                {
                    "name": "x",
                    "description": "y",
                    "when_to_use": VALID_WHEN,
                    "references": [
                        {
                            "path": "../../etc/passwd",
                            "description": "bad",
                            "when_to_load": "never",
                        }
                    ],
                },
                "Body.",
            )
            result = self._result(self._loader(workspace).validate_skills(), "unsafe")
            self.assertFalse(result.ok)
            self.assertTrue(any("unsafe" in e for e in result.errors))

    def test_reports_missing_reference_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            write_definition(
                workspace / "skills",
                "missing-ref",
                "SKILL.md",
                {
                    "name": "x",
                    "description": "y",
                    "when_to_use": VALID_WHEN,
                    "references": [
                        {
                            "path": "docs/missing.md",
                            "description": "gone",
                            "when_to_load": "later",
                        }
                    ],
                },
                "Body.",
            )
            result = self._result(
                self._loader(workspace).validate_skills(), "missing-ref"
            )
            self.assertFalse(result.ok)
            self.assertTrue(any("file not found" in e for e in result.errors))

    def test_valid_skill_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            write_definition(
                workspace / "skills",
                "ok",
                "SKILL.md",
                {"name": "x", "description": "y", "when_to_use": VALID_WHEN},
                "Body.",
            )
            result = self._result(self._loader(workspace).validate_skills(), "ok")
            self.assertTrue(result.ok)


class SkillToolProgressiveLoadingTests(unittest.TestCase):
    def test_description_exposes_metadata_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            write_definition(
                workspace / "skills",
                "weather",
                "SKILL.md",
                {
                    "name": "weather-information",
                    "description": "Look up weather.",
                    "when_to_use": ["When the user asks about weather"],
                },
                "SECRET-BODY-CONTENT should not be exposed in the tool schema.",
            )
            loader = SkillLoader.from_config(Config.load(workspace))
            tool = create_skill_tool(loader)

            self.assertIsNotNone(tool)
            self.assertIn("weather-information", tool.description)
            self.assertIn("When the user asks about weather", tool.description)
            self.assertNotIn("SECRET-BODY-CONTENT", tool.description)

    def test_invocation_returns_body_and_manifest_not_file_contents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            write_definition(
                workspace / "skills",
                "weather",
                "SKILL.md",
                {
                    "name": "weather-information",
                    "description": "Look up weather.",
                    "when_to_use": ["When the user asks about weather"],
                    "references": [
                        {
                            "path": "docs/guide.md",
                            "description": "Deep guide.",
                            "when_to_load": "for detail",
                        }
                    ],
                },
                "FULL-BODY-CONTENT here.",
            )
            write_file(
                workspace / "skills" / "weather" / "docs" / "guide.md",
                "REFERENCE-FILE-CONTENT must not auto-load.",
            )
            loader = SkillLoader.from_config(Config.load(workspace))
            tool = create_skill_tool(loader)

            result = asyncio.run(tool.execute(session=None, skill_name="weather"))

            self.assertIn("FULL-BODY-CONTENT", result)
            self.assertIn("docs/guide.md", result)
            self.assertIn(
                str(workspace / "skills" / "weather" / "docs" / "guide.md"),
                result,
            )
            self.assertNotIn("REFERENCE-FILE-CONTENT", result)


if __name__ == "__main__":
    unittest.main()
