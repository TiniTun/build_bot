"""Tasks provider protocol, data models, and a null provider.

``get_task_provider`` selects ``TodoistTaskProvider`` when the tasks domain is
enabled with ``provider == "todoist"``; otherwise it returns a
``NullTaskProvider`` that raises ``AuthMissingError`` so tools surface a stable
``AUTH_MISSING`` error instead of failing opaquely.

``delete`` and ``bulk_update`` mutate destructively and are only reached through
the confirmed-execution path; the proposal tools never call them directly.
"""

from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from provider.external_errors import AuthMissingError

if TYPE_CHECKING:
    from utils.config import Config

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
