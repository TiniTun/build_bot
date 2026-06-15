import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from tests.helpers import make_context, make_workspace
from tools.base import ToolErrorCode
from tools.memory_tools import build_memory_capabilities


def _session(context):
    return SimpleNamespace(shared_context=context)


def _tool(config, cap_id):
    return next(t for cap, t in build_memory_capabilities(config) if cap.id == cap_id)


class MemoryToolsTests(unittest.TestCase):
    def _setup(self, tmp: str):
        workspace = make_workspace(Path(tmp))
        context = make_context(workspace)
        return workspace, context

    def test_capability_ids_and_tool_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, context = self._setup(tmp)
            pairs = build_memory_capabilities(context.config)
            mapping = {cap.id: cap.tool_name for cap, _ in pairs}
            self.assertEqual(
                mapping,
                {
                    "memory.search": "memory_search",
                    "memory.store_fact": "memory_store_fact",
                    "memory.store_preference": "memory_store_preference",
                    "memory.update_user_profile": "memory_update_user_profile",
                    "memory.update_assistant_preferences": "memory_update_assistant_preferences",
                    "memory.store_project_context": "memory_store_project_context",
                    "memory.store_decision": "memory_store_decision",
                    "memory.append_daily_note": "memory_append_daily_note",
                },
            )

    def test_store_fact_durable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, context = self._setup(tmp)
            tool = _tool(context.config, "memory.store_fact")
            out = asyncio.run(
                tool.execute(session=_session(context), content="the moon orbits earth")
            )
            self.assertIn("stored", out.lower())
            fact_file = context.config.memories_path / "facts" / "facts.md"
            self.assertTrue(fact_file.is_file())
            self.assertIn("the moon orbits earth", fact_file.read_text())

    def test_store_preference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, context = self._setup(tmp)
            tool = _tool(context.config, "memory.store_preference")
            asyncio.run(
                tool.execute(session=_session(context), content="metric units")
            )
            pref = context.config.memories_path / "preferences" / "preferences.md"
            self.assertIn("metric units", pref.read_text())

    def test_search_returns_snippets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, context = self._setup(tmp)
            store_fact = _tool(context.config, "memory.store_fact")
            asyncio.run(
                store_fact.execute(
                    session=_session(context), content="the capital of Japan is Tokyo"
                )
            )
            search = _tool(context.config, "memory.search")
            out = asyncio.run(
                search.execute(session=_session(context), query="Tokyo")
            )
            self.assertIn("facts", out)
            self.assertIn("Tokyo", out)

    def test_search_empty_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, context = self._setup(tmp)
            search = _tool(context.config, "memory.search")
            out = asyncio.run(
                search.execute(session=_session(context), query="nothinghere")
            )
            self.assertEqual(out, "No matching memories found.")

    def test_project_context_requires_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, context = self._setup(tmp)
            tool = _tool(context.config, "memory.store_project_context")
            out = asyncio.run(
                tool.execute(
                    session=_session(context), project="   ", content="ctx"
                )
            )
            payload = json.loads(out)
            self.assertFalse(payload["ok"])
            self.assertEqual(
                payload["error"]["code"], ToolErrorCode.INVALID_ARGS.value
            )

    def test_project_context_traversal_invalid_args(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, context = self._setup(tmp)
            tool = _tool(context.config, "memory.store_project_context")
            out = asyncio.run(
                tool.execute(
                    session=_session(context), project="../escape", content="ctx"
                )
            )
            payload = json.loads(out)
            self.assertEqual(
                payload["error"]["code"], ToolErrorCode.INVALID_ARGS.value
            )

    def test_store_decision_with_rationale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, context = self._setup(tmp)
            tool = _tool(context.config, "memory.store_decision")
            asyncio.run(
                tool.execute(
                    session=_session(context),
                    content="use markdown store",
                    rationale="inspectable and simple",
                )
            )
            dec = context.config.memories_path / "decisions" / "decisions.md"
            text = dec.read_text()
            self.assertIn("use markdown store", text)
            self.assertIn("inspectable and simple", text)

    def test_store_fact_blank_invalid_args(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, context = self._setup(tmp)
            tool = _tool(context.config, "memory.store_fact")
            out = asyncio.run(
                tool.execute(session=_session(context), content="   ")
            )
            payload = json.loads(out)
            self.assertEqual(
                payload["error"]["code"], ToolErrorCode.INVALID_ARGS.value
            )


if __name__ == "__main__":
    unittest.main()
