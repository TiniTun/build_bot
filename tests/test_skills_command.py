import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.commands.handlers import SkillsCommand
from tests.helpers import make_context, make_workspace, write_definition, write_file


def _session(workspace: Path) -> SimpleNamespace:
    return SimpleNamespace(shared_context=make_context(workspace))


class SkillsCommandTests(unittest.TestCase):
    def test_list_mode_shows_metadata(self) -> None:
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
                "BODY-CONTENT must not appear in list mode.",
            )

            output = asyncio.run(SkillsCommand().execute("", _session(workspace)))

            self.assertIn("weather", output)
            self.assertIn("Look up weather.", output)
            self.assertIn("When the user asks about weather", output)
            self.assertNotIn("BODY-CONTENT", output)

    def test_detail_mode_shows_full_contract_without_file_contents(self) -> None:
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
                    "required_tools": ["read"],
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
                "REFERENCE-FILE-CONTENT must not load.",
            )

            output = asyncio.run(
                SkillsCommand().execute("weather", _session(workspace))
            )

            self.assertIn("When to use", output)
            self.assertIn("Required tools", output)
            self.assertIn("docs/guide.md", output)
            self.assertIn("FULL-BODY-CONTENT", output)
            self.assertNotIn("REFERENCE-FILE-CONTENT", output)

    def test_detail_mode_unknown_skill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            output = asyncio.run(
                SkillsCommand().execute("nope", _session(workspace))
            )
            self.assertIn("not found", output)


if __name__ == "__main__":
    unittest.main()
