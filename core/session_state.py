
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from litellm.types.completion import ChatCompletionMessageParam as Message

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
    