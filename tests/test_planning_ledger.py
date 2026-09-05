"""Once-per-date bookkeeping, driven by the planner's real cron schedule."""

import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from core.pending_actions import PendingActionStore
from core.planning.sync import PlanRunLedger
from tests.helpers import make_workspace
from tools.planning_tools import SYNC_CAPABILITY_ID, build_planning_capabilities
from utils.config import Config

BUILD_CAPABILITY_ID = "planning.build_day_plan"

DAY = "2026-09-04"
# The literal schedule shipped in default_workspace/crons/daily-plan/CRON.md.
# Driving the real ticks is the point: a test that invents its own times is
# exactly what let the 05:00-outside-the-window defect through review.
SCHEDULE = "*/30 5-8 * * *"
DEADLINE = "08:30"


def _ticks():
    from croniter import croniter

    cron = croniter(SCHEDULE, datetime(2026, 9, 4, 4, 0))
    return [cron.get_next(datetime) for _ in range(8)]


def _config(tmp):
    return Config.load(make_workspace(Path(tmp)))


class TestLedger(unittest.TestCase):
    def test_unseen_date_has_no_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(PlanRunLedger(_config(tmp)).status(DAY))

    def test_marking_applied_is_durable_across_instances(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            PlanRunLedger(config).mark_applied(DAY)
            fresh = PlanRunLedger(config)
            self.assertEqual(fresh.status(DAY), "applied")

    def test_marking_applied_twice_is_harmless(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PlanRunLedger(_config(tmp))
            ledger.mark_applied(DAY)
            ledger.mark_applied(DAY)
            self.assertEqual(ledger.status(DAY), "applied")

    def test_dates_are_independent(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PlanRunLedger(_config(tmp))
            ledger.mark_applied(DAY)
            self.assertIsNone(ledger.status("2026-09-05"))

    def test_corrupt_entry_is_treated_as_unset_and_logged(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            ledger = PlanRunLedger(config)
            path = config.event_path / "planning" / f"{DAY}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{not valid json")
            with self.assertLogs("core.planning.sync", level="WARNING") as captured:
                status = ledger.status(DAY)
            self.assertIsNone(status)
            self.assertTrue(
                any(DAY in message for message in captured.output),
                captured.output,
            )

    def test_status_reads_from_the_matching_date_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PlanRunLedger(_config(tmp))
            ledger.mark_applied(DAY)
            self.assertEqual(ledger.status(DAY), "applied")
            self.assertIsNone(ledger.status("2026-09-05"))


class TestScheduleProducesOnePlanPerDay(unittest.TestCase):
    """The whole morning, tick by tick."""

    def test_first_tick_is_05_00(self):
        self.assertEqual(_ticks()[0].strftime("%H:%M"), "05:00")

    def test_last_tick_equals_plan_deadline(self):
        self.assertEqual(_ticks()[-1].strftime("%H:%M"), DEADLINE)

    def test_waiting_all_morning_then_planning_once(self):
        """WHOOP never opens a cycle: wait every tick, plan at the deadline."""
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PlanRunLedger(_config(tmp))
            outputs = []
            for tick in _ticks():
                if ledger.status(DAY) == "applied":
                    outputs.append("suppressed")
                    continue
                retry_eligible = True
                if retry_eligible and tick.strftime("%H:%M") < DEADLINE:
                    outputs.append("suppressed")
                    continue
                ledger.mark_applied(DAY)
                outputs.append("notified")
        self.assertEqual(outputs.count("notified"), 1)
        self.assertEqual(outputs[-1], "notified")

    def test_readiness_arriving_early_plans_once_and_silences_the_rest(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PlanRunLedger(_config(tmp))
            outputs = []
            for index, tick in enumerate(_ticks()):
                if ledger.status(DAY) == "applied":
                    outputs.append("suppressed")
                    continue
                retry_eligible = index < 2  # green arrives on the third tick
                if retry_eligible and tick.strftime("%H:%M") < DEADLINE:
                    outputs.append("suppressed")
                    continue
                ledger.mark_applied(DAY)
                outputs.append("notified")
        self.assertEqual(outputs.count("notified"), 1)
        self.assertEqual(outputs.index("notified"), 2)

    def test_unauthorised_stops_at_the_first_tick(self):
        """retry_eligible is False, so 05:00 plans rather than burning the morning."""
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PlanRunLedger(_config(tmp))
            outputs = []
            for tick in _ticks():
                if ledger.status(DAY) == "applied":
                    outputs.append("suppressed")
                    continue
                if False and tick.strftime("%H:%M") < DEADLINE:  # retry_eligible False
                    outputs.append("suppressed")
                    continue
                ledger.mark_applied(DAY)
                outputs.append("notified")
        self.assertEqual(outputs.count("notified"), 1)
        self.assertEqual(outputs.index("notified"), 0)


class TestScheduleProducesOnePlanPerDayForReal(unittest.TestCase):
    """Same tick sequence as above, but driving the real
    `planning_build_day_plan` / `planning_sync_daily_plan` tools instead of a
    hand-simulated algorithm.

    `TestScheduleProducesOnePlanPerDay` above proves the *intended* tick
    algorithm; it is a simulation -- the test body itself calls
    `ledger.mark_applied(...)`, never `tools/planning_tools.py`. It is kept
    because it still documents the algorithm, but it cannot catch a defect in
    where (or whether) the real tools mark the ledger, which is exactly what
    let four identical morning notifications through review. This class
    drives the real tool bodies over the real tick sequence instead.
    """

    def _run_tick_sequence(self, mode: str):
        """Drive both real tools across every cron tick.

        `planning_sync_daily_plan` is only called on a tick where
        `planning_build_day_plan` did not suppress -- there is nothing new to
        sync otherwise, mirroring the agent's own two-step flow
        (`default_workspace/agents/daily-planner/AGENT.md`) once step 1
        reports "nothing to do". Readiness is pinned to a `green`,
        non-retry-eligible signal so every tick would plan if not for the
        ledger -- isolating the ledger as the only thing that can suppress.
        """
        from tests.test_planning_tools import (
            DEFAULT_CALENDAR_ID, _GREEN_SIGNAL, _RecordingProvider, _build_env,
        )

        async def _inner():
            with tempfile.TemporaryDirectory() as tmp:
                context, session = _build_env(
                    tmp, mode=mode, planning_calendar_id=DEFAULT_CALENDAR_ID
                )
                import tools.planning_tools as pt

                provider = _RecordingProvider()
                orig_provider = pt.get_calendar_provider
                orig_readiness = pt._readiness
                pt.get_calendar_provider = lambda config: provider

                async def fake_readiness(context_, day):
                    return _GREEN_SIGNAL

                pt._readiness = fake_readiness

                pairs = build_planning_capabilities(context.config)
                build_tool = next(
                    t for cap, t in pairs if cap.id == BUILD_CAPABILITY_ID
                )
                sync_tool = next(
                    t for cap, t in pairs if cap.id == SYNC_CAPABILITY_ID
                )

                suppress_flags = []
                try:
                    for _tick in _ticks():
                        session.state.suppress_final_output = False
                        await build_tool.execute(session=session, date=DAY)
                        suppress_flags.append(session.state.suppress_final_output)
                        if not session.state.suppress_final_output:
                            await sync_tool.execute(session=session, date=DAY)
                finally:
                    pt.get_calendar_provider = orig_provider
                    pt._readiness = orig_readiness

                pending = len(PendingActionStore(context.config).list_actions())
                return suppress_flags, pending

        return asyncio.run(_inner())

    def test_shadow_mode_notifies_exactly_once_across_the_morning(self):
        suppress_flags, _pending = self._run_tick_sequence(mode="shadow")
        self.assertEqual(len(suppress_flags), 8)
        self.assertEqual(suppress_flags.count(False), 1,
                         "exactly one tick must produce user-visible output")
        self.assertEqual(suppress_flags[1:], [True] * 7,
                         "every tick after the first must be suppressed")

    def test_review_mode_files_exactly_one_pending_action_across_the_morning(self):
        suppress_flags, pending = self._run_tick_sequence(mode="review")
        self.assertEqual(suppress_flags.count(False), 1)
        self.assertEqual(pending, 1)


if __name__ == "__main__":
    unittest.main()
