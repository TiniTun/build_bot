"""A cron tick that has nothing to say must publish nothing.

`AgentWorker` publishes the model's own final text as the outbound message.
A tool cannot make the model itself stay silent, so a tool that has decided
there is nothing to report sets `SessionState.suppress_final_output`, and
`AgentWorker.exec_session` must skip publishing when it sees that flag on the
success path only -- a failed session must still surface, even a suppressed
one.
"""

import asyncio
import dataclasses
import tempfile
import unittest
from pathlib import Path

from core.events import CronEventSource, DispatchEvent
from server.agent_worker import MAX_RETRIES, AgentWorker
from tests.helpers import make_context, make_workspace


class _StubState:
    def __init__(self, suppress: bool) -> None:
        self.suppress_final_output = suppress


class _StubSession:
    def __init__(self, response: str, suppress: bool, error: str | None) -> None:
        self.state = _StubState(suppress)
        self._response = response
        self._error = error

    async def chat(self, message: str) -> str:
        if self._error:
            raise RuntimeError(self._error)
        return self._response


class _StubAgent:
    def __init__(self, response: str, suppress: bool, error: str | None) -> None:
        self._response = response
        self._suppress = suppress
        self._error = error

    def resume_session(self, session_id: str) -> _StubSession:
        return _StubSession(self._response, self._suppress, self._error)


def _run_worker_with(response: str, suppress: bool, error: str | None = None) -> list:
    """Drive AgentWorker.exec_session against a cron DispatchEvent.

    Stubs `Agent.resume_session` (matching the pattern in
    tests/test_memory_workflows.py) so `session.chat()` returns `response`
    (or raises, if `error` is given) and `session.state.suppress_final_output`
    is `suppress`. Returns whatever landed on the real eventbus's queue.

    When `error` is set, the event is constructed with `retry_count` already
    at `MAX_RETRIES` so `exec_session`'s except branch emits the error
    response immediately instead of queuing a retry -- retry behavior isn't
    what this suite is about.
    """
    with tempfile.TemporaryDirectory() as tmp:
        workspace = make_workspace(Path(tmp))
        context = make_context(workspace)
        worker = AgentWorker(context)
        pickle_def = worker.context.agent_loader.load("pickle")

        import server.agent_worker as worker_mod

        original_agent = worker_mod.Agent
        worker_mod.Agent = lambda *_args, **_kwargs: _StubAgent(
            response, suppress, error
        )
        try:
            event = DispatchEvent(
                session_id="s1",
                source=CronEventSource("morning"),
                content="tick",
                retry_count=MAX_RETRIES if error else 0,
            )
            asyncio.run(worker.exec_session(event, pickle_def))
        finally:
            worker_mod.Agent = original_agent

        published = []
        while not worker.context.eventbus._queue.empty():
            published.append(worker.context.eventbus._queue.get_nowait())
        return published


class TestSuppressFinalOutput(unittest.TestCase):
    def test_session_state_defaults_to_not_suppressed(self):
        from core.session_state import SessionState

        self.assertIn("suppress_final_output", SessionState.__annotations__)

        # A default of True would silence every session, not just the ones a
        # tool deliberately opts into -- check the actual default, not just
        # that the field exists.
        field = next(
            f
            for f in dataclasses.fields(SessionState)
            if f.name == "suppress_final_output"
        )
        self.assertIs(field.default, False)

    def test_worker_publishes_nothing_when_suppressed(self):
        """Even though the model produced text, nothing reaches the bus."""
        published = _run_worker_with(response="Plan already applied.", suppress=True)
        self.assertEqual(published, [])

    def test_worker_publishes_normally_when_not_suppressed(self):
        published = _run_worker_with(response="Here is your plan.", suppress=False)
        self.assertEqual(len(published), 1)
        self.assertEqual(published[0].content, "Here is your plan.")

    def test_suppression_does_not_swallow_an_error(self):
        """A failure must still surface even from a suppressed session."""
        published = _run_worker_with(response="", suppress=True, error="boom")
        self.assertEqual(len(published), 1)
        self.assertEqual(published[0].error, "boom")


class _CompactStubNewSessionState:
    """The fresh SessionState `Agent.new_session` hands back mid-compaction."""

    def __init__(self) -> None:
        self.messages: list = []
        # Real SessionState defaults this to False, same as a brand-new session.
        self.suppress_final_output = False

    def add_message(self, message) -> None:
        self.messages.append(message)


class _CompactStubNewSession:
    def __init__(self) -> None:
        self.session_id = "new-session"
        self.state = _CompactStubNewSessionState()


class _CompactStubLLM:
    async def chat(self, messages, tools):
        return "summary of the older messages", None


class _CompactStubAgent:
    """Stands in for `SessionState.agent` during compaction."""

    def __init__(self) -> None:
        self.llm = _CompactStubLLM()

    def new_session(self, source):
        return _CompactStubNewSession()


class _CompactStubRoutingTable:
    def config_source_session_cache(self, source_str: str, session_id: str) -> None:
        pass


class _CompactStubSharedContext:
    def __init__(self) -> None:
        self.routing_table = _CompactStubRoutingTable()


class TestSuppressFinalOutputSurvivesCompaction(unittest.TestCase):
    """Compaction replaces `SessionState` wholesale (core/context_guard.py);
    a mid-tick compaction must not silently un-silence a suppressed run."""

    def test_compaction_carries_the_flag_forward(self):
        from core.context_guard import ContextGuard
        from core.session_state import SessionState

        old_state = SessionState(
            session_id="old-session",
            agent=_CompactStubAgent(),
            messages=[
                {"role": "user", "content": "is my plan ready"},
                {"role": "assistant", "content": "checking"},
            ],
            source=CronEventSource("morning"),
            shared_context=None,  # unused by compact_and_roll itself
            suppress_final_output=True,
        )

        guard = ContextGuard(shared_context=_CompactStubSharedContext())
        new_state = asyncio.run(guard.compact_and_roll(old_state))

        self.assertTrue(new_state.suppress_final_output)


if __name__ == "__main__":
    unittest.main()
