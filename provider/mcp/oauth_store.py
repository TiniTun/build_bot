"""Persistent OAuth grant storage for a hosted MCP server.

One JSON file holds the client registration and the current token pair. Writes
are atomic (temp file + ``os.replace``) and owner-only. A sibling ``.lock`` file
serializes refreshes across processes (CLI login, server workers) using a
non-blocking ``flock`` polled from the event loop, so the loop never stalls.

The file's contents are secrets: they never appear in an exception message.
"""

import asyncio
import fcntl
import json
import os
import tempfile
import time
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import AsyncIterator

from provider.mcp.errors import McpConfigError, McpTimeoutError

_LOCK_POLL_SECONDS = 0.1


@dataclass
class OAuthGrant:
    """Client registration plus the current token pair. Times are epoch seconds."""

    client_id: str
    client_secret: str | None
    redirect_uri: str
    token_endpoint: str
    resource: str
    access_token: str
    refresh_token: str
    expires_at: float
    granted_at: float


class OAuthStore:
    """Reads and writes one ``OAuthGrant`` file with a cross-process lock."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock_path = path.with_name(path.name + ".lock")

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> OAuthGrant | None:
        """Return the stored grant, or None if no file exists."""
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise McpConfigError(f"cannot read OAuth store {self._path}: {type(exc).__name__}") from None
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("not an object")
            return OAuthGrant(**{f.name: data[f.name] for f in fields(OAuthGrant)})
        except (ValueError, KeyError, TypeError):
            # Deliberately drop the cause: it may quote file contents.
            raise McpConfigError(f"OAuth store {self._path} is malformed; run the login command again") from None

    def save(self, grant: OAuthGrant) -> None:
        """Atomically write the grant with mode 0600 (parent directory 0700)."""
        parent = self._path.parent
        parent.mkdir(parents=True, exist_ok=True)
        os.chmod(parent, 0o700)
        payload = json.dumps(asdict(grant), indent=2)
        tmp = tempfile.NamedTemporaryFile(
            "w", dir=parent, prefix=f".{self._path.name}.", delete=False, encoding="utf-8"
        )
        try:
            with tmp:
                os.chmod(tmp.name, 0o600)
                tmp.write(payload)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp.name, self._path)
        except BaseException:
            Path(tmp.name).unlink(missing_ok=True)
            raise

    def mtime(self) -> float | None:
        """Return the file's modification time, or None if it does not exist."""
        try:
            return self._path.stat().st_mtime
        except FileNotFoundError:
            return None

    @asynccontextmanager
    async def locked(self, timeout: float = 30.0) -> AsyncIterator[None]:
        """Hold the exclusive cross-process lock, or raise ``McpTimeoutError``."""
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise McpTimeoutError(f"timed out waiting for OAuth lock {self._lock_path}") from None
                    await asyncio.sleep(_LOCK_POLL_SECONDS)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
