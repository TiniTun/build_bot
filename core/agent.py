"""Agent and AgentSession"""

from core.history import HistorySession

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from litellm.types.completion import (
    ChatCompletionMessageParam as Message,
    ChatCompletionMessageToolCallParam,
)

from core.context_guard import ContextGuard
from core.session_state import SessionState
from core.events import EventSource
from provider.llm import LLMProvider
from tools.registry import ToolRegistry
from tools.capabilities import ToolPolicy
from tools.capability_catalog import build_capability_registry

if TYPE_CHECKING:
    from core.context import SharedContext
    from core.agent_loader import AgentDef
    from provider.llm import LLMToolCall


class Agent:
    """A configured agent that creates and manages conversation sessions."""

    def __init__(self, agent_def: "AgentDef", context: "SharedContext") -> None:
        self.agent_def = agent_def
        self.context = context
        self.llm = LLMProvider.from_config(
            agent_def.llm,
            logging_path=context.config.logging_path,
            agent_id=agent_def.id,
        )

    def _build_tools(self, include_post_message: bool) -> ToolRegistry:
        """Build a ToolRegistry with tools appropriate for the session.

        Tools are assembled as capabilities and filtered by the configured
        ``ToolPolicy``. With no ``tools`` config the policy is permissive, so the
        resulting tool set matches legacy behavior.
        """
        capabilities = build_capability_registry(
            self.agent_def, self.context, include_post_message
        )
        policy = ToolPolicy.from_config(
            self.context.config, self.agent_def.allowed_capabilities
        )
        return capabilities.build_tool_registry(policy)

    def _get_token_threshold(self) -> int:
        """Get token threshold based on model's context window."""
        # Default to 80% of 200k context
        return 160000

    def new_session(
        self,
        source: EventSource,
        session_id: str | None = None
    ) -> "AgentSession":
        """Create a new conversation session."""
        session_id = session_id or str(uuid.uuid4())

        include_post_message = source.is_cron
        tools = self._build_tools(include_post_message)

        context_guard = ContextGuard(
            shared_context=self.context,
            token_threshold=self._get_token_threshold()
        )

        state = SessionState(
            session_id=session_id,
            agent=self,
            messages=[],
            source=source,
            shared_context=self.context
        )

        session = AgentSession(
            agent=self,
            state=state,
            context_guard=context_guard,
            tools=tools,
        )
        self.context.history_store.create_session(
            self.agent_def.id, session_id, source
        ) # create_session

        return session
    
    def resume_session(self, session_id: str) -> "AgentSession":
        """Load an existing conversation session."""
        session_query = [
            session
            for session in self.context.history_store.list_sessions()
            if session.id == session_id
        ]
        if not session_query:
            raise ValueError(f"Session not found: {session_id}")

        session_info: HistorySession = session_query[0]
        source: EventSource = session_info.get_source()

        # Get all messages (no max_history limit)
        history_messages = self.context.history_store.get_messages(session_id)

        # Convert HistoryMessage to litellm Message format
        messages: list[Message] = [msg.to_message() for msg in history_messages]

        # Build tools for resumed session
        include_post_message = source.is_cron
        tools = self._build_tools(include_post_message)

        # Create context guard
        context_guard = ContextGuard(
            shared_context=self.context,
            token_threshold=self._get_token_threshold(),
        )

        # Create SessionState with loaded messages
        state = SessionState(
            session_id=session_info.id,
            agent=self,
            messages=messages,
            source=source,
            shared_context=self.context,
        )

        return AgentSession(
            agent=self,
            state=state,
            context_guard=context_guard,
            tools=tools,
        )


@dataclass
class AgentSession:
    """Chat orchestrator - operates on swappable SessionState."""

    agent: Agent
    state: SessionState
    context_guard: ContextGuard
    tools: ToolRegistry
    started_at: datetime = field(default_factory=datetime.now)

    @property
    def session_id(self) -> str:
        """Delegate to state."""
        return self.state.session_id

    @property
    def shared_context(self) -> "SharedContext":
        """Delegate to state."""
        return self.state.shared_context

    async def chat(self, message: str) -> str:
        """Send a message to the LLM and get a response."""
        user_msg: Message = {"role": "user", "content": message}
        self.state.add_message(user_msg)

        tool_schemas = self.tools.get_tool_schemas()

        while True:

            messages = self.state.build_messages()

            self.state = await self.context_guard.check_and_compact(self.state)

            content, tool_calls = await self.agent.llm.chat(
                messages, tool_schemas, trace_session_id=self.state.session_id
            )

            tool_call_dicts: list[ChatCompletionMessageToolCallParam] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": tc.arguments},
                }
                for tc in tool_calls
            ]

            assistant_msg: Message = {"role": "assistant", "content": content}

            if tool_call_dicts:
                assistant_msg["tool_calls"] = tool_call_dicts
            self.state.add_message(assistant_msg)

            if not tool_calls:
                break

            await self._handle_tool_calls(tool_calls)

            continue

        return content
    
    async def _handle_tool_calls(
        self,
        tool_calls: list["LLMToolCall"],
    ) -> None:
        """Handle tool calls from the LLM response."""
        tool_call_results = []
        for tool_call in tool_calls:
            tool_call_results.append(await self._execute_tool_call(tool_call))

        for tool_call, result in zip(tool_calls, tool_call_results):
            tool_msg: Message = {
                "role": "tool",
                "content": result,
                "tool_call_id": tool_call.id,
            }
            self.state.add_message(tool_msg)


    async def _execute_tool_call(
        self,
        tool_call: "LLMToolCall",
    ) -> str:
        """Execute a single tool call."""
        try:
            args = json.loads(tool_call.arguments)
        except json.JSONDecodeError:
            args = {}

        try:
            result =  await self.tools.execute_tool(tool_call.name, session=self, **args)
        except Exception as e:
            result = f"Error executing tool: {e}"

        return result
