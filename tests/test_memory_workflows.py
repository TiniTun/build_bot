"""Tests for durable-memory workflow toggles and the Stage B auto-extract hook."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.events import (
    AgentEventSource,
    CliEventSource,
    DispatchEvent,
    InboundEvent,
    OutboundEvent,
)
from server.agent_worker import AgentWorker, MEMORY_MANAGER_AGENT_ID
from tests.helpers import make_context, make_workspace


def _inbound(content: str = "I prefer dark mode") -> InboundEvent:
    return InboundEvent(
        session_id="s1",
        source=CliEventSource(),
        content=content,
    )


def _dispatch(content: str = "do work") -> DispatchEvent:
    return DispatchEvent(
        session_id="s2",
        source=AgentEventSource("pickle"),
        content=content,
    )


class MemoryWorkflowGuardTests(unittest.TestCase):
    def _worker(self, workspace: Path) -> AgentWorker:
        context = make_context(workspace)
        return AgentWorker(context)

    def _agent_def(self, worker: AgentWorker, agent_id: str):
        return worker.context.agent_loader.load(agent_id)

    def test_auto_extract_disabled_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            worker = self._worker(workspace)

            self.assertFalse(worker.context.config.memory.auto_extract)
            self.assertFalse(worker.context.config.memory.auto_retrieve)

            pickle_def = self._agent_def(worker, "pickle")
            self.assertFalse(
                worker._should_auto_extract(_inbound(), pickle_def)
            )

    def test_cookie_session_never_extracts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            worker = self._worker(workspace)
            worker.context.config.memory.auto_extract = True

            cookie_def = self._agent_def(worker, MEMORY_MANAGER_AGENT_ID)
            self.assertFalse(
                worker._should_auto_extract(_inbound(), cookie_def)
            )

    def test_dispatch_event_never_extracts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            worker = self._worker(workspace)
            worker.context.config.memory.auto_extract = True

            pickle_def = self._agent_def(worker, "pickle")
            self.assertFalse(
                worker._should_auto_extract(_dispatch(), pickle_def)
            )

    def test_user_facing_inbound_to_pickle_extracts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            worker = self._worker(workspace)
            worker.context.config.memory.auto_extract = True

            pickle_def = self._agent_def(worker, "pickle")
            self.assertTrue(
                worker._should_auto_extract(_inbound(), pickle_def)
            )


class MemoryExtractionRunTests(unittest.TestCase):
    def _worker(self, workspace: Path) -> AgentWorker:
        context = make_context(workspace)
        return AgentWorker(context)

    def test_extraction_emits_no_outbound_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            worker = self._worker(workspace)
            worker.context.config.memory.auto_extract = True

            chats: list[str] = []

            class _StubSession:
                async def chat(self, message: str) -> str:
                    chats.append(message)
                    return ""

            class _StubAgent:
                def __init__(self, *_args, **_kwargs) -> None:
                    pass

                def new_session(self, source) -> "_StubSession":
                    return _StubSession()

            import server.agent_worker as worker_mod

            original_agent = worker_mod.Agent
            worker_mod.Agent = _StubAgent
            try:
                pickle_def = worker.context.agent_loader.load("pickle")
                event = _inbound("Remember I live in Madrid")
                asyncio.run(worker._run_memory_extraction(event, pickle_def))
            finally:
                worker_mod.Agent = original_agent

            # Cookie was asked to extract from the user's message.
            self.assertEqual(len(chats), 1)
            self.assertIn("Madrid", chats[0])

            # No event of any kind was published, so no user-visible delivery.
            queued: list = []
            while not worker.context.eventbus._queue.empty():
                queued.append(worker.context.eventbus._queue.get_nowait())
            self.assertFalse(
                any(isinstance(e, OutboundEvent) for e in queued)
            )
            self.assertEqual(queued, [])

    def test_extraction_swallows_exceptions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            worker = self._worker(workspace)
            worker.context.config.memory.auto_extract = True

            class _BoomAgent:
                def __init__(self, *_args, **_kwargs) -> None:
                    pass

                def new_session(self, source):
                    raise RuntimeError("boom")

            import server.agent_worker as worker_mod

            original_agent = worker_mod.Agent
            worker_mod.Agent = _BoomAgent
            try:
                pickle_def = worker.context.agent_loader.load("pickle")
                # Must not raise.
                asyncio.run(
                    worker._run_memory_extraction(_inbound(), pickle_def)
                )
            finally:
                worker_mod.Agent = original_agent

            # Nothing published despite the failure.
            self.assertTrue(worker.context.eventbus._queue.empty())

    def test_extraction_skips_when_cookie_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            worker = self._worker(workspace)
            worker.context.config.memory.auto_extract = True

            from utils.def_loader import DefNotFoundError

            def _raise(_agent_id):
                raise DefNotFoundError("agent", "cookie")

            worker.context.agent_loader.load = _raise  # type: ignore[assignment]

            event = _inbound()
            # Should silently skip, no raise, nothing published.
            asyncio.run(worker._run_memory_extraction(event, SimpleNamespace(id="pickle")))
            self.assertTrue(worker.context.eventbus._queue.empty())


if __name__ == "__main__":
    unittest.main()
