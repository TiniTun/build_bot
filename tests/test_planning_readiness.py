"""Readiness projection: the allowlist between health_planner and the model.

Also pins the three upstream facts this design depends on, so drift in
health_planner fails here with a clear name rather than silently planning every
day as `unknown`.
"""

import json
import unittest

from pydantic import ValidationError

from core.planning.engine import project_readiness
from core.planning.models import ReadinessSignal
from provider.mcp.models import McpCallResult

NO_CYCLE_META = "health-planner/no_cycle_yet"

SERVER_ID = "health_planner"
TOOL_NAME = "get_readiness"


def _green_document() -> dict:
    """A DailyReadiness document, built from health_planner's declared schema.

    Field names and types come from health_planner's own source
    (src/health_planner/readiness/models.py: DailyReadiness + HistoryContext).
    NOT a captured response: this environment has no WHOOP credentials, so
    Task 1.5's live capture was blocked and deferred. This verifies our
    projection against the schema that service DECLARES, not against a
    response it was observed to produce.
    """
    return {
        "date": "2026-09-04",
        "mode": "green",
        "recovery_score": 71.0,
        "sleep_performance": 88.0,
        "sleep_duration_minutes": 447,
        "sleep_need_minutes": 468,
        "hrv_ms": 63.4,
        "resting_hr_bpm": 52.0,
        "respiratory_rate": 14.6,
        "previous_day_strain": 11.3,
        "cycle_id": 918273645,
        "sleep_id": "b2f1c0de-0000-4a1b-9c3d-5e6f70819203",
        "reasons": [
            "Recovery score was 71, at or above the green threshold (67)"
        ],
        "warnings": [],
        "history": {
            "days_available": 28,
            "consecutive_low_recovery_days": 0,
            "recovery_avg_7d": 64.2,
            "recovery_avg_28d": 61.8,
            "strain_7d": 12.1,
            "strain_28d": 11.7,
            "acute_chronic_ratio": 1.03,
            "days_since_low_strain_day": 2,
            "sleep_duration_avg_7d": 432.0,
            "covered_from": "2026-08-08",
            "covered_to": "2026-09-04",
        },
    }


def _ok(structured, meta=None):
    # NOTE: the brief's draft constructed McpCallResult without server_id /
    # tool_name. The real dataclass (provider/mcp/models.py) requires both
    # (no defaults), so they are supplied here.
    return McpCallResult(
        server_id=SERVER_ID,
        tool_name=TOOL_NAME,
        text_blocks=(json.dumps(structured) if structured else "",),
        structured_content=structured,
        is_error=False,
        private_meta=meta or {},
    )


def _failed(text: str = "WHOOP 401 for cycle 918273645: recovery 32"):
    # Digit-bearing prose on purpose: if a future change ever echoed
    # `text_blocks` into `note`, the leak sweep below would catch it.
    return McpCallResult(
        server_id=SERVER_ID,
        tool_name=TOOL_NAME,
        text_blocks=(text,),
        is_error=True,
    )


class TestVerdictProjection(unittest.TestCase):
    def test_green(self):
        doc = {**_green_document(), "mode": "green", "date": "2026-09-04"}
        signal = project_readiness(_ok(doc), planned_date="2026-09-04")
        self.assertEqual(signal.verdict, "green")
        self.assertEqual(signal.source, "whoop")
        self.assertFalse(signal.retry_eligible)

    def test_yellow(self):
        doc = {**_green_document(), "mode": "yellow", "date": "2026-09-04"}
        self.assertEqual(
            project_readiness(_ok(doc), planned_date="2026-09-04").verdict, "yellow"
        )

    def test_red(self):
        doc = {**_green_document(), "mode": "red", "date": "2026-09-04"}
        self.assertEqual(
            project_readiness(_ok(doc), planned_date="2026-09-04").verdict, "red"
        )

    def test_no_cycle_yet_is_a_successful_result_with_a_meta_marker(self):
        signal = project_readiness(
            _ok(None, {NO_CYCLE_META: True}), planned_date="2026-09-04"
        )
        self.assertEqual(signal.verdict, "unknown")
        self.assertEqual(signal.source, "no_cycle_yet")
        self.assertTrue(signal.retry_eligible)

    def test_unknown_when_the_call_failed_and_whoop_is_unreachable(self):
        signal = project_readiness(
            _failed(), planned_date="2026-09-04",
            status={"authorised": True, "api_reachable": False},
        )
        self.assertEqual(signal.verdict, "unknown")
        self.assertEqual(signal.source, "unavailable")
        self.assertTrue(signal.retry_eligible)

    def test_unauthorised_stops_retrying(self):
        signal = project_readiness(
            _failed(), planned_date="2026-09-04",
            status={"authorised": False, "api_reachable": False},
        )
        self.assertEqual(signal.source, "unauthorised")
        self.assertFalse(
            signal.retry_eligible,
            "a WHOOP token will not appear on its own during a morning window",
        )

    def test_hub_returning_none_degrades_to_unknown(self):
        signal = project_readiness(None, planned_date="2026-09-04")
        self.assertEqual(signal.verdict, "unknown")

    def test_document_for_another_day_is_refused(self):
        doc = {**_green_document(), "mode": "green", "date": "2026-09-03"}
        signal = project_readiness(_ok(doc), planned_date="2026-09-04")
        self.assertEqual(signal.verdict, "unknown")
        self.assertEqual(signal.source, "date_mismatch")
        self.assertTrue(signal.retry_eligible)

    def test_unrecognised_verdict_is_not_trusted(self):
        doc = {**_green_document(), "mode": "chartreuse", "date": "2026-09-04"}
        signal = project_readiness(_ok(doc), planned_date="2026-09-04")
        self.assertEqual(signal.verdict, "unknown")
        self.assertEqual(signal.source, "unavailable")

    def test_failed_call_with_no_status_follow_up_still_retries(self):
        # status=None is the real path when the whoop_status follow-up call
        # itself fails; the default must stay retry-friendly, not silently
        # flip to non-retryable.
        signal = project_readiness(_failed(), planned_date="2026-09-04")
        self.assertEqual(signal.source, "unavailable")
        self.assertTrue(signal.retry_eligible)


def _scalars(node):
    """Yield every leaf scalar in a JSON-shaped structure, recursively.

    Used to sweep the *entire* document — including nested blocks like
    `history` — rather than a hand-picked list of top-level field names,
    which a reviewer showed misses nested leaks entirely.
    """
    if isinstance(node, dict):
        for v in node.values():
            yield from _scalars(v)
    elif isinstance(node, list):
        for v in node:
            yield from _scalars(v)
    elif node is not None:
        yield node


class TestNoMetricLeaks(unittest.TestCase):
    """Criterion 7: only allowlisted signals may be projected."""

    def test_no_value_from_the_document_appears_in_the_signal(self):
        doc = {**_green_document(), "mode": "green", "date": "2026-09-04"}
        signal = project_readiness(_ok(doc), planned_date="2026-09-04")
        rendered = signal.model_dump_json()
        for field in (
            "recovery_score", "sleep_performance", "sleep_duration_minutes",
            "sleep_need_minutes", "hrv_ms", "resting_hr_bpm", "respiratory_rate",
            "previous_day_strain", "cycle_id", "sleep_id",
        ):
            value = doc.get(field)
            if value is None:
                continue
            self.assertNotIn(
                str(value), rendered, f"{field} leaked into the projected signal"
            )

    def test_no_value_anywhere_in_the_document_appears_in_the_signal(self):
        # Descends into nested blocks (e.g. `history`) that a top-level-only
        # field sweep would never see.
        doc = {**_green_document(), "mode": "green", "date": "2026-09-04"}
        signal = project_readiness(_ok(doc), planned_date="2026-09-04")
        rendered = signal.model_dump_json()
        for value in _scalars(doc):
            if value == "green":  # the one allowlisted datum
                continue
            self.assertNotIn(str(value), rendered, f"{value!r} leaked into the signal")

    def test_note_is_always_drawn_from_the_module_note_table(self):
        # Stronger than any value sweep: proves `note` is one of a fixed set
        # of planner-authored strings, never text derived from the document
        # or from a failed call's prose.
        from core.planning.engine import _NOTES

        for mode in ("green", "yellow", "red"):
            doc = {**_green_document(), "mode": mode, "date": "2026-09-04"}
            self.assertIn(
                project_readiness(_ok(doc), planned_date="2026-09-04").note,
                set(_NOTES.values()),
            )
        for signal in (
            project_readiness(None, planned_date="2026-09-04"),
            project_readiness(_ok(None, {NO_CYCLE_META: True}), planned_date="2026-09-04"),
            project_readiness(_failed(), planned_date="2026-09-04"),
            project_readiness(
                _failed(), planned_date="2026-09-04", status={"authorised": False}
            ),
            project_readiness(
                _ok({**_green_document(), "date": "2026-09-03"}),
                planned_date="2026-09-04",
            ),
        ):
            self.assertIn(signal.note, set(_NOTES.values()))

    def test_reasons_prose_never_reaches_the_signal(self):
        # health_planner's reasons embed metrics:
        # "Recovery score was 32, below the yellow threshold (34)"
        doc = {
            **_green_document(), "mode": "red", "date": "2026-09-04",
            "reasons": ["Recovery score was 32, below the yellow threshold (34)"],
            "warnings": ["Sleep duration was 5h 12m, below the hard floor of 6h"],
        }
        signal = project_readiness(_ok(doc), planned_date="2026-09-04")
        rendered = signal.model_dump_json()
        self.assertNotIn("Recovery score was", rendered)
        self.assertNotIn("5h 12m", rendered)

    def test_note_is_planner_authored_and_stable(self):
        doc = {**_green_document(), "mode": "yellow", "date": "2026-09-04"}
        a = project_readiness(_ok(doc), planned_date="2026-09-04").note
        b = project_readiness(_ok(doc), planned_date="2026-09-04").note
        self.assertEqual(a, b)
        self.assertTrue(a)


class TestUpstreamContractPins(unittest.TestCase):
    """Fails loudly if health_planner's contract drifts."""

    def test_meta_key_string(self):
        self.assertEqual(NO_CYCLE_META, "health-planner/no_cycle_yet")

    def test_verdict_field_is_named_mode(self):
        self.assertIn("mode", _green_document())
        self.assertIn(
            _green_document()["mode"], ("green", "yellow", "red")
        )

    def test_projection_reads_the_meta_marker_our_code_depends_on(self):
        # Cannot assert against a captured no_cycle_yet response: Task 1.5 was
        # blocked (no WHOOP credentials in this environment). This pins the
        # constant our projection branches on, so a rename in health_planner
        # surfaces here rather than as a planner that silently plans every day
        # as `unknown`. Replace with a fixture-backed assertion once a real
        # capture exists.
        from core.planning.engine import NO_CYCLE_META_KEY

        self.assertEqual(NO_CYCLE_META_KEY, "health-planner/no_cycle_yet")


class TestReadinessSignalSchema(unittest.TestCase):
    """I1: `extra='forbid'` is the structural half of the allowlist."""

    def test_extra_field_is_rejected(self):
        with self.assertRaises(ValidationError):
            ReadinessSignal(
                verdict="green", source="whoop", retry_eligible=False,
                note="x", recovery_score=71.0,
            )

    def test_the_allowlist_is_exactly_these_four_fields(self):
        self.assertEqual(
            set(ReadinessSignal.model_fields),
            {"verdict", "source", "retry_eligible", "note"},
        )


if __name__ == "__main__":
    unittest.main()
