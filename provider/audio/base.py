"""Audio transcription provider boundary.

Defines the ``AudioTranscriber`` protocol used to turn raw audio bytes into
text. Implementations (e.g. OpenAI) live alongside this module. The rest of the
runtime only ever sees the resulting transcript string, never audio bytes.
"""

from typing import Protocol, runtime_checkable


@runtime_checkable
class AudioTranscriber(Protocol):
    """Speech-to-text over raw audio bytes."""

    async def transcribe(
        self,
        audio: bytes,
        *,
        filename: str,
        content_type: str | None = None,
    ) -> str:
        """Transcribe ``audio`` and return the recognized text.

        ``filename`` carries an extension the provider can use to infer format
        (e.g. ``voice.wav``). ``content_type`` is an optional MIME hint.
        """
        ...
