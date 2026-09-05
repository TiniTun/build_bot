"""Hermetic contract checks across the Responses transport and persisted session."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from core.agent import AgentSession
from core.events import AgentEventSource
from core.history import HistoryStore
from core.session_state import SessionState
from provider.llm.base import LLMProvider
from provider.llm.models import ResponseCursorNotFound


def response(identifier, *, calls=False):
    output = (
        [
            {"type": "reasoning", "id": "rs_1", "encrypted_content": "PRIVATE_REASONING"},
            {
                "type": "function_call",
                "id": "fc_item",
                "call_id": "call_actual",
                "name": "read_status",
                "arguments": "{}",
            },
        ]
        if calls
        else [
            {"type": "message", "content": [{"type": "output_text", "text": "Ready"}]},
        ]
    )
    return {"id": identifier, "status": "completed", "output": output}


class ResponsesSessionContractTests(unittest.IsolatedAsyncioTestCase):
    def make_session(self, directory):
        history = HistoryStore(Path(directory))
        source = AgentEventSource("contract")
        history.create_session("contract", "session-contract", source)
        context = SimpleNamespace(
            history_store=history,
            prompt_builder=SimpleNamespace(build=lambda state: "Follow current instructions"),
        )
        agent = SimpleNamespace(
            llm=LLMProvider(
                model="gpt-5.6-sol",
                api_key="fake-key",
                api_mode="responses",
                reasoning_effort="medium",
                store=True,
            )
        )
        state = SessionState("session-contract", agent, [], source, context)
        tools = SimpleNamespace(
            get_tool_schemas=lambda: [
                {
                    "type": "function",
                    "function": {
                        "name": "read_status",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            execute_tool=AsyncMock(return_value="status: ready"),
        )
        guard = SimpleNamespace(check_and_compact=AsyncMock(side_effect=lambda state: state))
        return AgentSession(agent, state, guard, tools)

    async def test_tool_cycle_then_reload_sends_only_new_input(self):
        with tempfile.TemporaryDirectory() as directory:
            session = self.make_session(directory)
            with (
                patch(
                    "provider.llm.base.aresponses",
                    new=AsyncMock(
                        side_effect=[
                            response("resp_call", calls=True),
                            response("resp_done"),
                            response("resp_next"),
                        ]
                    ),
                ) as api,
                patch("provider.llm.base.acompletion", new=AsyncMock()) as chat,
            ):
                self.assertEqual(await session.chat("Check status"), "Ready")
                history = session.shared_context.history_store
                metadata = history.list_sessions()[0]
                cursor_fields = {
                    name: getattr(metadata, name)
                    for name in (
                        "last_response_id",
                        "last_response_model",
                        "last_api_mode",
                        "response_endpoint_fingerprint",
                        "response_message_count",
                        "response_prefix_hash",
                    )
                }
                session.state = SessionState(
                    session.session_id,
                    session.agent,
                    [message.to_message() for message in history.get_messages(session.session_id)],
                    session.state.source,
                    session.shared_context,
                    **cursor_fields,
                )
                await session.chat("And now?")

            chat.assert_not_awaited()
            session.tools.execute_tool.assert_awaited_once()
            first, second, third = [call.kwargs for call in api.await_args_list]
            self.assertNotIn("previous_response_id", first)
            self.assertEqual(second["previous_response_id"], "resp_call")
            self.assertEqual(
                second["input"],
                [
                    {
                        "type": "function_call_output",
                        "call_id": "call_actual",
                        "output": "status: ready",
                    }
                ],
            )
            self.assertEqual(third["previous_response_id"], "resp_done")
            self.assertEqual(third["input"], [{"role": "user", "content": "And now?"}])
            for request in (first, second, third):
                self.assertEqual(request["instructions"], "Follow current instructions")
                self.assertEqual(request["reasoning"], {"effort": "medium"})
            self.assertNotIn("PRIVATE_REASONING", repr(history.get_messages(session.session_id)))

    async def test_incomplete_response_does_not_execute_or_checkpoint_tool(self):
        with tempfile.TemporaryDirectory() as directory:
            session = self.make_session(directory)
            partial = response("resp_partial", calls=True)
            partial["status"] = "incomplete"
            partial["incomplete_details"] = {"reason": "max_output_tokens"}
            with patch("provider.llm.base.aresponses", new=AsyncMock(return_value=partial)):
                with self.assertRaises(RuntimeError):
                    await session.chat("Check")
            session.tools.execute_tool.assert_not_awaited()
            self.assertIsNone(session.state.last_response_id)
            self.assertEqual([message["role"] for message in session.state.messages], ["user"])

    async def test_changed_endpoint_replays_history_without_old_cursor(self):
        with tempfile.TemporaryDirectory() as directory:
            session = self.make_session(directory)
            with patch(
                "provider.llm.base.aresponses",
                new=AsyncMock(
                    side_effect=[
                        response("resp_first"),
                        response("resp_second"),
                    ]
                ),
            ) as api:
                await session.chat("First")
                session.agent.llm.api_base = "https://another.example/v1"
                await session.chat("Second")
            request = api.await_args.kwargs
            self.assertNotIn("previous_response_id", request)
            self.assertEqual(
                request["input"],
                [
                    {"role": "user", "content": "First"},
                    {"role": "assistant", "content": "Ready"},
                    {"role": "user", "content": "Second"},
                ],
            )

    async def test_expired_cursor_replays_local_history_once(self):
        with tempfile.TemporaryDirectory() as directory:
            session = self.make_session(directory)
            with patch.object(
                session.agent.llm,
                "chat",
                new=AsyncMock(),
            ) as llm:
                # The provider normally parses raw response dictionaries. Keep
                # this test at the session boundary with parsed equivalents.
                from provider.llm.responses import parse_response

                llm.side_effect = [
                    parse_response(response("resp_first")),
                    ResponseCursorNotFound("expired"),
                    parse_response(response("resp_recovered")),
                ]
                await session.chat("First")
                await session.chat("Second")

            first, failed, recovered = llm.await_args_list
            self.assertNotIn("previous_response_id", first.kwargs)
            self.assertEqual(failed.kwargs["previous_response_id"], "resp_first")
            self.assertNotIn("previous_response_id", recovered.kwargs)
            self.assertEqual(
                recovered.args[0],
                [
                    {"role": "system", "content": "Follow current instructions"},
                    {"role": "user", "content": "First"},
                    {"role": "assistant", "content": "Ready"},
                    {"role": "user", "content": "Second"},
                ],
            )
            self.assertEqual(session.state.last_response_id, "resp_recovered")
