"""Agent worker for executing agent jobs."""

from asyncio.locks import Semaphore


import asyncio
import logging
from dataclasses import replace
from typing import TYPE_CHECKING, Union

from .worker import SubscriberWorker
from core.agent import Agent
from core.events import (
    AgentEventSource,
    InboundEvent,
    OutboundEvent,
    DispatchEvent,
    DispatchResultEvent,
)
from utils.def_loader import DefNotFoundError

if TYPE_CHECKING:
    from core.context import SharedContext
    from core.agent_loader import AgentDef



# Maximum number of retry attempts for failed sessions
MAX_RETRIES = 3

# Agent id of the memory-manager that owns durable-memory extraction.
MEMORY_MANAGER_AGENT_ID = "cookie"

# Prompt handed to the memory manager for fire-and-forget Stage B extraction.
_MEMORY_EXTRACTION_PROMPT = (
    "Extract any durable facts, preferences, project updates, or decisions from "
    "the following user message and store them via your memory tools. Store concise "
    "entries only; never store raw conversation. If there is nothing durable worth "
    "saving, do nothing.\n\nUser message:\n{content}"
)

logger = logging.getLogger(__name__)

ProcessableEvent = Union[InboundEvent, DispatchEvent]


class AgentWorker(SubscriberWorker):
    """Dispatches events to session executors."""

    def __init__(self, context):
        super().__init__(context)
        self._semaphores: dict[str, asyncio.Semaphore] = {}

        # Auto-subscribe to events
        self.context.eventbus.subscribe(InboundEvent, self.dispatch_event)
        self.context.eventbus.subscribe(DispatchEvent, self.dispatch_event)
        self.logger.info("AgentWorker subscribed to InboundEvent and DispatchEvent events")

    async def dispatch_event(self, event: ProcessableEvent) -> None:
        """Create executor task for typed event."""
        # Get agent_id from session (single source of truth)
        session_info = self.context.history_store.get_session_info(event.session_id)
        if not session_info:
            logger.error(f"Session not found: {event.session_id}")
            return
        
        agent_id = session_info.agent_id

        try:
            agent_def = self.context.agent_loader.load(agent_id)
        except DefNotFoundError as e:
            logger.error(f"Agent not found: {agent_id}: {e}")

            return await self._emit_response(event, content="", agent_id=agent_def.id, error=str(e))

        asyncio.create_task(self.exec_session(event, agent_def))

    async def exec_session(
        self, event: ProcessableEvent, agent_def: "AgentDef"
    ) -> None:
        sem: Semaphore = self._get_or_create_semaphore(agent_def)
        session_id = event.session_id

        async with sem:
            try:
                agent = Agent(agent_def, self.context)
                if session_id:
                    try:
                        session = agent.resume_session(session_id)
                    except ValueError:
                        logger.warning(f"Session {session_id} not found, creating new")
                        session = agent.new_session(session_id=session_id)
                else:
                    session = agent.new_session()
                    session_id = session.session_id

                # Check for slash command FIRST
                if event.content.startswith("/"):
                    result = await self.context.command_registry.dispatch(
                        event.content, session
                    )
                    if result:
                        # Emit response and skip agent chat
                        await self._emit_response(event, content=result, agent_id=agent_def.id)
                        logger.info(f"Command completed: {session_id}")
                        return

                response = await session.chat(event.content)
                logger.info(f"Session completed: {session_id}")

                if session.state.suppress_final_output:
                    # A tool decided this run has nothing to report — a planner
                    # tick still waiting for WHOOP, or one whose plan is already
                    # applied. Without this the model's text would be published
                    # and the user notified once per tick.
                    logger.debug(
                        "Session %s suppressed its final output", session_id
                    )
                else:
                    await self._emit_response(
                        event, content=response, agent_id=agent_def.id
                    )

                if self._should_auto_extract(event, agent_def):
                    asyncio.create_task(
                        self._run_memory_extraction(event, agent_def)
                    )
            
            except Exception as e:
                logger.error(f"Session failed: {e}, ")

                if event.retry_count < MAX_RETRIES:
                    # Use dataclasses.replace() for retry logic
                    retry_event = replace(
                        event,
                        retry_count=event.retry_count + 1,
                        content="." # Minimal message for retry
                    )
                    await self.context.eventbus.publish(retry_event)
                else:
                    await self._emit_response(event, content="", agent_id=agent_def.id, error=str(e))

        self._maybe_cleanup_semaphores(agent_def)

    async def _emit_response(
        self,
        event: ProcessableEvent,
        content: str,
        agent_id: str,
        error: str | None = None,
    ) -> None:
        """Emit response event with content."""
        if isinstance(event, DispatchEvent) and event.source.is_cron:
            if not content.strip() and not error:
                logger.debug(
                    f"Cron job {event.source} produced no final response; skipping outbound event"
                )
                return

            result_event: OutboundEvent = OutboundEvent(
                session_id=event.session_id,
                source=AgentEventSource(agent_id),
                content=content,
                error=str(error) if error else None,
            )
        elif isinstance(event, DispatchEvent):
            result_event: OutboundEvent | DispatchResultEvent = DispatchResultEvent(
                session_id=event.session_id,
                source=AgentEventSource(agent_id),
                content=content,
                error=str(error) if error else None,
            )
        else:
            result_event: OutboundEvent = OutboundEvent(
                session_id=event.session_id,
                source=AgentEventSource(agent_id),
                content=content,
                error=str(error) if error else None,
            )
        await self.context.eventbus.publish(result_event)

    def _should_auto_extract(
        self, event: ProcessableEvent, agent_def: "AgentDef"
    ) -> bool:
        """Decide whether to run Stage B durable-memory extraction.

        Only user-facing ``InboundEvent`` turns qualify, never the memory
        manager's own sessions, and only when ``memory.auto_extract`` is enabled.
        """
        if not self.context.config.memory.auto_extract:
            return False
        if not isinstance(event, InboundEvent):
            return False
        if agent_def.id == MEMORY_MANAGER_AGENT_ID:
            return False
        return True

    async def _run_memory_extraction(
        self, event: ProcessableEvent, agent_def: "AgentDef"
    ) -> None:
        """Fire-and-forget durable-fact extraction via the memory manager.

        Runs Cookie on the user's message without producing any user-visible
        output: no event is published from here. Exceptions are swallowed and
        logged so extraction never breaks the main turn.
        """
        try:
            try:
                cookie_def = self.context.agent_loader.load(MEMORY_MANAGER_AGENT_ID)
            except DefNotFoundError:
                logger.debug(
                    "Memory manager agent '%s' not found; skipping auto-extract",
                    MEMORY_MANAGER_AGENT_ID,
                )
                return

            agent = Agent(cookie_def, self.context)
            session = agent.new_session(source=AgentEventSource(cookie_def.id))
            prompt = _MEMORY_EXTRACTION_PROMPT.format(content=event.content)
            await session.chat(prompt)
        except Exception as e:  # noqa: BLE001 - fire-and-forget must never bubble
            logger.warning("Memory extraction failed: %s", e)

    def _get_or_create_semaphore(self, agent_def: "AgentDef") -> asyncio.Semaphore:
        """Get existing or create new semaphore for agent."""
        if agent_def.id not in self._semaphores:
            self._semaphores[agent_def.id] = asyncio.Semaphore(
                agent_def.max_concurrency
            )
            logger.debug(
                f"Created semaphore for {agent_def.id} with value {agent_def.max_concurrency}"
            )
        return self._semaphores[agent_def.id]

    def _maybe_cleanup_semaphores(self, agent_def: "AgentDef") -> None:
        """Remove semaphores for certain agents."""
        if agent_def.id not in self._semaphores:
            return

        if not self._semaphores[agent_def.id]._waiters:
            del self._semaphores[agent_def.id]
