"""Validation tests for the optional ``mcp:`` configuration block."""

import shutil
import tempfile
import unittest
from pathlib import Path

import yaml
from pydantic import ValidationError

from tests.helpers import make_workspace, write_file
from utils.config import Config
from utils.mcp_config import McpConfig, McpServerConfig, McpToolPolicy

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_CONFIG = REPO_ROOT / "default_workspace" / "config.example.yaml"


def _server(**overrides) -> dict:
    base = {
        "url": "http://127.0.0.1:8765/mcp",
        "allowed_tools": ["movies_search_library"],
    }
    base.update(overrides)
    return base


def _write_mcp_config(workspace: Path, mcp_block: dict) -> Config:
    config_path = workspace / "config.user.yaml"
    data = yaml.safe_load(config_path.read_text())
    data["mcp"] = mcp_block
    write_file(config_path, yaml.safe_dump(data, sort_keys=False))
    return Config.load(workspace)


class McpConfigAbsenceTests(unittest.TestCase):
    def test_no_mcp_block_leaves_config_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            config = Config.load(workspace)
            self.assertIsNone(config.mcp)

    def test_empty_servers_mapping_is_valid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            config = _write_mcp_config(workspace, {"servers": {}})
            self.assertIsNotNone(config.mcp)
            self.assertEqual(config.mcp.servers, {})
            self.assertEqual(config.mcp.enabled_servers(), {})


class McpServerValidationTests(unittest.TestCase):
    def test_one_valid_server_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            config = _write_mcp_config(
                workspace,
                {"servers": {"movies_db": _server(token_env="MOVIES_DB_TOKEN")}},
            )
            server = config.mcp.servers["movies_db"]
            self.assertEqual(server.transport, "streamable_http")
            self.assertEqual(server.token_env, "MOVIES_DB_TOKEN")
            self.assertTrue(server.enabled)
            self.assertFalse(server.required)

    def test_multiple_servers_are_independent(self) -> None:
        cfg = McpConfig.model_validate(
            {
                "servers": {
                    "movies_db": _server(),
                    "notes_db": _server(
                        url="https://notes.internal/mcp",
                        enabled=False,
                        required=True,
                    ),
                }
            }
        )
        self.assertEqual(set(cfg.servers), {"movies_db", "notes_db"})
        self.assertEqual(set(cfg.enabled_servers()), {"movies_db"})
        self.assertTrue(cfg.servers["notes_db"].required)

    def test_invalid_server_id_rejected(self) -> None:
        for bad_id in ("Movies_DB", "movies-db", "1movies", "movies db", ""):
            with self.subTest(bad_id=bad_id):
                with self.assertRaises(ValidationError):
                    McpConfig.model_validate({"servers": {bad_id: _server()}})

    def test_non_http_url_rejected(self) -> None:
        for bad_url in ("ftp://host/mcp", "movies-db:8765/mcp", "ws://host/mcp"):
            with self.subTest(bad_url=bad_url):
                with self.assertRaises(ValidationError):
                    McpServerConfig.model_validate(_server(url=bad_url))

    def test_unknown_transport_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            McpServerConfig.model_validate(_server(transport="stdio"))

    def test_non_positive_or_oversized_timeout_rejected(self) -> None:
        for field, value in (
            ("connect_timeout_seconds", 0),
            ("read_timeout_seconds", -1),
            ("discovery_timeout_seconds", 100_000),
            ("health_timeout_seconds", 0),
        ):
            with self.subTest(field=field, value=value):
                with self.assertRaises(ValidationError):
                    McpServerConfig.model_validate(_server(**{field: value}))

    def test_invalid_concurrency_and_result_size_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            McpServerConfig.model_validate(_server(max_concurrent_calls=0))
        with self.assertRaises(ValidationError):
            McpServerConfig.model_validate(_server(max_concurrent_calls=10_000))
        with self.assertRaises(ValidationError):
            McpServerConfig.model_validate(_server(max_result_chars=0))

    def test_literal_secret_fields_rejected_with_actionable_message(self) -> None:
        for key in ("token", "api_key", "bearer_token", "authorization", "secret"):
            with self.subTest(key=key):
                with self.assertRaises(ValidationError) as ctx:
                    McpServerConfig.model_validate(_server(**{key: "s3cret"}))
                self.assertIn("token_env", str(ctx.exception))

    def test_unknown_field_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            McpServerConfig.model_validate(_server(oauth_client_id="x"))

    def test_token_env_holds_a_name_not_a_value(self) -> None:
        server = McpServerConfig.model_validate(_server(token_env="MOVIES_DB_TOKEN"))
        self.assertEqual(server.token_env, "MOVIES_DB_TOKEN")
        # Something that is obviously a value, not an env var name, is rejected.
        with self.assertRaises(ValidationError):
            McpServerConfig.model_validate(_server(token_env="Bearer abc.def-ghi"))

    def test_url_and_url_env_are_mutually_exclusive(self) -> None:
        with self.assertRaises(ValidationError):
            McpServerConfig.model_validate(
                {
                    "url": "http://127.0.0.1:8765/mcp",
                    "url_env": "MOVIES_DB_URL",
                    "allowed_tools": ["a"],
                }
            )
        with self.assertRaises(ValidationError):
            McpServerConfig.model_validate({"allowed_tools": ["a"]})

    def test_url_env_reference_is_retained_unresolved(self) -> None:
        server = McpServerConfig.model_validate({"url_env": "MOVIES_DB_URL", "allowed_tools": ["a"]})
        self.assertIsNone(server.url)
        self.assertEqual(server.url_env, "MOVIES_DB_URL")

    def test_health_path_must_be_absolute_and_exclusive(self) -> None:
        self.assertEqual(
            McpServerConfig.model_validate(_server(health_path="/health")).health_path,
            "/health",
        )
        with self.assertRaises(ValidationError):
            McpServerConfig.model_validate(_server(health_path="health"))
        with self.assertRaises(ValidationError):
            McpServerConfig.model_validate(_server(health_path="/health", health_url="http://h/health"))

    def test_duplicate_tool_names_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            McpServerConfig.model_validate(_server(allowed_tools=["a", "a"]))
        with self.assertRaises(ValidationError):
            McpServerConfig.model_validate(_server(allowed_tools=["a", "  "]))

    def test_contradictory_allow_and_deny_rejected(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            McpServerConfig.model_validate(_server(allowed_tools=["a", "b"], denied_tools=["b"]))
        self.assertIn("both allowed_tools and denied_tools", str(ctx.exception))

    def test_orphan_tool_policy_rejected(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            McpServerConfig.model_validate(_server(allowed_tools=["a"], tool_policies={"b": {"effect": "read"}}))
        self.assertIn("no matching allowed_tools", str(ctx.exception))


class McpToolPolicyTests(unittest.TestCase):
    def test_defaults_are_safe(self) -> None:
        policy = McpToolPolicy()
        self.assertEqual(policy.effect, "read")
        self.assertEqual(policy.confirmation, "none")
        self.assertEqual(policy.retry, "never")

    def test_mutations_cannot_opt_into_retry(self) -> None:
        for effect in ("write", "destructive"):
            with self.subTest(effect=effect):
                with self.assertRaises(ValidationError):
                    McpToolPolicy.model_validate({"effect": effect, "retry": "safe"})

    def test_unknown_enum_values_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            McpToolPolicy.model_validate({"effect": "delete"})
        with self.assertRaises(ValidationError):
            McpToolPolicy.model_validate({"confirmation": "maybe"})

    def test_lookup_helpers_are_fail_closed(self) -> None:
        server = McpServerConfig.model_validate(
            _server(
                allowed_tools=["movies_search_library", "movies_recommend"],
                denied_tools=["movies_delete_viewing"],
                tool_policies={"movies_recommend": {"effect": "read", "confirmation": "none"}},
            )
        )
        self.assertTrue(server.is_tool_allowed("movies_search_library"))
        self.assertFalse(server.is_tool_allowed("movies_delete_viewing"))
        # An unknown/newly discovered tool is quarantined by default.
        self.assertFalse(server.is_tool_allowed("movies_brand_new_tool"))
        self.assertEqual(server.policy_for("movies_search_library").effect, "read")


class ExampleConfigTests(unittest.TestCase):
    def test_example_config_is_loadable_as_a_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            shutil.copy(EXAMPLE_CONFIG, workspace / "config.user.yaml")
            config = Config.load(workspace)
            self.assertEqual(config.default_agent, "pickle")
            # The MCP block ships commented out, so behavior stays unchanged.
            self.assertIsNone(config.mcp)

    def test_example_config_contains_no_literal_secret_for_mcp(self) -> None:
        text = EXAMPLE_CONFIG.read_text()
        self.assertIn("token_env: MOVIES_DB_TOKEN", text)
        self.assertIn("url_env: MOVIES_DB_URL", text)
        self.assertNotIn("MOVIES_DB_TOKEN=", text)

    def test_documented_mcp_block_validates_once_uncommented(self) -> None:
        """The commented movies_db example must be valid config, not prose."""
        block = _uncomment_mcp_block(EXAMPLE_CONFIG.read_text())
        parsed = yaml.safe_load(block)
        cfg = McpConfig.model_validate(parsed["mcp"])
        server = cfg.servers["movies_db"]
        self.assertEqual(server.url_env, "MOVIES_DB_URL")
        self.assertEqual(server.token_env, "MOVIES_DB_TOKEN")
        self.assertEqual(server.health_path, "/health")
        self.assertEqual(
            server.allowed_tools,
            [
                "movies_get_taste_profile",
                "movies_get_viewing",
                "movies_recommend",
                "movies_resolve_title",
                "movies_search_library",
                "movies_search_catalog",
                "movies_log_viewing",
                "movies_update_viewing",
                "movies_record_feedback",
            ],
        )
        self.assertEqual(server.denied_tools, ["movies_delete_viewing"])
        for name in (
            "movies_log_viewing",
            "movies_update_viewing",
            "movies_record_feedback",
        ):
            policy = server.policy_for(name)
            self.assertEqual(policy.effect, "write")
            self.assertEqual(policy.confirmation, "none")
            self.assertEqual(policy.retry, "never")
        self.assertFalse(server.use_server_instructions)


def _uncomment_mcp_block(text: str) -> str:
    """Extract the commented ``# mcp:`` example and strip one comment level."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("# mcp:"))
    out: list[str] = []
    for line in lines[start:]:
        if not line.startswith("#"):
            break
        stripped = line[1:]
        stripped = stripped[1:] if stripped.startswith(" ") else stripped
        # Skip nested "commented out" examples inside the block.
        if stripped.lstrip().startswith("#"):
            continue
        out.append(stripped)
    return "\n".join(out)


if __name__ == "__main__":
    unittest.main()
