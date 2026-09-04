"""Planning capability tools: build a day plan, and sync it to a calendar.

This is the only planning module that performs I/O. It gathers inputs
(calendar, tasks, patterns, readiness via ``McpHub``), hands them to the pure
engine, and applies the resulting diff according to ``planning.mode``.

Readiness is fetched here, host-side. The agent is never granted the MCP
capability, so no WHOOP metric can reach a transcript.
"""

import logging
import uuid
from typing import TYPE_CHECKING

from pydantic import ValidationError

from core.pending_actions import PendingActionStore
from core.planning.engine import build_day_plan, project_readiness
from core.planning.models import DayPatternSet
from core.planning.sync import (
    GuardError, PlanRunLedger, SyncPlan, check_guards, diff, marker,
)
from provider.calendar import CreateEventRequest, get_calendar_provider
from provider.tasks import get_task_provider
from tools.base import BaseTool, ToolErrorCode, ToolResult, tool
from tools.capabilities import CapabilityDef, ToolRiskLevel
from tools.confirmed_executors import ConfirmedExecutor
from tools.external_support import provider_exception_to_result

if TYPE_CHECKING:
    from core.agent import AgentSession
    from core.context import SharedContext
    from core.planning.models import DayPlan, PatternLimits, ReadinessSignal
    from provider.calendar.base import CalendarProvider
    from utils.config import Config

logger = logging.getLogger(__name__)

SYNC_CAPABILITY_ID = "planning.sync_daily_plan"
HEALTH_SERVER_ID = "health_planner"
READINESS_TOOL = "get_daily_readiness"
STATUS_TOOL = "whoop_status"

_WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


async def _readiness(context: "SharedContext", day: str) -> "ReadinessSignal":
    """Fetch and project readiness. Never raises; degrades to `unknown`."""
    hub = getattr(context, "mcp_hub", None)
    if hub is None:
        return project_readiness(None, planned_date=day)
    try:
        # No `date` argument: health_planner's clock owns which day is today.
        result = await hub.call_tool(HEALTH_SERVER_ID, READINESS_TOOL, {})
    except Exception:  # noqa: BLE001 - an outage must degrade, not crash
        logger.warning("health_planner readiness call failed", exc_info=True)
        result = None

    status = None
    if result is None or result.is_error:
        try:
            probe = await hub.call_tool(HEALTH_SERVER_ID, STATUS_TOOL, {})
            status = probe.structured_content
        except Exception:  # noqa: BLE001 - the probe is best-effort
            logger.debug("whoop_status probe failed", exc_info=True)

    signal = project_readiness(result, planned_date=day, status=status)
    if result is not None and result.structured_content:
        # Operator-facing only. These carry recovery scores and sleep
        # durations and must never reach the model.
        logger.debug(
            "readiness reasons: %s",
            result.structured_content.get("reasons"),
        )
    return signal


def _limits_for(config: "Config", day: str) -> "PatternLimits":
    """The pattern limits governing this weekday, with its overrides applied."""
    from datetime import date as _date

    patterns = DayPatternSet.load(config.planning.patterns_path)
    weekday = _WEEKDAY_KEYS[_date.fromisoformat(day).weekday()]
    return patterns.limits_for(weekday)


def _read_calendar_id(config: "Config") -> str:
    """The already-resolved read calendar id, never `None`.

    `check_guards` compares this against `planning_calendar_id` verbatim and
    deliberately does not resolve it itself; an unresolved `None` would make
    that guard blind to the read calendar actually being the default
    "primary". Both the tool body and the confirmed executor call this same
    helper so the resolution can never drift between the two paths.
    """
    return config.external_tools.calendar.calendar_id or "primary"


def _config_or_provider_error(exc: Exception, config: "Config") -> ToolResult:
    """Map a malformed ``day_patterns.yaml`` or a provider failure alike.

    Checked in this order because a pattern-loading failure is a config
    problem (``invalid_args`` naming the file), never a provider outage --
    `provider_exception_to_result` has no branch for it and would otherwise
    mislabel it `provider_error`.
    """
    if isinstance(exc, (ValidationError, OSError)):
        return ToolResult.error(
            ToolErrorCode.INVALID_ARGS,
            f"{config.planning.patterns_path} is malformed: {exc}",
            user_action="Fix the day patterns file before syncing.",
        )
    return provider_exception_to_result(exc)


async def _compose(session: "AgentSession", day: str) -> "DayPlan":
    """Gather every input and hand them to the pure engine.

    The only place a DayPlan is built. The sync tool and the confirmed
    executor both go through here, so a plan proposed in `review` mode and the
    plan recomputed at `/confirm` time are produced by identical code.
    """
    from datetime import date as _date

    config = session.shared_context.config
    timezone = config.timezone or "UTC"
    weekday = _WEEKDAY_KEYS[_date.fromisoformat(day).weekday()]

    patterns = DayPatternSet.load(config.planning.patterns_path)
    limits = patterns.limits_for(weekday)
    activities = patterns.activities_for(weekday)

    calendar = get_calendar_provider(config)
    # The user's own calendar, read-only. These events are mandatory and are
    # only ever subtracted from free time.
    agenda = await calendar.list_day(day, timezone)

    tasks_provider = get_task_provider(config)
    try:
        today = await tasks_provider.list_today(limit=50)
        overdue = await tasks_provider.list_overdue(limit=50)
    except Exception:  # noqa: BLE001 - a task outage must not lose the day
        logger.warning("todoist unavailable; planning without tasks", exc_info=True)
        today, overdue = [], []

    overdue_ids = {task.id for task in overdue}
    # Overdue wins on duplicates; select_candidates orders them first anyway.
    merged = {task.id: task for task in today}
    merged.update({task.id: task for task in overdue})

    signal = await _readiness(session.shared_context, day)

    return build_day_plan(
        day=day, timezone=timezone, agenda=agenda, tasks=list(merged.values()),
        patterns=activities, limits=limits, signal=signal,
        weekday=weekday, overdue_ids=overdue_ids,
    )


def _render(sync: SyncPlan) -> str:
    """Human-readable diff for shadow mode and for the applied-run summary."""
    lines = [
        f"create: {a.title} ({a.start} → {a.end})" for a in sync.creates
    ] + [
        f"update: {a.title} ({a.start} → {a.end})" for a in sync.patches
    ] + [
        f"remove: {a.title}" for a in sync.deletes
    ]
    if sync.unchanged:
        lines.append(f"unchanged: {len(sync.unchanged)}")
    if sync.refused_foreign:
        lines.append(
            f"left alone (not ours): {len(sync.refused_foreign)}"
        )
    return "\n".join(lines) or "nothing to change"


def _render_plan(plan: "DayPlan") -> str:
    """The plan as text for the model to narrate.

    Carries the verdict word and the planner-authored note — never a metric,
    never the server's `reasons`.
    """
    lines = [f"readiness: {plan.readiness.verdict} — {plan.readiness.note}"]
    for event in plan.events:
        assumed = " (duration assumed)" if event.duration_assumed else ""
        lines.append(
            f"{event.start:%H:%M}-{event.end:%H:%M} {event.title}{assumed}"
        )
    for item in plan.unscheduled:
        lines.append(f"not scheduled: {item.title} ({item.reason})")
    for event in plan.mandatory:
        when = "all day" if event.all_day else f"{event.start} → {event.end}"
        lines.append(f"existing: {event.title} ({when})")
    return "\n".join(lines)


async def _apply(
    provider: "CalendarProvider", sync: SyncPlan, date: str, calendar_id: str
) -> str:
    """Apply a diff. Attendees are always empty; markers are always stamped."""
    for action in sync.creates:
        await provider.create_event(
            CreateEventRequest(
                title=action.title, start=action.start, end=action.end,
                attendees=[],                       # never invite anyone
                description=f"slot_key: {action.slot_key}",
                private_properties=marker(date, action.slot_key),
            ),
            calendar_id=calendar_id,
        )
    for action in sync.patches:
        await provider.update_event(
            action.event_id,
            CreateEventRequest(
                title=action.title, start=action.start, end=action.end,
                attendees=[],
                description=f"slot_key: {action.slot_key}",
                private_properties=marker(date, action.slot_key),
            ),
            calendar_id=calendar_id,
        )
    for action in sync.deletes:
        await provider.delete_event(action.event_id, calendar_id=calendar_id)
    return (
        f"Synced {date}: {len(sync.creates)} created, {len(sync.patches)} "
        f"updated, {len(sync.deletes)} removed, {len(sync.unchanged)} unchanged."
    )


def _before_deadline(config: "Config") -> bool:
    """True while the morning window is still open.

    Compared in the configured timezone — the same clock CronWorker ticks on,
    so `plan_deadline` and the schedule's last tick mean the same instant.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    timezone = config.timezone
    now = datetime.now(ZoneInfo(timezone)) if timezone else datetime.now()
    return now.strftime("%H:%M") < config.planning.plan_deadline


def build_planning_capabilities(config: "Config") -> list[tuple[CapabilityDef, BaseTool]]:
    if config.planning is None or not config.planning.enabled:
        return []

    @tool(
        name="planning_build_day_plan",
        description=(
            "Compose today's plan deterministically from the calendar, tasks, "
            "the weekly routine and WHOOP readiness. Writes nothing."
        ),
        parameters={
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "The day, as YYYY-MM-DD."},
            },
            "required": ["date"],
        },
    )
    async def planning_build_day_plan(date: str, session: "AgentSession") -> str:
        config = session.shared_context.config
        ledger = PlanRunLedger(config)
        if ledger.status(date) == "applied":
            # Already planned today. Stay silent rather than notifying again.
            session.state.suppress_final_output = True
            return ToolResult.success(
                f"{date} was already planned; nothing to do."
            ).to_tool_content()

        try:
            plan = await _compose(session, date)
        except Exception as e:  # noqa: BLE001 - mapped to stable error codes
            return _config_or_provider_error(e, config).to_tool_content()

        if plan.readiness.retry_eligible and _before_deadline(config):
            # WHOOP has not opened today's cycle. This is the ordinary answer
            # at 05:00, not a fault; wait for the next tick without notifying.
            session.state.suppress_final_output = True
            return ToolResult.success(
                f"Readiness not available yet ({plan.readiness.source}); "
                "waiting for the next tick."
            ).to_tool_content()

        # This is the deterministic point at which the day is considered
        # planned, in every mode including shadow: the once-per-date
        # guarantee cannot depend on the model going on to call
        # `planning_sync_daily_plan` (it may just narrate this and stop).
        ledger.mark_applied(date)
        return ToolResult.success(_render_plan(plan)).to_tool_content()

    @tool(
        name="planning_sync_daily_plan",
        description=(
            "Synchronize the dedicated Daily Plan calendar with today's plan, "
            "according to planning.mode (shadow/review/auto)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "The day, as YYYY-MM-DD."},
            },
            "required": ["date"],
        },
    )
    async def planning_sync_daily_plan(date: str, session: "AgentSession") -> str:
        config = session.shared_context.config
        planning = config.planning
        try:
            plan = await _compose(session, date)          # builds the DayPlan

            provider = get_calendar_provider(config)
            existing = await provider.list_day(
                date, config.timezone or "UTC",
                calendar_id=planning.planning_calendar_id,
            )
        except Exception as e:  # noqa: BLE001 - mapped to stable error codes
            return _config_or_provider_error(e, config).to_tool_content()

        sync = diff(plan.events, existing, plan_date=date)

        try:
            check_guards(
                plan, sync,
                planning_calendar_id=planning.planning_calendar_id,
                read_calendar_id=_read_calendar_id(config),
                limits=_limits_for(config, date),
                max_events_per_day=planning.max_events_per_day,
            )
        except GuardError as e:
            return ToolResult.error(
                ToolErrorCode.INVALID_ARGS, str(e),
                user_action="Fix planning configuration before syncing.",
            ).to_tool_content()
        except (ValidationError, OSError) as e:
            return _config_or_provider_error(e, config).to_tool_content()

        # Mark the ledger on every non-suppressed path, not only the write
        # path -- `shadow` and `review` never reach the old auto-only call
        # site below, which is exactly what let four identical morning
        # notifications through. `mark_applied` is idempotent, so this
        # overlapping with `planning_build_day_plan`'s own mark is harmless.
        PlanRunLedger(config).mark_applied(date)

        if planning.mode == "shadow":
            return ToolResult.success(
                "shadow mode — nothing written.\n" + _render(sync)
            ).to_tool_content()

        if planning.mode == "review":
            action_id = str(uuid.uuid4())
            summary = (
                f"Apply daily plan for {date}: {len(sync.creates)} new, "
                f"{len(sync.patches)} changed, {len(sync.deletes)} removed"
            )
            # Store the date and hash, not the events. The executor recomputes,
            # so a plan confirmed hours later cannot write a stale morning over
            # a day that has since changed.
            payload = {"date": date, "plan_hash": plan.plan_hash}
            PendingActionStore(config).create(
                action_id=action_id, capability_id=SYNC_CAPABILITY_ID,
                summary=summary, payload=payload)
            return ToolResult.requires_confirmation(
                action_id=action_id, capability_id=SYNC_CAPABILITY_ID,
                summary=summary, payload=payload).to_tool_content()

        try:
            applied = await _apply(provider, sync, date, planning.planning_calendar_id)
        except Exception as e:  # noqa: BLE001 - mapped to stable error codes
            return provider_exception_to_result(e).to_tool_content()
        return ToolResult.success(applied).to_tool_content()

    return [
        (CapabilityDef(
            id="planning.build_day_plan", tool_name="planning_build_day_plan",
            domain="planning", operation="build_day_plan",
            description="Compose today's plan from calendar, tasks, routine and readiness.",
            risk_level=ToolRiskLevel.READ,
            required_config=["planning"]), planning_build_day_plan),
        (CapabilityDef(
            id=SYNC_CAPABILITY_ID, tool_name="planning_sync_daily_plan",
            domain="planning", operation="sync_daily_plan",
            # Static WRITE, never CONFIRM_REQUIRED: a confirm-gated capability
            # is wrapped in ConfirmationRequiredTool, which never runs the tool
            # body — so shadow mode would record a pending action whose
            # confirmation writes to the calendar. The mode gate lives inside
            # the body, so the body must run.
            description="Synchronize the dedicated Daily Plan calendar.",
            risk_level=ToolRiskLevel.WRITE,
            required_config=["planning"]), planning_sync_daily_plan),
    ]


def build_planning_confirmed_executors(config: "Config") -> dict[str, ConfirmedExecutor]:
    """Executor for `/confirm` in review mode. Re-checks the mode itself."""
    if config.planning is None or not config.planning.enabled:
        return {}

    async def sync_confirmed(session: "AgentSession", payload: dict) -> str:
        planning = session.shared_context.config.planning
        if planning.mode == "shadow":
            # `/confirm` reaches here directly, bypassing the tool body, so the
            # mode gate has to exist on this path too.
            return ToolResult.error(
                ToolErrorCode.PERMISSION_DENIED,
                "planning.mode is 'shadow'; refusing to write.",
            ).to_tool_content()

        date, expected = payload.get("date"), payload.get("plan_hash")
        if not isinstance(date, str) or not isinstance(expected, str):
            # `/confirm` (core/commands/handlers.py) deletes the pending
            # action and calls this executor directly, with no try/except and
            # no ToolRegistry in between -- an unguarded `date=None` would
            # raise inside `_compose` after the action is already gone.
            return ToolResult.error(
                ToolErrorCode.INVALID_ARGS,
                "Stored plan action is missing a date or plan_hash.",
            ).to_tool_content()

        config = session.shared_context.config
        try:
            plan = await _compose(session, date)
        except Exception as e:  # noqa: BLE001 - mapped to stable error codes
            return _config_or_provider_error(e, config).to_tool_content()

        if plan.plan_hash != expected:
            return ToolResult.error(
                ToolErrorCode.INVALID_ARGS,
                "The day has changed since this plan was proposed; "
                "re-run the planner instead of applying a stale plan.",
            ).to_tool_content()

        provider = get_calendar_provider(config)
        try:
            existing = await provider.list_day(
                date, config.timezone or "UTC",
                calendar_id=planning.planning_calendar_id,
            )
        except Exception as e:  # noqa: BLE001 - mapped to stable error codes
            return provider_exception_to_result(e).to_tool_content()

        sync = diff(plan.events, existing, plan_date=date)
        try:
            # Guards run again here: /confirm reaches this executor directly,
            # so it cannot inherit the tool body's checks. The plan hash only
            # covers `plan.events`; planning_calendar_id and
            # max_events_per_day can both change between propose and confirm
            # without changing the hash, so this re-run is load-bearing.
            check_guards(
                plan, sync,
                planning_calendar_id=planning.planning_calendar_id,
                read_calendar_id=_read_calendar_id(config),
                limits=_limits_for(config, date),
                max_events_per_day=planning.max_events_per_day,
            )
        except GuardError as e:
            return ToolResult.error(
                ToolErrorCode.INVALID_ARGS, str(e)
            ).to_tool_content()
        except (ValidationError, OSError) as e:
            return _config_or_provider_error(e, config).to_tool_content()

        try:
            applied = await _apply(
                provider, sync, date, planning.planning_calendar_id
            )
        except Exception as e:  # noqa: BLE001 - mapped to stable error codes
            return provider_exception_to_result(e).to_tool_content()
        PlanRunLedger(config).mark_applied(date)
        return ToolResult.success(applied).to_tool_content()

    return {SYNC_CAPABILITY_ID: sync_confirmed}
