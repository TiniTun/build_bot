"""Transport-independent messages and responses used by the agent runtime."""

from dataclasses import dataclass, field
from typing import Any, Iterator, TypedDict


class ResponseCursorNotFound(RuntimeError):
    """The server can no longer continue from ``previous_response_id``."""


class Message(TypedDict, total=False):
    role: str
    content: Any
    tool_calls: list[dict[str, Any]]
    tool_call_id: str


@dataclass
class LLMToolCall:
    id: str
    name: str
    arguments: str


@dataclass
class LLMResponse:
    content: str
    tool_calls: list[LLMToolCall]
    response_id: str | None = None
    response_status: str | None = None
    usage: dict[str, int | None] = field(default_factory=dict)

    def __iter__(self) -> Iterator[Any]:
        # Preserve the established ``content, tool_calls = await chat(...)`` API.
        yield self.content
        yield self.tool_calls
