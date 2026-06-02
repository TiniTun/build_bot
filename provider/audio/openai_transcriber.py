"""OpenAI speech-to-text transcriber.

Wraps the async OpenAI SDK's ``audio.transcriptions`` endpoint. The ``openai``
dependency is imported lazily inside ``__init__`` so the package stays optional
for deployments that do not enable voice messages.
"""

import logging

logger = logging.getLogger(__name__)


class OpenAITranscriber:
    """Transcribe audio via OpenAI's async transcription API."""

    def __init__(
        self,
        api_key: str,
        *,
        model: str = "gpt-4o-mini-transcribe",
        request_timeout_seconds: int = 60,
    ) -> None:
        # Lazy import keeps the openai SDK optional unless voice is enabled.
        from openai import AsyncOpenAI

        self._model = model
        self._client = AsyncOpenAI(api_key=api_key, timeout=request_timeout_seconds)

    async def transcribe(
        self,
        audio: bytes,
        *,
        filename: str,
        content_type: str | None = None,
    ) -> str:
        """Transcribe audio bytes to text. Returns a stripped transcript."""
        # The SDK accepts a (filename, bytes[, content_type]) tuple as a file,
        # so we never write a temp file. The extension lets OpenAI infer format.
        file_tuple: tuple = (
            (filename, audio, content_type)
            if content_type is not None
            else (filename, audio)
        )
        # Never log transcript content (it may contain private user data).
        logger.debug("Transcribing %d bytes via model %s", len(audio), self._model)
        response = await self._client.audio.transcriptions.create(
            model=self._model,
            file=file_tuple,
        )
        return (response.text or "").strip()
