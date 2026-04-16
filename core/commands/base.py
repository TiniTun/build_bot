"""Base classes for slash commands."""

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.agent import AgentSession


class Command(ABC):
    """Base class for slash commands."""

    name: str
    aliases: list[str] = []
    desctiption: str = ""

    @abstractmethod
    async def execute(self, args: str, session: "AgentSession") -> str:
        """Execute the command and return response string."""
        pass