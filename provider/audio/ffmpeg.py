"""ffmpeg-based audio conversion helpers.

Telegram voice messages arrive as OGG/Opus; OpenAI transcription wants a
supported container such as WAV. This module shells out to the ``ffmpeg`` binary
(must be on PATH) via asyncio subprocess, streaming bytes through stdin/stdout so
no temporary files touch the workspace.
"""

import asyncio
import logging

logger = logging.getLogger(__name__)


class FfmpegError(RuntimeError):
    """Raised when ffmpeg is missing or exits non-zero."""


async def ogg_to_wav(audio: bytes) -> bytes:
    """Convert OGG/Opus ``audio`` bytes to WAV bytes using ffmpeg.

    Reads input from stdin (``pipe:0``) and writes WAV to stdout (``pipe:1``)
    so nothing is written to disk.

    Raises:
        FfmpegError: if the ``ffmpeg`` binary is not found on PATH, the process
            exits non-zero, or it produces no output.
    """
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-i", "pipe:0",  # read input from stdin
        "-f", "wav",  # force WAV container on stdout
        "pipe:1",  # write output to stdout
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        logger.error("ffmpeg binary not found on PATH")
        raise FfmpegError("ffmpeg binary not found on PATH") from exc

    stdout, stderr = await proc.communicate(input=audio)

    if proc.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()
        logger.error("ffmpeg failed (exit %s): %s", proc.returncode, detail)
        raise FfmpegError(f"ffmpeg conversion failed (exit {proc.returncode})")

    if not stdout:
        logger.error("ffmpeg produced no output")
        raise FfmpegError("ffmpeg produced no output")

    return stdout
