"""Tests for TelegramChannel recovery from a failed start (e.g. no network at boot)."""

import asyncio
import unittest
from unittest import mock

import httpx
from telegram.error import NetworkError

from channel import telegram_channel
from channel.telegram_channel import TelegramChannel
from utils.config import TelegramConfig


class FakeUpdater:
    def __init__(self, polling_error: Exception | None = None):
        self.polling_error = polling_error
        self.running = False

    async def start_polling(self) -> None:
        if self.polling_error is not None:
            raise self.polling_error
        self.running = True

    async def stop(self) -> None:
        self.running = False


class FakeApplication:
    """Records lifecycle calls of python-telegram-bot's Application."""

    def __init__(
        self,
        init_error: Exception | None = None,
        polling_error: Exception | None = None,
    ):
        self.init_error = init_error
        self.updater = FakeUpdater(polling_error)
        self.running = False
        self.initialized = False
        self.calls: list[str] = []

    def add_handler(self, handler) -> None:
        pass

    def add_error_handler(self, handler) -> None:
        pass

    async def initialize(self) -> None:
        self.calls.append("initialize")
        if self.init_error is not None:
            raise self.init_error
        self.initialized = True

    async def start(self) -> None:
        self.calls.append("start")
        self.running = True

    async def stop(self) -> None:
        self.calls.append("stop")
        if not self.running:
            raise RuntimeError("This Application is not running!")
        self.running = False

    async def shutdown(self) -> None:
        self.calls.append("shutdown")
        self.initialized = False


class FakeBuilder:
    def __init__(self, apps: list[FakeApplication]):
        self._apps = apps

    def __getattr__(self, name):
        # token(), connect_timeout(), read_timeout(), media_write_timeout()
        return lambda *args, **kwargs: self

    def build(self) -> FakeApplication:
        return self._apps.pop(0)


async def on_message(message, source) -> None:
    pass


class TelegramStartupFailureTest(unittest.IsolatedAsyncioTestCase):
    async def _assert_second_run_starts(self, failing: FakeApplication, failure: type[Exception]) -> None:
        healthy = FakeApplication()
        apps = [failing, healthy]
        channel = TelegramChannel(TelegramConfig(bot_token="test-token"))

        with mock.patch.object(telegram_channel.Application, "builder", lambda: FakeBuilder(apps)):
            with self.assertRaises(failure):
                await channel.run(on_message)

            self.assertIsNone(channel.application)

            # The restart the server monitor does must bring the channel up.
            task = asyncio.create_task(channel.run(on_message))
            for _ in range(50):
                if healthy.updater.running:
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(healthy.updater.running)
            self.assertIs(channel.application, healthy)

            await channel.stop()
            await asyncio.wait_for(task, timeout=3)

    async def test_run_recovers_after_initialize_fails(self) -> None:
        failing = FakeApplication(init_error=httpx.ConnectError("[Errno -3] Temporary failure in name resolution"))
        await self._assert_second_run_starts(failing, httpx.ConnectError)
        # Never started, so only shutdown is safe to call.
        self.assertEqual(failing.calls, ["initialize", "shutdown"])

    async def test_run_recovers_after_start_polling_fails(self) -> None:
        failing = FakeApplication(polling_error=NetworkError("connection reset"))
        await self._assert_second_run_starts(failing, NetworkError)
        self.assertEqual(failing.calls, ["initialize", "start", "stop", "shutdown"])
        self.assertFalse(failing.running)


if __name__ == "__main__":
    unittest.main()
