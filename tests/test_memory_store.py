import tempfile
import unittest
from pathlib import Path

from core.memory_store import CATEGORIES, MemoryStore, SearchHit
from tests.helpers import make_workspace
from utils.config import Config


class MemoryStoreTests(unittest.TestCase):
    def _store(self, tmp: str) -> MemoryStore:
        workspace = make_workspace(Path(tmp))
        config = Config.load(workspace)
        return MemoryStore(config)

    def test_creates_category_directories_under_memories_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            store.store_fact("the sky is blue")
            store.store_preference("dark mode")
            store.store_project_context("build-bot", "uses litellm")
            store.store_decision("use sqlite", "simplest option")
            store.append_daily_note("shipped memory store")
            for category in CATEGORIES:
                self.assertTrue(
                    (store.root / category).is_dir(),
                    f"missing category dir {category}",
                )

    def test_constructs_from_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "mem"
            store = MemoryStore(root)
            path = store.store_fact("hi")
            self.assertEqual(store.root, root)
            self.assertTrue(path.is_file())

    def test_rejects_project_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            with self.assertRaises(ValueError):
                store.store_project_context("../escape", "bad")
            with self.assertRaises(ValueError):
                store.store_project_context("a/b", "bad")
            # nothing escaped the root
            escaped = (store.root.parent / "escape.md")
            self.assertFalse(escaped.exists())

    def test_normalizes_project_id_to_safe_filename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            path = store.store_project_context("My Cool Project!!", "ctx")
            self.assertEqual(path.name, "my-cool-project.md")
            self.assertTrue(
                path.resolve().is_relative_to(store.root.resolve())
            )

    def test_rejects_blank_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            with self.assertRaises(ValueError):
                store.store_fact("   ")
            with self.assertRaises(ValueError):
                store.store_preference("")

    def test_search_returns_hits_with_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            store.store_fact("Paris is the capital of France")
            store.store_preference("prefers tea over coffee")
            hits = store.search("CAPITAL")
            self.assertTrue(hits)
            hit = hits[0]
            self.assertIsInstance(hit, SearchHit)
            self.assertEqual(hit.category, "facts")
            self.assertEqual(hit.path, "facts/facts.md")
            self.assertIn("capital", hit.snippet.lower())

    def test_search_matches_multi_word_queries_across_project_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            store.store_project_context(
                "build-bot",
                "User preference for build_bot: prefer to write a short technical specification first, then write code.",
            )

            hits = store.search("durable preferences related to build_bot")

            self.assertTrue(hits)
            self.assertEqual(hits[0].category, "projects")
            self.assertEqual(hits[0].path, "projects/build-bot.md")
            self.assertIn("short technical specification", hits[0].snippet)

    def test_search_blank_query_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            with self.assertRaises(ValueError):
                store.search("  ")

    def test_search_respects_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            for i in range(5):
                store.store_fact(f"alpha entry {i}")
            hits = store.search("alpha", limit=2)
            self.assertEqual(len(hits), 2)

    def test_decision_records_rationale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            path = store.store_decision("adopt uv", "faster installs")
            text = path.read_text()
            self.assertIn("adopt uv", text)
            self.assertIn("Rationale: faster installs", text)


if __name__ == "__main__":
    unittest.main()
