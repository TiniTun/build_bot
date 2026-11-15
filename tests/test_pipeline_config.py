"""Properties of the Drone pipeline and the Pi deploy script that a broken edit would lose.

Nothing here can prove an image tag exists or a Drone secret is set: only a real pipeline run does.
"""

import os
import unittest
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
PIPELINE = ROOT / ".drone.yml"
RETRY_SCRIPT = ROOT / "ci" / "retry.sh"
DEPLOY_SCRIPT = ROOT / "scripts" / "deploy.sh"

GATE_STEPS = ["lint", "test"]
DEPLOY_STEPS = ["deploy"]


def _pipeline() -> dict[str, Any]:
    document = yaml.safe_load(PIPELINE.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def _steps() -> dict[str, dict[str, Any]]:
    return {step["name"]: step for step in _pipeline()["steps"]}


class DronePipelineTest(unittest.TestCase):
    def test_steps_are_gate_then_deploy(self) -> None:
        self.assertEqual(list(_steps()), GATE_STEPS + DEPLOY_STEPS)

    def test_gate_steps_serial_and_unfiltered(self) -> None:
        steps = _steps()
        for name in GATE_STEPS:
            self.assertNotIn("when", steps[name], f"{name} must never be skipped by a filter")
            self.assertEqual(steps[name]["environment"]["UV_PROJECT_ENVIRONMENT"], "/tmp/venv")
            self.assertTrue(steps[name]["image"].endswith("python3.11-trixie-slim"))
        # Serial: concurrent `uv sync` runs make the Pi's DNS drop lookups.
        self.assertEqual(steps["test"]["depends_on"], ["lint"])

    def test_gate_uses_locked_sync_with_retry(self) -> None:
        steps = _steps()
        for name in GATE_STEPS:
            self.assertIn("./ci/retry.sh uv sync --locked", steps[name]["commands"])

    def test_lint_step_runs_ruff_check(self) -> None:
        self.assertIn("uv run ruff check .", _steps()["lint"]["commands"])

    def test_test_step_runs_unittest(self) -> None:
        self.assertIn(
            "uv run python -m unittest discover -s tests -p 'test_*.py'",
            _steps()["test"]["commands"],
        )

    def test_deploy_only_after_gate_on_push_to_master(self) -> None:
        deploy = _steps()["deploy"]
        self.assertEqual(deploy["depends_on"], GATE_STEPS)
        # Both filters: for a pull_request DRONE_BRANCH is the target branch, so `branch: master`
        # alone would deploy unreviewed PR code.
        self.assertEqual(deploy["when"], {"branch": ["master"], "event": ["push"]})

    def test_deploy_ssh_plugin_pinned_by_digest(self) -> None:
        # Drone re-pulls a tag before every step; only a digest pins what runs with the ssh key.
        self.assertIn("@sha256:", _steps()["deploy"]["image"])

    def test_deploy_checks_out_tested_commit(self) -> None:
        settings = _steps()["deploy"]["settings"]
        self.assertEqual(settings["envs"], ["DRONE_COMMIT_SHA"])
        script = settings["script"]
        checkout = script.index('git checkout --quiet --detach "$DRONE_COMMIT_SHA"')
        run = script.index("./scripts/deploy.sh")
        self.assertLess(checkout, run)
        self.assertIn("cd ~/dev_projects/build_bot", script)

    def test_retry_script_executable(self) -> None:
        self.assertTrue(os.access(RETRY_SCRIPT, os.X_OK))


class DeployScriptTest(unittest.TestCase):
    def _script(self) -> str:
        return DEPLOY_SCRIPT.read_text(encoding="utf-8")

    def test_deploy_script_strict_and_executable(self) -> None:
        script = self._script()
        self.assertTrue(script.startswith("#!/bin/bash\n"))
        self.assertIn("set -euo pipefail", script)
        self.assertTrue(os.access(DEPLOY_SCRIPT, os.X_OK))

    def test_deploy_script_frozen_sync_with_retry(self) -> None:
        # --frozen: a lockfile out of step with pyproject.toml fails the deploy instead of re-resolving.
        self.assertIn("ci/retry.sh uv sync --frozen", self._script())

    def test_deploy_script_restarts_and_checks_unit(self) -> None:
        script = self._script()
        for needle in ("sudo systemctl restart", "ActiveState", "NRestarts", "journalctl"):
            self.assertIn(needle, script)

    def test_deploy_script_never_destroys_untracked(self) -> None:
        # The Pi's config.user.yaml, history and secrets are untracked and must survive a deploy.
        script = self._script()
        self.assertNotIn("git clean", script)
        self.assertNotIn("reset --hard", script)


if __name__ == "__main__":
    unittest.main()
