"""Compatibility and validation for explicit Responses configuration."""

import unittest

from utils.config import LLMConfig


class ResponsesConfigTests(unittest.TestCase):
    def config(self, **kwargs):
        return LLMConfig(provider="openai", model="gpt-5.6-sol", api_key="test", **kwargs)

    def test_existing_config_keeps_chat_completions(self):
        self.assertEqual(self.config().api_mode, "chat_completions")

    def test_reasoning_effort_matches_responses_api_values(self):
        self.assertEqual(self.config(reasoning_effort="minimal").reasoning_effort, "minimal")
        with self.assertRaises(ValueError):
            self.config(reasoning_effort="max")

    def test_responses_requires_stored_continuation(self):
        for store in (None, False):
            with self.subTest(store=store), self.assertRaises(ValueError):
                self.config(api_mode="responses", store=store)
        self.assertTrue(self.config(api_mode="responses", store=True).store)
        self.config(api_mode="responses", extra={"store": True})

    def test_duplicate_conflicting_settings_rejected(self):
        with self.assertRaises(ValueError):
            self.config(store=True, extra={"store": False})
        with self.assertRaises(ValueError):
            self.config(reasoning_effort="medium", extra={"reasoning_effort": "none"})

    def test_extra_cannot_override_session_state(self):
        for key in ("previous_response_id", "input", "instructions", "stream", "background"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.config(api_mode="responses", store=True, extra={key: "override"})
