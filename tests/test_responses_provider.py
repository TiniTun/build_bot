import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import litellm

from provider.llm.base import LLMProvider
from provider.llm.models import LLMResponse, ResponseCursorNotFound
from provider.llm.responses import build_input, parse_response


def completed(output):
    return {
        "id": "resp_1",
        "status": "completed",
        "output": output,
        "usage": {"input_tokens": 10, "output_tokens": 20, "output_tokens_details": {"reasoning_tokens": 12}},
    }


class ResponsesProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_transport_maps_options_tools_and_cursor(self):
        provider = LLMProvider(
            "gpt-5.6-sol", "secret", api_mode="responses", reasoning_effort="medium", max_tokens=123, store=True
        )
        output = completed(
            [
                {"type": "reasoning", "encrypted_content": "hidden"},
                {"type": "function_call", "id": "fc_item", "call_id": "call_actual", "name": "read", "arguments": "{}"},
            ]
        )
        with (
            patch("provider.llm.base.aresponses", new=AsyncMock(return_value=output)) as request,
            patch("provider.llm.base.acompletion", new=AsyncMock()) as legacy,
        ):
            result = await provider.chat(
                [
                    {"role": "system", "content": "rules"},
                    {"role": "tool", "tool_call_id": "call_previous", "content": "ok"},
                ],
                tools=[
                    {
                        "type": "function",
                        "function": {"name": "read", "parameters": {"type": "object", "properties": {}}},
                    }
                ],
                previous_response_id="resp_previous",
            )
        legacy.assert_not_awaited()
        args = request.await_args.kwargs
        self.assertEqual(args["instructions"], "rules")
        self.assertEqual(args["input"], [{"type": "function_call_output", "call_id": "call_previous", "output": "ok"}])
        self.assertEqual(args["previous_response_id"], "resp_previous")
        self.assertEqual(args["reasoning"], {"effort": "medium"})
        self.assertEqual(args["max_output_tokens"], 123)
        self.assertFalse(args["tools"][0]["strict"])
        for key in ("messages", "max_tokens", "reasoning_effort", "temperature"):
            self.assertNotIn(key, args)
        self.assertIsInstance(result, LLMResponse)
        content, calls = result
        self.assertEqual(calls[0].id, "call_actual")
        self.assertEqual(result.usage["reasoning_tokens"], 12)

    def test_replay_history_uses_call_id_and_text(self):
        instructions, items = build_input(
            [
                {"role": "system", "content": "rules"},
                {
                    "role": "assistant",
                    "content": "checking",
                    "tool_calls": [{"id": "call_1", "function": {"name": "read", "arguments": "{}"}}],
                },
                {"role": "tool", "tool_call_id": "call_1", "content": "result"},
            ]
        )
        self.assertEqual(items[1], {"type": "function_call", "call_id": "call_1", "name": "read", "arguments": "{}"})
        self.assertEqual(items[2]["call_id"], "call_1")

    def test_rejects_noncompleted_before_parsing_calls(self):
        for status in ("failed", "incomplete", "in_progress", None):
            with self.subTest(status=status), self.assertRaises(RuntimeError):
                parse_response(
                    {
                        "status": status,
                        "output": [{"type": "function_call", "call_id": "call_1", "name": "write", "arguments": "{}"}],
                    }
                )

    def test_parses_all_text_parts_and_rejects_refusals_unknown_outputs(self):
        result = parse_response(
            completed(
                [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "one"}, {"type": "output_text", "text": "two"}],
                    }
                ]
            )
        )
        self.assertEqual(result.content, "onetwo")
        for output in (
            [{"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}],
            [{"type": "web_search_call"}],
            [],
        ):
            with self.subTest(output=output), self.assertRaises(RuntimeError):
                parse_response(completed(output))

    async def test_rejects_stateless_and_does_not_forward_cursor_to_legacy(self):
        with self.assertRaises(ValueError):
            await LLMProvider("gpt-5.6-sol", "key", api_mode="responses", store=False).chat([])

    async def test_maps_only_explicit_missing_previous_response_errors(self):
        provider = LLMProvider("gpt-5.6-sol", "key", api_mode="responses", store=True)
        missing = litellm.BadRequestError(
            message="No response found with id 'resp_expired' for previous_response_id",
            model="gpt-5.6-sol",
            llm_provider="openai",
            body={
                "error": {
                    "message": "No response found with id 'resp_expired'",
                    "param": "previous_response_id",
                    "code": "response_not_found",
                }
            },
        )
        with patch("provider.llm.base.aresponses", new=AsyncMock(side_effect=missing)):
            with self.assertRaises(ResponseCursorNotFound):
                await provider.chat(
                    [{"role": "user", "content": "hi"}],
                    previous_response_id="resp_expired",
                )

        generic = litellm.BadRequestError(
            message="Tool schema is invalid",
            model="gpt-5.6-sol",
            llm_provider="openai",
            body={"error": {"message": "Tool schema is invalid", "param": "tools"}},
        )
        with patch("provider.llm.base.aresponses", new=AsyncMock(side_effect=generic)):
            with self.assertRaises(litellm.BadRequestError):
                await provider.chat(
                    [{"role": "user", "content": "hi"}],
                    previous_response_id="resp_still_valid",
                )

    async def test_trace_has_safe_usage_and_never_opaque_reasoning(self):
        provider = LLMProvider(
            "gpt-5.6-sol",
            "secret",
            api_mode="responses",
            store=True,
            debug=SimpleNamespace(enabled=True, include_content=True),
            logging_path=Path("/unused"),
        )
        output = completed(
            [
                {"type": "reasoning", "encrypted_content": "opaque-sensitive"},
                {"type": "message", "content": [{"type": "output_text", "text": "ok"}]},
            ]
        )
        with (
            patch("provider.llm.base.aresponses", new=AsyncMock(return_value=output)),
            patch("provider.llm.trace.write_record") as write,
        ):
            await provider.chat([{"role": "user", "content": "hi"}])
        record = write.call_args.args[1]
        self.assertEqual(record["endpoint"], "/v1/responses")
        self.assertEqual(record["response_id"], "resp_1")
        self.assertEqual(record["reasoning_tokens"], 12)
        self.assertNotIn("opaque-sensitive", str(record))
        with (
            patch("provider.llm.base.aresponses", new=AsyncMock(side_effect=RuntimeError("opaque-sensitive"))),
            patch("provider.llm.trace.write_record") as write,
        ):
            with self.assertRaises(RuntimeError):
                await provider.chat([{"role": "user", "content": "hi"}])
        self.assertNotIn("opaque-sensitive", str(write.call_args.args[1]))
