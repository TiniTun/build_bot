"""Deterministic planning: readiness projection, interval maths, placement.

Pure. Nothing here performs I/O; callers hand in already-fetched data. That is
what makes every acceptance criterion testable without a network, a calendar,
or an LLM.
"""

from typing import TYPE_CHECKING, Any, Mapping

from core.planning.models import ReadinessSignal, ReadinessSource

if TYPE_CHECKING:
    from provider.mcp.models import McpCallResult

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
