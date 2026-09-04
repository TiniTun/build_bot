"""Deterministic planning: readiness projection, interval maths, placement.

Pure. Nothing here performs I/O; callers hand in already-fetched data. That is
what makes every acceptance criterion testable without a network, a calendar,
or an LLM.
"""

import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Mapping
from zoneinfo import ZoneInfo

from core.planning.models import Interval, ReadinessSignal, ReadinessSource, TimeWindow

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
