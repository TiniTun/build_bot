"""Explicit LiteLLM Responses API translation, without transport fallbacks."""

from typing import Any

from provider.llm.models import LLMResponse, LLMToolCall, Message


def _get(value: Any, name: str, default: Any = None) -> Any:
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def build_input(messages: list[Message]) -> tuple[str, list[dict[str, Any]]]:
    instructions: list[str] = []
    items: list[dict[str, Any]] = []
    for message in messages:
        role = message["role"]
        content = message.get("content")
        if role in ("system", "developer"):
            if not isinstance(content, str):
                raise ValueError("Responses instructions must be text")
            instructions.append(content)
        elif role == "tool":
            items.append({"type": "function_call_output", "call_id": message["tool_call_id"], "output": content or ""})
        elif role in ("user", "assistant"):
            if content:
                if not isinstance(content, str):
                    raise ValueError("Responses adapter currently supports text messages only")
                items.append({"role": role, "content": content})
            for call in message.get("tool_calls", []):
                items.append(
                    {
                        "type": "function_call",
                        "call_id": call["id"],
                        "name": call["function"]["name"],
                        "arguments": call["function"]["arguments"],
                    }
                )
        else:
            raise ValueError(f"Unsupported message role: {role}")
    return "\n\n".join(instructions), items


def convert_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for tool in tools:
        if tool.get("type") != "function":
            raise ValueError("Only function tools are supported by the Responses adapter")
        spec = dict(tool.get("function", tool))
        spec["type"] = "function"
        # Responses otherwise normalizes optional arguments into required fields.
        spec.setdefault("strict", False)
        result.append(spec)
    return result


def parse_response(response: Any) -> LLMResponse:
    status = _get(response, "status")
    if status != "completed":
        # Never execute calls from truncated, failed, or still-running responses.
        reason = _get(_get(response, "incomplete_details"), "reason")
        code = _get(_get(response, "error"), "code")
        raise RuntimeError(f"Responses request did not complete (status={status}, reason={reason or code})")
    content: list[str] = []
    calls: list[LLMToolCall] = []
    for item in _get(response, "output", []) or []:
        kind = _get(item, "type")
        if kind == "reasoning":
            continue  # Server cursor retains reasoning; never copy it to local history/trace.
        if kind == "function_call":
            call_id, name, arguments = (_get(item, key) for key in ("call_id", "name", "arguments"))
            if not call_id or not name or not isinstance(arguments, str):
                raise RuntimeError("Responses returned a malformed function call")
            calls.append(LLMToolCall(call_id, name, arguments))
        elif kind == "message":
            for part in _get(item, "content", []) or []:
                part_type = _get(part, "type")
                if part_type == "output_text":
                    content.append(_get(part, "text", ""))
                elif part_type == "refusal":
                    raise RuntimeError("Responses model refused the request")
                else:
                    raise RuntimeError(f"Unsupported Responses message content: {part_type}")
        else:
            raise RuntimeError(f"Unsupported Responses output item: {kind}")
    if not content and not calls:
        raise RuntimeError("Responses returned no text or function calls")
    response_id = _get(response, "id")
    if not response_id:
        raise RuntimeError("Responses returned no response id")
    usage = _get(response, "usage")
    return LLMResponse(
        "".join(content),
        calls,
        response_id,
        status,
        {
            "input_tokens": _get(usage, "input_tokens"),
            "output_tokens": _get(usage, "output_tokens"),
            "reasoning_tokens": _get(_get(usage, "output_tokens_details"), "reasoning_tokens"),
        },
    )
