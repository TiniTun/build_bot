from typing import Any, TYPE_CHECKING

from channel.base import Channel
from core.agent_loader import AgentLoader
from core.commands.registry import CommandRegistry
from core.cron_loader import CronLoader
from core.history import HistoryStore
from core.prompt_builder import PromptBuilder
from core.routing import RoutingTable
from core.skill_loader import SkillLoader
from core.eventbus import EventBus
from provider.mcp.hub import McpHub
from utils.config import Config

if TYPE_CHECKING:
    from server.websocket_worker import WebSocketWorker


class SharedContext:
    """Global shared state for the application."""

    config: Config
    history_store: HistoryStore
    agent_loader: AgentLoader
    skill_loader: SkillLoader
    cron_loader: CronLoader
    command_registry: CommandRegistry
    routing_table: RoutingTable
    prompt_builder: PromptBuilder
    channels: list[Channel[Any]]
    eventbus: EventBus
    mcp_hub: McpHub
    websocket_worker: "WebSocketWorker | None"

    def __init__(
        self, config: Config, channels: list[Channel[Any]] | None = None
    ) -> None:
        self.config = config
        self.history_store = HistoryStore.from_config(config)
        self.agent_loader = AgentLoader.from_config(config)
        self.skill_loader = SkillLoader.from_config(config)
        self.cron_loader = CronLoader.from_config(config)
        self.command_registry = CommandRegistry.with_builtins()
        self.routing_table = RoutingTable(self)
        self.prompt_builder = PromptBuilder(self)
        
        if channels is not None:
            self.channels = channels
        else:
            self.channels = Channel.from_config(config)
        
        self.eventbus = EventBus(self)
        # Constructed synchronously and holds no connection until `start()` is
        # awaited by the CLI chat loop or the server, before any worker runs.
        self.mcp_hub = McpHub(config)
        self.websocket_worker = None