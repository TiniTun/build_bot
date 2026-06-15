import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from core.agent_loader import AgentLoader
from provider.llm import LLMProvider
from utils.config import Config, LLMConfig

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
