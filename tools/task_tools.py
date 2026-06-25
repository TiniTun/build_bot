"""Tasks capability tool factories (quick_add / list / search / update / ...).

External task tools are disabled by default. When ``external_tools.tasks`` is not
enabled, ``build_task_capabilities`` returns an empty list (tools hidden). When
enabled but no token is set, the provider surfaces a stable ``AUTH_MISSING``
error.

Read/quick-add/update/complete execute directly. ``tasks.delete`` and
``tasks.bulk_update`` are destructive: invoking them records a pending action and
returns a ``requires_confirmation`` result; they never reach the provider's
destructive methods without an explicit ``/confirm`` step.
"""

import uuid
from typing import TYPE_CHECKING

from pydantic import ValidationError

from core.pending_actions import PendingActionStore
from provider.tasks import (
    BulkTaskUpdateRequest,
    TaskUpdateRequest,
    get_task_provider,
)
from provider.tasks.base import Task
from tools.base import BaseTool, ToolErrorCode, ToolResult, tool
from tools.capabilities import CapabilityDef, ToolRiskLevel
from tools.confirmed_executors import ConfirmedExecutor
from tools.external_support import provider_exception_to_result

if TYPE_CHECKING:
    from core.agent import AgentSession
    from utils.config import Config


DELETE_CAPABILITY_ID = "tasks.delete"
BULK_UPDATE_CAPABILITY_ID = "tasks.bulk_update"

_REQUIRED_CONFIG = ["external_tools.tasks"]


def _format_task(task: Task) -> str:
    """Render a single task as one compact line for the LLM."""
    due = f" (due {task.due_string})" if task.due_string else ""
    labels = f" [{', '.join(task.labels)}]" if task.labels else ""
    return f"- [{task.id}] p{task.priority} {task.content}{due}{labels}"


def _format_tasks(tasks: list[Task], empty: str) -> str:
    if not tasks:
        return ToolResult.success(empty).to_tool_content()
    return ToolResult.success(
        "\n".join(_format_task(t) for t in tasks)
    ).to_tool_content()


def build_task_capabilities(
    config: "Config",
) -> list[tuple[CapabilityDef, BaseTool]]:
    """Build task capability/tool pairs, or [] when tasks are disabled."""
    if not config.external_tools.tasks.enabled:
        return []

    provider = get_task_provider(config)

    @tool(
        name="tasks_quick_add",
        description=(
            "Quickly add a task from natural language (Todoist quick-add syntax, "
            "e.g. 'Pay rent tomorrow at 9am #Finance p1')."
        ),
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Quick-add task text."},
            },
            "required": ["text"],
        },
    )
    async def tasks_quick_add(text: str, session: "AgentSession") -> str:
        if not text.strip():
            return ToolResult.error(
                ToolErrorCode.INVALID_ARGS, "Task text must not be empty."
            ).to_tool_content()
        try:
            task = await provider.quick_add(text)
        except Exception as e:  # noqa: BLE001 - mapped to stable error codes
            return provider_exception_to_result(e).to_tool_content()
        return ToolResult.success(
            f"Task added (id={task.id}): {task.content}"
        ).to_tool_content()

    @tool(
        name="tasks_inbox",
        description="List tasks in the Inbox.",
        parameters={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Max tasks to return.",
                    "default": 20,
                },
            },
        },
    )
    async def tasks_inbox(session: "AgentSession", limit: int = 20) -> str:
        try:
            tasks = await provider.list_inbox(limit)
        except Exception as e:  # noqa: BLE001 - mapped to stable error codes
            return provider_exception_to_result(e).to_tool_content()
        return _format_tasks(tasks, "Inbox is empty.")

    @tool(
        name="tasks_today",
        description="List tasks due today.",
        parameters={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Max tasks to return.",
                    "default": 20,
                },
            },
        },
    )
    async def tasks_today(session: "AgentSession", limit: int = 20) -> str:
        try:
            tasks = await provider.list_today(limit)
        except Exception as e:  # noqa: BLE001 - mapped to stable error codes
            return provider_exception_to_result(e).to_tool_content()
        return _format_tasks(tasks, "No tasks due today.")

    @tool(
        name="tasks_overdue",
        description="List overdue tasks.",
        parameters={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Max tasks to return.",
                    "default": 20,
                },
            },
        },
    )
    async def tasks_overdue(session: "AgentSession", limit: int = 20) -> str:
        try:
            tasks = await provider.list_overdue(limit)
        except Exception as e:  # noqa: BLE001 - mapped to stable error codes
            return provider_exception_to_result(e).to_tool_content()
        return _format_tasks(tasks, "No overdue tasks.")

    @tool(
        name="tasks_search",
        description=(
            "Search tasks using a Todoist filter query "
            "(e.g. 'today & @work', '#Project', 'p1')."
        ),
        parameters={
            "type": "object",
            "properties": {
                "filter": {"type": "string", "description": "Todoist filter query."},
                "limit": {
                    "type": "integer",
                    "description": "Max tasks to return.",
                    "default": 20,
                },
            },
            "required": ["filter"],
        },
    )
    async def tasks_search(
        filter: str, session: "AgentSession", limit: int = 20
    ) -> str:
        if not filter.strip():
            return ToolResult.error(
                ToolErrorCode.INVALID_ARGS, "A filter query is required."
            ).to_tool_content()
        try:
            tasks = await provider.search(filter, limit)
        except Exception as e:  # noqa: BLE001 - mapped to stable error codes
            return provider_exception_to_result(e).to_tool_content()
        return _format_tasks(tasks, "No matching tasks found.")

    @tool(
        name="tasks_update",
        description="Update fields of an existing task.",
        parameters={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task id to update."},
                "content": {"type": "string", "description": "New task content."},
                "due_string": {
                    "type": "string",
                    "description": "Natural-language due date (e.g. 'tomorrow 9am').",
                },
                "priority": {
                    "type": "integer",
                    "description": "Priority 1 (normal) to 4 (urgent).",
                },
                "labels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Replacement label set.",
                },
                "project_id": {"type": "string", "description": "Move to project id."},
                "section_id": {"type": "string", "description": "Move to section id."},
            },
            "required": ["task_id"],
        },
    )
    async def tasks_update(
        task_id: str,
        session: "AgentSession",
        content: str | None = None,
        due_string: str | None = None,
        priority: int | None = None,
        labels: list[str] | None = None,
        project_id: str | None = None,
        section_id: str | None = None,
    ) -> str:
        try:
            request = TaskUpdateRequest(
                task_id=task_id,
                content=content,
                due_string=due_string,
                priority=priority,
                labels=labels,
                project_id=project_id,
                section_id=section_id,
            )
        except ValidationError as e:
            return ToolResult.error(
                ToolErrorCode.INVALID_ARGS,
                f"Invalid update request: {e.error_count()} field error(s).",
                user_action="Provide a task_id and at least one valid field.",
            ).to_tool_content()
        if not request.changed_fields():
            return ToolResult.error(
                ToolErrorCode.INVALID_ARGS,
                "No fields to update were provided.",
                user_action="Provide at least one field to change.",
            ).to_tool_content()
        try:
            task = await provider.update(request)
        except Exception as e:  # noqa: BLE001 - mapped to stable error codes
            return provider_exception_to_result(e).to_tool_content()
        return ToolResult.success(
            f"Task updated (id={task.id}): {task.content}"
        ).to_tool_content()

    @tool(
        name="tasks_complete",
        description="Mark a task as complete.",
        parameters={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task id to complete."},
            },
            "required": ["task_id"],
        },
    )
    async def tasks_complete(task_id: str, session: "AgentSession") -> str:
        if not task_id.strip():
            return ToolResult.error(
                ToolErrorCode.INVALID_ARGS, "A task_id is required."
            ).to_tool_content()
        try:
            await provider.complete(task_id)
        except Exception as e:  # noqa: BLE001 - mapped to stable error codes
            return provider_exception_to_result(e).to_tool_content()
        return ToolResult.success(f"Task {task_id} completed.").to_tool_content()

    @tool(
        name="tasks_delete",
        description=(
            "Propose deleting a task. This requires user confirmation and does "
            "not delete the task directly."
        ),
        parameters={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task id to delete."},
            },
            "required": ["task_id"],
        },
    )
    async def tasks_delete(task_id: str, session: "AgentSession") -> str:
        # Self-gate: record a pending action; never call the provider here.
        if not task_id.strip():
            return ToolResult.error(
                ToolErrorCode.INVALID_ARGS,
                "A non-empty task_id is required.",
                user_action="Provide the task_id to delete.",
            ).to_tool_content()

        action_id = str(uuid.uuid4())
        summary = f"Delete task {task_id}"
        payload = {"task_id": task_id}
        PendingActionStore(session.shared_context.config).create(
            action_id=action_id,
            capability_id=DELETE_CAPABILITY_ID,
            summary=summary,
            payload=payload,
        )
        return ToolResult.requires_confirmation(
            action_id=action_id,
            capability_id=DELETE_CAPABILITY_ID,
            summary=summary,
            payload=payload,
        ).to_tool_content()

    @tool(
        name="tasks_bulk_update",
        description=(
            "Propose a bulk operation (update/complete/delete) over several tasks. "
            "This requires user confirmation and does not change tasks directly."
        ),
        parameters={
            "type": "object",
            "properties": {
                "task_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Task ids to operate on.",
                },
                "operation": {
                    "type": "string",
                    "enum": ["update", "complete", "delete"],
                    "description": "Operation to apply to every task.",
                },
                "fields": {
                    "type": "object",
                    "description": "Fields to set when operation is 'update'.",
                },
            },
            "required": ["task_ids", "operation"],
        },
    )
    async def tasks_bulk_update(
        task_ids: list[str],
        operation: str,
        session: "AgentSession",
        fields: dict | None = None,
    ) -> str:
        # Self-gate: validate the request shape, then record a pending action.
        try:
            request = BulkTaskUpdateRequest(
                task_ids=task_ids,
                operation=operation,  # type: ignore[arg-type]
                fields=fields or {},
            )
        except ValidationError as e:
            return ToolResult.error(
                ToolErrorCode.INVALID_ARGS,
                f"Invalid bulk request: {e.error_count()} field error(s).",
                user_action=(
                    "Provide a non-empty task_ids list and a valid operation "
                    "(update/complete/delete)."
                ),
            ).to_tool_content()

        action_id = str(uuid.uuid4())
        summary = f"Bulk {request.operation} on {len(request.task_ids)} task(s)"
        payload = request.model_dump()
        PendingActionStore(session.shared_context.config).create(
            action_id=action_id,
            capability_id=BULK_UPDATE_CAPABILITY_ID,
            summary=summary,
            payload=payload,
        )
        return ToolResult.requires_confirmation(
            action_id=action_id,
            capability_id=BULK_UPDATE_CAPABILITY_ID,
            summary=summary,
            payload=payload,
        ).to_tool_content()

    return [
        (
            CapabilityDef(
                id="tasks.quick_add",
                tool_name="tasks_quick_add",
                domain="tasks",
                operation="quick_add",
                description="Quickly add a task from natural language.",
                risk_level=ToolRiskLevel.WRITE,
                required_config=_REQUIRED_CONFIG,
            ),
            tasks_quick_add,
        ),
        (
            CapabilityDef(
                id="tasks.inbox",
                tool_name="tasks_inbox",
                domain="tasks",
                operation="inbox",
                description="List Inbox tasks.",
                risk_level=ToolRiskLevel.READ,
                required_config=_REQUIRED_CONFIG,
            ),
            tasks_inbox,
        ),
        (
            CapabilityDef(
                id="tasks.today",
                tool_name="tasks_today",
                domain="tasks",
                operation="today",
                description="List tasks due today.",
                risk_level=ToolRiskLevel.READ,
                required_config=_REQUIRED_CONFIG,
            ),
            tasks_today,
        ),
        (
            CapabilityDef(
                id="tasks.overdue",
                tool_name="tasks_overdue",
                domain="tasks",
                operation="overdue",
                description="List overdue tasks.",
                risk_level=ToolRiskLevel.READ,
                required_config=_REQUIRED_CONFIG,
            ),
            tasks_overdue,
        ),
        (
            CapabilityDef(
                id="tasks.search",
                tool_name="tasks_search",
                domain="tasks",
                operation="search",
                description="Search tasks with a filter query.",
                risk_level=ToolRiskLevel.READ,
                required_config=_REQUIRED_CONFIG,
            ),
            tasks_search,
        ),
        (
            CapabilityDef(
                id="tasks.update",
                tool_name="tasks_update",
                domain="tasks",
                operation="update",
                description="Update fields of a task.",
                risk_level=ToolRiskLevel.WRITE,
                required_config=_REQUIRED_CONFIG,
            ),
            tasks_update,
        ),
        (
            CapabilityDef(
                id="tasks.complete",
                tool_name="tasks_complete",
                domain="tasks",
                operation="complete",
                description="Complete a task.",
                risk_level=ToolRiskLevel.WRITE,
                required_config=_REQUIRED_CONFIG,
            ),
            tasks_complete,
        ),
        (
            CapabilityDef(
                id=DELETE_CAPABILITY_ID,
                tool_name="tasks_delete",
                domain="tasks",
                operation="delete",
                description="Delete a task after explicit confirmation.",
                risk_level=ToolRiskLevel.CONFIRM_REQUIRED,
                required_config=_REQUIRED_CONFIG,
            ),
            tasks_delete,
        ),
        (
            CapabilityDef(
                id=BULK_UPDATE_CAPABILITY_ID,
                tool_name="tasks_bulk_update",
                domain="tasks",
                operation="bulk_update",
                description="Apply a bulk task operation after explicit confirmation.",
                risk_level=ToolRiskLevel.CONFIRM_REQUIRED,
                required_config=_REQUIRED_CONFIG,
            ),
            tasks_bulk_update,
        ),
    ]


def build_task_confirmed_executors(
    config: "Config",
) -> dict[str, ConfirmedExecutor]:
    """Confirmed executors for task mutations, or {} when tasks are disabled.

    Each executor performs the real provider mutation exactly once. ``/confirm``
    routes a stored pending action here instead of re-invoking the proposal tool,
    so confirming never creates another pending action.
    """
    if not config.external_tools.tasks.enabled:
        return {}

    provider = get_task_provider(config)

    async def delete_confirmed(session: "AgentSession", payload: dict) -> str:
        task_id = payload.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            return ToolResult.error(
                ToolErrorCode.INVALID_ARGS,
                "Stored delete request is invalid.",
            ).to_tool_content()
        try:
            await provider.delete(task_id)
        except Exception as e:  # noqa: BLE001 - mapped to stable error codes
            return provider_exception_to_result(e).to_tool_content()
        return ToolResult.success(f"Task {task_id} deleted.").to_tool_content()

    async def bulk_update_confirmed(
        session: "AgentSession", payload: dict
    ) -> str:
        try:
            request = BulkTaskUpdateRequest(**payload)
        except (ValidationError, TypeError):
            return ToolResult.error(
                ToolErrorCode.INVALID_ARGS,
                "Stored bulk request is invalid.",
            ).to_tool_content()
        try:
            count = await provider.bulk_update(request)
        except Exception as e:  # noqa: BLE001 - mapped to stable error codes
            return provider_exception_to_result(e).to_tool_content()
        return ToolResult.success(
            f"Bulk {request.operation} applied to {count} task(s)."
        ).to_tool_content()

    return {
        DELETE_CAPABILITY_ID: delete_confirmed,
        BULK_UPDATE_CAPABILITY_ID: bulk_update_confirmed,
    }
