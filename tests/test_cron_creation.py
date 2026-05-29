import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from tests.helpers import make_context, make_workspace
from tools.registry import ToolRegistry


class CronCreationTests(unittest.TestCase):
    def test_create_cron_job_writes_to_configured_crons_path_and_loads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = make_context(make_workspace(Path(tmp)))
            session = type("Session", (), {"shared_context": context})()

            result = asyncio.run(
                ToolRegistry.with_builtins().execute_tool(
                    "create_cron_job",
                    session=session,
                    name="Morning brief",
                    description="Send the morning brief.",
                    agent="pickle",
                    schedule="*/5 * * * *",
                    prompt="Use post_message to send a summary.",
                )
            )

            cron_file = context.config.crons_path / "morning-brief" / "CRON.md"
            cron_def = context.cron_loader.load("morning-brief")

            self.assertIn("Created cron job `morning-brief`", result)
            self.assertTrue(cron_file.exists())
            self.assertEqual(cron_def.name, "Morning brief")
            self.assertEqual(cron_def.agent, "pickle")
            self.assertEqual(cron_def.prompt, "Use post_message to send a summary.")

    def test_create_cron_job_rejects_unknown_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = make_context(make_workspace(Path(tmp)))
            session = type("Session", (), {"shared_context": context})()

            result = asyncio.run(
                ToolRegistry.with_builtins().execute_tool(
                    "create_cron_job",
                    session=session,
                    name="Bad job",
                    description="Should not be created.",
                    agent="missing",
                    schedule="*/5 * * * *",
                    prompt="Do something.",
                )
            )

            payload = json.loads(result)

            self.assertEqual(payload["error"]["code"], "not_found")
            self.assertIn("Agent not found: missing", payload["error"]["message"])
            self.assertFalse((context.config.crons_path / "bad-job").exists())
