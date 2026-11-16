"""Tests for the server-mode OAuth auth (bearer + rotation-safe refresh)."""

import json
import logging
import shutil
import tempfile
import unittest
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import AsyncIterator, Callable
from urllib.parse import parse_qs

import httpx2

from provider.mcp.errors import McpConnectionError
from provider.mcp.oauth import LoginRequired, McpOAuthAuth
from provider.mcp.oauth_store import OAuthGrant, OAuthStore

NOW = 1_800_000_000.0
MCP_URL = "https://mcp.example.com/mcp"
TOKEN_URL = "https://mcp.example.com/token"

OLD_ACCESS = "old-access-SECRET-a1"
OLD_REFRESH = "old-refresh-SECRET-r1"
NEW_ACCESS = "new-access-SECRET-a2"
NEW_REFRESH = "new-refresh-SECRET-r2"
OTHER_ACCESS = "other-access-SECRET-a3"
CLIENT_SECRET = "client-SECRET-c1"
ALL_SECRETS = (OLD_ACCESS, OLD_REFRESH, NEW_ACCESS, NEW_REFRESH, OTHER_ACCESS, CLIENT_SECRET)


def _grant(*, expires_in: float, access: str = OLD_ACCESS, refresh: str = OLD_REFRESH) -> OAuthGrant:
    return OAuthGrant(
        client_id="client-id",
        client_secret=CLIENT_SECRET,
        redirect_uri="http://127.0.0.1:33418/callback",
        token_endpoint=TOKEN_URL,
        resource=MCP_URL,
        access_token=access,
        refresh_token=refresh,
        expires_at=NOW + expires_in,
        granted_at=NOW - 86_400,
    )


def _token_ok(request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(
        200,
        json={"access_token": NEW_ACCESS, "refresh_token": NEW_REFRESH, "expires_in": 28_800, "token_type": "bearer"},
    )


def _mcp_ok(request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(200, json={"ok": True})


class _Server:
    """Routes ``/token`` and ``/mcp`` through one MockTransport and records every request."""

    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []
        # Snapshotted per send: a retried request is the same object with new headers.
        self._mcp_bearers: list[str | None] = []
        self.token_handler: Callable[[httpx2.Request], httpx2.Response] = _token_ok
        self.mcp_handler: Callable[[httpx2.Request], httpx2.Response] = _mcp_ok
        self.transport = httpx2.MockTransport(self._handle)

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if request.url.path == "/token":
            return self.token_handler(request)
        self._mcp_bearers.append(request.headers.get("Authorization"))
        return self.mcp_handler(request)

    def token_requests(self) -> list[httpx2.Request]:
        return [r for r in self.requests if r.url.path == "/token"]

    def mcp_bearers(self) -> list[str | None]:
        return list(self._mcp_bearers)


class _LogCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.lines: list[str] = []
        self.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


class _RacingStore(OAuthStore):
    """Simulates another process saving a fresh grant while we wait on the lock."""

    def __init__(self, path: Path, fresh: OAuthGrant) -> None:
        super().__init__(path)
        self._fresh = fresh

    @asynccontextmanager
    async def locked(self, timeout: float = 30.0) -> AsyncIterator[None]:
        self.save(self._fresh)
        async with super().locked(timeout):
            yield


class _FlakyStore(OAuthStore):
    """Store whose ``save`` fails while ``fail`` is set (a full or read-only disk)."""

    fail = False

    def save(self, grant: OAuthGrant) -> None:
        if self.fail:
            raise OSError(28, "No space left on device")
        super().save(grant)


def _rotating_token_handler(sent: list[str]) -> Callable[[httpx2.Request], httpx2.Response]:
    """Token endpoint that records each refresh token and rotates to ``refresh-N``."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(parse_qs(request.content.decode())["refresh_token"][0])
        n = len(sent)
        return httpx2.Response(
            200, json={"access_token": f"access-{n}-SECRET", "refresh_token": f"refresh-{n}-SECRET", "expires_in": 28_800}
        )

    return handler


class McpOAuthAuthTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = OAuthStore(self.tmp / "secrets" / "notion.json")
        self.server = _Server()
        self.now = NOW
        self.secrets: list[str] = []
        self.logs = _LogCapture()
        root = logging.getLogger()
        root.addHandler(self.logs)
        previous_level = root.level
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.setLevel, previous_level)
        self.addCleanup(root.removeHandler, self.logs)

    def _auth(self, store: OAuthStore | None = None) -> McpOAuthAuth:
        return McpOAuthAuth(
            store or self.store,
            on_secret=self.secrets.append,
            clock=lambda: self.now,
            token_transport=self.server.transport,
        )

    async def _call(self, auth: McpOAuthAuth) -> httpx2.Response:
        async with httpx2.AsyncClient(transport=self.server.transport, auth=auth) as client:
            return await client.post(MCP_URL, json={"jsonrpc": "2.0"})

    def _assert_no_secrets(self, text: str) -> None:
        for secret in ALL_SECRETS:
            self.assertNotIn(secret, text)

    # -- happy paths -------------------------------------------------------

    async def test_no_store_is_login_required_without_network(self) -> None:
        auth = self._auth()
        with self.assertRaises(LoginRequired):
            await self._call(auth)
        self.assertTrue(auth.login_required)
        self.assertEqual(self.server.requests, [])

    async def test_valid_token_adds_bearer(self) -> None:
        self.store.save(_grant(expires_in=3600))
        auth = self._auth()
        response = await self._call(auth)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.server.mcp_bearers(), [f"Bearer {OLD_ACCESS}"])
        self.assertEqual(self.server.token_requests(), [])
        self.assertFalse(auth.login_required)

    async def test_refreshes_within_margin(self) -> None:
        self.store.save(_grant(expires_in=500))
        auth = self._auth()
        response = await self._call(auth)
        self.assertEqual(response.status_code, 200)
        token_requests = self.server.token_requests()
        self.assertEqual(len(token_requests), 1)
        form = parse_qs(token_requests[0].content.decode())
        self.assertEqual(
            form,
            {
                "grant_type": ["refresh_token"],
                "refresh_token": [OLD_REFRESH],
                "client_id": ["client-id"],
                "resource": [MCP_URL],
                "client_secret": [CLIENT_SECRET],
            },
        )
        self.assertEqual(self.server.mcp_bearers(), [f"Bearer {NEW_ACCESS}"])
        saved = self.store.load()
        assert saved is not None
        self.assertEqual(saved.access_token, NEW_ACCESS)
        self.assertEqual(saved.refresh_token, NEW_REFRESH)
        self.assertEqual(saved.expires_at, NOW + 28_800)
        self.assertEqual(saved.granted_at, NOW - 86_400)

    async def test_refresh_without_client_secret_omits_it(self) -> None:
        grant = _grant(expires_in=0)
        grant.client_secret = None
        self.store.save(grant)
        await self._call(self._auth())
        form = parse_qs(self.server.token_requests()[0].content.decode())
        self.assertNotIn("client_secret", form)

    async def test_refresh_keeps_old_refresh_token_when_omitted(self) -> None:
        self.store.save(_grant(expires_in=0))
        self.server.token_handler = lambda r: httpx2.Response(200, json={"access_token": NEW_ACCESS, "expires_in": 60})
        await self._call(self._auth())
        saved = self.store.load()
        assert saved is not None
        self.assertEqual(saved.access_token, NEW_ACCESS)
        self.assertEqual(saved.refresh_token, OLD_REFRESH)

    async def test_restart_with_valid_token_no_refresh(self) -> None:
        self.store.save(_grant(expires_in=3600))
        await self._call(self._auth())
        await self._call(self._auth())
        self.assertEqual(self.server.token_requests(), [])
        self.assertEqual(self.server.mcp_bearers(), [f"Bearer {OLD_ACCESS}"] * 2)

    async def test_restart_with_expired_token_refreshes_once(self) -> None:
        self.store.save(_grant(expires_in=-60))
        auth = self._auth()
        await self._call(auth)
        await self._call(auth)
        restarted = self._auth()
        await self._call(restarted)
        self.assertEqual(len(self.server.token_requests()), 1)
        self.assertEqual(self.server.mcp_bearers(), [f"Bearer {NEW_ACCESS}"] * 3)

    async def test_on_secret_receives_new_tokens(self) -> None:
        self.store.save(_grant(expires_in=0))
        await self._call(self._auth())
        self.assertIn(NEW_ACCESS, self.secrets)
        self.assertIn(NEW_REFRESH, self.secrets)

    async def test_reloaded_grant_avoids_refresh(self) -> None:
        self.store.save(_grant(expires_in=0))
        racing = _RacingStore(self.store.path, _grant(expires_in=3600, access=OTHER_ACCESS, refresh=NEW_REFRESH))
        auth = self._auth(racing)
        await self._call(auth)
        self.assertEqual(self.server.token_requests(), [])
        self.assertEqual(self.server.mcp_bearers(), [f"Bearer {OTHER_ACCESS}"])

    # -- refresh failures --------------------------------------------------

    async def test_refresh_invalid_grant_is_login_required(self) -> None:
        self.store.save(_grant(expires_in=0))
        self.server.token_handler = lambda r: httpx2.Response(400, json={"error": "invalid_grant"})
        auth = self._auth()
        with self.assertRaises(LoginRequired):
            await self._call(auth)
        self.assertTrue(auth.login_required)
        self.assertIsNotNone(auth.last_error)
        self.assertEqual(len(self.server.token_requests()), 1)
        self.assertEqual(self.server.mcp_bearers(), [])

    async def test_refresh_invalid_client_is_login_required(self) -> None:
        self.store.save(_grant(expires_in=0))
        self.server.token_handler = lambda r: httpx2.Response(401, json={"error": "invalid_client"})
        auth = self._auth()
        with self.assertRaises(LoginRequired):
            await self._call(auth)
        self.assertTrue(auth.login_required)

    async def test_login_required_does_not_replay_refresh_token(self) -> None:
        self.store.save(_grant(expires_in=0))
        self.server.token_handler = lambda r: httpx2.Response(400, json={"error": "invalid_grant"})
        auth = self._auth()
        with self.assertRaises(LoginRequired):
            await self._call(auth)
        with self.assertRaises(LoginRequired):
            await self._call(auth)
        self.assertEqual(len(self.server.token_requests()), 1)

    async def test_new_login_clears_login_required(self) -> None:
        auth = self._auth()
        with self.assertRaises(LoginRequired):
            await self._call(auth)
        self.store.save(_grant(expires_in=3600))
        response = await self._call(auth)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(auth.login_required)

    async def test_refresh_5xx_keeps_tokens(self) -> None:
        grant = _grant(expires_in=0)
        self.store.save(grant)
        self.server.token_handler = lambda r: httpx2.Response(503, text="unavailable")
        auth = self._auth()
        with self.assertRaises(McpConnectionError):
            await self._call(auth)
        self.assertFalse(auth.login_required)
        self.assertEqual(self.store.load(), grant)
        self.assertEqual(len(self.server.token_requests()), 1)

    async def test_refresh_connect_error_keeps_tokens(self) -> None:
        grant = _grant(expires_in=0)
        self.store.save(grant)

        def refuse(request: httpx2.Request) -> httpx2.Response:
            raise httpx2.ConnectError("connection refused", request=request)

        self.server.token_handler = refuse
        auth = self._auth()
        with self.assertRaises(McpConnectionError):
            await self._call(auth)
        self.assertFalse(auth.login_required)
        self.assertEqual(self.store.load(), grant)
        self.assertEqual(len(self.server.token_requests()), 1)

    async def test_refresh_timeout_retries_once(self) -> None:
        grant = _grant(expires_in=0)
        self.store.save(grant)

        def time_out(request: httpx2.Request) -> httpx2.Response:
            raise httpx2.ReadTimeout("timed out", request=request)

        self.server.token_handler = time_out
        auth = self._auth()
        with self.assertRaises(LoginRequired):
            await self._call(auth)
        self.assertTrue(auth.login_required)
        token_requests = self.server.token_requests()
        self.assertEqual(len(token_requests), 2)
        self.assertEqual(token_requests[0].content, token_requests[1].content)
        self.assertEqual(self.store.load(), grant)

    async def test_refresh_timeout_then_success_uses_retry(self) -> None:
        self.store.save(_grant(expires_in=0))
        calls = {"n": 0}

        def flaky(request: httpx2.Request) -> httpx2.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx2.RemoteProtocolError("peer closed", request=request)
            return _token_ok(request)

        self.server.token_handler = flaky
        auth = self._auth()
        response = await self._call(auth)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(auth.login_required)
        self.assertEqual(len(self.server.token_requests()), 2)
        self.assertEqual(self.server.mcp_bearers(), [f"Bearer {NEW_ACCESS}"])

    # -- 401 from the MCP endpoint ----------------------------------------

    async def test_401_with_valid_token_reloads_then_login_required(self) -> None:
        self.store.save(_grant(expires_in=3600))
        self.server.mcp_handler = lambda r: httpx2.Response(401)
        auth = self._auth()
        with self.assertRaises(LoginRequired):
            await self._call(auth)
        self.assertTrue(auth.login_required)
        # Store unchanged on reload → exactly one forced refresh, then one retry.
        self.assertEqual(len(self.server.token_requests()), 1)
        self.assertEqual(self.server.mcp_bearers(), [f"Bearer {OLD_ACCESS}", f"Bearer {NEW_ACCESS}"])

    async def test_401_uses_token_rotated_by_other_process(self) -> None:
        self.store.save(_grant(expires_in=3600))
        auth = self._auth()
        await self._call(auth)  # caches OLD_ACCESS in memory
        self.store.save(_grant(expires_in=3600, access=OTHER_ACCESS, refresh=NEW_REFRESH))
        self.server.mcp_handler = lambda r: (
            httpx2.Response(401) if r.headers["Authorization"] == f"Bearer {OLD_ACCESS}" else _mcp_ok(r)
        )
        response = await self._call(auth)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(auth.login_required)
        self.assertEqual(self.server.token_requests(), [])
        self.assertEqual(
            self.server.mcp_bearers(),
            [f"Bearer {OLD_ACCESS}", f"Bearer {OLD_ACCESS}", f"Bearer {OTHER_ACCESS}"],
        )

    # -- redaction ---------------------------------------------------------

    # -- store write failures ----------------------------------------------

    async def test_failed_save_never_replays_the_rotated_out_refresh_token(self) -> None:
        store = _FlakyStore(self.store.path)
        store.save(_grant(expires_in=100))
        sent: list[str] = []
        self.server.token_handler = _rotating_token_handler(sent)
        auth = self._auth(store)

        store.fail = True
        response = await self._call(auth)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.server.mcp_bearers(), ["Bearer access-1-SECRET"])
        self.assertIn("could not be saved", "\n".join(self.logs.lines))
        self._assert_no_secrets("\n".join(self.logs.lines))

        store.fail = False
        self.now += 28_800
        response = await self._call(auth)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(sent, [OLD_REFRESH, "refresh-1-SECRET"])
        self.assertEqual(self.server.mcp_bearers()[-1], "Bearer access-2-SECRET")
        saved = store.load()
        assert saved is not None
        self.assertEqual(saved.refresh_token, "refresh-2-SECRET")

    async def test_401_with_stale_store_refreshes_from_memory(self) -> None:
        store = _FlakyStore(self.store.path)
        store.save(_grant(expires_in=100))
        sent: list[str] = []
        self.server.token_handler = _rotating_token_handler(sent)
        auth = self._auth(store)

        store.fail = True
        await self._call(auth)
        store.fail = False
        self.server.mcp_handler = lambda request: (
            httpx2.Response(401) if request.headers["Authorization"] == "Bearer access-1-SECRET" else _mcp_ok(request)
        )
        response = await self._call(auth)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(sent, [OLD_REFRESH, "refresh-1-SECRET"])
        self.assertEqual(self.server.mcp_bearers()[-1], "Bearer access-2-SECRET")

    # -- grant state shared across auth instances (one per reconnect) ---------

    async def test_reconnect_after_failed_save_never_replays_the_rotated_out_refresh_token(self) -> None:
        store = _FlakyStore(self.store.path)
        store.save(_grant(expires_in=100))
        sent: list[str] = []
        self.server.token_handler = _rotating_token_handler(sent)

        store.fail = True
        await self._call(self._auth(store))
        self.assertEqual(sent, [OLD_REFRESH])

        # The hub drops the client and builds a new client, auth and store object.
        self.now += 28_800
        response = await self._call(self._auth(_FlakyStore(self.store.path)))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(sent, [OLD_REFRESH, "refresh-1-SECRET"])
        self.assertEqual(self.server.mcp_bearers()[-1], "Bearer access-2-SECRET")
        saved = store.load()
        assert saved is not None
        self.assertEqual(saved.refresh_token, "refresh-2-SECRET")

    async def test_new_auth_uses_the_in_memory_grant_while_the_store_is_stale(self) -> None:
        store = _FlakyStore(self.store.path)
        store.save(_grant(expires_in=100))
        sent: list[str] = []
        self.server.token_handler = _rotating_token_handler(sent)
        store.fail = True
        await self._call(self._auth(store))

        await self._call(self._auth(store))
        self.assertEqual(sent, [OLD_REFRESH])
        self.assertEqual(self.server.mcp_bearers(), ["Bearer access-1-SECRET", "Bearer access-1-SECRET"])

    async def test_new_login_after_failed_save_is_adopted(self) -> None:
        store = _FlakyStore(self.store.path)
        store.save(_grant(expires_in=100))
        sent: list[str] = []
        self.server.token_handler = _rotating_token_handler(sent)
        store.fail = True
        await self._call(self._auth(store))
        store.fail = False

        # The operator runs mcp-login: a new grant with a new refresh token.
        login = replace(
            _grant(expires_in=40_000, access="login-access-SECRET", refresh="login-refresh-SECRET"), granted_at=NOW
        )
        store.save(login)
        await self._call(self._auth(store))
        self.assertEqual(self.server.mcp_bearers()[-1], "Bearer login-access-SECRET")
        self.assertEqual(sent, [OLD_REFRESH])

        self.now += 40_000
        await self._call(self._auth(store))
        self.assertEqual(sent, [OLD_REFRESH, "login-refresh-SECRET"])

    async def test_old_auth_adopts_a_new_login_once_its_token_expires(self) -> None:
        store = _FlakyStore(self.store.path)
        store.save(_grant(expires_in=100))
        sent: list[str] = []
        self.server.token_handler = _rotating_token_handler(sent)
        auth = self._auth(store)
        store.fail = True
        await self._call(auth)
        store.fail = False

        store.save(
            replace(_grant(expires_in=60_000, access="login-access-SECRET", refresh="login-refresh-SECRET"), granted_at=NOW)
        )
        self.now += 28_800
        await self._call(auth)
        self.assertEqual(sent, [OLD_REFRESH])
        self.assertEqual(self.server.mcp_bearers()[-1], "Bearer login-access-SECRET")

    async def test_new_auth_never_resends_a_dead_refresh_token(self) -> None:
        self.store.save(_grant(expires_in=0))
        sent: list[str] = []

        def rejected(request: httpx2.Request) -> httpx2.Response:
            sent.append(parse_qs(request.content.decode())["refresh_token"][0])
            return httpx2.Response(400, json={"error": "invalid_grant"})

        self.server.token_handler = rejected
        with self.assertRaises(LoginRequired):
            await self._call(self._auth())
        with self.assertRaises(LoginRequired):
            await self._call(self._auth())
        self.assertEqual(sent, [OLD_REFRESH])

    async def test_errors_contain_no_secrets(self) -> None:
        def time_out(request: httpx2.Request) -> httpx2.Response:
            raise httpx2.ReadTimeout(f"timed out {OLD_REFRESH}", request=request)

        def refuse(request: httpx2.Request) -> httpx2.Response:
            raise httpx2.ConnectError(f"refused {OLD_REFRESH}", request=request)

        echo_body = json.dumps({"error": "invalid_grant", "error_description": f"bad token {OLD_REFRESH}"})
        scenarios: list[tuple[str, Callable[[httpx2.Request], httpx2.Response], Callable[..., httpx2.Response]]] = [
            ("invalid_grant", lambda r: httpx2.Response(400, text=echo_body), _mcp_ok),
            ("5xx", lambda r: httpx2.Response(503, text=f"oops {OLD_REFRESH} {CLIENT_SECRET}"), _mcp_ok),
            ("connect", refuse, _mcp_ok),
            ("timeout", time_out, _mcp_ok),
            ("garbage-200", lambda r: httpx2.Response(200, text=f"<html>{OLD_REFRESH}</html>"), _mcp_ok),
            ("mcp-401", _token_ok, lambda r: httpx2.Response(401, text=f"no {NEW_ACCESS}")),
        ]
        for name, token_handler, mcp_handler in scenarios:
            with self.subTest(name):
                self.store.save(_grant(expires_in=0 if name != "mcp-401" else 3600))
                self.server.token_handler = token_handler
                self.server.mcp_handler = mcp_handler
                self.logs.lines.clear()
                auth = self._auth()
                with self.assertRaises(Exception) as ctx:
                    await self._call(auth)
                exc = ctx.exception
                self._assert_no_secrets(str(exc))
                self._assert_no_secrets(repr(exc))
                self._assert_no_secrets(str(exc.__cause__ or ""))
                if not exc.__suppress_context__:
                    self._assert_no_secrets(str(exc.__context__ or ""))
                self._assert_no_secrets(str(auth.last_error or ""))
                self._assert_no_secrets("\n".join(self.logs.lines))


if __name__ == "__main__":
    unittest.main()
