"""Tests for Telegram confirmation inline buttons.

Hermetic: no network or Telegram auth. The pure ``channel.confirmations``
helpers are tested directly, and the channel's reply/callback paths use a fake
bot and fake callback query.
"""

import unittest
from types import SimpleNamespace

from telegram.constants import ParseMode

from channel.confirmations import (
    ConfirmationPrompt,
    callback_to_command,
    parse_confirmation,
)
from channel.telegram_channel import TelegramChannel, TelegramEventSource
from utils.config import TelegramConfig

_ID = "22222222-2222-4222-8222-222222222222"


# --- Pure helper tests ---------------------------------------------------


class ParseConfirmationTests(unittest.TestCase):
    def test_confirm_and_reject_extracted_and_text_sanitized(self):
        content = (
            "Чтобы создать встречу, отправьте:\n"
            f"/confirm {_ID}\n"
            "Или чтобы отменить:\n"
            f"/reject {_ID}"
        )
        text, prompt = parse_confirmation(content)
        self.assertIsInstance(prompt, ConfirmationPrompt)
        self.assertEqual(prompt.confirm_id, _ID)
        self.assertEqual(prompt.reject_id, _ID)
        # Raw command instructions are not visible in the final text.
        self.assertNotIn("/confirm", text)
        self.assertNotIn("/reject", text)
        self.assertNotIn("отправьте", text)
        self.assertNotIn("отменить", text)

    def test_leading_context_is_preserved(self):
        content = (
            "Встреча: Test Smoke Meeting (завтра 10:00)\n"
            "Подтвердите действие:\n"
            f"/confirm {_ID}\n"
            f"/reject {_ID}"
        )
        text, prompt = parse_confirmation(content)
        self.assertIn("Test Smoke Meeting", text)
        self.assertIsNotNone(prompt)

    def test_text_without_commands_is_unchanged(self):
        content = "Just a normal message with no actions."
        text, prompt = parse_confirmation(content)
        self.assertEqual(text, content)
        self.assertIsNone(prompt)

    def test_reject_only_mirrors_id_to_confirm(self):
        text, prompt = parse_confirmation(f"Cancel:\n/reject {_ID}")
        self.assertEqual(prompt.confirm_id, _ID)
        self.assertEqual(prompt.reject_id, _ID)


class CallbackToCommandTests(unittest.TestCase):
    def test_confirm_callback_maps_to_command(self):
        self.assertEqual(callback_to_command(f"confirm:{_ID}"), f"/confirm {_ID}")

    def test_reject_callback_maps_to_command(self):
        self.assertEqual(callback_to_command(f"reject:{_ID}"), f"/reject {_ID}")

    def test_unrecognized_callback_is_ignored(self):
        self.assertIsNone(callback_to_command("something:else"))
        self.assertIsNone(callback_to_command(None))
        self.assertIsNone(callback_to_command(f"delete:{_ID}"))


# --- Channel integration tests -------------------------------------------


class FakeBot:
    def __init__(self):
        self.calls: list[dict] = []

    async def send_message(self, **kwargs):
        self.calls.append(kwargs)


def _make_channel() -> TelegramChannel:
    return TelegramChannel(TelegramConfig(bot_token="x"))


class FakeCallbackQuery:
    def __init__(self, data, user_id="55", chat_id="100"):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = SimpleNamespace(chat=SimpleNamespace(id=chat_id))
        self.answered = False
        self.markup_cleared = False

    async def answer(self):
        self.answered = True

    async def edit_message_reply_markup(self, reply_markup=None):
        self.markup_cleared = reply_markup is None


class TelegramConfirmationReplyTests(unittest.IsolatedAsyncioTestCase):
    def _channel_with_bot(self, bot: FakeBot) -> TelegramChannel:
        channel = _make_channel()
        channel.application = SimpleNamespace(bot=bot)
        return channel

    async def test_reply_sanitizes_text_and_attaches_keyboard(self):
        bot = FakeBot()
        channel = self._channel_with_bot(bot)
        source = SimpleNamespace(chat_id="100")
        content = (
            "Встреча готова к созданию.\n"
            "Чтобы подтвердить, отправьте:\n"
            f"/confirm {_ID}\n"
            "Или чтобы отменить:\n"
            f"/reject {_ID}"
        )

        await channel.reply(content, source)

        self.assertEqual(len(bot.calls), 1)
        sent = bot.calls[0]
        # Raw command instructions are not visible in the final message.
        self.assertNotIn("/confirm", sent["text"])
        self.assertNotIn("/reject", sent["text"])
        self.assertIn("Встреча готова", sent["text"])
        # Inline keyboard with confirm/reject callbacks is attached.
        keyboard = sent["reply_markup"].inline_keyboard
        self.assertEqual(keyboard[0][0].callback_data, f"confirm:{_ID}")
        self.assertEqual(keyboard[0][1].callback_data, f"reject:{_ID}")

    async def test_plain_reply_has_no_keyboard(self):
        bot = FakeBot()
        channel = self._channel_with_bot(bot)
        source = SimpleNamespace(chat_id="100")

        await channel.reply("just text", source)

        sent = bot.calls[0]
        self.assertEqual(sent["text"], "just text")
        self.assertIsNone(sent["reply_markup"])
        self.assertEqual(sent["parse_mode"], ParseMode.HTML)


class TelegramCallbackRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def _run_callback(self, data):
        channel = _make_channel()
        dispatched: list[tuple[str, TelegramEventSource]] = []

        async def on_message(message, source):
            dispatched.append((message, source))

        query = FakeCallbackQuery(data)
        update = SimpleNamespace(callback_query=query)
        await channel._handle_callback_update(update, on_message)
        return dispatched, query

    async def test_confirm_click_dispatches_confirm_command(self):
        dispatched, query = await self._run_callback(f"confirm:{_ID}")
        self.assertTrue(query.answered)
        self.assertTrue(query.markup_cleared)
        self.assertEqual(len(dispatched), 1)
        message, source = dispatched[0]
        self.assertEqual(message, f"/confirm {_ID}")
        self.assertEqual(source.user_id, "55")
        self.assertEqual(source.chat_id, "100")

    async def test_reject_click_dispatches_reject_command(self):
        dispatched, _ = await self._run_callback(f"reject:{_ID}")
        self.assertEqual(dispatched[0][0], f"/reject {_ID}")

    async def test_unrecognized_callback_dispatches_nothing(self):
        dispatched, query = await self._run_callback("garbage")
        self.assertTrue(query.answered)
        self.assertEqual(dispatched, [])


if __name__ == "__main__":
    unittest.main()
