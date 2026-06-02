"""Tests for the ffmpeg OGG/Opus -> WAV conversion helper."""

import unittest
from unittest.mock import patch

from provider.audio.ffmpeg import FfmpegError, ogg_to_wav


class FakeProc:
    """Stand-in for an asyncio subprocess."""

    def __init__(self, *, stdout: bytes = b"WAVDATA", stderr: bytes = b"", returncode: int = 0):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode

    async def communicate(self, input: bytes | None = None):
        return self._stdout, self._stderr


class FfmpegTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_returns_wav_bytes(self):
        async def fake_exec(*args, **kwargs):
            return FakeProc(stdout=b"WAVDATA")

        with patch(
            "provider.audio.ffmpeg.asyncio.create_subprocess_exec", new=fake_exec
        ):
            out = await ogg_to_wav(b"OGGDATA")

        self.assertEqual(out, b"WAVDATA")

    async def test_nonzero_exit_raises_ffmpeg_error(self):
        async def fake_exec(*args, **kwargs):
            return FakeProc(returncode=1, stderr=b"bad input")

        with patch(
            "provider.audio.ffmpeg.asyncio.create_subprocess_exec", new=fake_exec
        ):
            with self.assertRaises(FfmpegError):
                await ogg_to_wav(b"OGGDATA")

    async def test_empty_output_raises_ffmpeg_error(self):
        async def fake_exec(*args, **kwargs):
            return FakeProc(stdout=b"", returncode=0)

        with patch(
            "provider.audio.ffmpeg.asyncio.create_subprocess_exec", new=fake_exec
        ):
            with self.assertRaises(FfmpegError):
                await ogg_to_wav(b"OGGDATA")

    async def test_missing_binary_raises_ffmpeg_error(self):
        async def fake_exec(*args, **kwargs):
            raise FileNotFoundError("ffmpeg")

        with patch(
            "provider.audio.ffmpeg.asyncio.create_subprocess_exec", new=fake_exec
        ):
            with self.assertRaises(FfmpegError):
                await ogg_to_wav(b"OGGDATA")


if __name__ == "__main__":
    unittest.main()
