"""Base LLM provider abstraction."""

import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, cast

import litellm
from litellm import acompletion, Choices, TYPE_CHECKING
from litellm.types.completion import ChatCompletionMessageParam as Message

from provider.llm import trace

if TYPE_CHECKING:
    from utils.config import LLMConfig, LLMDebugConfig

logger = logging.getLogger("provider.llm")

# Drop params a given model does not support (e.g. gpt-5 rejects temperature!=1)
# instead of erroring, so a single config works across heterogeneous models.
litellm.drop_params = True


@dataclass
class LLMToolCall:
    """A tool/function call from the LLM."""

    id: str
    name: str
    arguments: str # JSON string


def _supports_custom_temperature(model: str) -> bool:
    """Return whether the configured model accepts non-default temperature."""
    model_name = model.rsplit("/", 1)[-1].lower()
    return not model_name.startswith("gpt-5")


class LLMProvider:
    """LLM provider using litellm for multi-provider support."""

    def __init__(
        self,
        model: str,
        api_key: str,
        api_base: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 2048,
        debug: Optional["LLMDebugConfig"] = None,
        logging_path: Optional[Path] = None,
        agent_id: Optional[str] = None,
        **kwargs: Any,
        ):
        """Initialize LLM provider."""
        self.model = model
        self.api_key = api_key
        self.api_base = api_base
        self.temperature = temperature
        self.max_tokens = max_tokens
        # Trace context (optional; tracing is off unless debug.enabled).
        self._debug = debug
        self._logging_path = logging_path
        self._agent_id = agent_id
        self._settings = kwargs

    @classmethod
    def from_config(
        cls,
        config: "LLMConfig",
        *,
        logging_path: Optional[Path] = None,
        agent_id: Optional[str] = None,
    ) -> "LLMProvider":
        """Create provider from LLMConfig.

        Provider-specific parameters from ``config.extra`` are forwarded to
        LiteLLM on every request, so Anthropic, OpenAI, and others share the
        same flow without a provider registry. ``debug`` is a known field and is
        kept out of ``extra``, so it never reaches LiteLLM.
        """
        return cls(
            model=config.model,
            api_key=config.api_key,
            api_base=config.api_base,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            debug=config.debug,
            logging_path=logging_path,
            agent_id=agent_id,
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
            "max_tokens": self.max_tokens,
        }
        if _supports_custom_temperature(self.model):
            request_kwargs["temperature"] = self.temperature

        if self.api_base:
            request_kwargs["api_base"] = self.api_base
        if tools:
            request_kwargs["tools"] = tools
        # Reserved trace metadata; popped so it is never forwarded to LiteLLM.
        trace_session_id = kwargs.pop("trace_session_id", None)

        # Provider-specific extras (from LLMConfig.extra), then per-call overrides.
        request_kwargs.update(self._settings)
        request_kwargs.update(kwargs)

        debug_on = bool(
            self._debug
            and self._debug.enabled
            and self._logging_path is not None
        )
        trace_id = uuid.uuid4().hex
        start = time.perf_counter()

        try:
            response = await acompletion(**request_kwargs)
        except Exception as error:
            if debug_on:
                self._emit_trace(
                    trace_id=trace_id,
                    session_id=trace_session_id,
                    request_kwargs=request_kwargs,
                    messages=messages,
                    tools=tools,
                    latency_ms=(time.perf_counter() - start) * 1000,
                    success=False,
                    error=error,
                    output="",
                    tool_call_names=[],
                    request_id=None,
                )
            raise

        latency_ms = (time.perf_counter() - start) * 1000
        message = cast(Choices, response.choices[0]).message
        content = message.content or ""
        tool_calls = [
            LLMToolCall(
                id=tc["id"],
                name=tc["function"]["name"],
                arguments=tc["function"]["arguments"],
            )
            for tc in (message.tool_calls or [])
        ]

        if debug_on:
            self._emit_trace(
                trace_id=trace_id,
                session_id=trace_session_id,
                request_kwargs=request_kwargs,
                messages=messages,
                tools=tools,
                latency_ms=latency_ms,
                success=True,
                error=None,
                output=content,
                tool_call_names=[tc.name for tc in tool_calls],
                request_id=trace.extract_request_id(response),
            )

        return content, tool_calls

    def _emit_trace(
        self,
        *,
        trace_id: str,
        session_id: Optional[str],
        request_kwargs: dict[str, Any],
        messages: list[Any],
        tools: Optional[list[dict[str, Any]]],
        latency_ms: float,
        success: bool,
        error: Optional[Exception],
        output: str,
        tool_call_names: list[str],
        request_id: Optional[str],
    ) -> None:
        """Write a JSONL trace record and one summary log line. Best-effort."""
        record = trace.build_record(
            trace_id=trace_id,
            agent_id=self._agent_id,
            session_id=session_id,
            model=self.model,
            request_kwargs=request_kwargs,
            messages=messages,
            tools=tools,
            latency_ms=latency_ms,
            success=success,
            error=error,
            output=output,
            tool_call_names=tool_call_names,
            request_id=request_id,
            include_content=bool(self._debug and self._debug.include_content),
            secrets=[self.api_key],
        )
        trace.write_record(cast(Path, self._logging_path), record)
        logger.info(
            "LLM request trace_id=%s model=%s store=%s tools_count=%s "
            "request_id=%s latency_ms=%s",
            trace_id,
            self.model,
            request_kwargs.get("store"),
            len(tools or []),
            request_id,
            round(latency_ms, 2),
        )
