import asyncio
import json
import unittest
from types import SimpleNamespace

from core.agent import AgentSession
from provider.llm.base import LLMToolCall


class _FakeTools:
    def __init__(self):
        self.active = 0
        self.max_active = 0

    async def execute_tool(self, tool_name, session, **kwargs):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0)
        self.active -= 1
        return tool_name


class _FakeState:
    def __init__(self):
        self.messages = []

    def add_message(self, message):
        self.messages.append(message)


class AgentToolCallTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_calls_run_sequentially_to_avoid_parallel_heavy_tools(self):
        session = AgentSession.__new__(AgentSession)
        session.tools = _FakeTools()
        session.state = _FakeState()

        calls = [
            LLMToolCall(id="1", name="webread", arguments=json.dumps({"url": "https://a.example"})),
            LLMToolCall(id="2", name="webread", arguments=json.dumps({"url": "https://b.example"})),
        ]

        await session._handle_tool_calls(calls)

        self.assertEqual(session.tools.max_active, 1)
        self.assertEqual([m["content"] for m in session.state.messages], ["webread", "webread"])


if __name__ == "__main__":
    unittest.main()
