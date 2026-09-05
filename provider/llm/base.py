"""Base LLM provider abstraction."""

import logging
import time
import uuid
from pathlib import Path
from typing import Any, Optional, cast

import litellm
from litellm import acompletion, aresponses, Choices, TYPE_CHECKING
from provider.llm.models import (
    LLMResponse,
    LLMToolCall,
    Message,
    ResponseCursorNotFound,
)
from provider.llm.responses import build_input, convert_tools, parse_response

from provider.llm import trace

if TYPE_CHECKING:
    from utils.config import LLMConfig, LLMDebugConfig

logger = logging.getLogger("provider.llm")

# Drop params a given model does not support (e.g. gpt-5 rejects temperature!=1)
# instead of erroring, so a single config works across heterogeneous models.
litellm.drop_params = True


def _supports_custom_temperature(model: str) -> bool:
    """Return whether the configured model accepts non-default temperature."""
    model_name = model.rsplit("/", 1)[-1].lower()
    return not model_name.startswith("gpt-5")


def _is_missing_response_cursor(error: Exception) -> bool:
    """Recognize only API errors that explicitly reject the response cursor."""
    if not isinstance(error, (litellm.BadRequestError, litellm.NotFoundError)):
        return False

    body = getattr(error, "body", None)
    if not isinstance(body, dict):
        body = {}
    details = body.get("error", body)
    if not isinstance(details, dict):
        details = {}

    param = str(details.get("param", "")).lower()
    code = str(details.get("code", "")).lower()
    message = " ".join(
        part
        for part in (
            str(details.get("message", "")),
            str(getattr(error, "message", "")),
        )
        if part
    ).lower()
    cursor_is_named = (
        param == "previous_response_id"
        or "previous_response_id" in message
        or "previous response" in message
        or "response with id" in message
    )
    cursor_is_unusable = any(
        marker in f"{code} {message}" for marker in ("not_found", "not found", "no response", "expired", "invalid")
    )
    return cursor_is_named and cursor_is_unusable


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
        api_mode: str = "chat_completions",
        reasoning_effort: Optional[str] = None,
        store: Optional[bool] = None,
        **kwargs: Any,
    ):
        """Initialize LLM provider."""
        self.model = model
        self.api_key = api_key
        self.api_base = api_base
        self.temperature = temperature
        self.max_tokens = max_tokens
        if api_mode not in ("chat_completions", "responses"):
            raise ValueError(f"Unsupported LLM API mode: {api_mode}")
        self.api_mode = api_mode
        self.reasoning_effort = reasoning_effort
        self.store = store
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
        settings = dict(config.extra)
        for name in ("api_mode", "reasoning_effort", "store"):
            value = getattr(config, name)
            if value is not None:
                settings[name] = value
        return cls(
            model=config.model,
            api_key=config.api_key,
            api_base=config.api_base,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            debug=config.debug,
            logging_path=logging_path,
            agent_id=agent_id,
            **settings,
        )

    async def chat(
        self,
        messages: list[Message],
        tools: Optional[list[dict[str, Any]]] = None,
        **kwargs: Any,
    ) -> tuple[str, list[LLMToolCall]] | LLMResponse:
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
        previous_response_id = kwargs.pop("previous_response_id", None)
        if self.reasoning_effort is not None:
            request_kwargs["reasoning_effort"] = self.reasoning_effort
        if self.store is not None:
            request_kwargs["store"] = self.store

        # Provider-specific extras (from LLMConfig.extra), then per-call overrides.
        request_kwargs.update(self._settings)
        request_kwargs.update(kwargs)
        if self.api_mode == "responses":
            instructions, input_items = build_input(messages)
            request_kwargs.pop("messages", None)
            request_kwargs["input"] = input_items
            request_kwargs["instructions"] = instructions
            request_kwargs["max_output_tokens"] = request_kwargs.pop("max_tokens")
            effort = request_kwargs.pop("reasoning_effort", None)
            if effort is not None:
                request_kwargs["reasoning"] = {"effort": effort}
            if tools:
                request_kwargs["tools"] = convert_tools(tools)
            if previous_response_id:
                request_kwargs["previous_response_id"] = previous_response_id
            if request_kwargs.get("store") is not True:
                raise ValueError("Responses mode requires store=true for server-side reasoning state")
            request_kwargs["store"] = True
            if not _supports_custom_temperature(self.model):
                request_kwargs.pop("temperature", None)

        debug_on = bool(self._debug and self._debug.enabled and self._logging_path is not None)
        trace_id = uuid.uuid4().hex
        start = time.perf_counter()

        try:
            if self.api_mode == "responses":
                response = await aresponses(**request_kwargs)
                parsed = parse_response(response)
            else:
                response = await acompletion(**request_kwargs)
                message = cast(Choices, response.choices[0]).message
                parsed = LLMResponse(
                    message.content or "",
                    [
                        LLMToolCall(id=tc["id"], name=tc["function"]["name"], arguments=tc["function"]["arguments"])
                        for tc in (message.tool_calls or [])
                    ],
                )
        except Exception as error:
            cursor_error = None
            if previous_response_id and _is_missing_response_cursor(error):
                cursor_error = ResponseCursorNotFound("The previous Responses API cursor is no longer available")
            if debug_on:
                self._emit_trace(
                    trace_id=trace_id,
                    session_id=trace_session_id,
                    request_kwargs=request_kwargs,
                    messages=messages,
                    tools=tools,
                    latency_ms=(time.perf_counter() - start) * 1000,
                    success=False,
                    error=cursor_error or error,
                    output="",
                    tool_call_names=[],
                    request_id=None,
                )
            if cursor_error is not None:
                raise cursor_error from error
            raise

        latency_ms = (time.perf_counter() - start) * 1000
        content, tool_calls = parsed

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
                parsed=parsed,
            )

        return parsed if self.api_mode == "responses" else (content, tool_calls)

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
        parsed: Optional[LLMResponse] = None,
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
        record.update(
            {
                "api_mode": self.api_mode,
                "endpoint": "/v1/responses" if self.api_mode == "responses" else "/v1/chat/completions",
                "uses_previous_response_id": bool(request_kwargs.get("previous_response_id")),
                "response_id": parsed.response_id if parsed else None,
                "response_status": parsed.response_status if parsed else None,
                **(parsed.usage if parsed else {}),
            }
        )
        if self.api_mode == "responses" and error is not None:
            # Provider exceptions may embed the upstream response, including
            # opaque reasoning items. Keep only the error class in this trace.
            record["error"] = type(error).__name__
        trace.write_record(cast(Path, self._logging_path), record)
        logger.info(
            "LLM request trace_id=%s model=%s store=%s tools_count=%s request_id=%s latency_ms=%s",
            trace_id,
            self.model,
            request_kwargs.get("store"),
            len(tools or []),
            request_id,
            round(latency_ms, 2),
        )
