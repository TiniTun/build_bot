"""Confirmed-execution path for mutation capabilities.

Confirm-required tools record a pending action on first call and never mutate
external state. ``/confirm`` then looks up a *confirmed executor* by capability
id and runs it exactly once. Keeping the proposal (LLM-facing) tool separate
from the confirmed executor (``/confirm``-facing) guarantees that confirming an
action never re-invokes the proposal tool and creates another pending action.

Executors close over a provider built from config and perform the real provider
mutation through the shared error mapping, so confirmation does not bypass
provider error handling.
"""

from typing import TYPE_CHECKING, Any, Awaitable, Callable

if TYPE_CHECKING:
    from core.agent import AgentSession

# (session, payload) -> tool content string (success or mapped error).
ConfirmedExecutor = Callable[["AgentSession", dict[str, Any]], Awaitable[str]]


class ConfirmedExecutorRegistry:
    """Maps a dotted capability id to the function that executes it after confirm."""

    def __init__(self) -> None:
        self._executors: dict[str, ConfirmedExecutor] = {}

    def register(self, capability_id: str, executor: ConfirmedExecutor) -> None:
        self._executors[capability_id] = executor

    def merge(self, executors: dict[str, ConfirmedExecutor]) -> None:
        self._executors.update(executors)

    def get(self, capability_id: str) -> ConfirmedExecutor | None:
        return self._executors.get(capability_id)
