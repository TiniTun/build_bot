from typing import Any, TYPE_CHECKING
import channel
from channel.base import Channel
from core.agent_loader import AgentLoader
from core.commands.registry import CommandRegistry
from core.history import HistoryStore
from core.skill_loader import SkillLoader
from core.eventbus import EventBus
from utils.config import Config

if TYPE_CHECKING:
    from server.websocket_worker import WebSocketWorker


class SharedContext:
    """Global shared state for the application."""

    config: Config
    history_store: HistoryStore
    agent_loader: AgentLoader
    skill_loader: SkillLoader
    command_registry: CommandRegistry
    channels: list[Channel[Any]]
    eventbus: EventBus
    websocket_worker: "WebSocketWorker | None"

    def __init__(
        self, config: Config, channels: list[Channel[Any]] | None = None
    ) -> None:
        self.config = config
        self.history_store = HistoryStore.from_config(config)
        self.agent_loader = AgentLoader.from_config(config)
        self.skill_loader = SkillLoader.from_config(config)
        self.command_registry = CommandRegistry.with_builtins()
        
        if channels is not None:
            self.channels = channels
        else:
            self.channels = Channel.from_config(config)
        
        self.eventbus = EventBus(self)
        self.websocket_worker = None