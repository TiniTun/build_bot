"""Tests for operator notices sent straight to the default delivery source."""

import tempfile
import unittest
from pathlib import Path

from channel.telegram_channel import TelegramEventSource
from server.operator_notice import make_mcp_notice_callback, mcp_notice_text, send_operator_notice
from tests.helpers import make_context, make_workspace


class _FakeChannel:
    platform_name = "telegram"

    def __init__(self, *, failures: int = 0) -> None:
        self.failures = failures
        self.sent: list[tuple[str, str]] = []

    async def reply(self, content: str, source) -> None:
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("TelegramChannel not started")
        self.sent.append((content, str(source)))


def _context(tmp: str, channel: _FakeChannel, default_source: str | None):
    context = make_context(make_workspace(Path(tmp)))
    context.channels = [channel]
    context.config.default_delivery_source = default_source
    return context


class SendOperatorNoticeTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_operator_notice_uses_default_source(self) -> None:
        channel = _FakeChannel()
        with tempfile.TemporaryDirectory() as tmp:
            context = _context(tmp, channel, "platform-telegram:42:4242")
            ok = await send_operator_notice(context, "hello")
        self.assertTrue(ok)
        self.assertEqual(channel.sent, [("hello", str(TelegramEventSource(user_id="42", chat_id="4242")))])

    async def test_send_operator_notice_without_source_returns_false(self) -> None:
        channel = _FakeChannel()
        with tempfile.TemporaryDirectory() as tmp:
            context = _context(tmp, channel, None)
            with self.assertLogs("server.operator_notice", level="WARNING"):
                ok = await send_operator_notice(context, "hello")
        self.assertFalse(ok)
        self.assertEqual(channel.sent, [])

    async def test_send_operator_notice_non_platform_source_returns_false(self) -> None:
        channel = _FakeChannel()
        with tempfile.TemporaryDirectory() as tmp:
            context = _context(tmp, channel, "agent:pickle")
            ok = await send_operator_notice(context, "hello")
        self.assertFalse(ok)
        self.assertEqual(channel.sent, [])

    async def test_send_failure_returns_false(self) -> None:
        channel = _FakeChannel(failures=1)
        with tempfile.TemporaryDirectory() as tmp:
            context = _context(tmp, channel, "platform-telegram:42:4242")
            with self.assertLogs("server.operator_notice", level="WARNING"):
                ok = await send_operator_notice(context, "hello")
        self.assertFalse(ok)


class McpNoticeTests(unittest.TestCase):
    def test_notice_texts_are_exact(self) -> None:
        self.assertEqual(
            mcp_notice_text("notion", "login_required"),
            "Notion отключён: нужен повторный вход. На bakeryd: build-bot --workspace ./default_workspace mcp-login notion",
        )
        self.assertEqual(
            mcp_notice_text("notion", "refresh_cap_soon"),
            "Доступ Notion истечёт через ~14 дней. На bakeryd: build-bot --workspace ./default_workspace mcp-login notion",
        )
        self.assertEqual(
            mcp_notice_text("work_notion", "login_required"),
            "Notion отключён: нужен повторный вход. На bakeryd: build-bot --workspace ./default_workspace mcp-login work_notion",
        )


class _FakeSleep:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


class McpNoticeCallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_callback_retries_until_channel_is_ready(self) -> None:
        channel = _FakeChannel(failures=2)
        sleep = _FakeSleep()
        with tempfile.TemporaryDirectory() as tmp:
            context = _context(tmp, channel, "platform-telegram:42:4242")
            callback = make_mcp_notice_callback(context, sleep=sleep)
            with self.assertLogs("server.operator_notice", level="WARNING"):
                delivered = await callback("notion", "login_required")
        self.assertTrue(delivered)
        self.assertEqual(sleep.delays, [10.0, 20.0])
        self.assertEqual(len(channel.sent), 1)
        self.assertIn("mcp-login notion", channel.sent[0][0])

    async def test_backoff_doubles_to_ten_minutes_for_at_least_an_hour(self) -> None:
        channel = _FakeChannel(failures=1000)
        sleep = _FakeSleep()
        with tempfile.TemporaryDirectory() as tmp:
            context = _context(tmp, channel, "platform-telegram:42:4242")
            callback = make_mcp_notice_callback(context, sleep=sleep)
            with self.assertLogs("server.operator_notice", level="ERROR") as logs:
                delivered = await callback("notion", "login_required")
        self.assertFalse(delivered)
        self.assertEqual(sleep.delays[:7], [10.0, 20.0, 40.0, 80.0, 160.0, 320.0, 600.0])
        self.assertTrue(all(d <= 600.0 for d in sleep.delays))
        self.assertGreaterEqual(sum(sleep.delays), 3600.0)
        # Bounded: it stops once an hour has been spent, not much later.
        self.assertLess(sum(sleep.delays) - sleep.delays[-1], 3600.0)
        self.assertEqual(channel.sent, [])
        self.assertTrue(any("not delivered" in line for line in logs.output))

    async def test_no_default_source_is_not_retried_and_logged_once(self) -> None:
        channel = _FakeChannel()
        sleep = _FakeSleep()
        with tempfile.TemporaryDirectory() as tmp:
            context = _context(tmp, channel, None)
            callback = make_mcp_notice_callback(context, sleep=sleep)
            with self.assertLogs("server.operator_notice", level="WARNING") as logs:
                first = await callback("notion", "login_required")
                second = await callback("notion", "login_required")
        self.assertFalse(first)
        self.assertFalse(second)
        self.assertEqual(sleep.delays, [])
        self.assertEqual(len(logs.output), 1)
        self.assertIn("default_delivery_source", logs.output[0])


if __name__ == "__main__":
    unittest.main()
