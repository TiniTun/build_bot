"""Base LLM provider abstraction."""

from dataclasses import dataclass
from typing import Any, Optional, cast

import litellm
from litellm import acompletion, Choices, TYPE_CHECKING
from litellm.types.completion import ChatCompletionMessageParam as Message

if TYPE_CHECKING:
    from utils.config import LLMConfig

# Drop params a given model does not support (e.g. gpt-5 rejects temperature!=1)
# instead of erroring, so a single config works across heterogeneous models.
litellm.drop_params = True


@dataclass
class LLMToolCall:
    """A tool/function call from the LLM."""

    id: str
    name: str
    arguments: str # JSON string


class LLMProvider:
    """LLM provider using litellm for multi-provider support."""

    def __init__(
        self,
        model: str,
        api_key: str,
        api_base: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 2048,
        **kwargs: Any,
        ):
        """Initialize LLM provider."""
        self.model = model
        self.api_key = api_key
        self.api_base = api_base
        self.temperature = temperature
        self.max_tokens = max_tokens
        self._settings = kwargs

    @classmethod
    def from_config(cls, config: "LLMConfig") -> "LLMProvider":
        """Create provider from LLMConfig.

        Provider-specific parameters from ``config.extra`` are forwarded to
        LiteLLM on every request, so Anthropic, OpenAI, and others share the
        same flow without a provider registry.
        """
        return cls(
            model=config.model,
            api_key=config.api_key,
            api_base=config.api_base,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            **config.extra,
        )

    async def chat(
        self,
        messages: list[Message],
        tools: Optional[list[dict[str, Any]]] = None,
        **kwargs: Any,
    ) -> tuple[str, list[LLMToolCall]]:
        """Default implementation using litellm. Subclasses can override."""
        request_kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "api_key": self.api_key,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }

        if self.api_base:
            request_kwargs["api_base"] = self.api_base
        if tools:
            request_kwargs["tools"] = tools
        # Provider-specific extras (from LLMConfig.extra), then per-call overrides.
        request_kwargs.update(self._settings)
        request_kwargs.update(kwargs)

        response = await acompletion(**request_kwargs)

        message = cast(Choices, response.choices[0]).message

        return (
            message.content or "",
            [
                LLMToolCall(
                    id=tc["id"],
                    name=tc["function"]["name"],
                    arguments=tc["function"]["arguments"],
                )
                for tc in (message.tool_calls or [])
            ],
        )