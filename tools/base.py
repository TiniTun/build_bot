"""Base tool interface and decorator."""

import asyncio
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from core.agent import AgentSession


class ToolErrorCode(str, Enum):
    """Stable error codes for failed tool calls."""

    PERMISSION_DENIED = "permission_denied"
    AUTH_MISSING = "auth_missing"
    RATE_LIMITED = "rate_limited"
    INVALID_ARGS = "invalid_args"
    NOT_FOUND = "not_found"
    PROVIDER_ERROR = "provider_error"


@dataclass(frozen=True)
class ToolError:
    """Machine-readable tool error."""

    code: ToolErrorCode
    message: str
    retryable: bool = False
    user_action: str | None = None
    details: dict[str, Any] | None = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code.value,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.user_action:
            payload["user_action"] = self.user_action
        if self.details:
            payload["details"] = self.details
        return payload


@dataclass(frozen=True)
class ToolResult:
    """Normalized result returned to the LLM after a tool call."""

    ok: bool
    content: str = ""
    error_value: ToolError | None = None

    @classmethod
    def success(cls, content: str) -> "ToolResult":
        return cls(ok=True, content=content)

    @classmethod
    def error(
        cls,
        code: ToolErrorCode,
        message: str,
        *,
        retryable: bool = False,
        user_action: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> "ToolResult":
        return cls(
            ok=False,
            error_value=ToolError(
                code=code,
                message=message,
                retryable=retryable,
                user_action=user_action,
                details=details,
            ),
        )

    def to_tool_content(self) -> str:
        if self.ok:
            return self.content
        if self.error_value is None:
            raise ValueError("ToolResult error is missing error_value")
        return json.dumps(
            {"ok": False, "error": self.error_value.to_payload()},
            sort_keys=True,
        )

    def __str__(self) -> str:
        return self.to_tool_content()


class BaseTool(ABC):
    """Abstract base class for all tools."""

    name: str
    description: str
    parameters: dict[str, Any]

    @abstractmethod
    async def execute(self, session: "AgentSession", **kwargs: Any) -> str:
        """Execute the tool."""

    def get_tool_schema(self) -> dict[str, Any]:
        """Get the tool/function schema for LiteLLM."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def tool(name: str, description: str, parameters: dict[str, Any]) -> Callable:
    """Decorator to register a function as a tool."""

    def decorator(func: Callable) -> "FunctionTool":
        return FunctionTool(name, description, parameters, func)
    
    return decorator
    


class FunctionTool(BaseTool):
    """A tool created from a function using the @tool decorator."""

    def __init__(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        func: Callable,
    ):
        self.name = name
        self.description = description
        self.parameters = parameters
        self._func = func

    async def execute(self, session: "AgentSession", **kwargs: Any) -> str:
        """Execute the underlying function."""
        result = self._func(session=session, **kwargs)
        if asyncio.iscoroutine(result):
            result = await result
        if isinstance(result, ToolResult):
            return result.to_tool_content()
        return str(result)
