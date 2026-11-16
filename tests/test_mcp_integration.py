"""Cross-cutting integration tests for the MCP slice.

Covers the paths that only appear when the layers are wired together: a full
Pickle -> movie-assistant -> MCP dispatch over a fake Streamable HTTP server, and
graceful shutdown while discovery and reconnect work is in flight.
"""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import httpx2

import core.agent as agent_module
from core.agent import Agent
from core.events import CliEventSource, InboundEvent, OutboundEvent
from provider.llm.base import LLMToolCall
from provider.mcp.client import StreamableHttpMcpClient
from provider.mcp.errors import McpConnectionError, McpError
from provider.mcp.hub import McpHub, McpServerState
from provider.mcp.oauth import McpOAuthAuth
from provider.mcp.oauth_store import OAuthGrant, OAuthStore
from server.agent_worker import AgentWorker
from tests.helpers import make_context, make_workspace, write_definition
from tests.mcp_fakes import FakeMcpClient, FakeMcpHttpServer, fake_tool, text_result, tool_spec
from tests.test_mcp_hub import _config, _oauth_server
from utils.mcp_config import McpConfig

TOKEN = "integration-token-5c2d9a"
URL = "http://movies-db:8765/mcp"

READ_ONLY_TOOLS = [
    "movies_get_taste_profile",
    "movies_recommend",
    "movies_resolve_title",
    "movies_search_library",
]
MUTATION_TOOLS = [
    "movies_log_viewing",
    "movies_update_viewing",
    "movies_record_feedback",
    "movies_delete_viewing",
]


class _ScriptedLLM:
    """Replays a fixed list of (content, tool_calls) turns for one agent."""

    def __init__(self, turns: list[tuple[str, list[LLMToolCall]]]) -> None:
        self.turns = list(turns)
        self.seen_schemas: list[list[dict]] = []

    async def chat(self, messages, tool_schemas, trace_session_id=None):
        self.seen_schemas.append(tool_schemas)
        if not self.turns:
            return "done", []
        return self.turns.pop(0)

    def tool_names(self) -> set[str]:
        return {schema["function"]["name"] for call in self.seen_schemas for schema in call}


class _LLMFactory:
    """Stands in for ``LLMProvider``, handing each agent its scripted turns."""

    def __init__(self, scripts: dict[str, _ScriptedLLM]) -> None:
        self.scripts = scripts

    def from_config(self, llm_config, logging_path=None, agent_id=None):
        return self.scripts.setdefault(agent_id, _ScriptedLLM([]))


def _movies_workspace(root: Path) -> Path:
    workspace = make_workspace(root)
    # Mirror the shipped Pickle: a coordinator that dispatches and holds no
    # movie capabilities of its own.
    write_definition(
        workspace / "agents",
        "pickle",
        "AGENT.md",
        {
            "name": "Pickle",
            "description": "Coordinator",
            "allowed_capabilities": ["agent.subagent_dispatch"],
        },
        "You are Pickle.",
    )
    write_definition(
        workspace / "agents",
        "movie-assistant",
        "AGENT.md",
        {
            "name": "Movie Assistant",
            "description": "Read-only movie library specialist.",
            "allowed_capabilities": [f"mcp.movies_db.{name}" for name in READ_ONLY_TOOLS],
        },
        "You are the Movie Assistant. Read-only.",
    )
    return workspace


def _all_tools() -> list[dict]:
    return [tool_spec(name, properties={"query": {"type": "string"}}) for name in READ_ONLY_TOOLS + MUTATION_TOOLS]


class DispatchIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """Pickle dispatches to the specialist, which calls a real MCP tool."""

    async def asyncSetUp(self) -> None:
        self._original_provider = agent_module.LLMProvider

    async def asyncTearDown(self) -> None:
        agent_module.LLMProvider = self._original_provider

    async def test_full_pickle_to_movie_assistant_dispatch(self) -> None:
        server = FakeMcpHttpServer(
            tools=_all_tools(),
            require_token=TOKEN,
            call_handler=lambda name, args: {
                "content": [{"type": "text", "text": f"{name} found 2 titles"}],
                "structuredContent": {"titles": ["Heat", "Ronin"]},
            },
        )

        with tempfile.TemporaryDirectory() as tmp:
            workspace = _movies_workspace(Path(tmp))
            context = make_context(workspace)
            context.config.mcp = McpConfig.model_validate(
                {
                    "servers": {
                        "movies_db": {
                            "url_env": "MOVIES_DB_URL",
                            "token_env": "MOVIES_DB_TOKEN",
                            "health_path": "/health",
                            "allowed_tools": READ_ONLY_TOOLS,
                        }
                    }
                }
            )
            env = {"MOVIES_DB_URL": URL, "MOVIES_DB_TOKEN": TOKEN}
            context.mcp_hub = McpHub(
                context.config,
                client_factory=lambda sid, cfg: StreamableHttpMcpClient(
                    sid, cfg, env=env, transport_factory=server.transport
                ),
            )
            await context.mcp_hub.start()

            pickle_llm = _ScriptedLLM(
                [
                    (
                        "",
                        [
                            LLMToolCall(
                                id="d1",
                                name="subagent_dispatch",
                                arguments=json.dumps(
                                    {
                                        "agent_id": "movie-assistant",
                                        "task": "Find crime films in the library.",
                                    }
                                ),
                            )
                        ],
                    ),
                    ("You have Heat and Ronin.", []),
                ]
            )
            movie_llm = _ScriptedLLM(
                [
                    (
                        "",
                        [
                            LLMToolCall(
                                id="m1",
                                name="mcp_movies_db_movies_search_library",
                                arguments=json.dumps({"query": "crime"}),
                            )
                        ],
                    ),
                    ("summary: found Heat and Ronin", []),
                ]
            )
            agent_module.LLMProvider = _LLMFactory({"pickle": pickle_llm, "movie-assistant": movie_llm})

            workers = [context.eventbus, AgentWorker(context)]
            outbound: asyncio.Queue[OutboundEvent] = asyncio.Queue()

            async def collect(event: OutboundEvent) -> None:
                await outbound.put(event)
                context.eventbus.ack(event)

            context.eventbus.subscribe(OutboundEvent, collect)
            for worker in workers:
                worker.start()

            try:
                pickle_def = context.agent_loader.load("pickle")
                session = Agent(pickle_def, context).new_session(CliEventSource())
                await context.eventbus.publish(
                    InboundEvent(
                        session_id=session.session_id,
                        source=CliEventSource(),
                        content="What crime films do I have?",
                    )
                )
                event = await asyncio.wait_for(outbound.get(), timeout=10)
            finally:
                for worker in workers:
                    await worker.stop()
                await context.mcp_hub.stop()

            # Pickle answered from the specialist's result.
            self.assertEqual(event.content, "You have Heat and Ronin.")

            # The remote tool really was called, over an authenticated session.
            calls = [r for r in server.requests if r.rpc_method == "tools/call"]
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0].authorization, f"Bearer {TOKEN}")

            # Pickle never saw a movie tool; the specialist saw exactly the four.
            self.assertFalse(any(n.startswith("mcp_") for n in pickle_llm.tool_names()))
            self.assertEqual(
                {n for n in movie_llm.tool_names() if n.startswith("mcp_")},
                {f"mcp_movies_db_{name}" for name in READ_ONLY_TOOLS},
            )
            # No mutation tool was ever offered to the model.
            for mutation in MUTATION_TOOLS:
                self.assertNotIn(f"mcp_movies_db_{mutation}", movie_llm.tool_names())

    async def test_specialist_sees_no_tools_when_server_is_offline(self) -> None:
        server = FakeMcpHttpServer(tools=_all_tools(), health_status=503)
        with tempfile.TemporaryDirectory() as tmp:
            workspace = _movies_workspace(Path(tmp))
            context = make_context(workspace)
            context.config.mcp = McpConfig.model_validate(
                {
                    "servers": {
                        "movies_db": {
                            "url": URL,
                            "health_path": "/health",
                            "allowed_tools": READ_ONLY_TOOLS,
                        }
                    }
                }
            )
            context.mcp_hub = McpHub(
                context.config,
                client_factory=lambda sid, cfg: StreamableHttpMcpClient(
                    sid, cfg, env={}, transport_factory=server.transport
                ),
                sleep=lambda _: asyncio.sleep(0),
            )
            await context.mcp_hub.start()

            agent_module.LLMProvider = _LLMFactory({})
            movie_def = context.agent_loader.load("movie-assistant")
            session = Agent(movie_def, context).new_session(CliEventSource())

            # The bot still works; the specialist simply has no movie tools.
            names = {schema["function"]["name"] for schema in session.tools.get_tool_schemas()}
            self.assertEqual(names, set())
            await context.mcp_hub.stop()


class SdkContainmentTests(unittest.TestCase):
    """The MCP SDK must stay behind ``provider/mcp/client.py``."""

    ALLOWED = {Path("provider/mcp/client.py")}
    SDK_IMPORTS = ("import mcp", "from mcp", "import mcp_types", "from mcp_types")

    def test_no_sdk_import_outside_the_client_module(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        offenders: list[str] = []
        for path in repo_root.rglob("*.py"):
            relative = path.relative_to(repo_root)
            # `.claude` holds git worktrees: full copies of the repo, client.py included.
            if relative.parts[0] in {".venv", ".venv-dev", ".uv-cache", ".claude", "tests"}:
                continue
            if relative in self.ALLOWED or path.name.startswith("._"):
                # `._*` are macOS resource forks, not source.
                continue
            for line_number, line in enumerate(
                path.read_text(encoding="utf-8", errors="ignore").splitlines(),
                start=1,
            ):
                stripped = line.strip()
                if stripped.startswith(self.SDK_IMPORTS):
                    offenders.append(f"{relative}:{line_number}: {stripped}")
        self.assertEqual(offenders, [], msg="MCP SDK types leaked out of provider/mcp/client.py")

    def test_capability_and_agent_layers_use_only_internal_models(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        for module in ("tools/mcp_tools.py", "provider/mcp/hub.py", "core/agent.py"):
            text = (repo_root / module).read_text()
            self.assertNotIn("mcp_types", text, msg=module)


class ShutdownIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_during_active_discovery_and_reconnect(self) -> None:
        healthy = FakeMcpClient("movies_db", tools=(fake_tool("movies_db", "alpha"),))
        broken = FakeMcpClient("notes_db", health_error=McpConnectionError("[notes_db] down"))
        clients = {"movies_db": healthy, "notes_db": broken}

        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            context = make_context(workspace)
            context.config.mcp = McpConfig.model_validate(
                {
                    "servers": {
                        "movies_db": {"url": URL, "allowed_tools": ["alpha"]},
                        "notes_db": {
                            "url": "https://notes/mcp",
                            "allowed_tools": ["alpha"],
                        },
                    }
                }
            )
            context.mcp_hub = McpHub(
                context.config,
                client_factory=lambda sid, cfg: clients[sid],
                sleep=lambda _: asyncio.sleep(0),
            )
            await context.mcp_hub.start()

            # A rediscovery that never finishes, plus an active reconnect loop.
            blocked = asyncio.Event()

            async def hanging_list():
                await blocked.wait()
                return healthy.tools

            healthy.list_tools = hanging_list  # type: ignore[assignment]
            healthy.emit_tools_changed()
            await asyncio.sleep(0)

            before = asyncio.all_tasks()
            self.assertTrue(
                any("mcp-" in (t.get_name() or "") for t in before),
                msg="expected MCP background work to be in flight",
            )

            await context.mcp_hub.stop()
            await asyncio.sleep(0)

            leftover = [t for t in asyncio.all_tasks() if "mcp-" in (t.get_name() or "") and not t.done()]
            self.assertEqual(leftover, [], msg="MCP tasks outlived shutdown")
            self.assertEqual(healthy.close_count, 1)
            self.assertEqual(broken.close_count, 1)
            self.assertEqual(context.mcp_hub.snapshots(), {})

    async def test_shutdown_is_safe_to_repeat_after_a_failed_start(self) -> None:
        broken = FakeMcpClient(
            "notes_db",
            connect_error=McpConnectionError("[notes_db] refused"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            context = make_context(workspace)
            context.config.mcp = McpConfig.model_validate(
                {
                    "servers": {
                        "notes_db": {
                            "url": "https://notes/mcp",
                            "required": True,
                            "allowed_tools": ["alpha"],
                        }
                    }
                }
            )
            context.mcp_hub = McpHub(context.config, client_factory=lambda sid, cfg: broken)
            with self.assertRaises(McpConnectionError):
                await context.mcp_hub.start()
            await context.mcp_hub.stop()
            await context.mcp_hub.stop()


class OAuthSessionRecoveryTests(unittest.IsolatedAsyncioTestCase):
    """Real client + OAuth auth + hub: a failed token refresh mid-session."""

    async def test_token_endpoint_503_leaves_connected_and_recovers(self) -> None:
        now = [1_800_000_000.0]
        token_mode = {"status": 200}
        server = FakeMcpHttpServer(tools=[tool_spec("alpha")], call_handler=lambda name, args: text_result("ok"))

        def token_handler(request: httpx2.Request) -> httpx2.Response:
            if token_mode["status"] != 200:
                return httpx2.Response(token_mode["status"], text="unavailable")
            return httpx2.Response(
                200,
                json={"access_token": "ACCESS-TOKEN-2222", "refresh_token": "REFRESH-TOKEN-2222", "expires_in": 28_800},
            )

        async def fast_sleep(seconds: float) -> None:
            await asyncio.sleep(0.01)

        async def wait_for(state: McpServerState) -> None:
            for _ in range(300):
                if hub.state("notion") is state:
                    return
                await asyncio.sleep(0.01)
            self.fail(f"notion never reached {state.value}; it is {hub.state('notion').value}")

        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            store_path = Path(tmp) / "secrets" / "notion.json"
            OAuthStore(store_path).save(
                OAuthGrant(
                    client_id="cid",
                    client_secret=None,
                    redirect_uri="http://127.0.0.1:33418/callback",
                    token_endpoint="https://notion.example/token",
                    resource="https://notion.example/mcp",
                    access_token="ACCESS-TOKEN-1111",
                    refresh_token="REFRESH-TOKEN-1111",
                    expires_at=now[0] + 3600,
                    granted_at=now[0],
                )
            )

            def factory(server_id, server_config):
                client = StreamableHttpMcpClient(server_id, server_config, env={}, transport_factory=server.transport)
                client._oauth_auth = McpOAuthAuth(
                    OAuthStore(server_config.auth.token_store),
                    on_secret=client._redactor.add,
                    clock=lambda: now[0],
                    token_transport=httpx2.MockTransport(token_handler),
                )
                return client

            hub = McpHub(
                _config(workspace, {"notion": _oauth_server(store_path, connect_timeout_seconds=5.0)}),
                client_factory=factory,
                sleep=fast_sleep,
            )
            try:
                await hub.start()
                self.assertEqual(hub.state("notion"), McpServerState.CONNECTED)
                self.assertEqual((await hub.call_tool("notion", "alpha", {})).text_blocks, ("ok",))

                # The access token expires; the refresh POST gets a 503 mid-call.
                now[0] += 3600
                token_mode["status"] = 503
                with self.assertRaises(McpError):
                    await hub.call_tool("notion", "alpha", {})
                self.assertIsNot(hub.state("notion"), McpServerState.CONNECTED)

                # The token endpoint recovers: the reconnect loop restores the session.
                token_mode["status"] = 200
                await wait_for(McpServerState.CONNECTED)
                self.assertEqual((await hub.call_tool("notion", "alpha", {})).text_blocks, ("ok",))
            finally:
                await hub.stop()


if __name__ == "__main__":
    unittest.main()
