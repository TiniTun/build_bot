"""Tasks provider protocol, data models, and a null provider.

``get_task_provider`` selects ``TodoistTaskProvider`` when the tasks domain is
enabled with ``provider == "todoist"``; otherwise it returns a
``NullTaskProvider`` that raises ``AuthMissingError`` so tools surface a stable
``AUTH_MISSING`` error instead of failing opaquely.

``delete`` and ``bulk_update`` mutate destructively and are only reached through
the confirmed-execution path; the proposal tools never call them directly.
"""

import re
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from provider.external_errors import AuthMissingError

if TYPE_CHECKING:
    from utils.config import Config

# A duration label is the WHOLE label, never a substring: `sprint-30max` is a
# project name, not half an hour. Minutes must be positive; `0m` is a typo, not
# a zero-length task.
_DURATION_LABEL = re.compile(r"^(?P<value>\d+(?:\.\d+)?)(?P<unit>m|h)$", re.IGNORECASE)


def parse_duration_label(labels: list[str]) -> int | None:
    """Return the estimated minutes named by the first duration-shaped label.

    Recognizes `15m`/`30m`/`60m`/`90m` and the `1h`/`1.5h` forms. Labels are
    scanned in the order Todoist returned them, so the first match wins and the
    result is stable across runs. Returns None when no label names a duration;
    the caller supplies the configured fallback and marks it assumed.
    """
    for label in labels:
        match = _DURATION_LABEL.match(label.strip())
        if match is None:
            continue
        value = float(match.group("value"))
        minutes = int(round(value * 60)) if match.group("unit").lower() == "h" else int(round(value))
        if minutes > 0:
            return minutes
    return None


BulkOperation = Literal["update", "complete", "delete"]


class Task(BaseModel):
    """A single task as returned by the provider."""

    id: str
    content: str
    description: str = ""
    project_id: str | None = None
    section_id: str | None = None
    priority: int = 1
    labels: list[str] = Field(default_factory=list)
    due_string: str | None = None
    due_date: str | None = None
    is_completed: bool = False
    url: str | None = None
    estimated_minutes: int | None = None
    # True when `estimated_minutes` came from configuration rather than a label,
    # so the planner can mark the block as an assumption in its summary.
    duration_assumed: bool = False


class TaskUpdateRequest(BaseModel):
    """Validated request to update a single task.

    Only fields that are set (non-None) are sent to the provider.
    """

    task_id: str
    content: str | None = None
    due_string: str | None = None
    priority: int | None = Field(default=None, ge=1, le=4)
    labels: list[str] | None = None
    project_id: str | None = None
    section_id: str | None = None

    def changed_fields(self) -> dict:
        """Return only the mutable fields the caller actually set."""
        return self.model_dump(
            exclude={"task_id"}, exclude_none=True
        )


class BulkTaskUpdateRequest(BaseModel):
    """Validated request to apply one operation across many tasks."""

    task_ids: list[str] = Field(min_length=1)
    operation: BulkOperation
    fields: dict = Field(default_factory=dict)


@runtime_checkable
class TaskProvider(Protocol):
    """Quick-add/list/search/update/complete plus gated delete/bulk operations."""

    async def quick_add(self, text: str) -> Task: ...

    async def list_inbox(self, limit: int) -> list[Task]: ...

    async def list_today(self, limit: int) -> list[Task]: ...

    async def list_overdue(self, limit: int) -> list[Task]: ...

    async def search(self, filter: str, limit: int) -> list[Task]: ...

    async def update(self, request: TaskUpdateRequest) -> Task: ...

    async def complete(self, task_id: str) -> None: ...

    async def delete(self, task_id: str) -> None: ...

    async def bulk_update(self, request: BulkTaskUpdateRequest) -> int: ...


class NullTaskProvider:
    """Placeholder provider that reports missing authentication."""

    async def quick_add(self, text: str) -> Task:
        raise AuthMissingError("tasks provider is not configured")

    async def list_inbox(self, limit: int) -> list[Task]:
        raise AuthMissingError("tasks provider is not configured")

    async def list_today(self, limit: int) -> list[Task]:
        raise AuthMissingError("tasks provider is not configured")

    async def list_overdue(self, limit: int) -> list[Task]:
        raise AuthMissingError("tasks provider is not configured")

    async def search(self, filter: str, limit: int) -> list[Task]:
        raise AuthMissingError("tasks provider is not configured")

    async def update(self, request: TaskUpdateRequest) -> Task:
        raise AuthMissingError("tasks provider is not configured")

    async def complete(self, task_id: str) -> None:
        raise AuthMissingError("tasks provider is not configured")

    async def delete(self, task_id: str) -> None:
        raise AuthMissingError("tasks provider is not configured")

    async def bulk_update(self, request: BulkTaskUpdateRequest) -> int:
        raise AuthMissingError("tasks provider is not configured")


def get_task_provider(config: "Config") -> TaskProvider:
    """Return the configured task provider, or a null provider if unavailable."""
    external = config.external_tools.tasks
    if not external.enabled:
        return NullTaskProvider()
    if external.provider == "todoist":
        from provider.tasks.todoist import TodoistTaskProvider

        return TodoistTaskProvider(config, external)
    # Unknown/unspecified provider: behave as auth-missing rather than raising.
    return NullTaskProvider()
