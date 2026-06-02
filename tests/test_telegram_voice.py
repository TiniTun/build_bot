"""Tests for Telegram voice-message ingestion and voice config."""

import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from pydantic import ValidationError

from channel.telegram_channel import TelegramChannel
from utils.config import Config, TelegramConfig, TelegramVoiceConfig

EXAMPLE_CONFIG = (
    Path(__file__).resolve().parent.parent / "default_workspace" / "config.example.yaml"
)


class FakeFile:
    """Stand-in for a telegram File."""

    def __init__(self, data: bytes):
        self._data = data

    async def download_as_bytearray(self) -> bytearray:
        return bytearray(self._data)


class FakeVoice:
    """Stand-in for telegram message.voice."""

    def __init__(self, *, duration: int = 1, file_size: int | None = 1000, data: bytes = b"OGG"):
        self.duration = duration
        self.file_size = file_size
        self._data = data
        self.get_file_calls = 0

    async def get_file(self) -> FakeFile:
        self.get_file_calls += 1
        return FakeFile(self._data)


class FakeTranscriber:
    """Records calls; returns canned text or raises."""

    def __init__(self, *, text: str = "hello world", raises: Exception | None = None):
        self.text = text
        self.raises = raises
        self.calls: list[bytes] = []

    async def transcribe(self, audio: bytes, *, filename: str, content_type: str | None = None) -> str:
        self.calls.append(audio)
        if self.raises is not None:
            raise self.raises
        return self.text


class Recorder:
    """Generic async call recorder for download / convert / on_message / reply."""

    def __init__(self, result=None, raises: Exception | None = None):
        self.calls: list[tuple] = []
        self.result = result
        self.raises = raises

    async def __call__(self, *args):
        self.calls.append(args)
        if self.raises is not None:
            raise self.raises
        return self.result

    @property
    def called(self) -> bool:
        return bool(self.calls)


def make_update(*, user_id: str = "42", chat_id: str = "100", text=None, voice=None):
    message = SimpleNamespace(text=text, voice=voice, from_user=SimpleNamespace(id=user_id))
    return SimpleNamespace(message=message, effective_chat=SimpleNamespace(id=chat_id))


def make_channel(
    *,
    allowed=("42",),
    voice_enabled=True,
    max_file_size_mb=20,
    max_duration_seconds=300,
    transcriber=None,
    converter=None,
) -> TelegramChannel:
    config = TelegramConfig(
        bot_token="test-token",
        allowed_user_ids=list(allowed),
        voice=TelegramVoiceConfig(
            enabled=voice_enabled,
            max_file_size_mb=max_file_size_mb,
            max_duration_seconds=max_duration_seconds,
        ),
    )
    return TelegramChannel(config, transcriber=transcriber, ffmpeg_convert=converter)


class TextMessageTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_message_still_calls_on_message_unchanged(self):
        channel = make_channel(transcriber=FakeTranscriber())
        on_message = Recorder()
        update = make_update(text="hello there")

        await channel._handle_text_update(update, on_message)

        self.assertEqual(len(on_message.calls), 1)
        content, source = on_message.calls[0]
        self.assertEqual(content, "hello there")
        self.assertEqual(source.user_id, "42")
        self.assertEqual(source.chat_id, "100")


class VoiceHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_authorized_voice_transcribes_and_dispatches(self):
        transcriber = FakeTranscriber(text="  transcribed words  ")
        converter = Recorder(result=b"WAV")
        channel = make_channel(transcriber=transcriber, converter=converter)
        channel.reply = Recorder()  # do not touch the real Telegram bot
        on_message = Recorder()
        voice = FakeVoice(duration=5, file_size=1000, data=b"OGG")

        await channel._handle_voice_update(make_update(voice=voice), on_message)

        self.assertEqual(voice.get_file_calls, 1)
        self.assertEqual(converter.calls, [(b"OGG",)])
        self.assertEqual(transcriber.calls, [b"WAV"])
        self.assertEqual(len(on_message.calls), 1)
        content, source = on_message.calls[0]
        self.assertEqual(content, "transcribed words")
        self.assertEqual(source.chat_id, "100")
        self.assertFalse(channel.reply.called)

    async def test_unauthorized_voice_does_not_download_or_transcribe(self):
        transcriber = FakeTranscriber()
        converter = Recorder(result=b"WAV")
        channel = make_channel(allowed=("999",), transcriber=transcriber, converter=converter)
        channel.reply = Recorder()
        on_message = Recorder()
        voice = FakeVoice()

        await channel._handle_voice_update(make_update(user_id="42", voice=voice), on_message)

        self.assertEqual(voice.get_file_calls, 0)
        self.assertEqual(transcriber.calls, [])
        self.assertEqual(converter.calls, [])
        self.assertFalse(on_message.called)


class ProcessVoiceTests(unittest.IsolatedAsyncioTestCase):
    def _channel(self, **kwargs):
        return make_channel(**kwargs)

    async def test_over_duration_rejects_before_download(self):
        transcriber = FakeTranscriber()
        converter = Recorder(result=b"WAV")
        channel = self._channel(
            max_duration_seconds=60, transcriber=transcriber, converter=converter
        )
        download = Recorder(result=b"OGG")
        reply = Recorder()

        result = await channel._process_voice(
            duration=120, file_size=10, download=download, reply=reply
        )

        self.assertIsNone(result)
        self.assertFalse(download.called)
        self.assertEqual(transcriber.calls, [])
        self.assertEqual(len(reply.calls), 1)

    async def test_over_file_size_rejects_before_transcription(self):
        transcriber = FakeTranscriber()
        converter = Recorder(result=b"WAV")
        channel = self._channel(
            max_file_size_mb=1, transcriber=transcriber, converter=converter
        )
        download = Recorder(result=b"OGG")
        reply = Recorder()

        result = await channel._process_voice(
            duration=5, file_size=5 * 1024 * 1024, download=download, reply=reply
        )

        self.assertIsNone(result)
        self.assertFalse(download.called)
        self.assertEqual(transcriber.calls, [])
        self.assertEqual(len(reply.calls), 1)

    async def test_blank_transcription_does_not_dispatch(self):
        transcriber = FakeTranscriber(text="   ")
        converter = Recorder(result=b"WAV")
        channel = self._channel(transcriber=transcriber, converter=converter)
        download = Recorder(result=b"OGG")
        reply = Recorder()

        result = await channel._process_voice(
            duration=5, file_size=10, download=download, reply=reply
        )

        self.assertIsNone(result)
        self.assertEqual(transcriber.calls, [b"WAV"])
        self.assertEqual(len(reply.calls), 1)

    async def test_transcriber_error_replies_and_does_not_dispatch(self):
        transcriber = FakeTranscriber(raises=RuntimeError("provider down"))
        converter = Recorder(result=b"WAV")
        channel = self._channel(transcriber=transcriber, converter=converter)
        download = Recorder(result=b"OGG")
        reply = Recorder()

        result = await channel._process_voice(
            duration=5, file_size=10, download=download, reply=reply
        )

        self.assertIsNone(result)
        self.assertEqual(len(reply.calls), 1)

    async def test_missing_ffmpeg_replies_with_send_as_text(self):
        transcriber = FakeTranscriber()
        converter = Recorder(raises=FileNotFoundError("ffmpeg"))
        channel = self._channel(transcriber=transcriber, converter=converter)
        download = Recorder(result=b"OGG")
        reply = Recorder()

        result = await channel._process_voice(
            duration=5, file_size=10, download=download, reply=reply
        )

        self.assertIsNone(result)
        self.assertTrue(download.called)
        self.assertEqual(transcriber.calls, [])
        self.assertEqual(len(reply.calls), 1)
        self.assertIn("send it as text", reply.calls[0][0])

    async def test_voice_disabled_replies_and_does_not_dispatch(self):
        transcriber = FakeTranscriber()
        channel = self._channel(voice_enabled=False, transcriber=transcriber)
        download = Recorder(result=b"OGG")
        reply = Recorder()

        result = await channel._process_voice(
            duration=5, file_size=10, download=download, reply=reply
        )

        self.assertIsNone(result)
        self.assertFalse(download.called)
        self.assertEqual(len(reply.calls), 1)

    async def test_no_transcriber_configured_replies_and_does_not_dispatch(self):
        channel = self._channel(voice_enabled=True, transcriber=None)
        download = Recorder(result=b"OGG")
        reply = Recorder()

        result = await channel._process_voice(
            duration=5, file_size=10, download=download, reply=reply
        )

        self.assertIsNone(result)
        self.assertFalse(download.called)
        self.assertEqual(len(reply.calls), 1)

    async def test_missing_file_size_skips_size_guard(self):
        transcriber = FakeTranscriber(text="ok")
        converter = Recorder(result=b"WAV")
        channel = self._channel(transcriber=transcriber, converter=converter)
        download = Recorder(result=b"OGG")
        reply = Recorder()

        result = await channel._process_voice(
            duration=5, file_size=None, download=download, reply=reply
        )

        self.assertEqual(result, "ok")
        self.assertTrue(download.called)
        self.assertFalse(reply.called)


class VoiceConfigTests(unittest.TestCase):
    def test_voice_config_defaults(self):
        cfg = TelegramVoiceConfig()
        self.assertFalse(cfg.enabled)
        self.assertEqual(cfg.provider, "openai")
        self.assertEqual(cfg.model, "gpt-4o-mini-transcribe")
        self.assertIsNone(cfg.api_key)
        self.assertEqual(cfg.max_file_size_mb, 20)
        self.assertEqual(cfg.max_duration_seconds, 300)
        self.assertEqual(cfg.request_timeout_seconds, 60)

    def test_telegram_config_voice_optional(self):
        cfg = TelegramConfig(bot_token="x")
        self.assertIsInstance(cfg.voice, TelegramVoiceConfig)
        self.assertFalse(cfg.voice.enabled)

    def test_non_positive_limits_rejected(self):
        with self.assertRaises(ValidationError):
            TelegramVoiceConfig(max_duration_seconds=0)
        with self.assertRaises(ValidationError):
            TelegramVoiceConfig(max_file_size_mb=0)

    def test_example_config_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            ws.mkdir()
            shutil.copy(EXAMPLE_CONFIG, ws / "config.user.yaml")
            cfg = Config.load(ws)
            self.assertEqual(cfg.default_agent, "pickle")


if __name__ == "__main__":
    unittest.main()
