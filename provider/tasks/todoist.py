"""Todoist provider implementing the ``TaskProvider`` protocol.

Talks to the Todoist unified API v1 over ``httpx``. REST v2 was retired and
answers ``410 Gone``. List endpoints wrap tasks in ``results`` and paginate with
``next_cursor``; ``_list`` follows the cursor until ``limit`` tasks are read.
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
    apply_bulk_update,
    parse_duration_label,
)

if TYPE_CHECKING:
    from utils.config import Config, TasksProviderConfig


API_V1_BASE = "https://api.todoist.com/api/v1"
# Todoist caps a page of results at 200 tasks.
MAX_PAGE_SIZE = 200


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


def _to_task(data: dict[str, Any], default_minutes: int) -> Task:
    """Map a Todoist API v1 task object to the domain ``Task`` model."""
    due = data.get("due") or {}
    labels = list(data.get("labels", []) or [])
    labelled = parse_duration_label(labels)
    return Task(
        id=str(data.get("id", "")),
        content=data.get("content", ""),
        description=data.get("description", "") or "",
        project_id=_opt_str(data.get("project_id")),
        section_id=_opt_str(data.get("section_id")),
        priority=int(data.get("priority", 1) or 1),
        labels=labels,
        due_string=due.get("string"),
        due_date=due.get("date"),
        is_completed=_is_completed(data),
        url=data.get("url"),
        estimated_minutes=labelled if labelled is not None else default_minutes,
        duration_assumed=labelled is None,
    )


def _is_completed(data: dict[str, Any]) -> bool:
    """API v1 reports completion as ``checked``; older shapes used ``is_completed``."""
    return bool(
        data.get("checked")
        or data.get("is_completed")
        or data.get("completed_at")
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
        """Read up to ``limit`` tasks matching a Todoist filter query."""
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        while len(items) < limit:
            params: dict[str, Any] = {
                "query": filter,
                "limit": min(limit - len(items), MAX_PAGE_SIZE),
            }
            if cursor:
                params["cursor"] = cursor
            response = await self._request(
                "GET", f"{API_V1_BASE}/tasks/filter", params=params
            )
            page = response.json() or {}
            items.extend(page.get("results") or [])
            cursor = page.get("next_cursor")
            if not cursor:
                break
        default_minutes = self._provider_cfg.default_duration_minutes
        return [_to_task(item, default_minutes) for item in items[:limit]]

    async def quick_add(self, text: str) -> Task:
        response = await self._request(
            "POST", f"{API_V1_BASE}/tasks/quick", json={"text": text}
        )
        data = response.json() or {}
        if isinstance(data, dict) and data:
            return _to_task(data, self._provider_cfg.default_duration_minutes)
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
            f"{API_V1_BASE}/tasks/{request.task_id}",
            json=request.changed_fields(),
        )
        return _to_task(response.json(), self._provider_cfg.default_duration_minutes)

    async def complete(self, task_id: str) -> None:
        await self._request("POST", f"{API_V1_BASE}/tasks/{task_id}/close")

    async def delete(self, task_id: str) -> None:
        await self._request("DELETE", f"{API_V1_BASE}/tasks/{task_id}")

    async def bulk_update(self, request: BulkTaskUpdateRequest) -> int:
        """Apply one operation across every task id, returning the count done."""
        return await apply_bulk_update(self, request)
