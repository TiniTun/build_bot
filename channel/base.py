"""Abstract base class for channel implementations."""

from abc import ABC, abstractmethod
from collections.abc import Callable, Awaitable
from typing import Generic, TypeVar, Any

from core.events import EventSource
from utils.config import Config


T = TypeVar("T", bound=EventSource)

class Channel(ABC, Generic[T]):
    """Abstract base for messaging platforms with EventSource-based context."""

    @property
    @abstractmethod
    def platform_name(self) -> str:
        """Platform identifier."""
        pass

    @abstractmethod
    async def run(self, on_message: Callable[[str, T], Awaitable[None]]) -> None:
        """Run the channel. Blocks until stop() is called."""
        pass

    @abstractmethod
    def is_allowed(self, source: T) -> bool:
        """Check if sender is whitelisted."""
        pass

    @abstractmethod
    async def reply(self, content: str, source: T) -> None:
        """Reply to incoming message."""
        pass

    @abstractmethod
    async def stop(self) -> None:
        """Stop listening and cleanup resources."""
        pass

    @staticmethod
    def from_config(config: Config) -> list["Channel[Any]"]:
        """Create channel instances from configuration."""
        # Inline imports to avoid circular dependency
        from channel.telegram_channel import TelegramChannel

        channels: list["Channel[Any]"] = []
        channel_config = config.channels
        if channel_config.telegram and channel_config.telegram.enabled:
            channels.append(TelegramChannel(channel_config.telegram))

        return channels