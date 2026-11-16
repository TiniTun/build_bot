"""The ``mcp-login`` command: interactive OAuth login for a hosted MCP server.

A fake authorization server and a fake MCP server share one ``httpx2.MockTransport``;
a fake browser GETs the real loopback callback server on ``127.0.0.1``.
"""

import asyncio
import contextlib
import io
import json
import shutil
import socket
import tempfile
import unittest
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx2
import yaml
from typer.testing import CliRunner

from cli.main import app
from cli.mcp_login import LoginError, run_login
from provider.mcp.oauth_store import OAuthGrant, OAuthStore
from tests.helpers import make_workspace
from tests.mcp_fakes import FakeMcpHttpServer, text_result, tool_spec
from utils.mcp_config import McpServerConfig

ISSUER = "https://mcp.notion.test"
MCP_URL = f"{ISSUER}/mcp"
NEW_CLIENT_ID = "registered-client-1"
AUTH_CODE = "auth-code-SECRET-abc123"
ACCESS = "access-SECRET-a1b2c3d4"
REFRESH = "refresh-SECRET-e5f6g7h8"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class FakeNotion:
    """Authorization server (PRM, AS metadata, /register, /token) plus MCP endpoint."""

    def __init__(self) -> None:
        self.mcp = FakeMcpHttpServer(
            tools=[tool_spec("notion-get-self")],
            require_token=ACCESS,
            call_handler=self._call,
        )
        self.register_calls: list[dict[str, Any]] = []
        self.token_calls: list[dict[str, str]] = []
        self.rejected_client_ids: set[str] = set()

    def _call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        assert name == "notion-get-self"
        return text_result(json.dumps({"workspace": {"id": "w1", "name": "Test Workspace"}, "user": {"id": "u1"}}))

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self._handle)

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        if path == "/mcp":
            return self.mcp._handle(request)
        if path == "/.well-known/oauth-protected-resource/mcp":
            return httpx2.Response(200, json={"resource": MCP_URL, "authorization_servers": [ISSUER]})
        if path == "/.well-known/oauth-authorization-server":
            return httpx2.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": f"{ISSUER}/authorize",
                    "token_endpoint": f"{ISSUER}/token",
                    "registration_endpoint": f"{ISSUER}/register",
                    "response_types_supported": ["code"],
                    "grant_types_supported": ["authorization_code", "refresh_token"],
                    "code_challenge_methods_supported": ["S256"],
                    "token_endpoint_auth_methods_supported": ["none"],
                },
            )
        if path == "/register" and request.method == "POST":
            body = json.loads(request.content)
            self.register_calls.append(body)
            client_id = f"registered-client-{len(self.register_calls)}"
            return httpx2.Response(
                201,
                json={
                    "client_id": client_id,
                    "client_name": body.get("client_name"),
                    "redirect_uris": body["redirect_uris"],
                    "grant_types": body.get("grant_types"),
                    "response_types": ["code"],
                    "token_endpoint_auth_method": "none",
                },
            )
        if path == "/token" and request.method == "POST":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.token_calls.append(form)
            if form.get("client_id") in self.rejected_client_ids:
                return httpx2.Response(401, json={"error": "invalid_client"})
            if form.get("grant_type") != "authorization_code" or form.get("code") != AUTH_CODE:
                return httpx2.Response(400, json={"error": "invalid_grant"})
            return httpx2.Response(
                200,
                json={
                    "access_token": ACCESS,
                    "refresh_token": REFRESH,
                    "token_type": "bearer",
                    "expires_in": 28800,
                },
            )
        return httpx2.Response(404, json={"error": "not_found"})


class FakeBrowser:
    """Records each authorization URL and follows the redirect to the loopback callback."""

    def __init__(self, *, state_override: str | None = None, error: str | None = None) -> None:
        self.state_override = state_override
        self.error = error
        self.urls: list[str] = []
        self.pages: list[tuple[int, str]] = []
        self._tasks: list[asyncio.Task[None]] = []

    def open(self, url: str) -> None:
        self.urls.append(url)
        self._tasks.append(asyncio.get_running_loop().create_task(self._follow(url)))

    async def _follow(self, url: str) -> None:
        query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        params = {"state": self.state_override or query["state"]}
        if self.error:
            params["error"] = self.error
        else:
            params["code"] = AUTH_CODE
        async with httpx2.AsyncClient() as client:
            # A real browser also asks for a favicon; it must not end the login.
            await client.get(query["redirect_uri"].replace("/callback", "/favicon.ico"))
            response = await client.get(f"{query['redirect_uri']}?{urlencode(params)}")
        self.pages.append((response.status_code, response.text))

    async def settle(self) -> None:
        for task in self._tasks:
            with contextlib.suppress(Exception):
                await task


class LoginTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store_path = self.tmp / "secrets" / "notion.oauth.json"
        self.port = _free_port()
        self.redirect_uri = f"http://127.0.0.1:{self.port}/callback"
        self.config = McpServerConfig.model_validate(
            {
                "url": MCP_URL,
                "auth": {"type": "oauth", "token_store": str(self.store_path), "callback_port": self.port},
                "allowed_tools": ["notion-get-self"],
            }
        )
        self.notion = FakeNotion()

    def _store_grant(self, *, client_id: str, redirect_uri: str) -> None:
        OAuthStore(self.store_path).save(
            OAuthGrant(
                client_id=client_id,
                client_secret=None,
                redirect_uri=redirect_uri,
                token_endpoint=f"{ISSUER}/token",
                resource=MCP_URL,
                access_token="old-access-token",
                refresh_token="old-refresh-token",
                expires_at=0.0,
                granted_at=0.0,
            )
        )

    async def _login(self, browser: FakeBrowser, *, timeout: float = 10.0) -> tuple[OAuthGrant, str]:
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                grant = await run_login(
                    "notion", self.config, open_url=browser.open, transport=self.notion.transport(), timeout=timeout
                )
        finally:
            await browser.settle()
        return grant, out.getvalue()

    async def test_login_registers_and_saves_grant(self) -> None:
        browser = FakeBrowser()
        grant, output = await self._login(browser)

        self.assertEqual(len(self.notion.register_calls), 1)
        registration = self.notion.register_calls[0]
        self.assertEqual(registration["redirect_uris"], [self.redirect_uri])
        self.assertEqual(registration["token_endpoint_auth_method"], "none")
        self.assertEqual(registration["client_name"], "build_bot")
        self.assertEqual(set(registration["grant_types"]), {"authorization_code", "refresh_token"})

        (url,) = browser.urls
        query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        self.assertTrue(url.startswith(f"{ISSUER}/authorize?"))
        self.assertEqual(query["client_id"], NEW_CLIENT_ID)
        self.assertEqual(query["redirect_uri"], self.redirect_uri)
        self.assertEqual(query["code_challenge_method"], "S256")
        self.assertEqual(query["resource"], MCP_URL)
        self.assertEqual(self.notion.token_calls[0]["resource"], MCP_URL)

        saved = OAuthStore(self.store_path).load()
        self.assertEqual(saved, grant)
        self.assertEqual(grant.client_id, NEW_CLIENT_ID)
        self.assertIsNone(grant.client_secret)
        self.assertEqual(grant.redirect_uri, self.redirect_uri)
        self.assertEqual(grant.token_endpoint, f"{ISSUER}/token")
        self.assertEqual(grant.resource, MCP_URL)
        self.assertEqual(grant.access_token, ACCESS)
        self.assertEqual(grant.refresh_token, REFRESH)
        self.assertAlmostEqual(grant.expires_at - grant.granted_at, 28800, delta=1)

        self.assertEqual(browser.pages[0][0], 200)
        self.assertIn("Test Workspace", output)
        self.assertIn(str(self.store_path), output)
        for secret in (ACCESS, REFRESH, AUTH_CODE):
            self.assertNotIn(secret, output)
        self.assertIn("tools/call", self.notion.mcp.rpc_methods())

    async def test_login_reuses_existing_registration(self) -> None:
        self._store_grant(client_id="stored-client", redirect_uri=self.redirect_uri)
        browser = FakeBrowser()
        grant, _ = await self._login(browser)

        self.assertEqual(self.notion.register_calls, [])
        query = parse_qs(urlsplit(browser.urls[0]).query)
        self.assertEqual(query["client_id"], ["stored-client"])
        self.assertEqual(grant.client_id, "stored-client")
        self.assertEqual(grant.access_token, ACCESS)
        self.assertEqual(OAuthStore(self.store_path).load(), grant)

    async def test_login_reregisters_when_redirect_changes(self) -> None:
        self._store_grant(client_id="stored-client", redirect_uri="http://127.0.0.1:1/callback")
        browser = FakeBrowser()
        grant, _ = await self._login(browser)

        self.assertEqual(len(self.notion.register_calls), 1)
        self.assertEqual(grant.client_id, NEW_CLIENT_ID)
        self.assertEqual(grant.redirect_uri, self.redirect_uri)

    async def test_login_reregisters_when_stored_client_rejected(self) -> None:
        self._store_grant(client_id="stored-client", redirect_uri=self.redirect_uri)
        self.notion.rejected_client_ids.add("stored-client")
        browser = FakeBrowser()
        # The SDK logs the rejected exchange (scrubbed; the CLI routes it to the log file).
        with self.assertLogs("mcp.client.auth", "ERROR"):
            grant, _ = await self._login(browser)

        self.assertEqual(len(self.notion.register_calls), 1)
        self.assertEqual(len(browser.urls), 2)
        self.assertEqual(grant.client_id, NEW_CLIENT_ID)
        self.assertEqual(OAuthStore(self.store_path).load(), grant)

    async def test_login_rejects_wrong_state(self) -> None:
        browser = FakeBrowser(state_override="forged-state")
        with self.assertRaises(LoginError) as ctx, self.assertLogs("mcp.client.auth", "ERROR"):
            await self._login(browser)
        self.assertIn("state", str(ctx.exception))
        self.assertEqual(self.notion.token_calls, [])
        self.assertFalse(self.store_path.exists())
        self.assertEqual(browser.pages[0][0], 400)

    async def test_login_reports_authorize_error(self) -> None:
        browser = FakeBrowser(error="access_denied")
        with self.assertRaises(LoginError) as ctx, self.assertLogs("mcp.client.auth", "ERROR"):
            await self._login(browser)
        self.assertIn("access_denied", str(ctx.exception))
        self.assertFalse(self.store_path.exists())

    async def test_login_times_out(self) -> None:
        def ignore(url: str) -> None:
            pass

        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(LoginError) as ctx:
            await run_login("notion", self.config, open_url=ignore, transport=self.notion.transport(), timeout=0.2)
        self.assertIn("timed out", str(ctx.exception))
        self.assertFalse(self.store_path.exists())
        # The loopback port is released afterwards.
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", self.port))


class LoginCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.workspace = make_workspace(self.tmp)
        config_path = self.workspace / "config.user.yaml"
        config = yaml.safe_load(config_path.read_text())
        config["mcp"] = {"servers": {"movies": {"url": "http://movies.test/mcp", "allowed_tools": ["a"]}}}
        config_path.write_text(yaml.safe_dump(config, sort_keys=False))

    def _invoke(self, server_id: str):
        return CliRunner().invoke(app, ["--workspace", str(self.workspace), "mcp-login", server_id])

    def test_unknown_server_exits_nonzero(self) -> None:
        result = self._invoke("notion")
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("unknown MCP server 'notion'", result.output)

    def test_non_oauth_server_exits_nonzero(self) -> None:
        result = self._invoke("movies")
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("does not use OAuth", result.output)


if __name__ == "__main__":
    unittest.main()
