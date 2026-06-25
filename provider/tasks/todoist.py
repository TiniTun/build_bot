"""Todoist provider implementing the ``TaskProvider`` protocol.

Talks to the Todoist REST API v2 and API v1 quick-add over ``httpx``.
The API token is read from the environment variable named in config so it never
lives in config files or reaches the LLM. ``AuthMissingError`` is raised before
any request when the token is absent.

HTTP failures are normalized to provider exceptions (401/403 ->
``ProviderPermissionError``, 404 -> ``ProviderNotFoundError``, 400 ->
``ProviderInvalidRequestError``); unknown failures propagate and the tool layer
maps them to a generic ``provider_error``. Raw response bodies and tokens never
reach user-visible text.
"""

import os
from typing import TYPE_CHECKING, Any

import httpx

from provider.external_errors import (
    AuthMissingError,
    ProviderInvalidRequestError,
    ProviderNotFoundError,
    ProviderPermissionError,
)
from provider.tasks.base import (
    BulkTaskUpdateRequest,
    Task,
    TaskUpdateRequest,
)

if TYPE_CHECKING:
    from utils.config import Config, TasksProviderConfig


REST_BASE = "https://api.todoist.com/rest/v2"
API_V1_BASE = "https://api.todoist.com/api/v1"


def _map_status_error(exc: httpx.HTTPStatusError) -> Exception:
    """Map a Todoist HTTP status error to a provider exception."""
    status = exc.response.status_code
    if status in (401, 403):
        return ProviderPermissionError("todoist denied the request")
    if status == 404:
        return ProviderNotFoundError("todoist resource not found")
    if status == 400:
        return ProviderInvalidRequestError("todoist rejected the request")
    return exc


def _to_task(data: dict[str, Any]) -> Task:
    """Map a Todoist REST task object to the domain ``Task`` model."""
    due = data.get("due") or {}
    return Task(
        id=str(data.get("id", "")),
        content=data.get("content", ""),
        description=data.get("description", "") or "",
        project_id=_opt_str(data.get("project_id")),
        section_id=_opt_str(data.get("section_id")),
        priority=int(data.get("priority", 1) or 1),
        labels=list(data.get("labels", []) or []),
        due_string=due.get("string"),
        due_date=due.get("date"),
        is_completed=bool(data.get("is_completed", False)),
        url=data.get("url"),
    )


def _opt_str(value: Any) -> str | None:
    return str(value) if value is not None else None


class TodoistTaskProvider:
    """Todoist-backed implementation of the ``TaskProvider`` protocol."""

    def __init__(
        self,
        config: "Config",
        provider_cfg: "TasksProviderConfig",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._provider_cfg = provider_cfg
        self._token = os.environ.get(provider_cfg.api_token_env or "")
        self._client = client  # injected fake in tests

    def _auth_headers(self) -> dict[str, str]:
        if not self._token:
            raise AuthMissingError("todoist api token is not set")
        return {"Authorization": f"Bearer {self._token}"}

    async def _request(
        self, method: str, url: str, **kwargs: Any
    ) -> httpx.Response:
        """Perform an authenticated request, normalizing HTTP errors."""
        headers = {**self._auth_headers(), **kwargs.pop("headers", {})}
        if self._client is not None:
            response = await self._client.request(
                method, url, headers=headers, **kwargs
            )
        else:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.request(
                    method, url, headers=headers, **kwargs
                )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise _map_status_error(e) from e
        return response

    async def _list(self, filter: str, limit: int) -> list[Task]:
        response = await self._request(
            "GET", f"{REST_BASE}/tasks", params={"filter": filter}
        )
        items = response.json() or []
        return [_to_task(item) for item in items[:limit]]

    async def quick_add(self, text: str) -> Task:
        response = await self._request(
            "POST", f"{API_V1_BASE}/tasks/quick", json={"text": text}
        )
        data = response.json() or {}
        if isinstance(data, dict) and data:
            return _to_task(data)
        return Task(id="", content=text)

    async def list_inbox(self, limit: int) -> list[Task]:
        return await self._list("#Inbox", limit)

    async def list_today(self, limit: int) -> list[Task]:
        return await self._list("today", limit)

    async def list_overdue(self, limit: int) -> list[Task]:
        return await self._list("overdue", limit)

    async def search(self, filter: str, limit: int) -> list[Task]:
        return await self._list(filter, limit)

    async def update(self, request: TaskUpdateRequest) -> Task:
        response = await self._request(
            "POST",
            f"{REST_BASE}/tasks/{request.task_id}",
            json=request.changed_fields(),
        )
        return _to_task(response.json())

    async def complete(self, task_id: str) -> None:
        await self._request("POST", f"{REST_BASE}/tasks/{task_id}/close")

    async def delete(self, task_id: str) -> None:
        await self._request("DELETE", f"{REST_BASE}/tasks/{task_id}")

    async def bulk_update(self, request: BulkTaskUpdateRequest) -> int:
        """Apply one operation across every task id, returning the count done."""
        count = 0
        for task_id in request.task_ids:
            if request.operation == "complete":
                await self.complete(task_id)
            elif request.operation == "delete":
                await self.delete(task_id)
            else:  # "update"
                await self.update(
                    TaskUpdateRequest(task_id=task_id, **request.fields)
                )
            count += 1
        return count
