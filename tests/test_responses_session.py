import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from core.agent import AgentSession
from core.events import CliEventSource
from core.history import HistorySession, HistoryStore
from core.session_state import SessionState
from provider.llm.base import LLMToolCall


class ResponsesSessionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = HistoryStore(Path(self.tmp.name))
        self.source = CliEventSource()
        self.store.create_session("agent", "session", self.source)
        self.context = SimpleNamespace(
            history_store=self.store,
            prompt_builder=SimpleNamespace(build=lambda state: "Current instructions"),
        )
        self.agent = SimpleNamespace(llm=SimpleNamespace(model="test", api_mode="responses", api_base=None))
        self.state = SessionState("session", self.agent, [], self.source, self.context)

    def test_old_index_has_no_cursor(self):
        old = HistorySession(id="old", agent_id="a", source=str(self.source), created_at="now", update_at="now")
        self.assertIsNone(old.last_response_id)
        self.assertEqual(old.response_message_count, 0)

    def test_interrupted_call_repair_invalidates_cursor(self):
        self.state.add_message(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "read", "arguments": "{}"}}],
            }
        )
        self.state.checkpoint_response("resp_1")
        self.state.add_message({"role": "user", "content": "Continue"})
        messages, cursor = self.state.build_response_messages()
        self.assertIsNone(cursor)
        self.assertEqual(messages[2]["tool_call_id"], "call_1")
        self.assertIn("interrupted", messages[2]["content"])
        self.assertIsNone(self.store.get_session_info("session").last_response_id)

    def test_uncheckpointed_assistant_response_invalidates_older_cursor(self):
        self.state.add_message({"role": "user", "content": "First"})
        self.state.checkpoint_response("resp_first")
        self.state.add_message({"role": "assistant", "content": "Uncheckpointed"})

        messages, cursor = self.state.build_response_messages()

        self.assertIsNone(cursor)
        self.assertEqual(
            messages,
            [
                {"role": "system", "content": "Current instructions"},
                {"role": "user", "content": "First"},
                {"role": "assistant", "content": "Uncheckpointed"},
            ],
        )
        self.assertIsNone(self.store.get_session_info("session").last_response_id)

    async def test_each_result_is_durable_before_next_tool(self):
        calls = [LLMToolCall("one", "read", "{}"), LLMToolCall("two", "read", "{}")]

        async def execute(name, session, **kwargs):
            if self.state.messages:
                persisted = self.store.get_messages("session")
                self.assertEqual(persisted[0].tool_call_id, "one")
                raise asyncio.CancelledError()
            return "first result"

        session = AgentSession(self.agent, self.state, None, SimpleNamespace(execute_tool=execute))
        with self.assertRaises(asyncio.CancelledError):
            await session._handle_tool_calls(calls)
        self.assertEqual(len(self.store.get_messages("session")), 1)

    async def test_chat_builds_request_after_compaction_and_resets_cursor(self):
        self.state.add_message({"role": "user", "content": "old content"})
        self.state.checkpoint_response("old_response")
        self.store.create_session("agent", "compacted", self.source)
        compacted = SessionState("compacted", self.agent, [], self.source, self.context)
        compacted.add_message({"role": "user", "content": "summary"})

        async def compact(state):
            return compacted

        async def chat(messages, tools, **kwargs):
            self.assertEqual([m["content"] for m in messages], ["Current instructions", "summary"])
            self.assertIsNone(kwargs.get("previous_response_id"))
            self.assertEqual(kwargs["trace_session_id"], "compacted")
            return "done", []

        self.agent.llm.chat = chat
        session = AgentSession(
            self.agent,
            self.state,
            SimpleNamespace(check_and_compact=compact),
            SimpleNamespace(get_tool_schemas=lambda: []),
        )
        self.assertEqual(await session.chat("new request"), "done")


if __name__ == "__main__":
    unittest.main()
