"""The planning tools: shadow writes nothing, review proposes once, auto applies."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import yaml

from core.pending_actions import PendingActionStore
from core.planning.models import ReadinessSignal
from core.planning.sync import OWNER, PlanRunLedger
from tests.helpers import make_context, make_workspace
from tools.capabilities import (
    DEFAULT_RISK_ACTIONS,
    CapabilityRegistry,
    ConfirmationRequiredTool,
    ToolPolicy,
    ToolRiskLevel,
)
from tools.planning_tools import (
    SYNC_CAPABILITY_ID,
    build_planning_capabilities,
    build_planning_confirmed_executors,
)

DATE = "2026-02-02"  # a Monday
DEFAULT_CALENDAR_ID = "plan@group.calendar.google.com"
BUILD_CAPABILITY_ID = "planning.build_day_plan"

# A signal shaped like a real WHOOP response: not retry-eligible, so a plan
# built from it renders instead of suppressing, and carrying real "leaky"
# words (whoop/readiness) so the never-leaks tests exercise a genuine risk
# instead of trivially passing against the default `unknown` verdict.
_GREEN_SIGNAL = ReadinessSignal(
    verdict="green", source="whoop", retry_eligible=False,
    note="WHOOP reports full readiness.",
)

# Two low-stakes activities admitted under every verdict, repeated for every
# weekday so the plan is identical regardless of which weekday DATE falls on.
# Two events (not one) so a batch-vs-per-event pending action, and a
# `for request, _ in provider.created` loop that only ran once, both have
# something to catch.
_ACTIVITIES = [
    {"name": "Deep Work", "duration_minutes": 30},
    {"name": "Walk", "duration_minutes": 30},
]
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
            day: _ACTIVITIES
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
        # Full request kept (not just id/calendar_id): the patch branch needs
        # the same attendees/marker/no-leak assertions the create branch gets.
        self.patched.append((event_id, request, calendar_id))
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


def _event_blob(request) -> str:
    """Everything on a create/update request an operator might read back."""
    return " ".join(filter(None, [
        request.title,
        request.description,
        request.location,
        json.dumps(request.private_properties, sort_keys=True),
    ])).lower()


class TestReadinessNeverLeaks(unittest.TestCase):
    """Criterion 4, closing the read-back loop.

    Forces a real, non-`unknown` verdict via `_readiness` so the leak check
    exercises actual risk -- the default `unknown`/`unavailable` verdict never
    says a leaky word on its own, so a test against it would pass even with a
    verdict wired straight into `description` or `location`.
    """

    def test_no_event_body_mentions_readiness(self):
        provider = _RecordingProvider()

        async def _inner():
            with tempfile.TemporaryDirectory() as tmp:
                context, session = _build_env(
                    tmp, mode="auto", planning_calendar_id=DEFAULT_CALENDAR_ID
                )
                import tools.planning_tools as pt

                orig_provider = pt.get_calendar_provider
                orig_readiness = pt._readiness
                pt.get_calendar_provider = lambda config: provider

                async def fake_readiness(context_, day):
                    return _GREEN_SIGNAL

                pt._readiness = fake_readiness
                try:
                    pairs = build_planning_capabilities(context.config)
                    tool_obj = next(
                        t for cap, t in pairs if cap.id == SYNC_CAPABILITY_ID
                    )
                    await tool_obj.execute(session=session, date=DATE)
                finally:
                    pt.get_calendar_provider = orig_provider
                    pt._readiness = orig_readiness

        asyncio.run(_inner())
        self.assertGreater(len(provider.created), 0)
        for request, _ in provider.created:
            blob = _event_blob(request)
            for word in ("green", "yellow", "red", "recovery score", "hrv",
                         "readiness", "whoop"):
                self.assertNotIn(word, blob)
            # The plan's own signal, verbatim -- not a guessed word list.
            self.assertNotIn(_GREEN_SIGNAL.verdict, blob)
            self.assertNotIn(_GREEN_SIGNAL.note.lower(), blob)


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


class TestRiskLevel(unittest.TestCase):
    """Important 2: the static WRITE risk level is the whole mode-safety story.

    A `confirm_required` capability is wrapped in `ConfirmationRequiredTool`,
    which never runs the wrapped tool body -- so shadow mode's gate (which
    lives inside that body) would never even be reached; the wrapper would
    record a pending action whose confirmation writes unconditionally.
    """

    def test_sync_capability_is_static_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            context, _session = _build_env(
                tmp, mode="auto", planning_calendar_id=DEFAULT_CALENDAR_ID
            )
            cap = next(
                c for c, _ in build_planning_capabilities(context.config)
                if c.id == SYNC_CAPABILITY_ID
            )
            self.assertIs(cap.risk_level, ToolRiskLevel.WRITE)

    def test_sync_tool_is_not_confirmation_wrapped_under_a_configured_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            context, _session = _build_env(
                tmp, mode="auto", planning_calendar_id=DEFAULT_CALENDAR_ID
            )
            registry = CapabilityRegistry()
            for cap, tool_obj in build_planning_capabilities(context.config):
                registry.register(cap, tool_obj)
            # A non-permissive policy: enabled_capabilities is not None, so
            # `ToolPolicy.resolve` actually consults risk_actions instead of
            # short-circuiting to ALLOW.
            policy = ToolPolicy(
                enabled_capabilities={SYNC_CAPABILITY_ID},
                risk_actions=dict(DEFAULT_RISK_ACTIONS),
            )
            built = registry.build_tool_registry(policy)
            tool_obj = built.get("planning_sync_daily_plan")
            self.assertIsNotNone(tool_obj)
            self.assertNotIsInstance(tool_obj, ConfirmationRequiredTool)


class TestApplyPatchAndDelete(unittest.TestCase):
    """Important 4: `_apply`'s patch and delete branches, exercised for real.

    Seeds the planning calendar with an owned event at a different time for
    `pattern:mon:deep-work` (forces a patch) and an owned event for a slot
    key no longer wanted (forces a delete), then re-runs the attendees /
    marker / no-leak checks over the patch branch specifically.
    """

    def test_patch_and_delete_branches(self):
        from provider.calendar.base import CalendarEvent
        from core.planning.sync import marker

        provider = _RecordingProvider()
        # A different start/end than what `_compose` will place -- forces a
        # patch instead of "unchanged".
        provider._plan = [
            CalendarEvent(
                id="existing-deep-work",
                title="Deep Work",
                start="2026-02-02T05:00:00+00:00",
                end="2026-02-02T05:30:00+00:00",
                private_properties=marker(DATE, "pattern:mon:deep-work"),
            ),
            CalendarEvent(
                id="stale-1",
                title="Old Activity",
                start="2026-02-02T04:00:00+00:00",
                end="2026-02-02T04:15:00+00:00",
                private_properties=marker(DATE, "pattern:mon:stale-thing"),
            ),
        ]

        async def _inner():
            with tempfile.TemporaryDirectory() as tmp:
                context, session = _build_env(
                    tmp, mode="auto", planning_calendar_id=DEFAULT_CALENDAR_ID
                )
                import tools.planning_tools as pt

                orig_provider = pt.get_calendar_provider
                orig_readiness = pt._readiness
                pt.get_calendar_provider = lambda config: provider

                async def fake_readiness(context_, day):
                    return _GREEN_SIGNAL

                pt._readiness = fake_readiness
                try:
                    pairs = build_planning_capabilities(context.config)
                    tool_obj = next(
                        t for cap, t in pairs if cap.id == SYNC_CAPABILITY_ID
                    )
                    await tool_obj.execute(session=session, date=DATE)
                finally:
                    pt.get_calendar_provider = orig_provider
                    pt._readiness = orig_readiness

        asyncio.run(_inner())

        self.assertEqual(len(provider.patched), 1)
        _event_id, request, calendar_id = provider.patched[0]
        self.assertEqual(calendar_id, DEFAULT_CALENDAR_ID)
        self.assertEqual(request.attendees, [])
        self.assertEqual(request.private_properties["owner"], OWNER)
        self.assertEqual(request.private_properties["slot_key"], "pattern:mon:deep-work")
        blob = _event_blob(request)
        for word in ("green", "yellow", "red", "recovery score", "hrv",
                     "readiness", "whoop"):
            self.assertNotIn(word, blob)

        self.assertGreater(len(provider.deleted), 0)
        deleted_ids = {event_id for event_id, _cal in provider.deleted}
        self.assertIn("stale-1", deleted_ids)


class TestExecutorReRunsGuards(unittest.TestCase):
    """Important 5: the executor's own `check_guards` re-run is load-bearing.

    `plan_hash` only covers `plan.events`; `max_events_per_day` and
    `planning_calendar_id` can both change between propose and confirm
    without changing the hash. Only re-running the guards catches that.
    """

    def test_max_events_per_day_shrunk_after_propose_is_still_enforced(self):
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

                    # Configuration tightened between propose and confirm.
                    # plan_hash is unaffected (it only covers plan.events).
                    context.config.planning.max_events_per_day = 0
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


class TestExecutorPayloadAndProviderErrors(unittest.TestCase):
    """Important 6: `/confirm` calls the executor directly (core/commands/
    handlers.py), deleting the pending action first and with no try/except in
    between. A malformed payload or a provider failure must come back as a
    mapped `ToolResult.error`, never an escaping exception.
    """

    def test_malformed_payload_is_rejected_not_raised(self):
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
                    result = await executor(session, {"date": None, "plan_hash": None})
                finally:
                    pt.get_calendar_provider = orig
                return result

        result = asyncio.run(_inner())
        payload = json.loads(result)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "invalid_args")
        self.assertEqual(provider.mutations, 0)

    def test_missing_payload_keys_are_rejected_not_raised(self):
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
                    result = await executor(session, {})
                finally:
                    pt.get_calendar_provider = orig
                return result

        result = asyncio.run(_inner())
        payload = json.loads(result)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "invalid_args")

    def test_provider_failure_during_apply_is_mapped_not_raised(self):
        from provider.external_errors import ProviderPermissionError

        class _RaisingProvider(_RecordingProvider):
            async def create_event(self, request, calendar_id=None):
                raise ProviderPermissionError("nope")

        provider = _RaisingProvider()

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
        payload = json.loads(result)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "permission_denied")


class TestToolBodyProviderErrors(unittest.TestCase):
    """Important 3: the tool body wraps `_compose` and its own provider calls
    the same way the confirmed executor already does. Before this, a calendar
    outage or a malformed `day_patterns.yaml` reaching the tool body directly
    (not through `/confirm`) escaped as a raw exception, surfacing to the
    model as `core/agent.py`'s generic "Error executing tool: <exception>"
    fallback -- no stable `ToolErrorCode`, no `user_action`.
    """

    def test_calendar_outage_in_compose_is_mapped_not_raised(self):
        from provider.external_errors import AuthMissingError

        class _RaisingProvider(_RecordingProvider):
            async def list_day(self, day, timezone, calendar_id=None):
                raise AuthMissingError("nope")

        result = _run_sync(mode="shadow", provider=_RaisingProvider())
        payload = json.loads(result)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "auth_missing")

    def test_apply_failure_in_auto_mode_is_mapped_not_raised(self):
        from provider.external_errors import ProviderPermissionError

        class _RaisingProvider(_RecordingProvider):
            async def create_event(self, request, calendar_id=None):
                raise ProviderPermissionError("nope")

        result = _run_sync(mode="auto", provider=_RaisingProvider())
        payload = json.loads(result)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "permission_denied")

    def test_malformed_pattern_file_maps_to_invalid_args_naming_the_file(self):
        provider = _RecordingProvider()

        async def _inner():
            with tempfile.TemporaryDirectory() as tmp:
                context, session = _build_env(
                    tmp, mode="shadow", planning_calendar_id=DEFAULT_CALENDAR_ID
                )
                # Syntactically valid YAML, but a version this build refuses
                # to guess at -- DayPatternSet.load raises a pydantic
                # ValidationError, not a provider exception.
                context.config.planning.patterns_path.write_text(
                    "version: 99\ndefaults: "
                    "{schedulable_window: {start: '10:00', end: '11:00'}}\n"
                )
                import tools.planning_tools as pt

                orig = pt.get_calendar_provider
                pt.get_calendar_provider = lambda config: provider
                try:
                    pairs = build_planning_capabilities(context.config)
                    tool_obj = next(
                        t for cap, t in pairs if cap.id == SYNC_CAPABILITY_ID
                    )
                    result = await tool_obj.execute(session=session, date=DATE)
                finally:
                    pt.get_calendar_provider = orig
                return result, str(context.config.planning.patterns_path)

        result, patterns_path = asyncio.run(_inner())
        payload = json.loads(result)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "invalid_args")
        self.assertIn(patterns_path, payload["error"]["message"])


class TestLedger(unittest.TestCase):
    """Bundled minor: `PlanRunLedger.mark_applied` after an auto apply."""

    def test_auto_mode_marks_the_ledger_applied(self):
        provider = _RecordingProvider()

        async def _inner():
            with tempfile.TemporaryDirectory() as tmp:
                context, session = _build_env(
                    tmp, mode="auto", planning_calendar_id=DEFAULT_CALENDAR_ID
                )
                import tools.planning_tools as pt

                orig = pt.get_calendar_provider
                pt.get_calendar_provider = lambda config: provider
                try:
                    pairs = build_planning_capabilities(context.config)
                    tool_obj = next(
                        t for cap, t in pairs if cap.id == SYNC_CAPABILITY_ID
                    )
                    await tool_obj.execute(session=session, date=DATE)
                finally:
                    pt.get_calendar_provider = orig
                ledger = PlanRunLedger(context.config)
                return ledger.status(DATE)

        status = asyncio.run(_inner())
        self.assertEqual(status, "applied")

    def test_shadow_mode_marks_the_ledger_applied_on_its_own(self):
        """Critical 1a: the sync tool marks the ledger in EVERY mode branch,
        not only after a real write -- otherwise shadow mode never marks the
        ledger anywhere and every tick re-notifies. Calls the sync tool
        directly, without `planning_build_day_plan`, so this cannot pass by
        riding on 1b's independent mark inside the build tool.
        """
        provider = _RecordingProvider()

        async def _inner():
            with tempfile.TemporaryDirectory() as tmp:
                context, session = _build_env(
                    tmp, mode="shadow", planning_calendar_id=DEFAULT_CALENDAR_ID
                )
                import tools.planning_tools as pt

                orig = pt.get_calendar_provider
                pt.get_calendar_provider = lambda config: provider
                try:
                    pairs = build_planning_capabilities(context.config)
                    tool_obj = next(
                        t for cap, t in pairs if cap.id == SYNC_CAPABILITY_ID
                    )
                    await tool_obj.execute(session=session, date=DATE)
                finally:
                    pt.get_calendar_provider = orig
                return PlanRunLedger(context.config).status(DATE)

        self.assertEqual(asyncio.run(_inner()), "applied")

    def test_review_mode_marks_the_ledger_applied_on_proposal(self):
        """Same as above for the review branch: the pending action alone
        cannot suppress future ticks, only the ledger can.
        """
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
                    await tool_obj.execute(session=session, date=DATE)
                finally:
                    pt.get_calendar_provider = orig
                return PlanRunLedger(context.config).status(DATE)

        self.assertEqual(asyncio.run(_inner()), "applied")


class TestBuildDayPlan(unittest.TestCase):
    """Bundled minor: `planning_build_day_plan` had no coverage at all."""

    def test_second_call_is_suppressed_even_when_sync_is_never_called(self):
        """Critical 1b: the once-per-date guarantee cannot depend on the
        model going on to call `planning_sync_daily_plan` at all -- if it
        just narrates `planning_build_day_plan`'s own output and stops (as
        the shipped agent prompt is free to do once told there's nothing to
        sync), the ledger must already be marked by `build_day_plan` itself.
        """
        provider = _RecordingProvider()

        async def _inner():
            with tempfile.TemporaryDirectory() as tmp:
                context, session = _build_env(
                    tmp, mode="shadow", planning_calendar_id=DEFAULT_CALENDAR_ID
                )
                import tools.planning_tools as pt

                orig_provider = pt.get_calendar_provider
                orig_readiness = pt._readiness
                pt.get_calendar_provider = lambda config: provider

                async def fake_readiness(context_, day):
                    return _GREEN_SIGNAL  # retry_eligible=False: never waits

                pt._readiness = fake_readiness
                try:
                    pairs = build_planning_capabilities(context.config)
                    tool_obj = next(
                        t for cap, t in pairs if cap.id == BUILD_CAPABILITY_ID
                    )
                    await tool_obj.execute(session=session, date=DATE)
                    session.state.suppress_final_output = False
                    await tool_obj.execute(session=session, date=DATE)
                finally:
                    pt.get_calendar_provider = orig_provider
                    pt._readiness = orig_readiness
                return session.state.suppress_final_output

        self.assertTrue(asyncio.run(_inner()))

    def test_already_applied_short_circuits_and_suppresses_output(self):
        async def _inner():
            with tempfile.TemporaryDirectory() as tmp:
                context, session = _build_env(
                    tmp, mode="shadow", planning_calendar_id=DEFAULT_CALENDAR_ID
                )
                PlanRunLedger(context.config).mark_applied(DATE)
                pairs = build_planning_capabilities(context.config)
                tool_obj = next(
                    t for cap, t in pairs if cap.id == BUILD_CAPABILITY_ID
                )
                result = await tool_obj.execute(session=session, date=DATE)
                return result, session.state.suppress_final_output

        result, suppressed = asyncio.run(_inner())
        self.assertIn("already planned", result.lower())
        self.assertTrue(suppressed)

    def test_retry_eligible_before_deadline_suppresses_output(self):
        provider = _RecordingProvider()

        async def _inner():
            with tempfile.TemporaryDirectory() as tmp:
                context, session = _build_env(
                    tmp, mode="shadow", planning_calendar_id=DEFAULT_CALENDAR_ID
                )
                import tools.planning_tools as pt

                orig_provider = pt.get_calendar_provider
                pt.get_calendar_provider = lambda config: provider
                # No health_planner server is configured, so `_readiness`
                # already degrades to retry_eligible=True on its own; pin
                # `_before_deadline` deterministically instead of depending
                # on the real wall clock relative to plan_deadline.
                orig_deadline = pt._before_deadline
                pt._before_deadline = lambda config: True
                try:
                    pairs = build_planning_capabilities(context.config)
                    tool_obj = next(
                        t for cap, t in pairs if cap.id == BUILD_CAPABILITY_ID
                    )
                    result = await tool_obj.execute(session=session, date=DATE)
                finally:
                    pt.get_calendar_provider = orig_provider
                    pt._before_deadline = orig_deadline
                return result, session.state.suppress_final_output

        result, suppressed = asyncio.run(_inner())
        self.assertIn("waiting for the next tick", result.lower())
        self.assertTrue(suppressed)

    def test_render_plan_carries_only_the_verdict_and_note(self):
        provider = _RecordingProvider()

        async def _inner():
            with tempfile.TemporaryDirectory() as tmp:
                context, session = _build_env(
                    tmp, mode="shadow", planning_calendar_id=DEFAULT_CALENDAR_ID
                )
                import tools.planning_tools as pt

                orig_provider = pt.get_calendar_provider
                orig_readiness = pt._readiness
                pt.get_calendar_provider = lambda config: provider

                async def fake_readiness(context_, day):
                    return _GREEN_SIGNAL

                pt._readiness = fake_readiness
                try:
                    pairs = build_planning_capabilities(context.config)
                    tool_obj = next(
                        t for cap, t in pairs if cap.id == BUILD_CAPABILITY_ID
                    )
                    result = await tool_obj.execute(session=session, date=DATE)
                finally:
                    pt.get_calendar_provider = orig_provider
                    pt._readiness = orig_readiness
                return result, session.state.suppress_final_output

        result, suppressed = asyncio.run(_inner())
        self.assertFalse(suppressed)
        self.assertIn(_GREEN_SIGNAL.verdict, result)
        self.assertIn(_GREEN_SIGNAL.note, result)
        for word in ("hrv", "recovery score", "sleep_", "cycle_id", "reasons"):
            self.assertNotIn(word, result.lower())


class TestRenderPlanAllDay(unittest.TestCase):
    """Minor 8: an all-day mandatory event must not read as a two-day meeting."""

    def test_all_day_event_renders_distinctly(self):
        import tools.planning_tools as pt
        from core.planning.models import DayPlan, MandatoryEvent

        plan = DayPlan(
            date=DATE, timezone="UTC", readiness=_GREEN_SIGNAL,
            mandatory=[
                MandatoryEvent(title="OOO", start="2026-09-04",
                               end="2026-09-05", all_day=True),
            ],
        )
        rendered = pt._render_plan(plan)
        self.assertIn("existing: OOO (all day)", rendered)
        self.assertNotIn("→", rendered)

    def test_timed_mandatory_event_still_renders_its_span(self):
        import tools.planning_tools as pt
        from core.planning.models import DayPlan, MandatoryEvent

        plan = DayPlan(
            date=DATE, timezone="UTC", readiness=_GREEN_SIGNAL,
            mandatory=[
                MandatoryEvent(title="Standup", start="10:00", end="10:30",
                               all_day=False),
            ],
        )
        rendered = pt._render_plan(plan)
        self.assertIn("existing: Standup (10:00 → 10:30)", rendered)


class TestReadinessHubAbsent(unittest.TestCase):
    """Bundled minor: `session.shared_context.mcp_hub = None` still composes."""

    def test_plan_still_composes_with_no_hub(self):
        provider = _RecordingProvider()

        async def _inner():
            with tempfile.TemporaryDirectory() as tmp:
                context, session = _build_env(
                    tmp, mode="shadow", planning_calendar_id=DEFAULT_CALENDAR_ID
                )
                session.shared_context.mcp_hub = None
                import tools.planning_tools as pt

                orig = pt.get_calendar_provider
                pt.get_calendar_provider = lambda config: provider
                try:
                    plan = await pt._compose(session, DATE)
                finally:
                    pt.get_calendar_provider = orig
                return plan

        plan = asyncio.run(_inner())
        self.assertEqual(plan.readiness.verdict, "unknown")


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

    def test_executor_path_also_resolves_none_to_primary(self):
        """The tool body and the executor share `_read_calendar_id`; pin both."""
        provider = _RecordingProvider()
        seen = []

        async def _inner():
            with tempfile.TemporaryDirectory() as tmp:
                context, session = _build_env(
                    tmp, mode="review", planning_calendar_id=DEFAULT_CALENDAR_ID
                )
                import tools.planning_tools as pt

                orig_provider = pt.get_calendar_provider
                orig_guard = pt.check_guards
                pt.get_calendar_provider = lambda config: provider

                def spy(*args, **kwargs):
                    seen.append(kwargs.get("read_calendar_id"))
                    return orig_guard(*args, **kwargs)

                try:
                    pairs = build_planning_capabilities(context.config)
                    tool_obj = next(
                        t for cap, t in pairs if cap.id == SYNC_CAPABILITY_ID
                    )
                    raw = await tool_obj.execute(session=session, date=DATE)
                    payload = json.loads(raw)["action"]["payload"]

                    # Only spy from here on: the propose call above already
                    # used one legitimate "primary" resolution.
                    seen.clear()
                    pt.check_guards = spy
                    executors = build_planning_confirmed_executors(context.config)
                    executor = executors[SYNC_CAPABILITY_ID]
                    await executor(session, payload)
                finally:
                    pt.get_calendar_provider = orig_provider
                    pt.check_guards = orig_guard

        asyncio.run(_inner())
        self.assertEqual(seen, ["primary"])


if __name__ == "__main__":
    unittest.main()
