"""In-process fakes for MCP tests.

``FakeMcpHttpServer`` speaks just enough Streamable HTTP to drive the real MCP
SDK client through an ``httpx2.MockTransport``. Using the real transport (rather
than stubbing our own client) is what lets tests assert protocol-level facts
such as "every request in this session carried Authorization".

``FakeMcpClient`` is the cheap alternative for hub and bridge tests that care
about lifecycle and policy rather than wire format.
"""

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

import httpx2

from provider.mcp.errors import McpError, McpNotConnectedError
from provider.mcp.models import McpCallResult, McpServerInfo, McpToolDef


@dataclass
class RecordedRequest:
    """One HTTP exchange the fake server saw."""

    method: str
    url: str
    headers: Mapping[str, str]
    rpc_method: str | None = None

    @property
    def authorization(self) -> str | None:
        return self.headers.get("authorization")


def text_result(text: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    payload.update(extra)
    return payload


class FakeMcpHttpServer:
    """A minimal Streamable HTTP MCP server backed by ``httpx2.MockTransport``."""

    def __init__(
        self,
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_pages: list[list[dict[str, Any]]] | None = None,
        server_name: str = "fake-mcp",
        server_version: str = "1.2.3",
        instructions: str | None = None,
        list_changed: bool = True,
        require_token: str | None = None,
        health_status: int = 200,
        call_handler: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
        mcp_path: str = "/mcp",
    ) -> None:
        # ``tool_pages`` takes precedence and exercises cursor pagination.
        self.tool_pages = tool_pages or [tools or []]
        self.server_name = server_name
        self.server_version = server_version
        self.instructions = instructions
        self.list_changed = list_changed
        self.require_token = require_token
        self.health_status = health_status
        self.call_handler = call_handler
        self.mcp_path = mcp_path

        self.requests: list[RecordedRequest] = []
        self.session_id = "fake-session-1"
        self.list_tools_calls = 0

    # ---- assertions helpers -------------------------------------------

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self._handle)

    def transport_factory(self) -> Callable[[], httpx2.MockTransport]:
        return self.transport

    def mcp_requests(self) -> list[RecordedRequest]:
        """Requests against the MCP endpoint (health probes excluded)."""
        return [r for r in self.requests if r.url.endswith(self.mcp_path)]

    def rpc_methods(self) -> list[str]:
        return [r.rpc_method for r in self.requests if r.rpc_method]

    def set_tools(self, tools: list[dict[str, Any]]) -> None:
        self.tool_pages = [tools]

    # ---- transport handler ---------------------------------------------

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        headers = {k.lower(): v for k, v in request.headers.items()}
        record = RecordedRequest(method=request.method, url=str(request.url), headers=headers)
        self.requests.append(record)

        if not str(request.url).endswith(self.mcp_path):
            # Health endpoint: deliberately unauthenticated.
            return httpx2.Response(self.health_status, text="ok")

        if self.require_token is not None:
            if headers.get("authorization") != f"Bearer {self.require_token}":
                return httpx2.Response(401, json={"error": "unauthorized"})

        if request.method == "GET":
            # This fake does not offer the server-initiated GET stream.
            return httpx2.Response(405, text="method not allowed")
        if request.method == "DELETE":
            return httpx2.Response(200, text="")
        if request.method != "POST":  # pragma: no cover - defensive
            return httpx2.Response(405, text="method not allowed")

        body = json.loads(request.content or b"{}")
        rpc_method = body.get("method")
        record.rpc_method = rpc_method
        rpc_id = body.get("id")
        params = body.get("params") or {}

        if rpc_method == "initialize":
            return self._initialize_response(rpc_id, params)
        if isinstance(rpc_method, str) and rpc_method.startswith("notifications/"):
            return httpx2.Response(202, text="")
        if rpc_method == "tools/list":
            return self._list_tools_response(rpc_id, params)
        if rpc_method == "tools/call":
            return self._call_tool_response(rpc_id, params)
        return self._error(rpc_id, -32601, f"method not found: {rpc_method}")

    def _json_rpc(self, rpc_id: Any, result: dict[str, Any], **kwargs) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={"jsonrpc": "2.0", "id": rpc_id, "result": result},
            headers={"content-type": "application/json", **kwargs.pop("headers", {})},
            **kwargs,
        )

    def _error(self, rpc_id: Any, code: int, message: str) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": rpc_id,
                "error": {"code": code, "message": message},
            },
            headers={"content-type": "application/json"},
        )

    def _initialize_response(self, rpc_id: Any, params: dict) -> httpx2.Response:
        result: dict[str, Any] = {
            # Echo the client's requested version so the handshake always agrees.
            "protocolVersion": params.get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {"listChanged": self.list_changed}},
            "serverInfo": {"name": self.server_name, "version": self.server_version},
        }
        if self.instructions is not None:
            result["instructions"] = self.instructions
        return self._json_rpc(rpc_id, result, headers={"mcp-session-id": self.session_id})

    def _list_tools_response(self, rpc_id: Any, params: dict) -> httpx2.Response:
        self.list_tools_calls += 1
        cursor = params.get("cursor")
        index = int(cursor) if cursor is not None else 0
        if index >= len(self.tool_pages):  # pragma: no cover - defensive
            return self._error(rpc_id, -32602, "bad cursor")
        result: dict[str, Any] = {"tools": self.tool_pages[index]}
        if index + 1 < len(self.tool_pages):
            result["nextCursor"] = str(index + 1)
        return self._json_rpc(rpc_id, result)

    def _call_tool_response(self, rpc_id: Any, params: dict) -> httpx2.Response:
        name = params.get("name", "")
        arguments = params.get("arguments") or {}
        if self.call_handler is None:
            return self._json_rpc(rpc_id, text_result(f"called {name}"))
        try:
            return self._json_rpc(rpc_id, self.call_handler(name, arguments))
        except _RawResponse as raw:
            return raw.response


class _RawResponse(Exception):
    """Lets a ``call_handler`` return a non-standard HTTP response."""

    def __init__(self, response: httpx2.Response) -> None:
        self.response = response


def raise_response(response: httpx2.Response) -> None:
    raise _RawResponse(response)


def tool_spec(
    name: str,
    *,
    description: str = "",
    properties: dict[str, Any] | None = None,
    required: list[str] | None = None,
    annotations: dict[str, Any] | None = None,
    output_schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a ``tools/list`` entry in the server's wire shape."""
    spec: dict[str, Any] = {
        "name": name,
        "description": description or f"{name} description",
        "inputSchema": {
            "type": "object",
            "properties": properties if properties is not None else {},
            "required": required or [],
        },
    }
    if annotations is not None:
        spec["annotations"] = annotations
    if output_schema is not None:
        spec["outputSchema"] = output_schema
    return spec


@dataclass
class FakeMcpClient:
    """A hub-level fake: no HTTP, deterministic outcomes, records lifecycle calls."""

    server_id: str
    tools: tuple[McpToolDef, ...] = ()
    info: McpServerInfo | None = None
    health_error: Exception | None = None
    connect_error: Exception | None = None
    list_error: Exception | None = None
    call_error: Exception | None = None
    call_result: McpCallResult | None = None
    call_delay: float = 0.0

    connected: bool = False
    close_count: int = 0
    health_probes: int = 0
    connect_calls: int = 0
    list_calls: int = 0
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    max_observed_concurrency: int = 0
    _in_flight: int = 0
    _tools_changed: Callable[[str], None] | None = None

    @property
    def is_connected(self) -> bool:
        return self.connected

    @property
    def server_info(self) -> McpServerInfo | None:
        return self.info if self.connected else None

    def set_tools_changed_callback(self, callback) -> None:
        self._tools_changed = callback

    def emit_tools_changed(self) -> None:
        if self._tools_changed is not None:
            self._tools_changed(self.server_id)

    async def probe_health(self) -> None:
        self.health_probes += 1
        if self.health_error is not None:
            raise self.health_error

    async def connect(self) -> McpServerInfo:
        self.connect_calls += 1
        if self.connect_error is not None:
            raise self.connect_error
        self.connected = True
        if self.info is None:
            self.info = McpServerInfo(server_id=self.server_id, name=self.server_id, version="1.0")
        return self.info

    async def list_tools(self) -> tuple[McpToolDef, ...]:
        self.list_calls += 1
        if not self.connected:
            raise McpNotConnectedError(f"[{self.server_id}] is not connected")
        if self.list_error is not None:
            raise self.list_error
        return self.tools

    async def call_tool(self, tool_name: str, arguments: Mapping[str, Any] | None = None) -> McpCallResult:
        import asyncio

        if not self.connected:
            raise McpNotConnectedError(f"[{self.server_id}] is not connected")
        self.calls.append((tool_name, dict(arguments or {})))
        self._in_flight += 1
        self.max_observed_concurrency = max(self.max_observed_concurrency, self._in_flight)
        try:
            if self.call_delay:
                await asyncio.sleep(self.call_delay)
            if self.call_error is not None:
                raise self.call_error
            if self.call_result is not None:
                return self.call_result
            return McpCallResult(
                server_id=self.server_id,
                tool_name=tool_name,
                text_blocks=(f"{tool_name} ok",),
            )
        finally:
            self._in_flight -= 1

    async def close(self) -> None:
        self.close_count += 1
        self.connected = False


def fake_tool(
    server_id: str,
    name: str,
    *,
    description: str = "",
    properties: dict[str, Any] | None = None,
    annotations: dict[str, Any] | None = None,
) -> McpToolDef:
    return McpToolDef(
        server_id=server_id,
        name=name,
        description=description or f"{name} description",
        input_schema={
            "type": "object",
            "properties": properties if properties is not None else {},
        },
        annotations=annotations,
    )


__all__ = [
    "FakeMcpClient",
    "FakeMcpHttpServer",
    "McpError",
    "RecordedRequest",
    "fake_tool",
    "raise_response",
    "text_result",
    "tool_spec",
]
