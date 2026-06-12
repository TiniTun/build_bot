"""Tests for the Gmail email provider and confirmation-gated send/delete.

All tests are hermetic: no network or Google auth happens. A fake Gmail service
is injected via ``GmailProvider(config, provider_cfg, service=FakeService())``,
or ``tools.email_tools.get_email_provider`` is monkeypatched to return a
fake-backed provider.
"""

import base64
import json
import tempfile
import unittest
from email.mime.text import MIMEText
from pathlib import Path
from types import SimpleNamespace

from tests.helpers import make_context, make_workspace
from tools.base import ToolErrorCode
from tools.email_tools import (
    build_email_capabilities,
    build_email_confirmed_executors,
)
from provider.email.gmail import GmailProvider
from utils.config import (
    Config,
    ExternalProviderConfig,
    ExternalToolsConfig,
)


def _b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")


class _Executor:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class _Messages:
    def __init__(self, service):
        self._s = service

    def list(self, *, userId, q, maxResults):
        self._s.calls.append(("list", q, maxResults))
        return _Executor({"messages": [{"id": "m1"}, {"id": "m2"}]})

    def get(self, *, userId, id, format, metadataHeaders=None):
        self._s.calls.append(("get", id, format))
        return _Executor(self._s.details[id])

    def send(self, *, userId, body):
        self._s.calls.append(("send", body))
        return _Executor({"id": "sent-1"})

    def trash(self, *, userId, id):
        self._s.calls.append(("trash", id))
        return _Executor({"id": id, "labelIds": ["TRASH"]})


class _Drafts:
    def __init__(self, service):
        self._s = service

    def create(self, *, userId, body):
        self._s.calls.append(("draft_create", body))
        return _Executor({"id": "draft-1"})


class _Users:
    def __init__(self, service):
        self._messages = _Messages(service)
        self._drafts = _Drafts(service)

    def messages(self):
        return self._messages

    def drafts(self):
        return self._drafts


class FakeService:
    """In-memory stand-in for the googleapiclient Gmail service."""

    def __init__(self, details=None):
        self.calls: list = []
        self.details = details or {}
        self._users = _Users(self)

    def users(self):
        return self._users


def _metadata_detail(msg_id, subject, sender, snippet, date):
    return {
        "id": msg_id,
        "snippet": snippet,
        "threadId": f"t-{msg_id}",
        "payload": {
            "headers": [
                {"name": "Subject", "value": subject},
                {"name": "From", "value": sender},
                {"name": "Date", "value": date},
                {"name": "Message-ID", "value": f"<{msg_id}@mail>"},
            ]
        },
    }


def _full_detail(msg_id, subject, sender, to, body):
    return {
        "id": msg_id,
        "snippet": body[:20],
        "threadId": f"t-{msg_id}",
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [
                {"name": "Subject", "value": subject},
                {"name": "From", "value": sender},
                {"name": "To", "value": to},
                {"name": "Date", "value": "Mon, 01 Jun 2026 10:00:00 +0000"},
            ],
            "parts": [
                {
                    "mimeType": "text/plain",
                    "body": {"data": _b64url(body)},
                }
            ],
        },
    }


def _gmail_config(workspace, *, credentials=False):
    config = Config.load(workspace)
    provider_cfg = ExternalProviderConfig(
        enabled=True,
        provider="gmail",
        credentials_path="creds.json" if credentials else None,
        token_path="token.json" if credentials else None,
        scopes=["https://www.googleapis.com/auth/gmail.modify"] if credentials else [],
    )
    config.external_tools = ExternalToolsConfig(email=provider_cfg)
    return config


def _session(context):
    return SimpleNamespace(shared_context=context)


def _tool(pairs, cap_id):
    return next(t for cap, t in pairs if cap.id == cap_id)


class GmailAuthMissingTests(unittest.IsolatedAsyncioTestCase):
    async def test_enabled_gmail_without_credentials_returns_auth_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            config = _gmail_config(workspace, credentials=False)
            search = _tool(build_email_capabilities(config), "email.search")
            raw = await search.execute(session=object(), query="hello")
            payload = json.loads(raw)
            self.assertFalse(payload["ok"])
            self.assertEqual(
                payload["error"]["code"], ToolErrorCode.AUTH_MISSING.value
            )


class GmailProviderReadTests(unittest.IsolatedAsyncioTestCase):
    def _provider(self, workspace, service):
        config = _gmail_config(workspace, credentials=True)
        return GmailProvider(config, config.external_tools.email, service=service)

    async def test_search_maps_messages_to_summaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            service = FakeService(
                details={
                    "m1": _metadata_detail(
                        "m1", "Hello", "a@x.com", "snip one", "date1"
                    ),
                    "m2": _metadata_detail(
                        "m2", "World", "b@x.com", "snip two", "date2"
                    ),
                }
            )
            provider = self._provider(workspace, service)
            results = await provider.search("inbox", 10)
            self.assertEqual([r.id for r in results], ["m1", "m2"])
            self.assertEqual(results[0].subject, "Hello")
            self.assertEqual(results[0].sender, "a@x.com")
            self.assertEqual(results[0].snippet, "snip one")
            self.assertEqual(results[0].date, "date1")

    async def test_read_decodes_body_and_headers(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            service = FakeService(
                details={
                    "m1": _full_detail(
                        "m1",
                        "Subj",
                        "sender@x.com",
                        "me@x.com, other@x.com",
                        "Hello body text",
                    )
                }
            )
            provider = self._provider(workspace, service)
            msg = await provider.read("m1")
            self.assertEqual(msg.subject, "Subj")
            self.assertEqual(msg.sender, "sender@x.com")
            self.assertEqual(msg.recipients, ["me@x.com", "other@x.com"])
            self.assertEqual(msg.body, "Hello body text")

    async def test_draft_reply_creates_draft_and_does_not_send(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            service = FakeService(
                details={
                    "m1": _full_detail(
                        "m1", "Subj", "sender@x.com", "me@x.com", "Body"
                    )
                }
            )
            provider = self._provider(workspace, service)
            result = await provider.draft_reply("m1", "My reply")
            self.assertEqual(result.draft_id, "draft-1")
            kinds = [c[0] for c in service.calls]
            self.assertIn("draft_create", kinds)
            self.assertNotIn("send", kinds)


class EmailSendConfirmTests(unittest.IsolatedAsyncioTestCase):
    async def test_email_send_pending_first_then_executes_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            context = make_context(workspace)
            service = FakeService(
                details={
                    "m1": _full_detail(
                        "m1", "Subj", "sender@x.com", "me@x.com", "Body"
                    )
                }
            )
            config = _gmail_config(workspace, credentials=True)
            context.config.external_tools = config.external_tools
            provider = GmailProvider(
                context.config, config.external_tools.email, service=service
            )

            # Proposal tool must not send; it records a pending action.
            send_tool = _tool(
                build_email_capabilities(context.config), "email.send"
            )
            raw = await send_tool.execute(
                session=_session(context), message_id="m1", body="hi"
            )
            payload = json.loads(raw)
            self.assertTrue(payload["requires_confirmation"])
            self.assertNotIn("send", [c[0] for c in service.calls])

            # Confirmed executor performs the real send exactly once.
            import tools.email_tools as email_tools

            orig = email_tools.get_email_provider
            email_tools.get_email_provider = lambda cfg: provider
            try:
                executors = build_email_confirmed_executors(context.config)
            finally:
                email_tools.get_email_provider = orig

            stored = payload["action"]["payload"]
            out = await executors["email.send"](_session(context), stored)
            self.assertIn("Reply sent", out)
            self.assertEqual(
                [c[0] for c in service.calls].count("send"), 1
            )

    async def test_email_delete_pending_first_then_trashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            context = make_context(workspace)
            service = FakeService(details={})
            config = _gmail_config(workspace, credentials=True)
            context.config.external_tools = config.external_tools
            provider = GmailProvider(
                context.config, config.external_tools.email, service=service
            )

            delete_tool = _tool(
                build_email_capabilities(context.config), "email.delete"
            )
            raw = await delete_tool.execute(
                session=_session(context), message_id="m1"
            )
            payload = json.loads(raw)
            self.assertTrue(payload["requires_confirmation"])
            self.assertNotIn("trash", [c[0] for c in service.calls])

            import tools.email_tools as email_tools

            orig = email_tools.get_email_provider
            email_tools.get_email_provider = lambda cfg: provider
            try:
                executors = build_email_confirmed_executors(context.config)
            finally:
                email_tools.get_email_provider = orig

            out = await executors["email.delete"](
                _session(context), payload["action"]["payload"]
            )
            self.assertIn("trash", out.lower())
            self.assertEqual(
                [c[0] for c in service.calls].count("trash"), 1
            )


class _ErrorMessages(_Messages):
    def __init__(self, service, status):
        super().__init__(service)
        self._status = status

    def list(self, *, userId, q, maxResults):
        from googleapiclient.errors import HttpError

        resp = SimpleNamespace(status=self._status, reason="err")
        raise HttpError(resp, b"{}")


class _ErrorUsers(_Users):
    def __init__(self, service, status):
        super().__init__(service)
        self._messages = _ErrorMessages(service, status)


class _ErrorService(FakeService):
    def __init__(self, status):
        super().__init__()
        self._users = _ErrorUsers(self, status)


class GmailErrorMappingTests(unittest.IsolatedAsyncioTestCase):
    def _search_tool(self, workspace, service):
        config = _gmail_config(workspace, credentials=True)
        provider = GmailProvider(config, config.external_tools.email, service=service)
        import tools.email_tools as email_tools

        orig = email_tools.get_email_provider
        email_tools.get_email_provider = lambda cfg: provider
        try:
            tool = _tool(build_email_capabilities(config), "email.search")
        finally:
            email_tools.get_email_provider = orig
        return tool

    async def test_permission_denied_maps_to_stable_error(self):
        try:
            import googleapiclient.errors  # noqa: F401
        except ImportError:
            self.skipTest("googleapiclient not installed")
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            tool = self._search_tool(workspace, _ErrorService(403))
            raw = await tool.execute(session=object(), query="x")
            payload = json.loads(raw)
            self.assertEqual(
                payload["error"]["code"], ToolErrorCode.PERMISSION_DENIED.value
            )

    async def test_not_found_maps_to_stable_error(self):
        try:
            import googleapiclient.errors  # noqa: F401
        except ImportError:
            self.skipTest("googleapiclient not installed")
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            tool = self._search_tool(workspace, _ErrorService(404))
            raw = await tool.execute(session=object(), query="x")
            payload = json.loads(raw)
            self.assertEqual(
                payload["error"]["code"], ToolErrorCode.NOT_FOUND.value
            )


if __name__ == "__main__":
    unittest.main()
