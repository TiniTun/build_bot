"""The planning tools: shadow writes nothing, review proposes once, auto applies."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import yaml

from core.pending_actions import PendingActionStore
from core.planning.sync import OWNER
from tests.helpers import make_context, make_workspace
from tools.planning_tools import (
    SYNC_CAPABILITY_ID,
    build_planning_capabilities,
    build_planning_confirmed_executors,
)

DATE = "2026-02-02"
DEFAULT_CALENDAR_ID = "plan@group.calendar.google.com"

# Every weekday admits the same single, low-stakes activity, so the plan is
# identical regardless of which weekday DATE happens to fall on.
_ACTIVITY = {"name": "Deep Work", "duration_minutes": 30}
_PATTERNS_YAML = yaml.safe_dump(
    {
        "version": 1,
        "defaults": {
            "schedulable_window": {"start": "06:00", "end": "22:00"},
            "buffer_minutes": 10,
            "max_scheduled_minutes": 180,
            "min_free_minutes": 60,
            "min_block_minutes": 20,
        },
        "weekdays": {
            day: [_ACTIVITY]
            for day in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
        },
    }
)


class _RecordingProvider:
    """Counts every mutation so a shadow-mode test can assert zero."""

    def __init__(self, day_events=None, plan_events=None):
        self._day = day_events or []
        self._plan = plan_events or []
        self.created, self.patched, self.deleted = [], [], []

    async def list_day(self, day, timezone, calendar_id=None):
        return list(self._plan) if calendar_id else list(self._day)

    async def create_event(self, request, calendar_id=None):
        self.created.append((request, calendar_id))
        from provider.calendar.base import CalendarEvent
        return CalendarEvent(id="new", title=request.title,
                             start=request.start, end=request.end)

    async def update_event(self, event_id, request, calendar_id=None):
        self.patched.append((event_id, calendar_id))
        from provider.calendar.base import CalendarEvent
        return CalendarEvent(id=event_id, title=request.title,
                             start=request.start, end=request.end)

    async def delete_event(self, event_id, calendar_id=None):
        self.deleted.append((event_id, calendar_id))

    @property
    def mutations(self):
        return len(self.created) + len(self.patched) + len(self.deleted)


def _build_env(tmp: str, *, mode: str, planning_calendar_id):
    """An isolated workspace with a planning block wired to `_PATTERNS_YAML`."""
    workspace = make_workspace(Path(tmp))

    patterns_path = workspace / "planning" / "day_patterns.yaml"
    patterns_path.parent.mkdir(parents=True, exist_ok=True)
    patterns_path.write_text(_PATTERNS_YAML)

    config_path = workspace / "config.user.yaml"
    config_data = yaml.safe_load(config_path.read_text())
    planning_block = {
        "enabled": True,
        "mode": mode,
        "max_events_per_day": 12,
        "plan_deadline": "08:30",
    }
    if planning_calendar_id is not None:
        planning_block["planning_calendar_id"] = planning_calendar_id
    config_data["planning"] = planning_block
    config_path.write_text(yaml.safe_dump(config_data, sort_keys=False))

    context = make_context(workspace)
    session = SimpleNamespace(
        shared_context=context, state=SimpleNamespace(suppress_final_output=False)
    )
    return context, session


def _run_sync(
    *,
    mode: str,
    provider: "_RecordingProvider",
    date: str = DATE,
    planning_calendar_id=DEFAULT_CALENDAR_ID,
    return_store: bool = False,
    echo_created: bool = False,
):
    """Build an isolated workspace, then execute `planning_sync_daily_plan`.

    `get_calendar_provider` is monkeypatched module-wide (per
    tests/test_google_calendar.py's pattern) so the tool body's calendar calls
    go to `provider` instead of a real one. `echo_created` feeds what a prior
    call created back as the planning calendar's own contents, for the
    re-run-is-a-no-op test.
    """

    async def _inner():
        with tempfile.TemporaryDirectory() as tmp:
            context, session = _build_env(
                tmp, mode=mode, planning_calendar_id=planning_calendar_id
            )
            import tools.planning_tools as pt

            orig = pt.get_calendar_provider
            pt.get_calendar_provider = lambda config: provider
            try:
                if echo_created:
                    from provider.calendar.base import CalendarEvent
                    provider._plan = [
                        CalendarEvent(
                            id=f"echo-{i}",
                            title=req.title,
                            start=req.start,
                            end=req.end,
                            private_properties=req.private_properties,
                        )
                        for i, (req, _cal) in enumerate(provider.created)
                    ]
                pairs = build_planning_capabilities(context.config)
                tool_obj = next(t for cap, t in pairs if cap.id == SYNC_CAPABILITY_ID)
                result = await tool_obj.execute(session=session, date=date)
            finally:
                pt.get_calendar_provider = orig
            # Read back while the temp workspace still exists -- the `with`
            # block above deletes it the moment this coroutine returns.
            actions = PendingActionStore(context.config).list_actions()
            return result, _FrozenStore(actions)

    result, store = asyncio.run(_inner())
    if return_store:
        return result, store
    return result


class _FrozenStore:
    """A `list_actions()` snapshot taken before its temp workspace was deleted."""

    def __init__(self, actions):
        self._actions = actions

    def list_actions(self):
        return self._actions


class TestModes(unittest.TestCase):
    def test_shadow_mode_performs_no_mutation(self):
        provider = _RecordingProvider()
        result = _run_sync(mode="shadow", provider=provider)
        self.assertEqual(provider.mutations, 0)
        self.assertIn("shadow", result.lower())

    def test_review_mode_records_exactly_one_pending_action(self):
        provider = _RecordingProvider()
        result, store = _run_sync(mode="review", provider=provider,
                                  return_store=True)
        payload = json.loads(result)
        self.assertTrue(payload["requires_confirmation"])
        self.assertEqual(len(store.list_actions()), 1,
                         "one batch action, never one per event")
        self.assertEqual(provider.mutations, 0)

    def test_review_payload_carries_date_and_hash_not_events(self):
        provider = _RecordingProvider()
        result = _run_sync(mode="review", provider=provider)
        payload = json.loads(result)["action"]["payload"]
        self.assertEqual(set(payload), {"date", "plan_hash"})

    def test_auto_mode_applies_and_targets_the_planning_calendar(self):
        provider = _RecordingProvider()
        _run_sync(mode="auto", provider=provider)
        self.assertGreater(len(provider.created), 0)
        for _request, calendar_id in provider.created:
            self.assertEqual(calendar_id, DEFAULT_CALENDAR_ID)

    def test_auto_mode_never_sends_attendees(self):
        provider = _RecordingProvider()
        _run_sync(mode="auto", provider=provider)
        for request, _ in provider.created:
            self.assertEqual(request.attendees, [])

    def test_auto_mode_stamps_the_ownership_marker(self):
        provider = _RecordingProvider()
        _run_sync(mode="auto", provider=provider)
        for request, _ in provider.created:
            self.assertEqual(request.private_properties["owner"], OWNER)

    def test_rerunning_auto_mode_creates_nothing(self):
        provider = _RecordingProvider()
        _run_sync(mode="auto", provider=provider)
        first = len(provider.created)
        # Feed what was created back as the planning calendar's contents.
        _run_sync(mode="auto", provider=provider, echo_created=True)
        self.assertEqual(len(provider.created), first,
                         "an identical re-run must create nothing")

    def test_guard_failure_produces_a_tool_error_not_an_exception(self):
        provider = _RecordingProvider()
        result = _run_sync(mode="auto", provider=provider,
                           planning_calendar_id=None)
        payload = json.loads(result)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "invalid_args")
        self.assertEqual(provider.mutations, 0)


class TestReadinessNeverLeaks(unittest.TestCase):
    """Criterion 4, closing the read-back loop."""

    def test_no_event_body_mentions_readiness(self):
        provider = _RecordingProvider()
        _run_sync(mode="auto", provider=provider)
        for request, _ in provider.created:
            blob = f"{request.title} {request.description or ''}"
            for word in ("green", "yellow", "red", "recovery score", "hrv",
                         "readiness", "whoop"):
                self.assertNotIn(word, blob.lower())


class TestConfirmedExecutor(unittest.TestCase):
    """Criterion 2: `/confirm` reaches the executor directly, bypassing the
    tool body's own mode gate, so the executor must re-check the mode itself.
    """

    def test_refuses_when_mode_is_shadow(self):
        provider = _RecordingProvider()

        async def _inner():
            with tempfile.TemporaryDirectory() as tmp:
                context, session = _build_env(
                    tmp, mode="review", planning_calendar_id=DEFAULT_CALENDAR_ID
                )
                import tools.planning_tools as pt

                orig = pt.get_calendar_provider
                pt.get_calendar_provider = lambda config: provider
                try:
                    pairs = build_planning_capabilities(context.config)
                    tool_obj = next(
                        t for cap, t in pairs if cap.id == SYNC_CAPABILITY_ID
                    )
                    raw = await tool_obj.execute(session=session, date=DATE)
                    payload = json.loads(raw)["action"]["payload"]

                    # A day later, planning.mode has been switched to shadow.
                    # /confirm calls the executor directly -- the proposal
                    # tool's gate is not on this path.
                    context.config.planning.mode = "shadow"
                    executors = build_planning_confirmed_executors(context.config)
                    executor = executors[SYNC_CAPABILITY_ID]
                    result = await executor(session, payload)
                finally:
                    pt.get_calendar_provider = orig
                return result

        result = asyncio.run(_inner())
        payload = json.loads(result)
        self.assertFalse(payload["ok"])
        self.assertEqual(provider.mutations, 0)

    def test_confirms_and_applies_in_review_mode(self):
        """Sanity check: the executor DOES apply when the mode allows it."""
        provider = _RecordingProvider()

        async def _inner():
            with tempfile.TemporaryDirectory() as tmp:
                context, session = _build_env(
                    tmp, mode="review", planning_calendar_id=DEFAULT_CALENDAR_ID
                )
                import tools.planning_tools as pt

                orig = pt.get_calendar_provider
                pt.get_calendar_provider = lambda config: provider
                try:
                    pairs = build_planning_capabilities(context.config)
                    tool_obj = next(
                        t for cap, t in pairs if cap.id == SYNC_CAPABILITY_ID
                    )
                    raw = await tool_obj.execute(session=session, date=DATE)
                    payload = json.loads(raw)["action"]["payload"]

                    executors = build_planning_confirmed_executors(context.config)
                    executor = executors[SYNC_CAPABILITY_ID]
                    result = await executor(session, payload)
                finally:
                    pt.get_calendar_provider = orig
                return result

        result = asyncio.run(_inner())
        self.assertGreater(len(provider.created), 0)
        self.assertNotIn('"ok": false', result)

    def test_stale_plan_hash_is_refused(self):
        """Criterion 3's other half: the executor recomputes and compares."""
        provider = _RecordingProvider()

        async def _inner():
            with tempfile.TemporaryDirectory() as tmp:
                context, session = _build_env(
                    tmp, mode="review", planning_calendar_id=DEFAULT_CALENDAR_ID
                )
                import tools.planning_tools as pt

                orig = pt.get_calendar_provider
                pt.get_calendar_provider = lambda config: provider
                try:
                    executors = build_planning_confirmed_executors(context.config)
                    executor = executors[SYNC_CAPABILITY_ID]
                    result = await executor(
                        session, {"date": DATE, "plan_hash": "stale-hash-value"}
                    )
                finally:
                    pt.get_calendar_provider = orig
                return result

        result = asyncio.run(_inner())
        payload = json.loads(result)
        self.assertFalse(payload["ok"])
        self.assertEqual(provider.mutations, 0)


class TestReadCalendarIdResolution(unittest.TestCase):
    """Criterion 5: the module resolves `calendar_id or "primary"` itself;

    `check_guards` never does this resolution, so a raw `None` would silently
    defeat the "planning calendar must differ from the read calendar" guard.
    """

    def test_none_calendar_id_resolves_to_primary_before_check_guards(self):
        provider = _RecordingProvider()
        seen = []

        async def _inner():
            with tempfile.TemporaryDirectory() as tmp:
                context, session = _build_env(
                    tmp, mode="shadow", planning_calendar_id=DEFAULT_CALENDAR_ID
                )
                import tools.planning_tools as pt

                orig_provider = pt.get_calendar_provider
                orig_guard = pt.check_guards
                pt.get_calendar_provider = lambda config: provider

                def spy(*args, **kwargs):
                    seen.append(kwargs.get("read_calendar_id"))
                    return orig_guard(*args, **kwargs)

                pt.check_guards = spy
                try:
                    pairs = build_planning_capabilities(context.config)
                    tool_obj = next(
                        t for cap, t in pairs if cap.id == SYNC_CAPABILITY_ID
                    )
                    await tool_obj.execute(session=session, date=DATE)
                finally:
                    pt.get_calendar_provider = orig_provider
                    pt.check_guards = orig_guard

        asyncio.run(_inner())
        self.assertEqual(seen, ["primary"])


if __name__ == "__main__":
    unittest.main()
