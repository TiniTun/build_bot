import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from tests.helpers import make_context, make_workspace, write_definition, write_file
from tools.capabilities import ToolPolicy
from tools.capability_catalog import build_capability_registry
from tools.skill_run_script_tool import create_skill_run_script_tool
from utils.config import Config, ToolsConfig


def _write_weather_skill(workspace: Path, *, script_name: str = "scripts/echo.py") -> None:
    write_definition(
        workspace / "skills",
        "weather-information",
        "SKILL.md",
        {
            "name": "weather-information",
            "description": "Weather skill for tests.",
            "when_to_use": ["The user asks about weather"],
            "required_tools": ["skill_run_script"],
            "scripts": [
                {
                    "path": script_name,
                    "description": "Echo args.",
                    "when_to_run": "On weather requests.",
                }
            ],
        },
        "Weather skill body.",
    )
    write_file(
        workspace / "skills" / "weather-information" / script_name,
        "import sys\nprint('ran ' + ' '.join(sys.argv[1:]))\n",
    )


def _agent_def(context, *, allow_skills: bool = True):
    agent_def = context.agent_loader.load("pickle")
    return agent_def.model_copy(update={"allow_skills": allow_skills})


def _session(context):
    return SimpleNamespace(shared_context=context)


def _run(coro):
    return asyncio.run(coro)


class SkillRunScriptTests(unittest.TestCase):
    def test_runs_declared_script_with_args(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            _write_weather_skill(workspace)
            context = make_context(workspace)
            runner = create_skill_run_script_tool(context.skill_loader)

            result = _run(
                runner.execute(
                    _session(context),
                    skill_name="weather-information",
                    script="scripts/echo.py",
                    args=["Brisbane"],
                )
            )
            self.assertIn("ran Brisbane", result)

    def test_rejects_undeclared_script(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            _write_weather_skill(workspace)
            # An on-disk but undeclared script must still be refused.
            write_file(
                workspace / "skills" / "weather-information" / "scripts" / "secret.py",
                "print('should not run')\n",
            )
            context = make_context(workspace)
            runner = create_skill_run_script_tool(context.skill_loader)

            result = _run(
                runner.execute(
                    _session(context),
                    skill_name="weather-information",
                    script="scripts/secret.py",
                )
            )
            self.assertIn("permission_denied", result)
            self.assertIn("not declared", result)

    def test_rejects_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            _write_weather_skill(workspace)
            context = make_context(workspace)
            runner = create_skill_run_script_tool(context.skill_loader)

            result = _run(
                runner.execute(
                    _session(context),
                    skill_name="weather-information",
                    script="../../etc/passwd",
                )
            )
            self.assertIn("permission_denied", result)

    def test_capability_filtered_by_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            _write_weather_skill(workspace)
            context = make_context(workspace)
            registry = build_capability_registry(
                _agent_def(context), context, include_post_message=False
            )
            ids = {c.id for c in registry.capabilities()}
            self.assertIn("skills.run_script", ids)

            # Excluded when a policy omits it.
            policy = ToolPolicy.from_config(
                Config.load(workspace).model_copy(
                    update={"tools": ToolsConfig(enabled_capabilities=["filesystem.read"])}
                )
            )
            tools = registry.build_tool_registry(policy)
            names = {s["function"]["name"] for s in tools.get_tool_schemas()}
            self.assertNotIn("skill_run_script", names)

            # Included when the policy enables it.
            policy = ToolPolicy.from_config(
                Config.load(workspace).model_copy(
                    update={"tools": ToolsConfig(enabled_capabilities=["skills.run_script"])}
                )
            )
            tools = registry.build_tool_registry(policy)
            names = {s["function"]["name"] for s in tools.get_tool_schemas()}
            self.assertIn("skill_run_script", names)


if __name__ == "__main__":
    unittest.main()
