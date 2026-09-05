import logging
import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from provider.llm.models import Message

from core.history import HistoryMessage

if TYPE_CHECKING:
    from core.agent import Agent
    from core.context import SharedContext
    from core.events import EventSource

logger = logging.getLogger(__name__)


def repair_tool_call_pairing(messages: list[Message]) -> list[Message]:
    """Return a copy of ``messages`` with assistant/tool pairing repaired.

    The OpenAI/LiteLLM chat API rejects a history where an assistant message
    carrying ``tool_calls`` is not immediately followed by a ``tool`` message
    for every ``tool_call_id``, or where a ``tool`` message has no matching
    call. A persisted session can reach that state two ways: the process dies
    between saving the assistant message and its tool results, or compaction
    splits a tool-call run. Either leaves a dangling assistant message that
    makes the session permanently un-resumable. Repair the gap (synthesizing
    placeholder tool results and dropping orphan tool messages) so the next
    LLM call succeeds instead of 400-ing.
    """
    repaired: list[Message] = []
    i = 0
    n = len(messages)
    while i < n:
        msg = messages[i]
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            repaired.append(msg)
            expected_ids = [tc.get("id") for tc in msg["tool_calls"]]
            i += 1
            # Consume the contiguous run of tool responses that follow.
            answered: dict[str, Message] = {}
            while i < n and messages[i].get("role") == "tool":
                tid = messages[i].get("tool_call_id")
                if tid in expected_ids and tid not in answered:
                    answered[tid] = messages[i]
                # Drop duplicates and orphan tool ids silently.
                i += 1
            for tid in expected_ids:
                if tid in answered:
                    repaired.append(answered[tid])
                else:
                    repaired.append(
                        {
                            "role": "tool",
                            "tool_call_id": tid,
                            "content": "Error: tool result unavailable (interrupted).",
                        }
                    )
        elif msg.get("role") == "tool":
            # Orphan tool message with no preceding assistant tool_calls.
            logger.warning("Dropping orphan tool message without a matching call")
            i += 1
        else:
            repaired.append(msg)
            i += 1
    return repaired


@dataclass
class SessionState:
    """Pure conversation state container."""

    session_id: str
    agent: "Agent"
    messages: list[Message]
    source: "EventSource"
    shared_context: "SharedContext"
    # Set by a tool that has concluded there is nothing to report. AgentWorker
    # skips publishing the final message for such a session. A tool cannot
    # otherwise stay silent: the worker publishes the model's own text, and a
    # model asked to say nothing will still say something.
    suppress_final_output: bool = False
    last_response_id: str | None = None
    last_response_model: str | None = None
    last_api_mode: str | None = None
    response_endpoint_fingerprint: str | None = None
    response_message_count: int = 0
    response_prefix_hash: str | None = None

    @staticmethod
    def _prefix_hash(messages: list[Message]) -> str:
        return hashlib.sha256(json.dumps(messages, sort_keys=True).encode()).hexdigest()

    def _endpoint_fingerprint(self) -> str:
        llm = self.agent.llm
        endpoint = getattr(llm, "api_base", None) or "https://api.openai.com/v1"
        return hashlib.sha256(endpoint.rstrip("/").encode()).hexdigest()

    def _save_cursor(self) -> None:
        names = (
            "last_response_id",
            "last_response_model",
            "last_api_mode",
            "response_endpoint_fingerprint",
            "response_message_count",
            "response_prefix_hash",
        )
        self.shared_context.history_store.update_response_cursor(
            self.session_id, **{name: getattr(self, name) for name in names}
        )

    def invalidate_response_cursor(self) -> None:
        if self.last_response_id is None:
            return
        self.last_response_id = None
        self.last_response_model = None
        self.last_api_mode = None
        self.response_endpoint_fingerprint = None
        self.response_message_count = 0
        self.response_prefix_hash = None
        self._save_cursor()

    def checkpoint_response(self, response_id: str) -> None:
        self.last_response_id = response_id
        self.last_response_model = self.agent.llm.model
        self.last_api_mode = "responses"
        self.response_endpoint_fingerprint = self._endpoint_fingerprint()
        self.response_message_count = len(self.messages)
        self.response_prefix_hash = self._prefix_hash(self.messages)
        self._save_cursor()

    def build_response_messages(self) -> tuple[list[Message], str | None]:
        """Send instructions plus only the history not represented by the cursor."""
        messages = self.build_messages()
        count = self.response_message_count
        delta = self.messages[count:]
        valid = (
            self.last_response_id is not None
            and self.last_api_mode == "responses"
            and self.last_response_model == self.agent.llm.model
            and self.response_endpoint_fingerprint == self._endpoint_fingerprint()
            and 0 <= count <= len(self.messages)
            and self.response_prefix_hash == self._prefix_hash(self.messages[:count])
            and messages[1:] == self.messages
            # An assistant item after the checkpoint means its response was
            # persisted but its newer cursor was not (for example, a crash
            # between those two durable writes). It must not be submitted as
            # fresh user input against the older server-side chain.
            and not any(message.get("role") == "assistant" for message in delta)
        )
        if not valid:
            self.invalidate_response_cursor()
            return messages, None
        return [messages[0], *delta], self.last_response_id

    def add_message(self, message: Message) -> None:
        """Add message to conversation history."""
        self.messages.append(message)

        history_msg = HistoryMessage.from_message(message)
        self.shared_context.history_store.save_message(self.session_id, history_msg)

    def build_messages(self) -> list[Message]:
        """Build messages list with system prompt."""
        system_prompt = self.shared_context.prompt_builder.build(self)
        messages: list[Message] = [{"role": "system", "content": system_prompt}]
        messages.extend(repair_tool_call_pairing(self.messages))
        return messages
