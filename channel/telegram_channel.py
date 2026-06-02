"""Telegram channel implementation."""

import asyncio
from dataclasses import dataclass
import logging
from collections.abc import Callable, Awaitable

from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

from core.events import EventSource
from channel.base import Channel
from provider.audio.base import AudioTranscriber
from provider.audio.ffmpeg import FfmpegError, ogg_to_wav
from utils.config import TelegramConfig

logger = logging.getLogger(__name__)

# Converts OGG/Opus voice bytes to a transcription-friendly WAV container.
FfmpegConverter = Callable[[bytes], Awaitable[bytes]]


@dataclass
class TelegramEventSource(EventSource):
    """Source for Telegram-originated events."""

    _namespace = "platform-telegram"
    user_id: str
    chat_id: str

    def __str__(self) -> str:
        return f"platform-telegram:{self.user_id}:{self.chat_id}"

    @classmethod
    def from_string(cls, s: str) -> "TelegramEventSource":
        _, user_id, chat_id = s.split(":")
        return cls(user_id=user_id, chat_id=chat_id)

    @property
    def platform_name(self) -> str:
        return "telegram"

class TelegramChannel(Channel[TelegramEventSource]):
    """Telegram platform implementation using python-telegram-bot."""

    platform_name = "telegram"

    def __init__(
        self,
        config: TelegramConfig,
        transcriber: AudioTranscriber | None = None,
        ffmpeg_convert: FfmpegConverter | None = None,
    ):
        """Initialize TelegramChannel.

        ``transcriber`` and ``ffmpeg_convert`` are injectable so voice handling
        can be unit-tested without the OpenAI SDK or the ffmpeg binary.
        """
        self.config: TelegramConfig = config
        self._transcriber: AudioTranscriber | None = transcriber
        self._ffmpeg_convert: FfmpegConverter = ffmpeg_convert or ogg_to_wav
        self.application: Application | None = None
        self._running_task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None

    def is_allowed(self, source: TelegramEventSource) -> bool:
        """Check if sender is whitelisted."""
        if not self.config.allowed_user_ids:
            return True
        return source.user_id in self.config.allowed_user_ids

    @staticmethod
    async def _download_voice_bytes(voice) -> bytes:
        """Download Telegram voice file bytes."""
        file = await voice.get_file()
        data = await file.download_as_bytearray()
        return bytes(data)

    async def _handle_text_update(
        self,
        update: Update,
        on_message: Callable[[str, "TelegramEventSource"], Awaitable[None]],
    ) -> None:
        """Extract a text message from an update and dispatch it unchanged."""
        if not (
            update.message
            and update.message.text
            and update.effective_chat
            and update.message.from_user
        ):
            return

        user_id: str = str(update.message.from_user.id)
        chat_id: str = str(update.effective_chat.id)
        message: str = update.message.text

        logger.info(f"Received Telegram message from user {user_id} in chat {chat_id}")

        source = TelegramEventSource(user_id=user_id, chat_id=chat_id)
        try:
            await on_message(message, source)
        except Exception as e:
            logger.error(f"Error in message callback: {e}")

    async def _handle_voice_update(
        self,
        update: Update,
        on_message: Callable[[str, "TelegramEventSource"], Awaitable[None]],
    ) -> None:
        """Extract a voice message, transcribe it, and dispatch the transcript."""
        if not (
            update.message
            and update.message.voice
            and update.effective_chat
            and update.message.from_user
        ):
            return

        user_id: str = str(update.message.from_user.id)
        chat_id: str = str(update.effective_chat.id)
        source = TelegramEventSource(user_id=user_id, chat_id=chat_id)

        # Allowlist BEFORE any download/transcription. The ChannelWorker also
        # checks is_allowed, but voice downloads happen before on_message, so we
        # must gate here too.
        if not self.is_allowed(source):
            logger.debug("Ignored non-whitelisted voice message")
            return

        logger.info(
            f"Received Telegram voice message from user {user_id} in chat {chat_id}"
        )

        voice = update.message.voice

        async def download() -> bytes:
            return await self._download_voice_bytes(voice)

        async def reply(text: str) -> None:
            await self.reply(text, source)

        transcript = await self._process_voice(
            duration=voice.duration,
            file_size=voice.file_size,
            download=download,
            reply=reply,
        )
        if transcript is None:
            return  # A guard already replied; do not enter the agent loop.

        try:
            await on_message(transcript, source)
        except Exception as e:
            logger.error(f"Error in voice message callback: {e}")

    async def _process_voice(
        self,
        *,
        duration: int,
        file_size: int | None,
        download: Callable[[], Awaitable[bytes]],
        reply: Callable[[str], Awaitable[None]],
    ) -> str | None:
        """Validate, download, convert, and transcribe a voice message.

        Returns the stripped transcript, or ``None`` when the message should not
        enter the agent loop (a short Telegram reply is sent for each rejection).
        Raw audio bytes never leave this method; only the transcript is returned.
        """
        voice_cfg = self.config.voice
        if not voice_cfg.enabled or self._transcriber is None:
            await reply("Voice messages aren't enabled.")
            return None

        # Duration and size guards run before download to protect cost/latency.
        if duration > voice_cfg.max_duration_seconds:
            await reply("That voice message is too long.")
            return None

        max_bytes = voice_cfg.max_file_size_mb * 1024 * 1024
        if file_size is not None and file_size > max_bytes:
            await reply("That voice message is too large.")
            return None

        audio = await download()

        try:
            wav = await self._ffmpeg_convert(audio)
        except (FfmpegError, FileNotFoundError) as e:
            logger.error("Voice conversion failed: %s", e)
            await reply(
                "I could not process that voice message. Please send it as text."
            )
            return None

        try:
            transcript = await self._transcriber.transcribe(
                wav, filename="voice.wav", content_type="audio/wav"
            )
        except Exception as e:
            # Log the provider error, never the transcript content.
            logger.error("Voice transcription failed: %s", e)
            await reply("Sorry, I couldn't process that voice message right now.")
            return None

        transcript = (transcript or "").strip()
        if not transcript:
            await reply("I couldn't hear anything in that voice message.")
            return None

        return transcript

    async def run(
        self, on_message: Callable[[str, TelegramEventSource], Awaitable[None]]
    ) -> None:
        """Run the Telegram channel. Blocks until stop() is called."""
        if self.application is not None: # @todo тут валится ошибка 
            raise RuntimeError("TelegramChannel already running")

        logger.info(f"Channel enabled with platform: {self.platform_name}")
        self.application = Application.builder().token(self.config.bot_token).build()
        self._stop_event = asyncio.Event()

        async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
            """Handle incoming Telegram text message."""
            await self._handle_text_update(update, on_message)

        async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
            """Handle incoming Telegram voice message."""
            await self._handle_voice_update(update, on_message)

        text_handler = MessageHandler(filters.TEXT, handle_message)
        self.application.add_handler(text_handler)
        voice_handler = MessageHandler(filters.VOICE, handle_voice)
        self.application.add_handler(voice_handler)

        # Start the bot
        await self.application.initialize()
        await self.application.start()
        if self.application.updater:
            await self.application.updater.start_polling()

        logger.info("TelegramChannel started")

        # Create the running task that monitors for stop
        async def run_until_stopped():
            """Run until stop() is called or updater stops unexpectedly."""
            while self.application and self.application.updater:
                if self.application.updater.running:
                    if self._stop_event and self._stop_event.is_set():
                        return # Graceful stop
                    await asyncio.sleep(1)
                else:
                    if self._stop_event and self._stop_event.is_set():
                        raise RuntimeError("Telegram updater stopped unexpectedly")
                    return

        self._running_task = asyncio.create_task(run_until_stopped())
        await self._running_task

    async def reply(self, content: str, source: TelegramEventSource) -> None:
        """Reply to incoming message."""
        if not self.application:
            raise RuntimeError("TelegramChannel not started")

        try:
            await self.application.bot.send_message(
                chat_id=int(source.chat_id), text=content
            )
            logger.debug(f"Sent Telegram reply to {source.chat_id}")
        except Exception as e:
            logger.error(f"Failed to send Telegram reply: {e}")
            raise

    async def stop(self) -> None:
        """Stop Telegram bot and cleanup."""
        # Idempotent: skip if not running
        if self.application is None:
            logger.debug("TelegramChannel not running, skipping stop")
            return

        # Signal the running task to stop
        if self._stop_event:
            self._stop_event.set()

        if self.application.updater and self.application.updater.running:
            await self.application.updater.stop()
        await self.application.stop()
        await self.application.shutdown()

        # Wait for running task to complete
        if self._running_task and not self._running_task.done():
            try:
                await asyncio.wait_for(self._running_task, timeout=2.0)
            except asyncio.TimeoutError:
                logger.warning("Running task did not complete in time")
            except Exception:
                pass  # Task may have already failed

        self.application = None
        self._running_task = None
        self._stop_event = None
        logger.info("TelegramChannel stopped")