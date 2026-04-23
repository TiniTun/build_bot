"""Central event bus for pub/sub event distribution."""

import asyncio
import logging
from logging import Logger
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import TypeAlias, TypeVar

from server.worker import Worker

from .events import Event

logger: Logger = logging.getLogger(name=__name__)

E = TypeVar("E", bound=Event)
Handler: TypeAlias = Callable[[Event], Awaitable[None]]


class EventBus(Worker):
    """Central event bus with subscription support and async dispatch."""

    def __init__(self, context) -> None:
        super().__init__(context)
        self.context = context
        self._subscribers: dict[type[Event], list[Handler]] = defaultdict(list)
        self._queue: asyncio.Queue[Event] = asyncio.Queue()

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

        # Process events from queue
        try:
            while True:
                event = await self._queue.get()
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