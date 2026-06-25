import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from core.agent_loader import AgentLoader
from provider.llm import LLMProvider
from utils.config import Config, LLMConfig, LLMDebugConfig

from tests.helpers import make_workspace, write_definition


def _fake_response(content: str = "hi") -> SimpleNamespace:
    message = SimpleNamespace(content=content, tool_calls=[])
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class LLMProviderFromConfigTests(unittest.TestCase):
    def test_from_config_preserves_fields(self) -> None:
        config = LLMConfig(
            provider="anthropic",
            model="anthropic/claude-opus-4.8",
            api_key="sk-ant-test",
            api_base="https://example.com",
            temperature=0.3,
            max_tokens=4096,
            extra={"top_p": 0.9},
        )

        provider = LLMProvider.from_config(config)

        self.assertEqual(provider.model, "anthropic/claude-opus-4.8")
        self.assertEqual(provider.api_key, "sk-ant-test")
        self.assertEqual(provider.api_base, "https://example.com")
        self.assertEqual(provider.temperature, 0.3)
        self.assertEqual(provider.max_tokens, 4096)
        self.assertEqual(provider._settings, {"top_p": 0.9})

    def test_blank_model_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            LLMConfig(provider="openai", model="  ", api_key="sk-test")


class LLMProviderChatTests(unittest.IsolatedAsyncioTestCase):
    async def test_chat_passes_temperature_and_max_tokens(self) -> None:
        provider = LLMProvider(
            model="gpt-4",
            api_key="sk-test",
            temperature=0.42,
            max_tokens=1234,
        )

        with patch(
            "provider.llm.base.acompletion",
            new=AsyncMock(return_value=_fake_response()),
        ) as mock_acompletion:
            content, tool_calls = await provider.chat(
                messages=[{"role": "user", "content": "hello"}]
            )

        self.assertEqual(content, "hi")
        self.assertEqual(tool_calls, [])
        kwargs = mock_acompletion.await_args.kwargs
        self.assertEqual(kwargs["model"], "gpt-4")
        self.assertEqual(kwargs["api_key"], "sk-test")
        self.assertEqual(kwargs["temperature"], 0.42)
        self.assertEqual(kwargs["max_tokens"], 1234)

    async def test_chat_omits_temperature_for_gpt5_models(self) -> None:
        provider = LLMProvider(
            model="gpt-5.5",
            api_key="sk-test",
            temperature=0.7,
            max_tokens=1234,
        )

        with patch(
            "provider.llm.base.acompletion",
            new=AsyncMock(return_value=_fake_response()),
        ) as mock_acompletion:
            await provider.chat(messages=[{"role": "user", "content": "hello"}])

        kwargs = mock_acompletion.await_args.kwargs
        self.assertEqual(kwargs["model"], "gpt-5.5")
        self.assertNotIn("temperature", kwargs)
        self.assertEqual(kwargs["max_tokens"], 1234)

    async def test_chat_forwards_extra_settings(self) -> None:
        config = LLMConfig(
            provider="anthropic",
            model="anthropic/claude-opus-4.8",
            api_key="sk-ant-test",
            extra={"top_p": 0.8},
        )
        provider = LLMProvider.from_config(config)

        with patch(
            "provider.llm.base.acompletion",
            new=AsyncMock(return_value=_fake_response()),
        ) as mock_acompletion:
            await provider.chat(messages=[{"role": "user", "content": "hi"}])

        kwargs = mock_acompletion.await_args.kwargs
        self.assertEqual(kwargs["model"], "anthropic/claude-opus-4.8")
        self.assertEqual(kwargs["top_p"], 0.8)


class LLMTraceTests(unittest.IsolatedAsyncioTestCase):
    def _provider(self, tmp: str, *, include_content: bool = False, **extra):
        return LLMProvider(
            model="gpt-4",
            api_key="sk-secret-DO-NOT-LOG",
            debug=LLMDebugConfig(enabled=True, include_content=include_content),
            logging_path=Path(tmp),
            agent_id="pickle",
            **extra,
        )

    def _trace_lines(self, tmp: str) -> list[dict]:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = Path(tmp) / "llm-traces" / f"{day}.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()]

    async def test_store_true_is_forwarded_to_acompletion(self) -> None:
        config = LLMConfig(
            provider="openai",
            model="gpt-4",
            api_key="sk-test",
            extra={"store": True},
        )
        provider = LLMProvider.from_config(config)
        with patch(
            "provider.llm.base.acompletion",
            new=AsyncMock(return_value=_fake_response()),
        ) as mock_acompletion:
            await provider.chat(messages=[{"role": "user", "content": "hi"}])
        self.assertIs(mock_acompletion.await_args.kwargs["store"], True)

    async def test_trace_written_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = self._provider(tmp, store=True)
            with patch(
                "provider.llm.base.acompletion",
                new=AsyncMock(return_value=_fake_response("hello there")),
            ):
                await provider.chat(
                    messages=[{"role": "user", "content": "hi"}],
                    trace_session_id="sess-1",
                )
            records = self._trace_lines(tmp)
            self.assertEqual(len(records), 1)
            rec = records[0]
            self.assertTrue(rec["success"])
            self.assertEqual(rec["agent_id"], "pickle")
            self.assertEqual(rec["session_id"], "sess-1")
            self.assertEqual(rec["model"], "gpt-4")
            self.assertEqual(rec["messages_count"], 1)
            self.assertEqual(rec["store"], True)
            self.assertIn("store", rec["request_keys"])
            self.assertEqual(rec["output_length"], len("hello there"))
            self.assertIn("trace_id", rec)
            self.assertIsInstance(rec["latency_ms"], (int, float))

    async def test_no_trace_when_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = LLMProvider(
                model="gpt-4",
                api_key="sk-test",
                debug=LLMDebugConfig(enabled=False),
                logging_path=Path(tmp),
            )
            with patch(
                "provider.llm.base.acompletion",
                new=AsyncMock(return_value=_fake_response()),
            ):
                await provider.chat(messages=[{"role": "user", "content": "hi"}])
            self.assertFalse((Path(tmp) / "llm-traces").exists())

    async def test_api_key_never_appears_in_trace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # Even with content opted in, the api key must not leak anywhere.
            provider = self._provider(tmp, include_content=True)
            with patch(
                "provider.llm.base.acompletion",
                new=AsyncMock(return_value=_fake_response()),
            ):
                await provider.chat(
                    messages=[{"role": "user", "content": "hi"}]
                )
            day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            raw = (Path(tmp) / "llm-traces" / f"{day}.jsonl").read_text()
            self.assertNotIn("sk-secret-DO-NOT-LOG", raw)

    async def test_content_omitted_unless_opted_in(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = self._provider(tmp, include_content=False)
            with patch(
                "provider.llm.base.acompletion",
                new=AsyncMock(return_value=_fake_response("secret-output")),
            ):
                await provider.chat(
                    messages=[{"role": "user", "content": "secret-input"}]
                )
            rec = self._trace_lines(tmp)[0]
            self.assertNotIn("messages", rec)
            self.assertNotIn("output", rec)
            raw_path = Path(tmp) / "llm-traces"
            day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            raw = (raw_path / f"{day}.jsonl").read_text()
            self.assertNotIn("secret-input", raw)
            self.assertNotIn("secret-output", raw)

    async def test_content_included_when_opted_in(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = self._provider(tmp, include_content=True)
            with patch(
                "provider.llm.base.acompletion",
                new=AsyncMock(return_value=_fake_response("answer")),
            ):
                await provider.chat(
                    messages=[{"role": "user", "content": "the-question"}]
                )
            rec = self._trace_lines(tmp)[0]
            self.assertEqual(rec["output"], "answer")
            self.assertEqual(rec["messages"][0]["content"], "the-question")

    async def test_error_path_writes_failed_trace_without_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = self._provider(tmp)
            boom = RuntimeError("auth failed for sk-secret-DO-NOT-LOG")
            with patch(
                "provider.llm.base.acompletion",
                new=AsyncMock(side_effect=boom),
            ):
                with self.assertRaises(RuntimeError):
                    await provider.chat(
                        messages=[{"role": "user", "content": "hi"}]
                    )
            rec = self._trace_lines(tmp)[0]
            self.assertFalse(rec["success"])
            self.assertEqual(rec["error_type"], "RuntimeError")
            self.assertNotIn("sk-secret-DO-NOT-LOG", rec["error"])
            self.assertIn("***", rec["error"])


class AgentLoaderLLMOverrideTests(unittest.TestCase):
    def test_per_agent_llm_override_merges_over_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            write_definition(
                workspace / "agents",
                "opus",
                "AGENT.md",
                {
                    "name": "Opus",
                    "description": "Anthropic agent",
                    "llm": {
                        "provider": "anthropic",
                        "model": "anthropic/claude-opus-4.8",
                        "max_tokens": 4096,
                    },
                },
                "You are Opus.",
            )

            loader = AgentLoader.from_config(Config.load(workspace))
            agent_def = loader.load("opus")

            # Overridden fields.
            self.assertEqual(agent_def.llm.provider, "anthropic")
            self.assertEqual(agent_def.llm.model, "anthropic/claude-opus-4.8")
            self.assertEqual(agent_def.llm.max_tokens, 4096)
            # Inherited from workspace defaults.
            self.assertEqual(agent_def.llm.api_key, "test-key")
            self.assertEqual(agent_def.llm.temperature, 0.7)


if __name__ == "__main__":
    unittest.main()
