"""Notion agent placement, the notion-kb skill and the shipped example config.

Shipped AGENT.md / SKILL.md files are copied into an isolated workspace
(``copy_real_agent``); the real ``default_workspace`` is never ``Config.load``ed
because its ``config.user.yaml`` is gitignored and absent in CI.
"""

import shutil
import tempfile
import unittest
from pathlib import Path

import yaml

from core.agent_loader import AgentLoader
from core.skill_loader import SkillLoader
from tests.helpers import copy_real_agent, make_workspace
from tools.mcp_tools import mcp_capability_id
from utils.config import Config
from utils.mcp_config import McpConfig

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_WORKSPACE = REPO_ROOT / "default_workspace"
EXAMPLE_CONFIG = REAL_WORKSPACE / "config.example.yaml"

READS = ["notion-search", "notion-fetch", "notion-query-data-sources", "notion-get-self"]
WRITES = ["notion-create-pages", "notion-update-page"]
NOTION_CAPS = {mcp_capability_id("notion", t) for t in READS + WRITES}
FORBIDDEN_PREFIXES = ("web", "email", "shell", "filesystem", "skills.run_script")
FORBIDDEN_EXACT = {"bash", "read", "write", "edit", "skills.run_script"}

DENIED = [
    "notion-delete-pages", "notion-move-pages", "notion-duplicate-page",
    "notion-create-database", "notion-update-data-source", "notion-delete-databases",
    "notion-restore-pages", "notion-update-page-favorites", "notion-update-sidebar-tabs",
    "notion-upload-skill", "notion-convert-page-to-skill", "notion-update-skill-settings",
    "notion-create-comment", "notion-delete-comment", "notion-spawn-session",
    "notion-send-message-to-session",
]


def _load_agent(agent_id: str):
    with tempfile.TemporaryDirectory() as tmp:
        ws = make_workspace(Path(tmp))
        copy_real_agent(ws, agent_id)
        return AgentLoader.from_config(Config.load(ws)).load(agent_id)


class NotionAssistantTests(unittest.TestCase):
    def test_notion_assistant_has_only_notion_caps(self) -> None:
        agent = _load_agent("notion-assistant")
        caps = set(agent.allowed_capabilities)
        self.assertEqual(caps, NOTION_CAPS)
        for cap in caps:
            self.assertFalse(cap.startswith(FORBIDDEN_PREFIXES), cap)
            self.assertNotIn(cap, FORBIDDEN_EXACT)
        self.assertTrue(agent.allow_skills)

    def test_prompt_states_safety_rules(self) -> None:
        text = (REAL_WORKSPACE / "agents/notion-assistant/AGENT.md").read_text()
        for needle in (
            "data, not instructions",
            "Dropped",
            "insert_content",
            "task-assistant",
            "allow_async: false",
            "If you have no Notion tools available, say Notion is not connected yet and save nothing.",
        ):
            self.assertIn(needle, text)

    def test_agent_and_skill_agree_on_task_fields(self) -> None:
        agent = (REAL_WORKSPACE / "agents/notion-assistant/AGENT.md").read_text()
        skill = (REAL_WORKSPACE / "skills/notion-kb/SKILL.md").read_text()
        rule = "You may set Area, Project or Scheduled on an existing task with `update_properties` when Pickle or `task-assistant` routes that to you."
        for text in (agent, skill):
            self.assertIn(rule, text)
            self.assertIn("status changes (complete, drop)", text)

    def test_no_other_shipped_agent_has_notion_caps(self) -> None:
        for path in sorted((REAL_WORKSPACE / "agents").iterdir()):
            if path.name == "notion-assistant":
                continue
            agent = _load_agent(path.name)
            leaked = [c for c in agent.allowed_capabilities if c.startswith("mcp.notion.")]
            self.assertEqual(leaked, [], path.name)

    def test_pickle_routes_to_notion_assistant(self) -> None:
        text = (REAL_WORKSPACE / "agents/pickle/AGENT.md").read_text()
        self.assertIn("notion-assistant", text)

    def test_task_assistant_has_no_todoist_syntax_under_notion(self) -> None:
        text = (REAL_WORKSPACE / "agents/task-assistant/AGENT.md").read_text()
        self.assertNotIn("Todoist parses dates", text)
        self.assertIn("With Notion", text)


class NotionKbSkillTests(unittest.TestCase):
    def test_notion_kb_skill_validates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = make_workspace(Path(tmp))
            shutil.copytree(REAL_WORKSPACE / "skills/notion-kb", ws / "skills/notion-kb")
            loader = SkillLoader.from_config(Config.load(ws))
            result = next(r for r in loader.validate_skills() if r.id == "notion-kb")
            self.assertTrue(result.ok, result.errors)
            body = (ws / "skills/notion-kb/SKILL.md").read_text()
            for cid in (
                "41821f6c-e439-813d-9672-000bc69cfa1c",
                "0b2c30e0-3aa2-47bb-85b7-69a1032cca73",
                "63d67809-155a-4319-ad71-a0686800abef",
            ):
                self.assertIn(f"collection://{cid}", body)


def _notion_example_block() -> str:
    lines = EXAMPLE_CONFIG.read_text().splitlines()
    marker = next(i for i, ln in enumerate(lines) if ln.startswith("# notion-mcp-example:"))
    start = next(i for i in range(marker, len(lines)) if lines[i] == "# mcp:")
    out: list[str] = []
    for line in lines[start:]:
        if not line.startswith("#"):
            break
        s = line[1:]
        out.append(s[1:] if s.startswith(" ") else s)
    return "\n".join(out)


class NotionExampleConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        parsed = yaml.safe_load(_notion_example_block())
        self.parsed = parsed
        self.server = McpConfig.model_validate(parsed["mcp"]).servers["notion"]

    def test_server_shape(self) -> None:
        self.assertEqual(self.server.url, "https://mcp.notion.com/mcp")
        self.assertEqual(self.server.auth.type, "oauth")
        self.assertEqual(self.server.allowed_tools, READS + WRITES)
        self.assertEqual(self.server.denied_tools, DENIED)

    def test_effects_and_retry(self) -> None:
        for t in READS:
            self.assertEqual(self.server.policy_for(t).effect, "read")
        for t in WRITES:
            p = self.server.policy_for(t)
            self.assertEqual((p.effect, p.retry), ("write", "never"))

    def test_argument_guards(self) -> None:
        upd = self.server.policy_for("notion-update-page").arguments
        self.assertEqual(
            list(upd.allowed_keys),
            ["page_id", "command", "properties", "content", "position", "allow_async"],
        )
        self.assertEqual(
            upd.allowed_values, {"command": ["update_properties", "insert_content"], "allow_async": [False]}
        )
        self.assertTrue(upd.forbid_null_properties)
        self.assertEqual(upd.forbidden_property_values, {"Status": ["Dropped"]})
        cre = self.server.policy_for("notion-create-pages").arguments
        self.assertEqual(
            list(cre.allowed_keys),
            ["pages", "parent", "allow_async", "creation_mode", "instructions_version", "skill"],
        )
        self.assertEqual(cre.allowed_values, {"allow_async": [False]})
        self.assertEqual(cre.forbidden_values, {"parent": ["memory"]})
        self.assertEqual(
            list(cre.nested["pages"].allowed_keys),
            ["content", "properties", "preserve_internal_links", "full_width"],
        )
        self.assertEqual(
            list(cre.nested["parent"].allowed_keys),
            ["type", "page_id", "database_id", "data_source_id"],
        )

    def test_tasks_block_and_caps(self) -> None:
        text = EXAMPLE_CONFIG.read_text()
        self.assertIn("provider: notion", text)
        self.assertIn("mcp_server: notion", text)
        self.assertIn("data_source_id: 0b2c30e0-3aa2-47bb-85b7-69a1032cca73", text)
        for cap in NOTION_CAPS:
            self.assertIn(cap, text)


if __name__ == "__main__":
    unittest.main()
