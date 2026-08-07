"""Deployment-contract tests for the Docker files.

These are deterministic and do not need Docker. When a Compose binary is present
the resolved-config smoke test also runs; otherwise it is skipped explicitly so
the gap is visible rather than silent.
"""

import os
import shutil
import subprocess
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = REPO_ROOT / "compose.yaml"
DOCKERFILE = REPO_ROOT / "Dockerfile"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"

MCP_PORT = "8765"
INTERNAL_URL = "http://movies-db:8765/mcp"


def _compose() -> dict:
    return yaml.safe_load(COMPOSE_FILE.read_text())


def _compose_binary() -> list[str] | None:
    if shutil.which("docker-compose"):
        return ["docker-compose"]
    if shutil.which("docker"):
        probe = subprocess.run(["docker", "compose", "version"], capture_output=True, text=True)
        if probe.returncode == 0:
            return ["docker", "compose"]
    return None


class ComposeContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.compose = _compose()
        self.services = self.compose["services"]
        self.app = self.services["build-bot"]

    def test_mcp_internal_is_declared_external(self) -> None:
        network = self.compose["networks"]["mcp_internal"]
        self.assertTrue(network["external"])

    def test_app_service_joins_mcp_internal(self) -> None:
        self.assertIn("mcp_internal", self.app["networks"])

    def test_every_agent_capable_service_joins_mcp_internal(self) -> None:
        # Guards against a later service being added without the attachment.
        for name, service in self.services.items():
            self.assertIn("mcp_internal", service.get("networks", []), msg=f"service {name}")

    def test_outbound_network_is_preserved(self) -> None:
        # The user-facing API and channels still need the default bridge.
        self.assertIn("default", self.app["networks"])

    def test_url_uses_internal_service_dns_name(self) -> None:
        self.assertEqual(self.app["environment"]["MOVIES_DB_URL"], INTERNAL_URL)

    def test_token_has_no_committed_value_or_default(self) -> None:
        raw = self.app["environment"]["MOVIES_DB_TOKEN"]
        self.assertTrue(raw.startswith("${MOVIES_DB_TOKEN:?"))
        # `:?` errors when unset; `:-` would silently substitute a default.
        self.assertNotIn(":-", raw)

    def test_mcp_port_is_never_published(self) -> None:
        for port in self.app.get("ports", []):
            self.assertNotIn(MCP_PORT, str(port))

    def test_no_service_publishes_the_mcp_port(self) -> None:
        for name, service in self.services.items():
            for port in service.get("ports", []):
                self.assertNotIn(MCP_PORT, str(port), msg=f"service {name}")

    def test_movies_db_is_not_redefined_here(self) -> None:
        # movies_db is deployed from its own repository; defining it here would
        # risk attaching it to a public network or publishing its port.
        self.assertNotIn("movies-db", self.services)
        self.assertNotIn("movies_db", self.services)

    def test_command_matches_the_supported_server_entrypoint(self) -> None:
        self.assertEqual(
            self.app["command"],
            ["build-bot", "--workspace", "/workspace", "server"],
        )

    def test_workspace_is_mounted_not_baked_in(self) -> None:
        self.assertIn("./default_workspace:/workspace", self.app["volumes"])


class DockerfileContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = DOCKERFILE.read_text()

    def test_runs_as_a_non_root_user(self) -> None:
        self.assertIn("useradd", self.text)
        self.assertIn("USER appuser", self.text.splitlines())

    def test_uses_the_lockfile_reproducibly(self) -> None:
        self.assertIn("uv.lock", self.text)
        self.assertIn("--frozen", self.text)
        self.assertTrue((REPO_ROOT / "uv.lock").is_file())

    def test_lockfile_is_not_gitignored(self) -> None:
        ignored = (REPO_ROOT / ".gitignore").read_text().splitlines()
        self.assertNotIn("uv.lock", [line.strip() for line in ignored])


class BuildContextHygieneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.patterns = [
            line.strip() for line in DOCKERIGNORE.read_text().splitlines() if line.strip() and not line.startswith("#")
        ]

    def test_local_virtualenvs_are_excluded(self) -> None:
        self.assertIn(".venv", self.patterns)

    def test_workspace_runtime_state_and_secrets_are_excluded(self) -> None:
        self.assertIn("default_workspace", self.patterns)
        self.assertIn(".env", self.patterns)
        self.assertIn("client_secret*.json", self.patterns)


class NoCommittedSecretTests(unittest.TestCase):
    def test_deployment_files_contain_no_token_value(self) -> None:
        for path in (COMPOSE_FILE, DOCKERFILE, DOCKERIGNORE):
            text = path.read_text()
            # Only the variable *name* may appear, never an assignment to a value.
            self.assertNotRegex(
                text,
                r"MOVIES_DB_TOKEN\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{8,}",
                msg=str(path),
            )


@unittest.skipIf(
    _compose_binary() is None,
    "Docker Compose is not available on this host; the resolved-config smoke test was not run.",
)
class ComposeResolutionSmokeTests(unittest.TestCase):
    """Runs the real Compose parser. Never builds or starts a container."""

    def _config(self, env: dict[str, str]) -> subprocess.CompletedProcess:
        binary = _compose_binary()
        assert binary is not None
        environment = {k: v for k, v in os.environ.items() if k != "MOVIES_DB_TOKEN"}
        environment.update(env)
        return subprocess.run(
            [*binary, "-f", str(COMPOSE_FILE), "config"],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            env=environment,
        )

    def test_config_succeeds_with_a_dummy_token(self) -> None:
        result = self._config({"MOVIES_DB_TOKEN": "dummy-deployment-token"})
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        resolved = yaml.safe_load(result.stdout)
        app = resolved["services"]["build-bot"]
        self.assertIn("mcp_internal", app["networks"])
        self.assertTrue(resolved["networks"]["mcp_internal"]["external"])
        self.assertEqual(app["environment"]["MOVIES_DB_URL"], INTERNAL_URL)
        for port in app.get("ports", []):
            self.assertNotIn(MCP_PORT, str(port))

    def test_config_fails_clearly_without_a_token(self) -> None:
        result = self._config({})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("MOVIES_DB_TOKEN", result.stderr)


if __name__ == "__main__":
    unittest.main()
