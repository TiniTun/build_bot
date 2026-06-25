"""Local LLM request tracing: redact, summarize, and append JSONL records.

Tracing is best-effort and must never affect the LLM request: every public
helper here swallows its own errors so a logging problem cannot break ``chat``.
Secrets (API keys) and full message content are never written unless content is
explicitly opted in via ``include_content``.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("provider.llm.trace")


def _redact(text: str, secrets: list[str | None]) -> str:
    """Replace any known secret substring in ``text`` with ``***``."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def extract_request_id(response: Any) -> str | None:
    """Best-effort extraction of the upstream (OpenAI) request id."""
    hidden = getattr(response, "_hidden_params", None) or {}
    if isinstance(hidden, dict):
        headers = hidden.get("additional_headers") or {}
        if isinstance(headers, dict):
            for key in ("x-request-id", "x-request-id".title(), "request_id"):
                if headers.get(key):
                    return str(headers[key])
        if hidden.get("request_id"):
            return str(hidden["request_id"])
    return None


def build_record(
    *,
    trace_id: str,
    agent_id: str | None,
    session_id: str | None,
    model: str,
    request_kwargs: dict[str, Any],
    messages: list[Any],
    tools: list[Any] | None,
    latency_ms: float,
    success: bool,
    error: Exception | None,
    output: str,
    tool_call_names: list[str],
    request_id: str | None,
    include_content: bool,
    secrets: list[str | None],
) -> dict[str, Any]:
    """Build one redacted JSONL trace record. Never includes secret values."""
    record: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "trace_id": trace_id,
        "agent_id": agent_id,
        "session_id": session_id,
        "model": model,
        "messages_count": len(messages),
        "tools_count": len(tools or []),
        # Key names only (no values) so secrets like api_key never appear.
        "request_keys": sorted(request_kwargs.keys()),
        "store": request_kwargs.get("store"),
        "request_id": request_id,
        "latency_ms": round(latency_ms, 2),
        "success": success,
        "error_type": type(error).__name__ if error else None,
        "error": _redact(str(error), secrets) if error else None,
        "output_length": len(output),
        "tool_call_names": tool_call_names,
    }
    if include_content:
        record["messages"] = messages
        record["output"] = output
    return record


def write_record(logging_path: Path, record: dict[str, Any]) -> None:
    """Append one JSONL record under ``<logging_path>/llm-traces/``.

    Best-effort: any failure is logged at debug level and otherwise ignored so
    tracing never breaks the request path.
    """
    try:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        trace_dir = Path(logging_path) / "llm-traces"
        trace_dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, default=str)
        with open(trace_dir / f"{day}.jsonl", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:  # noqa: BLE001 - tracing must never break chat
        logger.debug("LLM trace write failed: %s", e)
