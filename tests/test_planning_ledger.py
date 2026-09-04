"""Once-per-date bookkeeping, driven by the planner's real cron schedule."""

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from core.planning.sync import PlanRunLedger
from tests.helpers import make_workspace
from utils.config import Config

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
            PlanRunLedger(config).mark_applied(DAY, "hash1")
            fresh = PlanRunLedger(config)
            self.assertEqual(fresh.status(DAY), "applied")
            self.assertEqual(fresh.plan_hash(DAY), "hash1")

    def test_dates_are_independent(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PlanRunLedger(_config(tmp))
            ledger.mark_applied(DAY, "h")
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

    def test_status_and_plan_hash_read_from_the_matching_date_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PlanRunLedger(_config(tmp))
            ledger.mark_applied(DAY, "hash-for-day")
            ledger.mark_applied("2026-09-05", "hash-for-other-day")
            self.assertEqual(ledger.status(DAY), "applied")
            self.assertEqual(ledger.plan_hash(DAY), "hash-for-day")
            self.assertEqual(ledger.plan_hash("2026-09-05"), "hash-for-other-day")


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
                ledger.mark_applied(DAY, "unknown-plan")
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
                ledger.mark_applied(DAY, "green-plan")
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
                ledger.mark_applied(DAY, "unknown-plan")
                outputs.append("notified")
        self.assertEqual(outputs.count("notified"), 1)
        self.assertEqual(outputs.index("notified"), 0)


if __name__ == "__main__":
    unittest.main()
