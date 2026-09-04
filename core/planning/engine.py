"""Deterministic planning: readiness projection, interval maths, placement.

Pure. Nothing here performs I/O; callers hand in already-fetched data. That is
what makes every acceptance criterion testable without a network, a calendar,
or an LLM.
"""

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Mapping
from zoneinfo import ZoneInfo

from core.planning.models import (
    ENERGY_RANK,
    PRIORITY_RANK,
    Candidate,
    DayPattern,
    DayPlan,
    Interval,
    MandatoryEvent,
    PatternLimits,
    PlannedEvent,
    Priority,
    ReadinessSignal,
    ReadinessSource,
    TimeWindow,
    UnscheduledItem,
)

if TYPE_CHECKING:
    from provider.mcp.models import McpCallResult

logger = logging.getLogger(__name__)

# health_planner stamps this into `_meta` on the ordinary "you are not awake
# yet" answer. Pinned by tests/test_planning_readiness.py.
NO_CYCLE_META_KEY = "health-planner/no_cycle_yet"

_VALID_VERDICTS = ("green", "yellow", "red")

# Planner-authored. Never server text: health_planner's `reasons` carry the
# metrics themselves ("Recovery score was 32, ...").
_NOTES: dict[str, str] = {
    "green": "WHOOP reports full readiness.",
    "yellow": "WHOOP reports reduced readiness; optional load trimmed.",
    "red": "WHOOP reports low readiness; recovery and essentials only.",
    "no_cycle_yet": "WHOOP has not opened today's cycle yet.",
    "unavailable": "WHOOP could not be reached; planning conservatively.",
    "unauthorised": "WHOOP is not authorised; planning conservatively.",
    "date_mismatch": "WHOOP returned a different day; planning conservatively.",
}


def _unknown(source: ReadinessSource, *, retry: bool) -> ReadinessSignal:
    return ReadinessSignal(
        verdict="unknown", source=source, retry_eligible=retry, note=_NOTES[source]
    )


def project_readiness(
    result: "McpCallResult | None",
    *,
    planned_date: str,
    status: Mapping[str, Any] | None = None,
) -> ReadinessSignal:
    """Project a health_planner result onto the allowlist, and nothing else.

    Never fabricates a verdict. Anything unrecognized becomes `unknown`, which
    the policy layer treats conservatively, rather than an optimistic guess.

    `status` is a `whoop_status` payload, fetched by the caller only when the
    readiness call failed; it separates "no token" (no point retrying this
    morning) from "WHOOP is down" (worth another tick).
    """
    if result is None:
        return _unknown("unavailable", retry=True)

    # A missing cycle is a SUCCESSFUL result carrying a marker, not an error.
    # Branch on the marker before parsing text, per health_planner's contract.
    if bool(result.private_meta.get(NO_CYCLE_META_KEY)):
        return _unknown("no_cycle_yet", retry=True)

    if result.is_error or result.structured_content is None:
        authorised = bool((status or {}).get("authorised", True))
        if not authorised:
            return _unknown("unauthorised", retry=False)
        return _unknown("unavailable", retry=True)

    document = result.structured_content
    if document.get("date") != planned_date:
        # Two different days must never be mixed; a stale document is worse
        # than no document because it looks authoritative.
        return _unknown("date_mismatch", retry=True)

    verdict = document.get("mode")
    if verdict not in _VALID_VERDICTS:
        return _unknown("unavailable", retry=True)

    return ReadinessSignal(
        verdict=verdict, source="whoop", retry_eligible=False, note=_NOTES[verdict]
    )


def _at(day: str, hhmm: str, timezone: str) -> datetime:
    """A local wall-clock time on the planned day, as an aware datetime."""
    return datetime.combine(
        datetime.strptime(day, "%Y-%m-%d").date(),
        datetime.strptime(hhmm, "%H:%M").time(),
        tzinfo=ZoneInfo(timezone),
    )


def _busy_spans(events: list[Any], timezone: str) -> list[tuple[datetime, datetime]]:
    """Spans that genuinely occupy the user's time.

    All-day events, Google "Free" (transparent) events, and declined
    invitations are excluded: each is something to report, not something that
    consumes the day. Treating an all-day marker as busy would blank out any
    day carrying one.
    """
    spans: list[tuple[datetime, datetime]] = []
    zone = ZoneInfo(timezone)
    for event in events:
        if event.all_day or event.transparent or event.self_declined:
            continue
        try:
            start = datetime.fromisoformat(event.start)
            end = datetime.fromisoformat(event.end)
        except ValueError:
            # We cannot mark this busy: a busy span needs a start and end,
            # and those are precisely what failed to parse. Skipping is the
            # only option, so the consequence is that this stretch of time
            # is treated as free — the warning is what makes that visible
            # instead of a silent over-schedule.
            logger.warning(
                "planning: skipping event %r with unparseable start/end "
                "(start=%r, end=%r)",
                event.id,
                event.start,
                event.end,
            )
            continue
        spans.append((start.astimezone(zone), end.astimezone(zone)))
    return spans


def free_intervals(
    events: list[Any],
    window: TimeWindow,
    buffer_minutes: int,
    day: str,
    timezone: str,
) -> list[Interval]:
    """Free spans inside the schedulable window, with buffers around each event.

    Existing events are never modified — they are subtracted. This is the
    mechanism behind "existing calendar events are never overwritten": the
    planner can only ever place blocks in what is left over.
    """
    window_start = _at(day, window.start, timezone)
    window_end = _at(day, window.end, timezone)
    buffer = timedelta(minutes=buffer_minutes)

    blocked: list[tuple[datetime, datetime]] = []
    for start, end in _busy_spans(events, timezone):
        padded_start, padded_end = start - buffer, end + buffer
        if padded_end <= window_start or padded_start >= window_end:
            continue
        blocked.append((max(padded_start, window_start), min(padded_end, window_end)))

    merged: list[list[datetime]] = []
    for start, end in sorted(blocked):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    intervals: list[Interval] = []
    cursor = window_start
    for start, end in merged:
        if start > cursor:
            intervals.append(Interval(start=cursor, end=start))
        cursor = max(cursor, end)
    if cursor < window_end:
        intervals.append(Interval(start=cursor, end=window_end))
    return intervals


@dataclass(frozen=True)
class _VerdictPolicy:
    """How much a verdict allows. `unknown` deliberately reuses yellow's."""

    scheduled_ratio: float
    min_priority: Priority
    max_energy: str
    recovery_or_essential_only: bool = False


_POLICIES: dict[str, _VerdictPolicy] = {
    "green": _VerdictPolicy(1.0, "low", "high"),
    "yellow": _VerdictPolicy(0.6, "medium", "medium"),
    # On red the priority floor is deliberately NOT binding: the
    # recovery-or-essential rule is the whole gate. Applying both would drop a
    # medium-priority recovery walk, which is what a red day most wants to keep.
    "red": _VerdictPolicy(0.25, "low", "low", recovery_or_essential_only=True),
    "unknown": _VerdictPolicy(0.6, "medium", "medium"),
}


def budget_minutes(signal_verdict: str, limits: PatternLimits) -> int:
    """Minutes this verdict permits to be scheduled, before free time is known."""
    policy = _POLICIES[signal_verdict]
    return int(limits.max_scheduled_minutes * policy.scheduled_ratio)


def task_priority(task: Any, *, overdue: bool) -> Priority:
    """Map a Todoist priority (4 = p1 = urgent) onto the pattern scale.

    An overdue p1 is promoted to `essential` so it remains schedulable on a red
    day — requirement 5's "only essential work and recovery".
    """
    if task.priority >= 4:
        return "essential" if overdue else "high"
    if task.priority == 3:
        return "medium"
    return "low"


def _admits(policy: _VerdictPolicy, priority: str, energy: str, category: str) -> bool:
    if policy.recovery_or_essential_only:
        return category == "recovery" or priority == "essential"
    if PRIORITY_RANK[priority] > PRIORITY_RANK[policy.min_priority]:
        return False
    return ENERGY_RANK[energy] <= ENERGY_RANK[policy.max_energy]


def select_candidates(
    patterns: list[DayPattern],
    tasks: list[Any],
    signal: ReadinessSignal,
    limits: PatternLimits,
    *,
    weekday: str,
    overdue_ids: set[str],
) -> list[Candidate]:
    """Everything the verdict permits, in deterministic placement order.

    Ordering is total, so the same inputs always yield the same plan: pattern
    activities in file order first, then tasks — overdue before due-today,
    higher Todoist priority first, then task id.
    """
    policy = _POLICIES[signal.verdict]
    candidates: list[Candidate] = []

    for index, activity in enumerate(patterns):
        if not _admits(policy, activity.priority, activity.energy, activity.category):
            continue
        candidates.append(
            Candidate(
                slot_key=f"pattern:{weekday}:{activity.slug()}",
                title=activity.name,
                duration_minutes=activity.duration_minutes,
                priority=activity.priority,
                energy=activity.energy,
                category=activity.category,
                flexibility=activity.flexibility,
                fixed_time=activity.fixed_time,
                window=activity.window,
                order=index,
            )
        )

    # Overdue first, then most-urgent first, then id — a total order, so two
    # equally urgent tasks never swap places between runs.
    ordered = sorted(
        tasks,
        key=lambda t: (0 if t.id in overdue_ids else 1, -t.priority, t.id),
    )
    for index, task in enumerate(ordered):
        priority = task_priority(task, overdue=task.id in overdue_ids)
        if not _admits(policy, priority, "medium", "work"):
            continue
        candidates.append(
            Candidate(
                slot_key=f"task:{task.id}",
                title=task.content,
                duration_minutes=task.estimated_minutes or 30,
                priority=priority,
                energy="medium",
                category="work",
                flexibility="flexible",
                order=len(patterns) + index,
                duration_assumed=task.duration_assumed,
            )
        )

    return candidates


# Why a candidate was refused. Stable strings: they reach the user's summary
# and the tests assert on them.
REASON_ENERGY = "energy_above_yellow"
REASON_NOT_RECOVERY = "not_recovery_on_red"
REASON_PRIORITY = "priority_below_policy"
REASON_MIN_FREE = "min_free_minutes"
REASON_BUDGET = "max_scheduled_minutes"
REASON_MIN_BLOCK = "below_min_block_minutes"
REASON_NO_ROOM = "no_free_interval_fits"
REASON_FIXED = "fixed_time_unavailable"


def _refusal_reason(policy: "_VerdictPolicy", priority: str, energy: str,
                    category: str) -> str:
    """Which rule refused this candidate — for the user-visible summary."""
    if policy.recovery_or_essential_only:
        return REASON_NOT_RECOVERY
    if ENERGY_RANK[energy] > ENERGY_RANK[policy.max_energy]:
        return REASON_ENERGY
    return REASON_PRIORITY


def _place(interval_list: list[Interval], start: datetime, end: datetime) -> None:
    """Remove [start, end) from the free list, splitting the interval it lands in."""
    for index, interval in enumerate(interval_list):
        if interval.start <= start and end <= interval.end:
            replacement = []
            if interval.start < start:
                replacement.append(Interval(start=interval.start, end=start))
            if end < interval.end:
                replacement.append(Interval(start=end, end=interval.end))
            interval_list[index : index + 1] = replacement
            return


def build_day_plan(
    *,
    day: str,
    timezone: str,
    agenda: list[Any],
    tasks: list[Any],
    patterns: list[DayPattern],
    limits: PatternLimits,
    signal: Any,
    weekday: str,
    overdue_ids: set[str],
) -> DayPlan:
    """Compose one day's plan deterministically.

    Fixed-time activities are placed first (they cannot move), then everything
    else greedily into the earliest interval that fits. Three ceilings apply in
    order: `min_block_minutes` per block, the verdict's minute budget, and
    `min_free_minutes` of the window that must remain unscheduled.
    """
    policy = _POLICIES[signal.verdict]
    window = limits.schedulable_window
    intervals = free_intervals(agenda, window, limits.buffer_minutes, day, timezone)
    total_free = sum(i.minutes for i in intervals)

    # The window must keep `min_free_minutes` unscheduled, and the verdict caps
    # placed work. Whichever binds first is the real budget.
    budget = min(
        budget_minutes(signal.verdict, limits),
        max(total_free - limits.min_free_minutes, 0),
    )

    admitted = select_candidates(
        patterns, tasks, signal, limits, weekday=weekday, overdue_ids=overdue_ids
    )
    admitted_keys = {c.slot_key for c in admitted}

    events: list[PlannedEvent] = []
    unscheduled: list[UnscheduledItem] = []

    # Everything the policy refused, reported with the rule that refused it.
    for index, activity in enumerate(patterns):
        key = f"pattern:{weekday}:{activity.slug()}"
        if key not in admitted_keys:
            unscheduled.append(UnscheduledItem(
                slot_key=key, title=activity.name,
                reason=_refusal_reason(policy, activity.priority, activity.energy,
                                       activity.category),
            ))
    for task in tasks:
        key = f"task:{task.id}"
        if key not in admitted_keys:
            priority = task_priority(task, overdue=task.id in overdue_ids)
            unscheduled.append(UnscheduledItem(
                slot_key=key, title=task.content,
                reason=_refusal_reason(policy, priority, "medium", "work"),
            ))

    spent = 0
    # Fixed-time first: they cannot move, so they claim their slot before any
    # flexible block can take it.
    for candidate in sorted(admitted, key=lambda c: (c.fixed_time is None, c.order)):
        if candidate.duration_minutes < limits.min_block_minutes:
            unscheduled.append(UnscheduledItem(
                slot_key=candidate.slot_key, title=candidate.title,
                reason=REASON_MIN_BLOCK))
            continue
        if spent + candidate.duration_minutes > budget:
            unscheduled.append(UnscheduledItem(
                slot_key=candidate.slot_key, title=candidate.title,
                reason=REASON_MIN_FREE if total_free - spent
                - candidate.duration_minutes < limits.min_free_minutes
                else REASON_BUDGET))
            continue

        duration = timedelta(minutes=candidate.duration_minutes)
        placed: tuple[datetime, datetime] | None = None

        if candidate.fixed_time is not None:
            start = _at(day, candidate.fixed_time, timezone)
            end = start + duration
            if any(i.start <= start and end <= i.end for i in intervals):
                placed = (start, end)
            else:
                # `flexibility: fixed` forbids moving it; refusing is correct.
                unscheduled.append(UnscheduledItem(
                    slot_key=candidate.slot_key, title=candidate.title,
                    reason=REASON_FIXED))
                continue
        else:
            bounds = candidate.window
            for interval in intervals:
                start = interval.start
                if bounds is not None:
                    start = max(start, _at(day, bounds.start, timezone))
                end = start + duration
                if end <= interval.end and (
                    bounds is None or end <= _at(day, bounds.end, timezone)
                ):
                    placed = (start, end)
                    break
            if placed is None:
                unscheduled.append(UnscheduledItem(
                    slot_key=candidate.slot_key, title=candidate.title,
                    reason=REASON_NO_ROOM))
                continue

        start, end = placed
        events.append(PlannedEvent(
            slot_key=candidate.slot_key, title=candidate.title,
            start=start, end=end, duration_assumed=candidate.duration_assumed))
        _place(intervals, start, end)
        spent += candidate.duration_minutes

    digest = hashlib.sha256(
        "|".join(
            f"{e.slot_key}@{e.start.isoformat()}-{e.end.isoformat()}"
            for e in sorted(events, key=lambda e: e.slot_key)
        ).encode()
    ).hexdigest()[:16]

    return DayPlan(
        date=day, timezone=timezone, readiness=signal,
        events=events, unscheduled=unscheduled,
        mandatory=[
            MandatoryEvent(title=e.title, start=e.start, end=e.end, all_day=e.all_day)
            for e in agenda
        ],
        plan_hash=digest,
    )
