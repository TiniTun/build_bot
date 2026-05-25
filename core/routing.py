from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field
from re import Pattern
from typing import TYPE_CHECKING

from core.agent import Agent
from core.events import EventSource
from utils.config import SourceSessionConfig

if TYPE_CHECKING:
    from core.context import SharedContext

logger = logging.getLogger(__name__)

@dataclass
class Binding:
    """A routing binding that matches sources to agents."""

    agent: str
    value: str
    tier: int = field(init=False)
    pattern: Pattern = field(init=False)

    def __post_init__(self):
        self.pattern = re.compile(f"^{self.value}$")
        self.tier = self._compute_tier()

    def _compute_tier(self) -> int:
        """Compute specificity tier."""
        if not any(c in self.value for c in r".*+?[]()|^$"):
            return 0
        if ".*" in self.value:
            return 2
        return 1

    
@dataclass
class RoutingTable:
    """Routes sources to agents using regex bindings."""

    context: SharedContext
    bindings: list[Binding] | None = field(default=None, init=False)
    _config_hash: int | None = field(default=None, init=False)

    def _load_bindings(self) -> list[Binding]:
        """Load and sort bindings from config. Cached until config changes."""
        bindings_data = self.context.config.routing.get("bindings", [])
        current_hash = hash(tuple((b["agent"], b["value"]) for b in bindings_data))

        if self.bindings is not None and self._config_hash == current_hash:
            return self.bindings

        # Rebuild
        bindings_with_order: list[tuple[Binding, int]] = [
            (Binding(agent=b["agent"], value=b["value"]), i)
            for i, b in enumerate(bindings_data)
        ]
        bindings_with_order.sort(key=lambda x: (x[0].tier, x[1]))
        self.bindings = [b for b, _ in bindings_with_order]
        self._config_hash = current_hash

        return self.bindings

    def resolve(self, source: str) -> str:
        """Return agent_id for source, falling back to default_agent if no match."""
        for binding in self._load_bindings():
            if binding.pattern.match(source):
                return binding.agent
        return self.context.config.default_agent

    def get_or_create_session_id(self, source: EventSource) -> str:
        """Get existing or create new session_id for source."""
        source_str = str(source)

        source_session = self.context.config.sources.get(source_str)
        if source_session:
            session_id = source_session.session_id
            session_info = self.context.history_store.get_session_info(session_id)
            if session_info:
                return session_id
            logger.warning("Stale cached session for source %s: session %s not found in history; creating new session",
                source_str,
                session_id,
            )
            self.config_source_session_cache(source_str, None)


        agent_id = self.resolve(source_str)
        agent_def = self.context.agent_loader.load(agent_id)
        agent = Agent(agent_def, self.context)
        session = agent.new_session(source)

        # Cache the session
        self.config_source_session_cache(source_str, session.session_id)

        return session.session_id

    def persist_binding(self, source_pattern: str, agent_id: str) -> None:
        """Add and persist a routing binding to config.user.yaml."""
        bindings = self.context.config.routing.get("bindings", [])
        bindings.append({"agent": agent_id, "value": source_pattern})
        self.context.config.set_runtime("routing.bindings", bindings)

    def config_source_session_cache(
        self, source_str: str, session_id: str | None
    ) -> None:
        """Set or clear source -> session affinity in memory and runtime config."""
        if session_id is None:
            self.context.config.sources.pop(source_str, None)
        else:
            self.context.config.sources[source_str] = SourceSessionConfig(
                session_id=session_id
            )

        sources_payload = {
            source: value.model_dump()
            for source, value in self.context.config.sources.items()
        }

        self.context.config.set_runtime("sources", sources_payload)