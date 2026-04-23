from core.agent_loader import AgentLoader
from core.commands.registry import CommandRegistry
from core.history import HistoryStore
from core.skill_loader import SkillLoader
from core.eventbus import EventBus
from utils.config import Config


class SharedContext:
    """Global shared state for the application."""

    config: Config
    history_store: HistoryStore
    agent_loader: AgentLoader
    skill_loader: SkillLoader
    command_registry: CommandRegistry
    eventbus: EventBus

    def __init__(self, config: Config) -> None:
        self.config = config
        self.history_store = HistoryStore.from_config(config)
        self.agent_loader = AgentLoader.from_config(config)
        self.skill_loader = SkillLoader.from_config(config)
        self.command_registry = CommandRegistry.with_builtins()
        self.eventbus = EventBus(self)