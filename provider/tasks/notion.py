"""Notion provider implementing the ``TaskProvider`` protocol over the Notion MCP.

Every call is a host call through ``McpHub.call_tool`` (no LLM in the loop):
reads use ``notion-query-data-sources`` in SQL mode with every value bound
through ``params``; writes use ``notion-create-pages`` and ``notion-update-page``
(``update_properties``).

Query responses are parsed strictly. Any shape surprise, or an ``is_error``
result, raises ``ProviderInvalidResponseError`` whose message names the problem
but never carries row data. ``McpError`` from the hub propagates unchanged and
the tool layer maps it to a generic ``provider_error``.

Field mapping (spec §5): Name <-> content, P1..P4 <-> priority 4..1, Due ->
due_date/due_string, Estimate (min) -> estimated_minutes (else the configured
default, marked assumed). ``delete`` sets Status = Dropped rather than removing
the page; it is still only reachable through ``/confirm``.
"""

import json
import logging
import re
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from provider.external_errors import (
    ProviderInvalidRequestError,
    ProviderInvalidResponseError,
)
from provider.tasks.base import (
    BulkTaskUpdateRequest,
    Task,
    TaskUpdateRequest,
    apply_bulk_update,
)

if TYPE_CHECKING:
    from provider.mcp.hub import McpHub
    from provider.mcp.models import McpCallResult
    from utils.config import Config, TasksProviderConfig

logger = logging.getLogger(__name__)

QUERY_TOOL = "notion-query-data-sources"
CREATE_TOOL = "notion-create-pages"
UPDATE_TOOL = "notion-update-page"

STATUS_INBOX = "Inbox"
STATUS_DOING = "Doing"
STATUS_DONE = "Done"
STATUS_DROPPED = "Dropped"

_PRIORITY_TO_LABEL = {4: "P1", 3: "P2", 2: "P3", 1: "P4"}
_LABEL_TO_PRIORITY = {label: value for value, label in _PRIORITY_TO_LABEL.items()}

_COLUMNS = (
    "url",
    "Task ID",
    "Name",
    "Status",
    "Priority",
    "date:Due:start",
    "date:Due:is_datetime",
    "date:Scheduled:start",
    "Estimate (min)",
    "Area",
)
_SELECT = ", ".join(f'"{c}"' if c != "url" else c for c in _COLUMNS)
_ORDER = 'ORDER BY "Priority" IS NULL, "Priority"'
_MAX_LIMIT = 100

# A Notion page id is the trailing 32 hex digits of its URL path.
_PAGE_ID_IN_URL = re.compile(r"([0-9a-fA-F]{32})$")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")


def _page_id_from_url(url: str) -> str | None:
    path = url.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    match = _PAGE_ID_IN_URL.search(path)
    return match.group(1).lower() if match else None


def _require_ok(result: "McpCallResult", tool: str) -> None:
    if result.is_error:
        raise ProviderInvalidResponseError(f"notion {tool} returned an error result")


def _single_text(result: "McpCallResult", tool: str) -> str:
    if len(result.text_blocks) != 1:
        raise ProviderInvalidResponseError(
            f"notion {tool} returned {len(result.text_blocks)} text blocks, expected 1"
        )
    return result.text_blocks[0]


def _opt(row: dict, key: str, kinds: tuple[type, ...]) -> Any:
    """Return ``row[key]`` when it is None or one of ``kinds``; else raise."""
    if key not in row:
        raise ProviderInvalidResponseError(f"notion task row is missing column {key!r}")
    value = row[key]
    # bool is an int subclass; a checkbox where a number belongs is a mismatch.
    if value is None or (isinstance(value, kinds) and not isinstance(value, bool)):
        return value
    raise ProviderInvalidResponseError(f"notion task column {key!r} has an unexpected type")


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class NotionTaskProvider:
    """Notion-backed implementation of the ``TaskProvider`` protocol."""

    def __init__(
        self,
        config: "Config",
        provider_cfg: "TasksProviderConfig",
        hub: "McpHub",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not provider_cfg.mcp_server or not provider_cfg.data_source_id:
            raise ValueError("notion tasks need mcp_server and data_source_id")
        self._config = config
        self._provider_cfg = provider_cfg
        self._hub = hub
        self._server = provider_cfg.mcp_server
        self._data_source_id = provider_cfg.data_source_id
        self._collection = f"collection://{provider_cfg.data_source_id}"
        self._clock = clock or (lambda: datetime.now(UTC))

    # -- reads ---------------------------------------------------------------

    def _today(self) -> date:
        """Today in the configured timezone (UTC when none is configured)."""
        tz = ZoneInfo(self._config.timezone or "UTC")
        return self._clock().astimezone(tz).date()

    async def _query(self, where: str, params: list[Any], limit: int) -> list[Task]:
        bounded = max(1, min(int(limit), _MAX_LIMIT))
        query = (
            f'SELECT {_SELECT} FROM "{self._collection}" WHERE {where} '
            f"{_ORDER} LIMIT ?"
        )
        result = await self._hub.call_tool(
            self._server,
            QUERY_TOOL,
            {
                "data": {
                    "data_source_urls": [self._collection],
                    "query": query,
                    "params": [*params, bounded],
                }
            },
        )
        _require_ok(result, QUERY_TOOL)
        try:
            payload = json.loads(_single_text(result, QUERY_TOOL))
        except json.JSONDecodeError:
            raise ProviderInvalidResponseError(
                f"notion {QUERY_TOOL} returned non-JSON text"
            ) from None
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise ProviderInvalidResponseError(
                f"notion {QUERY_TOOL} payload has no results list"
            )
        return [self._to_task(row) for row in payload["results"][:bounded]]

    def _to_task(self, row: Any) -> Task:
        if not isinstance(row, dict):
            raise ProviderInvalidResponseError("notion task row is not an object")
        url = _opt(row, "url", (str,))
        page_id = _page_id_from_url(url) if url else None
        if page_id is None:
            raise ProviderInvalidResponseError("notion task row has no page id in its url")
        name = _opt(row, "Name", (str,)) or ""
        status = _opt(row, "Status", (str,))
        priority_label = _opt(row, "Priority", (str,))
        if priority_label is not None and priority_label not in _LABEL_TO_PRIORITY:
            raise ProviderInvalidResponseError("notion task has an unknown Priority value")
        due = _opt(row, "date:Due:start", (str,))
        _opt(row, "date:Due:is_datetime", (int,))
        _opt(row, "date:Scheduled:start", (str,))
        _opt(row, "Task ID", (int,))
        _opt(row, "Area", (str,))
        estimate = _opt(row, "Estimate (min)", (int, float))
        if due is not None and not (_ISO_DATE.match(due) or _ISO_DATETIME.match(due)):
            raise ProviderInvalidResponseError("notion task Due is not an ISO date")

        stated = int(round(estimate)) if estimate is not None else None
        if stated is not None and stated <= 0:
            stated = None
        return Task(
            id=page_id,
            content=name,
            priority=_LABEL_TO_PRIORITY.get(priority_label, 1),
            due_string=due,
            due_date=due[:10] if due else None,
            is_completed=status == STATUS_DONE,
            url=url,
            estimated_minutes=(
                stated if stated is not None
                else self._provider_cfg.default_duration_minutes
            ),
            duration_assumed=stated is None,
        )

    async def list_inbox(self, limit: int) -> list[Task]:
        return await self._query('"Status" = ?', [STATUS_INBOX], limit)

    async def list_today(self, limit: int) -> list[Task]:
        # "<= today" as "< tomorrow": a Due/Scheduled datetime later today is a
        # longer string than today's date and would fail a "<= today" compare.
        tomorrow = (self._today() + timedelta(days=1)).isoformat()
        where = (
            '("Status" = ? OR ("Status" NOT IN (?, ?) AND '
            '("date:Scheduled:start" < ? OR "date:Due:start" < ?)))'
        )
        params = [STATUS_DOING, STATUS_DONE, STATUS_DROPPED, tomorrow, tomorrow]
        return await self._query(where, params, limit)

    async def list_overdue(self, limit: int) -> list[Task]:
        where = '"Status" NOT IN (?, ?) AND "date:Due:start" < ?'
        params = [STATUS_DONE, STATUS_DROPPED, self._today().isoformat()]
        return await self._query(where, params, limit)

    async def search(self, filter: str, limit: int) -> list[Task]:
        # LIKE/LOWER fold ASCII only; non-ASCII names match case-sensitively.
        where = '"Status" NOT IN (?, ?) AND LOWER("Name") LIKE ? ESCAPE \'\\\''
        pattern = f"%{_escape_like(filter.strip().lower())}%"
        return await self._query(where, [STATUS_DONE, STATUS_DROPPED, pattern], limit)

    # -- writes --------------------------------------------------------------

    async def _update_properties(self, task_id: str, properties: dict[str, Any]) -> None:
        result = await self._hub.call_tool(
            self._server,
            UPDATE_TOOL,
            {"page_id": task_id, "command": "update_properties", "properties": properties},
        )
        _require_ok(result, UPDATE_TOOL)

    async def quick_add(self, text: str) -> Task:
        # Inbox means "not triaged yet": Area and Project stay empty, and no
        # date is parsed out of the text.
        result = await self._hub.call_tool(
            self._server,
            CREATE_TOOL,
            {
                "parent": {"type": "data_source_id", "data_source_id": self._data_source_id},
                "pages": [{"properties": {
                    "Name": text,
                    "Status": STATUS_INBOX,
                    "Source": "Telegram",
                    "Created by agent": "__YES__",
                }}],
            },
        )
        _require_ok(result, CREATE_TOOL)
        created = _created_page(result)
        if created is None:
            # The page exists already; raising would invite a duplicate on retry.
            logger.warning("notion create-pages response had no page id; task created")
            return Task(id="", content=text)
        page_id, url = created
        return Task(
            id=page_id,
            content=text,
            url=url,
            estimated_minutes=self._provider_cfg.default_duration_minutes,
            duration_assumed=True,
        )

    async def update(self, request: TaskUpdateRequest) -> Task:
        unsupported = [
            name for name in ("labels", "project_id", "section_id")
            if getattr(request, name) is not None
        ]
        if unsupported:
            raise ProviderInvalidRequestError(
                f"{', '.join(unsupported)} not supported by the notion provider"
            )
        properties: dict[str, Any] = {}
        if request.content is not None:
            properties["Name"] = request.content
        if request.due_string is not None:
            properties.update(_due_properties(request.due_string))
        if request.priority is not None:
            properties["Priority"] = _PRIORITY_TO_LABEL[request.priority]
        await self._update_properties(request.task_id, properties)
        return Task(
            id=request.task_id,
            content=request.content or "",
            priority=request.priority or 1,
            due_string=request.due_string,
        )

    async def complete(self, task_id: str) -> None:
        await self._update_properties(task_id, {"Status": STATUS_DONE})

    async def delete(self, task_id: str) -> None:
        await self._update_properties(task_id, {"Status": STATUS_DROPPED})

    async def bulk_update(self, request: BulkTaskUpdateRequest) -> int:
        return await apply_bulk_update(self, request)


def _due_properties(due_string: str) -> dict[str, Any]:
    """Map an ISO date/datetime to Notion's expanded Due properties."""
    value = due_string.strip()
    if _ISO_DATE.match(value):
        try:
            date.fromisoformat(value)
        except ValueError:
            raise ProviderInvalidRequestError("due_string is not a valid ISO date") from None
        return {"date:Due:start": value, "date:Due:is_datetime": 0}
    if _ISO_DATETIME.match(value):
        try:
            datetime.fromisoformat(value)
        except ValueError:
            raise ProviderInvalidRequestError(
                "due_string is not a valid ISO datetime"
            ) from None
        return {"date:Due:start": value, "date:Due:is_datetime": 1}
    raise ProviderInvalidRequestError(
        "the notion provider needs due_string as an ISO date (YYYY-MM-DD) or datetime"
    )


def _created_page(result: "McpCallResult") -> tuple[str, str | None] | None:
    """Best-effort (page id, url) from a create-pages result, or None."""
    payload: Any = result.structured_content
    if payload is None and len(result.text_blocks) == 1:
        try:
            payload = json.loads(result.text_blocks[0])
        except json.JSONDecodeError:
            return None
    if isinstance(payload, dict) and isinstance(payload.get("pages"), list):
        pages = payload["pages"]
        payload = pages[0] if len(pages) == 1 else None
    if not isinstance(payload, dict):
        return None
    url = payload.get("url") if isinstance(payload.get("url"), str) else None
    raw_id = payload.get("id")
    page_id = None
    if isinstance(raw_id, str) and re.fullmatch(r"[0-9a-fA-F-]{32,36}", raw_id):
        page_id = raw_id.replace("-", "").lower()
        if len(page_id) != 32:
            page_id = None
    if page_id is None and url:
        page_id = _page_id_from_url(url)
    return (page_id, url) if page_id else None
