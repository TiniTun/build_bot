"""End-to-end tests for the approved ``movies_db`` profile.

The profile under test is the one shipped in ``config.example.yaml`` — the tests
parse it rather than restating it, so documentation and behavior cannot drift.
Everything runs against a fake Streamable HTTP server; no test contacts the real
``movies_db``, TMDB, or any network service.
"""

import json
import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from provider.mcp.client import StreamableHttpMcpClient
from provider.mcp.hub import McpHub, McpServerState
from tests.helpers import make_context, make_workspace
from tests.mcp_fakes import FakeMcpHttpServer, text_result, tool_spec
from tests.test_mcp_config import EXAMPLE_CONFIG, _uncomment_mcp_block
from tools.mcp_tools import build_mcp_capabilities
from utils.mcp_config import McpConfig

import yaml

TOKEN = "movies-db-secret-token-9f3a"
URL = "http://movies-db:8765/mcp"

READ_TOOLS = [
    "movies_get_taste_profile",
    "movies_get_viewing",
    "movies_recommend",
    "movies_resolve_title",
    "movies_search_library",
    "movies_search_catalog",
]

WRITE_TOOLS = [
    "movies_log_viewing",
    "movies_update_viewing",
    "movies_record_feedback",
]

DENIED_TOOLS = [
    "movies_delete_viewing",
    "movies_sync_library",
]

APPROVED_TOOLS = READ_TOOLS + WRITE_TOOLS


def movies_db_profile() -> McpConfig:
    """The committed ``movies_db`` profile, read from the example config."""
    parsed = yaml.safe_load(_uncomment_mcp_block(EXAMPLE_CONFIG.read_text()))
    return McpConfig.model_validate(parsed["mcp"])


def _all_tools(extra: list[str] | None = None) -> list[dict]:
    names = APPROVED_TOOLS + DENIED_TOOLS + (extra or [])
    return [tool_spec(name, properties={"query": {"type": "string"}}) for name in names]


class MoviesDbProfileTestCase(unittest.IsolatedAsyncioTestCase):
    """Shared wiring: real client + real hub + fake HTTP server."""

    async def _hub(
        self,
        workspace: Path,
        server: FakeMcpHttpServer,
        *,
        env: dict[str, str] | None = None,
    ):
        context = make_context(workspace)
        context.config.mcp = movies_db_profile()
        resolved_env = env if env is not None else {"MOVIES_DB_URL": URL, "MOVIES_DB_TOKEN": TOKEN}

        def factory(server_id, config):
            return StreamableHttpMcpClient(server_id, config, env=resolved_env, transport_factory=server.transport)

        context.mcp_hub = McpHub(context.config, client_factory=factory)
        await context.mcp_hub.start()
        return context


class ProfileShapeTests(unittest.TestCase):
    def test_profile_is_optional_and_allows_only_approved_operations(self) -> None:
        server = movies_db_profile().servers["movies_db"]
        self.assertTrue(server.enabled)
        self.assertFalse(server.required)
        self.assertEqual(server.transport, "streamable_http")
        self.assertEqual(sorted(server.allowed_tools), sorted(APPROVED_TOOLS))
        for name in READ_TOOLS:
            policy = server.policy_for(name)
            self.assertEqual(policy.effect, "read")
            self.assertEqual(policy.confirmation, "none")
            self.assertEqual(policy.retry, "never")
        for name in WRITE_TOOLS:
            policy = server.policy_for(name)
            self.assertEqual(policy.effect, "write")
            self.assertEqual(policy.confirmation, "none")
            self.assertEqual(policy.retry, "never")

    def test_profile_reads_endpoint_and_token_from_exact_env_vars(self) -> None:
        server = movies_db_profile().servers["movies_db"]
        self.assertEqual(server.url_env, "MOVIES_DB_URL")
        self.assertEqual(server.token_env, "MOVIES_DB_TOKEN")
        self.assertIsNone(server.url)

    def test_destructive_and_unknown_operations_are_denied_or_absent(self) -> None:
        server = movies_db_profile().servers["movies_db"]
        for name in DENIED_TOOLS:
            self.assertFalse(server.is_tool_allowed(name), msg=name)


class DiscoveryTests(MoviesDbProfileTestCase):
    async def test_health_runs_before_initialize_and_list_tools(self) -> None:
        server = FakeMcpHttpServer(tools=_all_tools(), require_token=TOKEN)
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            context = await self._hub(workspace, server)

            kinds = ["health" if r.url.endswith("/health") else (r.rpc_method or r.method) for r in server.requests]
            self.assertEqual(kinds[0], "health")
            self.assertLess(kinds.index("health"), kinds.index("initialize"))
            self.assertLess(kinds.index("initialize"), kinds.index("tools/list"))
            await context.mcp_hub.stop()

    async def test_exactly_nine_tools_are_exposed(self) -> None:
        server = FakeMcpHttpServer(tools=_all_tools(), require_token=TOKEN)
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            context = await self._hub(workspace, server)

            snapshot = context.mcp_hub.snapshot("movies_db")
            self.assertEqual(len(snapshot.tools), 11)

            exposed = {cap.id for cap, _ in build_mcp_capabilities(context)}
            self.assertEqual(exposed, {f"mcp.movies_db.{name}" for name in APPROVED_TOOLS})
            await context.mcp_hub.stop()

    async def test_destructive_and_unlisted_tools_stay_quarantined(self) -> None:
        server = FakeMcpHttpServer(tools=_all_tools(), require_token=TOKEN)
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            context = await self._hub(workspace, server)

            status = context.mcp_hub.status("movies_db")
            self.assertEqual(status.discovered_tools, 11)
            self.assertEqual(status.enabled_tools, 9)
            self.assertEqual(status.quarantined_tools, 2)

            names = {cap.tool_name for cap, _ in build_mcp_capabilities(context)}
            for denied in DENIED_TOOLS:
                self.assertNotIn(f"mcp_movies_db_{denied}", names)
            await context.mcp_hub.stop()

    async def test_unknown_future_tool_stays_quarantined(self) -> None:
        server = FakeMcpHttpServer(tools=_all_tools(extra=["movies_wipe_everything"]), require_token=TOKEN)
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            context = await self._hub(workspace, server)
            exposed = {cap.id for cap, _ in build_mcp_capabilities(context)}
            self.assertEqual(exposed, {f"mcp.movies_db.{name}" for name in APPROVED_TOOLS})
            self.assertFalse(context.mcp_hub.is_tool_available("movies_db", "movies_wipe_everything"))
            await context.mcp_hub.stop()


class ResultHandlingTests(MoviesDbProfileTestCase):
    async def test_recommendation_structure_and_warnings_survive(self) -> None:
        def handler(name, args):
            self.assertEqual(name, "movies_recommend")
            return {
                "content": [{"type": "text", "text": "3 recommendations"}],
                "structuredContent": {
                    "recommendations": [{"title": "Heat", "reason": "matches your crime-drama taste"}],
                    "degraded": True,
                    "warnings": ["TMDB unavailable; used local library only"],
                },
                "_meta": {"upstream_token_hint": TOKEN},
            }

        server = FakeMcpHttpServer(tools=_all_tools(), require_token=TOKEN, call_handler=handler)
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            context = await self._hub(workspace, server)

            tool_obj = next(
                t for cap, t in build_mcp_capabilities(context) if cap.id == "mcp.movies_db.movies_recommend"
            )
            raw = await tool_obj.execute(session=SimpleNamespace(shared_context=context), query="crime")
            payload = json.loads(raw)
            self.assertEqual(payload["text"], "3 recommendations")
            self.assertEqual(
                payload["data"]["recommendations"][0]["reason"],
                "matches your crime-drama taste",
            )
            self.assertTrue(payload["data"]["degraded"])
            self.assertEqual(len(payload["data"]["warnings"]), 1)
            # Private metadata is never model-visible, even when it holds a secret.
            self.assertNotIn(TOKEN, raw)
            await context.mcp_hub.stop()

    async def test_large_result_is_capped_without_breaking_json(self) -> None:
        def handler(name, args):
            return {
                "content": [{"type": "text", "text": "x" * 100_000}],
                "structuredContent": {"rows": ["y" * 500 for _ in range(200)]},
            }

        server = FakeMcpHttpServer(tools=_all_tools(), require_token=TOKEN, call_handler=handler)
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            context = await self._hub(workspace, server)
            limit = context.config.mcp.servers["movies_db"].max_result_chars

            tool_obj = next(
                t for cap, t in build_mcp_capabilities(context) if cap.id == "mcp.movies_db.movies_search_library"
            )
            raw = await tool_obj.execute(session=SimpleNamespace(shared_context=context), query="all")
            self.assertLessEqual(len(raw), limit)
            payload = json.loads(raw)  # still valid JSON
            self.assertTrue(payload["truncated"])
            await context.mcp_hub.stop()

    async def test_all_nine_approved_tools_are_callable(self) -> None:
        server = FakeMcpHttpServer(
            tools=_all_tools(),
            require_token=TOKEN,
            call_handler=lambda name, args: text_result(f"{name} ok"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            context = await self._hub(workspace, server)
            session = SimpleNamespace(shared_context=context)

            for cap, tool_obj in build_mcp_capabilities(context):
                raw = await tool_obj.execute(session=session, query="x")
                self.assertEqual(json.loads(raw)["text"], f"{cap.operation.split('.')[1]} ok")
            self.assertEqual(
                sorted(r for r in server.rpc_methods() if r == "tools/call"),
                ["tools/call"] * 9,
            )
            await context.mcp_hub.stop()


class FailureIsolationTests(MoviesDbProfileTestCase):
    async def test_offline_server_degrades_without_breaking_the_bot(self) -> None:
        server = FakeMcpHttpServer(tools=_all_tools(), health_status=503)
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            context = await self._hub(workspace, server)

            self.assertEqual(context.mcp_hub.state("movies_db"), McpServerState.DEGRADED)
            self.assertEqual(build_mcp_capabilities(context), [])
            # The rest of the tool set is untouched.
            from tools.capabilities import ToolPolicy
            from tools.capability_catalog import build_capability_registry

            caps = build_capability_registry(context.agent_loader.load("pickle"), context, include_post_message=False)
            names = {
                s["function"]["name"]
                for s in caps.build_tool_registry(ToolPolicy.from_config(context.config)).get_tool_schemas()
            }
            self.assertIn("read", names)
            self.assertFalse(any(n.startswith("mcp_") for n in names))
            await context.mcp_hub.stop()

    async def test_missing_url_env_degrades_safely(self) -> None:
        server = FakeMcpHttpServer(tools=_all_tools())
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            context = await self._hub(workspace, server, env={"MOVIES_DB_TOKEN": TOKEN})
            status = context.mcp_hub.status("movies_db")
            self.assertEqual(status.state, McpServerState.DEGRADED)
            self.assertIn("MOVIES_DB_URL", status.last_error)
            await context.mcp_hub.stop()

    async def test_missing_token_env_degrades_safely(self) -> None:
        server = FakeMcpHttpServer(tools=_all_tools())
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            context = await self._hub(workspace, server, env={"MOVIES_DB_URL": URL})
            status = context.mcp_hub.status("movies_db")
            self.assertEqual(status.state, McpServerState.DEGRADED)
            self.assertIn("MOVIES_DB_TOKEN", status.last_error)
            self.assertNotIn(TOKEN, status.last_error)
            await context.mcp_hub.stop()

    async def test_bad_token_degrades_with_an_auth_specific_error(self) -> None:
        server = FakeMcpHttpServer(tools=_all_tools(), require_token="a-different-token")
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            context = await self._hub(workspace, server)
            status = context.mcp_hub.status("movies_db")
            self.assertEqual(status.state, McpServerState.DEGRADED)
            self.assertIn("authentication", status.last_error)
            self.assertNotIn(TOKEN, status.last_error)
            await context.mcp_hub.stop()


class SecretHygieneTests(MoviesDbProfileTestCase):
    async def test_token_never_reaches_logs_status_or_snapshots(self) -> None:
        server = FakeMcpHttpServer(
            tools=_all_tools(),
            require_token=TOKEN,
            instructions=f"internal token is {TOKEN}",
        )
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            with self.assertLogs(level=logging.DEBUG) as captured:
                context = await self._hub(workspace, server)
                logging.getLogger(__name__).debug("keep assertLogs happy")

            self.assertNotIn(TOKEN, "\n".join(captured.output))

            status = context.mcp_hub.status("movies_db")
            self.assertNotIn(TOKEN, repr(status))

            # Server instructions are off by default, so even a server echoing a
            # secret cannot get it into a prompt.
            from tools.mcp_tools import build_mcp_instructions_layer

            layer = build_mcp_instructions_layer(context, context.agent_loader.load("pickle"))
            self.assertEqual(layer, "")
            await context.mcp_hub.stop()

    async def test_committed_config_contains_no_token(self) -> None:
        text = EXAMPLE_CONFIG.read_text()
        self.assertNotIn(TOKEN, text)
        self.assertIn("token_env: MOVIES_DB_TOKEN", text)


if __name__ == "__main__":
    unittest.main()
