"""Agent-definition tests for the movie-library specialist.

Prompt assertions target durable rules: approved operations, safe mutation
behavior, deletion refusal, and treating server output as data.
"""

import unittest
from pathlib import Path

from core.agent_loader import AgentLoader
from utils.config import Config

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = REPO_ROOT / "default_workspace"

MOVIE_CAPABILITIES = {
    "mcp.movies_db.movies_get_taste_profile",
    "mcp.movies_db.movies_get_viewing",
    "mcp.movies_db.movies_recommend",
    "mcp.movies_db.movies_resolve_title",
    "mcp.movies_db.movies_search_library",
    "mcp.movies_db.movies_search_catalog",
    "mcp.movies_db.movies_log_viewing",
    "mcp.movies_db.movies_update_viewing",
    "mcp.movies_db.movies_record_feedback",
}

WRITE_TOOL_NAMES = [
    "movies_log_viewing",
    "movies_update_viewing",
    "movies_record_feedback",
]


def _loader() -> AgentLoader:
    # Load agent definitions straight from the shipped workspace. The LLM block
    # is never used here, so a placeholder config is enough.
    config = Config.model_validate(
        {
            "workspace": WORKSPACE,
            "llm": {"provider": "openai", "model": "test-model", "api_key": "test"},
            "default_agent": "pickle",
        }
    )
    return AgentLoader.from_config(config)


class MovieAssistantDefinitionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = _loader().load("movie-assistant")

    def test_agent_loads(self) -> None:
        self.assertEqual(self.agent.id, "movie-assistant")
        self.assertTrue(self.agent.description)

    def test_capability_allowlist_is_exactly_the_approved_movie_tools(self) -> None:
        self.assertEqual(set(self.agent.allowed_capabilities), MOVIE_CAPABILITIES)

    def test_no_privileged_or_unrelated_capabilities(self) -> None:
        forbidden_prefixes = (
            "filesystem.",
            "shell.",
            "email.",
            "calendar.",
            "tasks.",
            "memory.",
            "web.",
            "cron.",
            "skills.",
            "agent.",
            "messaging.",
        )
        for capability in self.agent.allowed_capabilities:
            self.assertFalse(
                capability.startswith(forbidden_prefixes),
                msg=f"unexpected capability {capability}",
            )
        self.assertFalse(self.agent.allow_skills)

    def test_no_wildcard_mcp_capability(self) -> None:
        for capability in self.agent.allowed_capabilities:
            self.assertNotIn("*", capability)
            self.assertTrue(capability.startswith("mcp.movies_db."))

    def test_approved_movie_writes_are_available_but_delete_is_not(self) -> None:
        for mutation in WRITE_TOOL_NAMES:
            self.assertIn(f"mcp.movies_db.{mutation}", self.agent.allowed_capabilities)
        self.assertNotIn(
            "mcp.movies_db.movies_delete_viewing",
            self.agent.allowed_capabilities,
        )

    def test_prompt_covers_all_approved_operations(self) -> None:
        body = self.agent.agent_md
        for operation in (
            "movies_resolve_title",
            "movies_search_library",
            "movies_search_catalog",
            "movies_get_taste_profile",
            "movies_recommend",
            "movies_get_viewing",
            "movies_log_viewing",
            "movies_update_viewing",
            "movies_record_feedback",
        ):
            self.assertIn(operation, body)

    def test_prompt_allows_explicit_writes_and_refuses_delete(self) -> None:
        body = self.agent.agent_md.lower()
        for phrase in ("log viewings", "update viewings", "record feedback"):
            self.assertIn(phrase, body)
        self.assertIn("explicit request", body)
        self.assertIn("never delete", body)

    def test_prompt_requires_safe_mutation_handling(self) -> None:
        body = self.agent.agent_md.lower()
        self.assertIn("resolve ambiguity before writing", body)
        self.assertIn("never retry", body)
        self.assertIn("outcome is unknown", body)

    def test_prompt_treats_server_output_as_untrusted_data(self) -> None:
        body = self.agent.agent_md.lower()
        self.assertIn("untrusted", body)
        self.assertIn("not instructions", body)

    def test_prompt_requires_reporting_server_reasons_without_inventing(self) -> None:
        body = self.agent.agent_md.lower()
        self.assertIn("reason", body)
        self.assertIn("do not invent", body)

    def test_prompt_surfaces_degraded_warnings(self) -> None:
        body = self.agent.agent_md.lower()
        self.assertIn("degraded", body)
        self.assertIn("warnings", body)

    def test_prompt_keeps_the_dispatch_output_contract(self) -> None:
        body = self.agent.agent_md
        for field in (
            "summary:",
            "findings:",
            "recommended_actions:",
            "needs_user_confirmation:",
            "memory_updates_suggested:",
        ):
            self.assertIn(field, body)


class PickleRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pickle = _loader().load("pickle")

    def test_pickle_has_no_direct_movie_capabilities(self) -> None:
        for capability in self.pickle.allowed_capabilities:
            self.assertFalse(capability.startswith("mcp."), msg=capability)

    def test_pickle_routes_movie_work_to_the_specialist(self) -> None:
        body = self.pickle.agent_md
        self.assertIn("movie-assistant", body)
        lowered = body.lower()
        for concept in ("library", "taste", "recommend", "title"):
            self.assertIn(concept, lowered)

    def test_pickle_routes_explicit_writes_and_refuses_delete(self) -> None:
        lowered = self.pickle.agent_md.lower()
        for concept in ("log viewings", "update existing viewings", "record ratings"):
            self.assertIn(concept, lowered)
        self.assertIn("movie deletion is unavailable", lowered)
        self.assertIn("do not retry", lowered)

    def test_pickle_remains_the_only_user_facing_agent(self) -> None:
        self.assertIn("only agent that speaks to the user", self.pickle.agent_md)


class WorkspaceCatalogTests(unittest.TestCase):
    def test_agents_md_lists_the_specialist_and_its_boundary(self) -> None:
        text = (WORKSPACE / "AGENTS.md").read_text()
        self.assertIn("movie-assistant", text)
        self.assertIn("log and update viewings", text)
        self.assertIn("Never deletes", text)
        self.assertIn("quarantined", text)

    def test_existing_specialists_keep_their_boundaries(self) -> None:
        loader = _loader()
        calendar = loader.load("calendar-assistant")
        self.assertIn("calendar.search", calendar.allowed_capabilities)
        for capability in calendar.allowed_capabilities:
            self.assertFalse(capability.startswith("mcp."))

    def test_subagent_dispatch_discovers_the_new_specialist(self) -> None:
        agents = {a.id for a in _loader().discover_agents()}
        self.assertIn("movie-assistant", agents)
        self.assertIn("pickle", agents)


if __name__ == "__main__":
    unittest.main()
