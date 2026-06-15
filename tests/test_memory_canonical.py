"""Tests for the canonical/raw memory redesign.

Covers canonical profile + assistant preference updates, synonym-aware search
(including Russian), canonical-over-raw ranking, backward compatibility with the
old layout, and the non-destructive migration helper.
"""

import tempfile
import unittest
from pathlib import Path

from core.memory_store import MemoryStore


def _store(tmp: str) -> MemoryStore:
    return MemoryStore(Path(tmp) / "memories")


class CanonicalWriteTests(unittest.TestCase):
    def test_profile_update_writes_canonical_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            path = store.update_user_profile("Home city", "Brisbane, Australia", section="Location")
            self.assertEqual(path, store.root / "profile" / "user.md")
            text = path.read_text()
            self.assertIn("# User Profile", text)
            self.assertIn("## Location", text)
            self.assertIn("- Home city: Brisbane, Australia", text)
            # Plain Markdown, never XML-ish.
            self.assertNotIn("<data>", text)
            self.assertNotIn("<fact>", text)

    def test_profile_update_replaces_existing_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            store.update_user_profile("Home city", "Madrid", section="Location")
            store.update_user_profile("Home city", "Brisbane, Australia", section="Location")
            text = (store.root / "profile" / "user.md").read_text()
            self.assertEqual(text.count("- Home city:"), 1)
            self.assertIn("Brisbane, Australia", text)
            self.assertNotIn("Madrid", text)

    def test_assistant_preferences_dedupe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            store.update_assistant_preference("Prefer concise answers.", section="Communication")
            store.update_assistant_preference("Prefer concise answers.", section="Communication")
            text = (store.root / "preferences" / "assistant.md").read_text()
            self.assertEqual(text.count("Prefer concise answers."), 1)
            self.assertIn("## Communication", text)

    def test_fact_written_as_markdown_bullet_under_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            path = store.store_fact("User said they live in Brisbane, Australia.")
            text = path.read_text()
            # Timestamp heading + bullet, not XML.
            self.assertRegex(text, r"## \d{4}-\d{2}-\d{2}T")
            self.assertIn("- User said they live in Brisbane, Australia.", text)
            self.assertNotIn("<data>", text)

    def test_daily_note_goes_to_episodes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            path = store.append_daily_note("Tested cron reminders.")
            self.assertEqual(path.parent, store.root / "episodes")
            self.assertIn("- Tested cron reminders.", path.read_text())


class CanonicalSearchTests(unittest.TestCase):
    def _seed(self, store: MemoryStore) -> None:
        store.update_user_profile("Home city", "Brisbane, Australia", section="Location")
        store.update_user_profile("Default weather location", "Brisbane, Australia", section="Location")
        store.update_user_profile("Timezone", "Australia/Brisbane", section="Location")
        store.store_fact("User said they live in Brisbane, Australia.")

    def test_weather_location_finds_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            self._seed(store)
            hits = store.search("weather location")
            self.assertTrue(hits)
            self.assertTrue(
                any(h.path == "profile/user.md" for h in hits),
                [h.path for h in hits],
            )

    def test_russian_weather_query_finds_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            self._seed(store)
            hits = store.search("погода завтра")
            self.assertTrue(hits)
            self.assertTrue(
                any(h.path == "profile/user.md" for h in hits),
                [h.path for h in hits],
            )

    def test_canonical_profile_ranks_before_raw_facts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            self._seed(store)
            hits = store.search("Brisbane")
            self.assertTrue(hits)
            # Both profile and facts mention Brisbane; canonical wins.
            paths = [h.path for h in hits]
            self.assertIn("profile/user.md", paths)
            self.assertIn("facts/facts.md", paths)
            self.assertLess(paths.index("profile/user.md"), paths.index("facts/facts.md"))
            self.assertTrue(hits[0].canonical)


class BackwardCompatTests(unittest.TestCase):
    def test_old_topics_user_profile_remains_searchable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            old = store.root / "topics" / "user-profile.md"
            old.parent.mkdir(parents=True, exist_ok=True)
            old.write_text("# User Profile\n\n- Name: Egor\n- Lives in Brisbane, Australia\n")
            hits = store.search("Brisbane")
            self.assertTrue(
                any(h.path == "topics/user-profile.md" for h in hits),
                [h.path for h in hits],
            )

    def test_legacy_daily_notes_remain_searchable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            old = store.root / "daily-notes" / "2026-01-01.md"
            old.parent.mkdir(parents=True, exist_ok=True)
            old.write_text("## 2026-01-01T00:00:00Z\n\n- Reviewed deployment plan.\n")
            hits = store.search("deployment plan")
            self.assertTrue(
                any(h.path == "daily-notes/2026-01-01.md" for h in hits),
                [h.path for h in hits],
            )


class MigrationTests(unittest.TestCase):
    def test_migrate_copies_old_profile_and_is_non_destructive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            old = store.root / "topics" / "user-profile.md"
            old.parent.mkdir(parents=True, exist_ok=True)
            old.write_text("# User Profile\n\n- Name: Egor\n- Lives in Brisbane, Australia\n")

            actions = store.migrate()

            profile = (store.root / "profile" / "user.md").read_text()
            self.assertIn("- Name: Egor", profile)
            self.assertIn("- Home city: Brisbane, Australia", profile)
            # Canonical files created.
            self.assertTrue((store.root / "preferences" / "assistant.md").is_file())
            self.assertTrue((store.root / "facts" / "facts.md").is_file())
            # Old file untouched.
            self.assertTrue(old.is_file())
            self.assertIn("Lives in Brisbane", old.read_text())
            self.assertTrue(actions)

    def test_migrate_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            old = store.root / "topics" / "user-profile.md"
            old.parent.mkdir(parents=True, exist_ok=True)
            old.write_text("# User Profile\n\n- Name: Egor\n")
            store.migrate()
            store.migrate()
            profile = (store.root / "profile" / "user.md").read_text()
            self.assertEqual(profile.count("- Name: Egor"), 1)


if __name__ == "__main__":
    unittest.main()
