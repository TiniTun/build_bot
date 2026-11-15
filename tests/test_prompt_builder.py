import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from core.prompt_builder import PromptBuilder


def _builder(timezone: str | None) -> PromptBuilder:
    context = SimpleNamespace(config=SimpleNamespace(timezone=timezone))
    return PromptBuilder(context)  # type: ignore[arg-type]


class RuntimeContextTests(unittest.TestCase):
    def test_runtime_shows_configured_timezone_and_offset(self) -> None:
        builder = _builder("Australia/Brisbane")

        runtime = builder._build_return_context("pickle", builder._now())

        self.assertIn("Timezone: Australia/Brisbane", runtime)
        time_line = next(line for line in runtime.splitlines() if line.startswith("Time: "))
        shown = datetime.fromisoformat(time_line.removeprefix("Time: "))
        self.assertEqual(shown.utcoffset(), timedelta(hours=10))

    def test_now_is_in_configured_timezone(self) -> None:
        now = _builder("Australia/Brisbane")._now()

        self.assertEqual(now.tzinfo, ZoneInfo("Australia/Brisbane"))

    def test_runtime_without_timezone_has_no_timezone_line(self) -> None:
        builder = _builder(None)

        runtime = builder._build_return_context("pickle", builder._now())

        self.assertNotIn("Timezone:", runtime)
        self.assertIn("Agent: pickle", runtime)


if __name__ == "__main__":
    unittest.main()
