"""Tests for the OAuth token store and its cross-process lock."""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from provider.mcp.errors import McpConfigError, McpTimeoutError
from provider.mcp.oauth_store import OAuthGrant, OAuthStore


def _grant(token: str = "access-1") -> OAuthGrant:
    return OAuthGrant(
        client_id="client-id",
        client_secret="client-secret",
        redirect_uri="http://127.0.0.1:8765/callback",
        token_endpoint="https://mcp.example.com/token",
        resource="https://mcp.example.com/mcp",
        access_token=token,
        refresh_token="refresh-1",
        expires_at=1_800_000_000.5,
        granted_at=1_700_000_000.25,
    )


class OAuthStoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = self.tmp / "secrets" / "notion.json"
        self.store = OAuthStore(self.path)

    def test_load_missing_returns_none(self) -> None:
        self.assertIsNone(self.store.load())
        self.assertIsNone(self.store.mtime())

    def test_save_then_load_roundtrip(self) -> None:
        grant = _grant()
        self.store.save(grant)
        self.assertEqual(self.store.load(), grant)
        self.assertIsNotNone(self.store.mtime())

    def test_roundtrip_without_client_secret(self) -> None:
        grant = _grant()
        grant.client_secret = None
        self.store.save(grant)
        self.assertEqual(self.store.load(), grant)

    def test_save_permissions(self) -> None:
        self.store.save(_grant())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)

    def test_save_is_atomic(self) -> None:
        self.store.save(_grant("original"))
        before = self.path.read_text()
        with mock.patch("os.replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                self.store.save(_grant("replacement"))
        self.assertEqual(self.path.read_text(), before)
        leftovers = [p for p in self.path.parent.iterdir() if p != self.path]
        self.assertEqual(leftovers, [])

    def test_malformed_raises_without_contents(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text('{"access_token": "SECRET"')
        with self.assertRaises(McpConfigError) as ctx:
            self.store.load()
        self.assertNotIn("SECRET", str(ctx.exception))

    def test_wrong_shape_raises_without_contents(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"access_token": "SECRET"}))
        with self.assertRaises(McpConfigError) as ctx:
            self.store.load()
        self.assertNotIn("SECRET", str(ctx.exception))

    async def test_lock_excludes_second_holder(self) -> None:
        async with self.store.locked():
            with self.assertRaises(McpTimeoutError):
                async with self.store.locked(timeout=0.3):
                    self.fail("second holder must not acquire the lock")
        async with self.store.locked(timeout=0.3):
            pass

    async def test_lock_released_on_exception(self) -> None:
        with self.assertRaises(RuntimeError):
            async with self.store.locked():
                raise RuntimeError("body failed")
        async with self.store.locked(timeout=0.3):
            pass
        self.assertTrue(os.path.exists(f"{self.path}.lock"))


if __name__ == "__main__":
    unittest.main()
