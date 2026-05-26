
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

@dataclass
class SessionState:
    """Pure conversation state container."""

    session_id: str
    agent: "Agent"
    messages: list[Message]
    source: "EventSource"
    shared_context: "SharedContext"

    def add_message(self, message: Message) -> None:
        """Add message to conversation history."""
        self.messages.append(message)

        history_msg = HistoryMessage.from_message(message)
        self.shared_context.history_store.save_message(self.session_id, history_msg)

    def build_messages(self) -> list[Message]:
        """Build messages list with system prompt."""
        system_prompt = self.shared_context.prompt_builder.build(self)
        messages: list[Message] = [{"role": "system", "content": system_prompt}]
        messages.extend(self.messages)
        return messages
    