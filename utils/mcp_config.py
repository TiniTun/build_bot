"""Typed configuration for MCP (Model Context Protocol) client servers.

Lives beside ``utils/config.py`` and is imported by ``Config`` so the main config
module stays readable. These models are *pure configuration*: they hold no
runtime connection state, resolve no environment variables, and open no sockets.
Secret material is referenced by environment-variable name only.

Local configuration is authoritative. Anything a remote MCP server reports
(annotations, descriptions, instructions, newly added tools) is an untrusted
hint and can never widen what these models allow.
"""

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Server ids are stable lowercase slugs; they become part of capability ids
# (``mcp.<server_id>.<tool>``) and of the LLM-visible tool name, so they must be
# restricted to characters that are safe in both.
SERVER_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")

# Remote tool names are matched exactly against what the server advertises. We
# do not constrain the server's naming, but we do reject blank/whitespace names
# so an empty allowlist entry can never match anything by accident.
_BLANK = re.compile(r"^\s*$")

# Upper bounds keep a typo (e.g. milliseconds pasted into a seconds field) from
# turning into an effectively infinite hang or an unbounded model-visible blob.
MAX_TIMEOUT_SECONDS = 600.0
MAX_CONCURRENT_CALLS = 64
MAX_RESULT_CHARS = 200_000

McpToolEffect = Literal["read", "write", "destructive"]
McpToolConfirmation = Literal["none", "host_before_call", "server_challenge"]
McpToolRetry = Literal["safe", "never"]


class McpToolPolicy(BaseModel):
    """Local per-tool policy overriding anything the server claims about a tool.

    Defaults are the safest option: a tool is treated as a read that is never
    retried. ``effect`` and ``confirmation`` drive the capability risk level in
    the bridge layer; ``retry`` is deliberately ``never`` by default so an
    ambiguous transport failure after a mutation is never replayed.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    effect: McpToolEffect = "read"
    confirmation: McpToolConfirmation = "none"
    retry: McpToolRetry = "never"

    @model_validator(mode="after")
    def retry_not_allowed_for_mutations(self) -> "McpToolPolicy":
        if self.effect in ("write", "destructive") and self.retry == "safe":
            raise ValueError(
                "retry: safe is not allowed for effect 'write' or 'destructive'; "
                "a mutation must never be replayed after an ambiguous failure"
            )
        return self


class McpServerConfig(BaseModel):
    """One configured MCP server.

    Exactly one of ``url`` and ``url_env`` must be given, so an endpoint can be
    committed for a fixed deployment or injected per environment. No literal
    token field exists: ``token_env`` names the environment variable that holds
    the Bearer token, and it is read only at connect time.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    transport: Literal["streamable_http"] = "streamable_http"

    url: str | None = None
    url_env: str | None = None

    # Optional unauthenticated liveness probe performed before MCP initialization.
    # ``health_path`` is resolved against the MCP URL's origin; ``health_url`` is
    # an absolute override for servers that expose health elsewhere.
    health_path: str | None = None
    health_url: str | None = None

    token_env: str | None = None

    connect_timeout_seconds: float = Field(default=10.0, gt=0, le=MAX_TIMEOUT_SECONDS)
    read_timeout_seconds: float = Field(default=30.0, gt=0, le=MAX_TIMEOUT_SECONDS)
    discovery_timeout_seconds: float = Field(default=20.0, gt=0, le=MAX_TIMEOUT_SECONDS)
    health_timeout_seconds: float = Field(default=5.0, gt=0, le=MAX_TIMEOUT_SECONDS)

    # A required server that cannot be brought up fails application startup.
    # Optional servers degrade on their own without taking the bot down.
    required: bool = False

    # Bounded exponential backoff for restoring *future* calls. Reconnecting
    # never replays a call that already failed.
    reconnect_initial_seconds: float = Field(default=2.0, gt=0, le=MAX_TIMEOUT_SECONDS)
    reconnect_max_seconds: float = Field(default=60.0, gt=0, le=MAX_TIMEOUT_SECONDS)

    max_concurrent_calls: int = Field(default=4, ge=1, le=MAX_CONCURRENT_CALLS)
    max_result_chars: int = Field(default=20_000, ge=1, le=MAX_RESULT_CHARS)

    # Fail-closed discovery: only exact names listed here are ever exposed.
    # ``denied_tools`` is a redundant explicit guard for tools that must stay
    # hidden even if someone later adds them to the allowlist by mistake.
    allowed_tools: list[str] = Field(default_factory=list)
    denied_tools: list[str] = Field(default_factory=list)

    tool_policies: dict[str, McpToolPolicy] = Field(default_factory=dict)

    # Server-supplied instructions are untrusted; keep them out of prompts unless
    # an operator explicitly opts in.
    use_server_instructions: bool = False
    max_instructions_chars: int = Field(default=2_000, ge=1, le=MAX_RESULT_CHARS)

    @model_validator(mode="before")
    @classmethod
    def reject_literal_secrets(cls, data: Any) -> Any:
        """Fail loudly on inline credentials instead of on a generic 'extra field'."""
        if not isinstance(data, dict):
            return data
        for key in ("token", "api_key", "bearer_token", "authorization", "secret"):
            if key in data:
                raise ValueError(
                    f"'{key}' is not accepted in MCP server config; reference the "
                    "environment variable holding the secret via 'token_env'"
                )
        return data

    @field_validator("url", "health_url")
    @classmethod
    def must_be_http_url(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not v.startswith(("http://", "https://")):
            raise ValueError("must be an http:// or https:// URL")
        return v

    @field_validator("url_env", "token_env")
    @classmethod
    def env_name_must_be_plausible(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", v):
            raise ValueError("must be an environment variable *name* (e.g. MOVIES_DB_TOKEN), not a value")
        return v

    @field_validator("health_path")
    @classmethod
    def health_path_must_be_absolute(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not v.startswith("/"):
            raise ValueError("health_path must start with '/' (e.g. /health)")
        return v

    @field_validator("allowed_tools", "denied_tools")
    @classmethod
    def tool_names_must_be_unique_and_non_blank(cls, v: list[str]) -> list[str]:
        seen: set[str] = set()
        for name in v:
            if _BLANK.match(name):
                raise ValueError("tool names must not be blank")
            if name in seen:
                raise ValueError(f"duplicate tool name: {name}")
            seen.add(name)
        return v

    @model_validator(mode="after")
    def validate_server(self) -> "McpServerConfig":
        if (self.url is None) == (self.url_env is None):
            raise ValueError("set exactly one of 'url' or 'url_env'")
        if self.health_url is not None and self.health_path is not None:
            raise ValueError("set at most one of 'health_url' or 'health_path'")
        if self.reconnect_max_seconds < self.reconnect_initial_seconds:
            raise ValueError("reconnect_max_seconds must be >= reconnect_initial_seconds")

        contradictory = sorted(set(self.allowed_tools) & set(self.denied_tools))
        if contradictory:
            raise ValueError("tools listed in both allowed_tools and denied_tools: " + ", ".join(contradictory))

        # A policy for a tool that is not exposed is dead configuration and is
        # more likely a typo than intent, so surface it instead of ignoring it.
        orphan = sorted(set(self.tool_policies) - set(self.allowed_tools))
        if orphan:
            raise ValueError("tool_policies entries have no matching allowed_tools entry: " + ", ".join(orphan))
        return self

    def policy_for(self, remote_tool_name: str) -> McpToolPolicy:
        """Local policy for a remote tool; safe defaults when unspecified."""
        return self.tool_policies.get(remote_tool_name, McpToolPolicy())

    def is_tool_allowed(self, remote_tool_name: str) -> bool:
        """Fail-closed allowlist check for an exact remote tool name."""
        if remote_tool_name in self.denied_tools:
            return False
        return remote_tool_name in self.allowed_tools


class McpConfig(BaseModel):
    """Top-level MCP block: a mapping of stable server ids to server configs."""

    model_config = ConfigDict(extra="forbid")

    servers: dict[str, McpServerConfig] = Field(default_factory=dict)

    @field_validator("servers")
    @classmethod
    def server_ids_must_be_slugs(cls, v: dict[str, McpServerConfig]) -> dict[str, McpServerConfig]:
        for server_id in v:
            if not SERVER_ID_PATTERN.match(server_id):
                raise ValueError(
                    f"invalid MCP server id '{server_id}': must be a lowercase slug matching [a-z][a-z0-9_]*"
                )
        return v

    def enabled_servers(self) -> dict[str, McpServerConfig]:
        """Configured servers that are switched on."""
        return {sid: cfg for sid, cfg in self.servers.items() if cfg.enabled}


def parse_mcp_config(raw: Any) -> McpConfig | None:
    """Validate a raw ``mcp:`` config block. ``None``/absent preserves legacy behavior."""
    if raw is None:
        return None
    return McpConfig.model_validate(raw)
