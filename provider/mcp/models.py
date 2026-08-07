"""Immutable, SDK-free models for MCP discovery and invocation results.

Everything above ``provider/mcp`` works with these types only. Two rules shape
them:

* model-visible content is kept strictly separate from private metadata, so a
  server cannot smuggle instructions or credentials into a prompt through
  ``_meta``;
* server-supplied ``annotations``/``description``/``instructions`` are retained
  for diagnostics but carry no authority.
"""

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping


def _freeze(value: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    """Return a read-only view so callers cannot mutate a shared snapshot."""
    if value is None:
        return None
    return MappingProxyType(dict(value))


@dataclass(frozen=True)
class McpToolDef:
    """One remote tool exactly as advertised, before any local policy applies."""

    server_id: str
    name: str
    description: str = ""
    title: str | None = None
    input_schema: Mapping[str, Any] = field(default_factory=dict)
    output_schema: Mapping[str, Any] | None = None
    # Untrusted server hints. Diagnostics only: they never grant access, lower
    # risk, enable retry, or bypass confirmation.
    annotations: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_schema", _freeze(self.input_schema) or {})
        object.__setattr__(self, "output_schema", _freeze(self.output_schema))
        object.__setattr__(self, "annotations", _freeze(self.annotations))


@dataclass(frozen=True)
class McpServerInfo:
    """Identity and capabilities reported during initialization."""

    server_id: str
    name: str = ""
    version: str = ""
    protocol_version: str = ""
    # Raw instructions as sent. They stay out of prompts unless the server's
    # local config explicitly opts in, and are truncated when it does.
    instructions: str | None = None
    supports_tools: bool = False
    supports_tool_list_changed: bool = False


@dataclass(frozen=True)
class McpCallResult:
    """A normalized ``tools/call`` result.

    ``text_blocks`` and ``structured_content`` are the only model-visible parts.
    ``private_meta`` holds ``_meta`` and any non-text content descriptors; it is
    for host-side diagnostics and must never be rendered into a prompt.
    """

    server_id: str
    tool_name: str
    text_blocks: tuple[str, ...] = ()
    structured_content: Mapping[str, Any] | None = None
    is_error: bool = False
    private_meta: Mapping[str, Any] = field(default_factory=dict)
    # Content blocks we deliberately do not forward (images, audio, resources).
    dropped_content_types: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "structured_content", _freeze(self.structured_content))
        object.__setattr__(self, "private_meta", _freeze(self.private_meta) or {})

    @property
    def is_empty(self) -> bool:
        return not self.text_blocks and self.structured_content is None
