import tempfile
import unittest
from pathlib import Path

from core.agent_loader import AgentLoader
from tests.helpers import make_workspace, write_definition
from utils.config import Config


class AgentLoaderAllowedCapabilitiesTests(unittest.TestCase):
    def test_parses_allowed_capabilities_from_frontmatter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            write_definition(
                workspace / "agents",
                "scoped",
                "AGENT.md",
                {
                    "name": "Scoped",
                    "description": "Narrowed agent",
                    "allowed_capabilities": [
                        "memory.search",
                        "agent.subagent_dispatch",
                    ],
                },
                "You are Scoped.",
            )
            loader = AgentLoader.from_config(Config.load(workspace))
            agent_def = loader.load("scoped")
            self.assertEqual(
                agent_def.allowed_capabilities,
                ["memory.search", "agent.subagent_dispatch"],
            )

    def test_missing_allowed_capabilities_is_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            loader = AgentLoader.from_config(Config.load(workspace))
            agent_def = loader.load("pickle")
            self.assertIsNone(agent_def.allowed_capabilities)


if __name__ == "__main__":
    unittest.main()
