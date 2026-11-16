"""``mcp-login``: interactive OAuth login for a hosted MCP server.

Meant to run on the headless host over ``ssh -L <port>:127.0.0.1:<port>``: the
command prints the authorization URL, the operator opens it in a local browser,
and the redirect to ``http://127.0.0.1:<port>/callback`` reaches this process
through the tunnel. The loopback server binds 127.0.0.1 only, accepts exactly
one callback whose ``state`` matches, and shuts down.

The OAuth protocol itself (discovery, registration, PKCE, code exchange) is the
MCP SDK's, driven through ``provider.mcp.client.authorize_interactively``. This
module owns the loopback callback, the registration-reuse decision and the
token store. Tokens are never printed.
"""

import asyncio
import json
import os
import secrets
import time
from typing import Callable
from urllib.parse import parse_qs, urlsplit

import httpx2
import typer

from provider.mcp.client import authorize_interactively
from provider.mcp.errors import McpConfigError, McpError, McpOAuthClientRejectedError
from provider.mcp.models import McpCallResult, OAuthCallback, OAuthClientRegistration, OAuthLoginResult
from provider.mcp.oauth import DEFAULT_EXPIRES_IN
from provider.mcp.oauth_store import OAuthGrant, OAuthStore
from utils.config import Config
from utils.logging import setup_logging
from utils.mcp_config import McpServerConfig

CALLBACK_PATH = "/callback"
# Called once with the new token to show which workspace the grant belongs to.
IDENTITY_TOOL = "notion-get-self"
DEFAULT_TIMEOUT_SECONDS = 600.0
_REQUEST_READ_TIMEOUT = 10.0
_MAX_SHOWN_CHARS = 200


class LoginError(Exception):
    """The login failed; the message is one secret-free line for the operator."""


def _printable(value: str) -> str:
    """Neutralize a peer-supplied string before it reaches the terminal."""
    cleaned = "".join(ch if ch.isprintable() else "?" for ch in value)
    return cleaned[:_MAX_SHOWN_CHARS]


class _CallbackServer:
    """One-shot loopback HTTP server for the authorization redirect."""

    def __init__(self, port: int) -> None:
        self._port = port
        self._expected_state: str | None = None
        self._result: asyncio.Future[OAuthCallback] = asyncio.get_running_loop().create_future()
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        try:
            self._server = await asyncio.start_server(self._handle, "127.0.0.1", self._port)
        except OSError as exc:
            raise LoginError(f"cannot listen on 127.0.0.1:{self._port}: {exc.strerror or exc}") from None

    def expect_state(self, state: str | None) -> None:
        self._expected_state = state

    async def wait(self) -> OAuthCallback:
        return await self._result

    async def close(self) -> None:
        if not self._result.done():
            self._result.cancel()
        elif not self._result.cancelled():
            # Mark an unawaited failure as retrieved.
            self._result.exception()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            async with asyncio.timeout(_REQUEST_READ_TIMEOUT):
                request_line = (await reader.readline()).decode("latin-1")
                while (await reader.readline()) not in (b"\r\n", b"\n", b""):
                    pass
            parts = request_line.split(" ")
            target = urlsplit(parts[1]) if len(parts) >= 3 and parts[0] == "GET" else None
            if target is None or target.path != CALLBACK_PATH:
                await self._respond(writer, 404, "Not found.")
                return
            if self._result.done():
                await self._respond(writer, 410, "This login has already finished.")
                return
            status, page, outcome = self._evaluate(parse_qs(target.query))
            await self._respond(writer, status, page)
            if isinstance(outcome, OAuthCallback):
                self._result.set_result(outcome)
            else:
                self._result.set_exception(outcome)
        except (TimeoutError, OSError, ValueError):
            pass
        finally:
            writer.close()

    def _evaluate(self, query: dict[str, list[str]]) -> tuple[int, str, OAuthCallback | Exception]:
        state = query.get("state", [""])[0]
        expected = self._expected_state
        if expected is None or not secrets.compare_digest(state.encode(), expected.encode()):
            return (
                400,
                "Login failed: unexpected state.",
                LoginError("callback state does not match the authorization request"),
            )
        if "error" in query:
            error = _printable(query["error"][0])
            description = _printable(query.get("error_description", [""])[0])
            reason = f"authorization failed: {error}" + (f" ({description})" if description else "")
            failure: Exception = (
                McpOAuthClientRejectedError(reason) if error == "invalid_client" else LoginError(reason)
            )
            return 400, "Login failed: the authorization server returned an error.", failure
        code = query.get("code", [""])[0]
        if not code:
            return 400, "Login failed: no authorization code.", LoginError("callback carried no authorization code")
        iss = query.get("iss", [None])[0]
        return 200, "Login received. You can close this tab.", OAuthCallback(code=code, state=state, iss=iss)

    @staticmethod
    async def _respond(writer: asyncio.StreamWriter, status: int, text: str) -> None:
        reason = {200: "OK", 400: "Bad Request", 404: "Not Found", 410: "Gone"}[status]
        body = text.encode()
        writer.write(
            f"HTTP/1.1 {status} {reason}\r\nContent-Type: text/plain; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            + body
        )
        await writer.drain()


def _resolve_url(server_id: str, config: McpServerConfig) -> str:
    if config.url is not None:
        return config.url
    assert config.url_env is not None  # enforced by config validation
    value = os.environ.get(config.url_env)
    if not value:
        raise LoginError(f"MCP server '{server_id}': environment variable {config.url_env} is not set")
    return value


def _reusable_registration(store: OAuthStore, redirect_uri: str) -> OAuthClientRegistration | None:
    """The stored client registration, unless missing, unreadable or for another redirect URI."""
    try:
        grant = store.load()
    except McpConfigError as exc:
        print(f"Ignoring the existing token store: {exc}")
        return None
    if grant is None or grant.redirect_uri != redirect_uri:
        return None
    return OAuthClientRegistration(client_id=grant.client_id, client_secret=grant.client_secret)


async def _attempt(
    server_id: str,
    url: str,
    port: int,
    registration: OAuthClientRegistration | None,
    *,
    open_url: Callable[[str], None],
    transport: httpx2.AsyncBaseTransport | None,
    request_timeout: float,
) -> OAuthLoginResult:
    callback = _CallbackServer(port)
    await callback.start()

    async def on_redirect(authorization_url: str) -> None:
        state = parse_qs(urlsplit(authorization_url).query).get("state", [None])[0]
        callback.expect_state(state)
        open_url(authorization_url)
        print(f"Waiting for the browser callback on 127.0.0.1:{port} ...", flush=True)

    try:
        return await authorize_interactively(
            server_id,
            url,
            redirect_uri=f"http://127.0.0.1:{port}{CALLBACK_PATH}",
            registration=registration,
            redirect_handler=on_redirect,
            callback_handler=callback.wait,
            transport=transport,
            identity_tool=IDENTITY_TOOL,
            request_timeout_seconds=request_timeout,
        )
    finally:
        await callback.close()


def _workspace_name(identity: McpCallResult | None) -> str | None:
    if identity is None or identity.is_error:
        return None
    for text in identity.text_blocks:
        try:
            data = json.loads(text)
        except ValueError:
            continue
        workspace = data.get("workspace") if isinstance(data, dict) else None
        name = workspace.get("name") if isinstance(workspace, dict) else None
        if isinstance(name, str) and name:
            return _printable(name)
    return None


async def run_login(
    server_id: str,
    config: McpServerConfig,
    *,
    open_url: Callable[[str], None],
    transport: httpx2.AsyncBaseTransport | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> OAuthGrant:
    """Authorize interactively, save the grant to the server's token store, return it.

    Reuses the stored client registration unless it is missing, was made for a
    different redirect URI, or the server rejects it (then registers once more).
    Raises ``LoginError`` with a one-line reason on any failure.
    """
    if config.auth is None:
        raise LoginError(f"MCP server '{server_id}' does not use OAuth")
    url = _resolve_url(server_id, config)
    port = config.auth.callback_port
    redirect_uri = f"http://127.0.0.1:{port}{CALLBACK_PATH}"
    store = OAuthStore(config.auth.token_store)
    registration = _reusable_registration(store, redirect_uri)
    print(
        "Reusing the stored client registration." if registration else "A new client will be registered.",
        flush=True,
    )

    def attempt(reg: OAuthClientRegistration | None):
        return _attempt(
            server_id,
            url,
            port,
            reg,
            open_url=open_url,
            transport=transport,
            request_timeout=config.read_timeout_seconds,
        )

    try:
        async with asyncio.timeout(timeout):
            try:
                result = await attempt(registration)
            except McpOAuthClientRejectedError:
                if registration is None:
                    raise
                print("The stored client registration was rejected; registering a new client.", flush=True)
                result = await attempt(None)
    except TimeoutError:
        raise LoginError(f"timed out after {timeout:g}s waiting for the login to finish") from None
    except McpError as exc:
        raise LoginError(str(exc)) from None

    if not result.refresh_token:
        raise LoginError("the authorization server issued no refresh token")
    now = time.time()
    expires_in = result.expires_in if result.expires_in is not None else DEFAULT_EXPIRES_IN
    grant = OAuthGrant(
        client_id=result.registration.client_id,
        client_secret=result.registration.client_secret,
        redirect_uri=redirect_uri,
        token_endpoint=result.token_endpoint,
        resource=result.resource,
        access_token=result.access_token,
        refresh_token=result.refresh_token,
        expires_at=now + expires_in,
        granted_at=now,
    )
    try:
        # The lock keeps a running server's refresh from interleaving with this write.
        async with store.locked():
            store.save(grant)
    except (McpError, OSError) as exc:
        raise LoginError(f"cannot write token store {store.path}: {type(exc).__name__}") from None

    workspace = _workspace_name(result.identity)
    if workspace is not None:
        print(f"Connected to Notion workspace: {workspace}")
    else:
        reason = result.identity_error or f"{IDENTITY_TOOL} returned no workspace name"
        print(f"Connected, but the workspace could not be read: {_printable(reason)}")
    print(f"Token store: {store.path}")
    return grant


def _print_url(url: str) -> None:
    print("Open this URL in a browser (the redirect reaches this host through the SSH tunnel):", flush=True)
    print(url, flush=True)


def mcp_login_command(ctx: typer.Context, server_id: str, timeout: float) -> None:
    """Typer entry point: resolve the server, run the login, map failures to an exit code."""
    config: Config = ctx.obj["config"]
    servers = config.mcp.servers if config.mcp is not None else {}
    server = servers.get(server_id)
    if server is None:
        typer.echo(f"mcp-login: unknown MCP server '{server_id}'", err=True)
        raise typer.Exit(2)
    if server.auth is None:
        typer.echo(f"mcp-login: MCP server '{server_id}' does not use OAuth", err=True)
        raise typer.Exit(2)

    # SDK diagnostics (scrubbed) go to the log file; the console gets one line.
    setup_logging(config, console_output=False)
    try:
        asyncio.run(run_login(server_id, server, open_url=_print_url, timeout=timeout))
    except LoginError as exc:
        typer.echo(f"mcp-login: {exc}", err=True)
        raise typer.Exit(1)
    except KeyboardInterrupt:
        typer.echo("mcp-login: interrupted", err=True)
        raise typer.Exit(130)
