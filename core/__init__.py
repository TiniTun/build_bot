"""Core agent components.

Keep this package initializer lightweight. Several core modules are imported by
tool modules during startup, so eager re-exports here can create circular imports.
"""

__all__ = [
    "Agent",
    "AgentSession",
    "AgentDef",
    "AgentLoader",
    "SharedContext",
    "HistoryStore",
    "HistoryMessage",
    "HistorySession",
]


def __getattr__(name: str):
    if name in {"Agent", "AgentSession"}:
        from core.agent import Agent, AgentSession

        return {"Agent": Agent, "AgentSession": AgentSession}[name]
    if name in {"AgentDef", "AgentLoader"}:
        from core.agent_loader import AgentDef, AgentLoader

        return {"AgentDef": AgentDef, "AgentLoader": AgentLoader}[name]
    if name == "SharedContext":
        from core.context import SharedContext

        return SharedContext
    if name in {"HistoryMessage", "HistorySession", "HistoryStore"}:
        from core.history import HistoryMessage, HistorySession, HistoryStore

        return {
            "HistoryMessage": HistoryMessage,
            "HistorySession": HistorySession,
            "HistoryStore": HistoryStore,
        }[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
