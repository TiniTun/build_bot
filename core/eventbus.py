"""Central event bus for pub/sub event distribution."""

import asyncio
import json
import logging
import os
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, TypeAlias, TypeVar

from server.worker import Worker

from core.events import (
    Event,
    OutboundEvent,
    deserialize_event,
)

if TYPE_CHECKING:
    from core.context import SharedContext

logger = logging.getLogger(name=__name__)

E = TypeVar("E", bound=Event)
Handler: TypeAlias = Callable[[Event], Awaitable[None]]


class EventBus(Worker):
    """Central event bus with subscription support and async dispatch."""

    def __init__(self, context) -> None:
        super().__init__(context)
        self.context = context
        self._subscribers: dict[type[Event], list[Handler]] = defaultdict(list)
        self._queue: asyncio.Queue[Event] = asyncio.Queue()
        self.pending_dir = context.config.event_path / "pending"
        self.pending_dir.mkdir(parents=True, exist_ok=True)

    def subscribe(
            self, event_class: type[E], handler: Callable[[E], Awaitable[None]]
    ) -> None:
        """Subscribe a handler to an event class."""
        self._subscribers[event_class].append(handler)
        logger.debug(f"Subscribed handler to {event_class.__name__} events")

    def unsubscribe(self, handler: Handler) -> None:
        """Remove a handler from all subscriptions."""
        for event_class in self._subscribers:
            if handler in self._subscribers[event_class]:
                self._subscribers[event_class].remove(handler)
                logger.debug(f"Unsubscribed handler from {event_class.__name__} events")

    async def publish(self, event: Event) -> None:
        """Publish an event to the internal queue (non-blocking)."""
        await self._queue.put(event)
        logger.debug(f"Queued {event.__class__.__name__} event")

    async def run(self) -> None:
        """Process events from queue, starting with recovery."""
        logger.info("EventBus started")

        # Run recovery first
        await self._recover()

        # Process events from queue
        try:
            while True:
                event: Event = await self._queue.get()
                try:
                    await self._dispatch(event)
                except Exception as e:
                    logger.error(f"Error dispatching event: {e}")
                finally:
                    self._queue.task_done()
        except asyncio. CancelledError:
            logger.info("EventBus stopping...")
            raise

    async def _dispatch(self, event: Event) -> None:
        """Persist if OUTBOUND, then notify subscribers."""
        await self._persist_outbound(event)
        await self._notify_subscribers(event)
        logger.debug(f"Dispatched {event.__class__.__name__} event")

    async def _notify_subscribers(self, event: Event) -> None:
        """Notify all subscribers of an event (waits for all handlers to complete)."""
        handlers = self._subscribers.get(type(event), [])
        if not handlers:
            return
        
        tasks: list[Awaitable[None]] = [handler(event) for handler in handlers]
        results: list[BaseException | None] = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                logger.error(f"Error in event handler: {result}")

    async def _persist_outbound(self, event: Event) -> None:
        """Persist event to disk (only OUTBOUND events)."""
        if not isinstance(event, OutboundEvent):
            return

        filename = f"{event.timestamp}_{event.session_id}.json"
        final_path = self.pending_dir / filename
        tmp_path = self.pending_dir / f".tmp.{os.getpid()}.{filename}"

        data = json.dumps(event.to_dict(), ensure_ascii=False)
        
        # Atomic write: tmp + fsync + rename
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())

        os.replace(str(tmp_path), str(final_path))
        logger.debug(f"Persisted event to {final_path}")

    async def _recover(self) -> int:
        """Recover pending events from previous crash. Returns count recovered."""
        pending_files = list(self.pending_dir.glob("*.json"))
        if not pending_files:
            return 0

        logger.info(f"Recovering {len(pending_files)} pending events")
        count = 0

        for file_path in pending_files:
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                # Use deserialize_event to handle typed events
                event: Event = deserialize_event(data)
                await self._notify_subscribers(event)
                count += 1
                logger.debug(f"Recovered event from {file_path.name}")
            except Exception as e:
                logger.error(f"Failed to recover {file_path}: {e}")

        logger.info(f"Recovered {count} events")
        return count
        
    def ack(self, event: Event) -> None:
        """Acknowledge successful delivery, delete persisted event."""
        filename = f"{event.timestamp}_{event.session_id}.json"
        final_path = self.pending_dir / filename

        if final_path.exists():
            final_path.unlink()
            logger.debug(f"Acked and deleted {filename}")