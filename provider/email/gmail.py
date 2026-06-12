"""Gmail provider implementing the ``EmailProvider`` protocol.

Wraps the synchronous ``googleapiclient`` Gmail service behind async methods
(blocking calls run via ``asyncio.to_thread``). Google libraries are imported
lazily so importing this module never requires them; ``build_service`` already
raises ``AuthMissingError`` before any API call when auth/libraries are missing.

Gmail ``HttpError`` is normalized to provider exceptions (401/403 ->
``ProviderPermissionError``, 404 -> ``ProviderNotFoundError``); unknown failures
propagate and the tool layer maps them to a generic ``provider_error``. Only
normalized fields ever reach user-visible model text; raw payloads/tokens never
do.
"""

import asyncio
import base64
from email.mime.text import MIMEText
from typing import TYPE_CHECKING, Any

from provider.email.base import DraftResult, EmailMessage, EmailSummary
from provider.external_errors import ProviderNotFoundError, ProviderPermissionError

if TYPE_CHECKING:
    from utils.config import Config, ExternalProviderConfig


def _map_http_error(exc: Exception) -> Exception:
    """Map a Gmail ``HttpError`` to a provider exception, else return it as-is."""
    try:
        from googleapiclient.errors import HttpError
    except ImportError:  # pragma: no cover - libs absent means build_service raised
        return exc
    if isinstance(exc, HttpError):
        status = getattr(getattr(exc, "resp", None), "status", None)
        if status in (401, 403):
            return ProviderPermissionError("gmail denied the request")
        if status == 404:
            return ProviderNotFoundError("gmail resource not found")
    return exc


def _header(headers: list[dict[str, Any]], name: str) -> str | None:
    """Return the value of the first header matching ``name`` (case-insensitive)."""
    lname = name.lower()
    for h in headers:
        if h.get("name", "").lower() == lname:
            return h.get("value")
    return None


def _decode_body(payload: dict[str, Any]) -> str:
    """Walk the MIME tree and return the first decoded ``text/plain`` part."""
    mime_type = payload.get("mimeType", "")
    body = payload.get("body", {})
    data = body.get("data")
    if mime_type == "text/plain" and data:
        return _b64url_decode(data)
    for part in payload.get("parts", []) or []:
        text = _decode_body(part)
        if text:
            return text
    return ""


def _b64url_decode(data: str) -> str:
    """Decode a base64url-encoded Gmail body segment to text."""
    raw = base64.urlsafe_b64decode(data.encode("ascii"))
    return raw.decode("utf-8", errors="replace")


class GmailProvider:
    """Gmail-backed implementation of the ``EmailProvider`` protocol."""

    def __init__(
        self,
        config: "Config",
        provider_cfg: "ExternalProviderConfig",
        service: Any = None,
    ) -> None:
        self._config = config
        self._provider_cfg = provider_cfg
        self._service = service  # injected fake in tests

    def _svc(self) -> Any:
        """Build (lazily) and cache the authenticated Gmail service client."""
        if self._service is None:
            from provider.google_auth import build_service

            self._service = build_service(
                self._config, self._provider_cfg, api="gmail", version="v1"
            )
        return self._service

    async def search(self, query: str, limit: int) -> list[EmailSummary]:
        svc = self._svc()
        try:
            listing = await asyncio.to_thread(
                lambda: svc.users()
                .messages()
                .list(userId="me", q=query, maxResults=limit)
                .execute()
            )
        except Exception as e:  # noqa: BLE001 - mapped to stable error codes
            raise _map_http_error(e) from e

        ids = [m["id"] for m in (listing.get("messages") or [])][:limit]
        summaries: list[EmailSummary] = []
        for msg_id in ids:
            try:
                detail = await asyncio.to_thread(
                    lambda mid=msg_id: svc.users()
                    .messages()
                    .get(
                        userId="me",
                        id=mid,
                        format="metadata",
                        metadataHeaders=["Subject", "From", "Date"],
                    )
                    .execute()
                )
            except Exception as e:  # noqa: BLE001 - mapped to stable error codes
                raise _map_http_error(e) from e
            headers = detail.get("payload", {}).get("headers", [])
            summaries.append(
                EmailSummary(
                    id=detail.get("id", msg_id),
                    subject=_header(headers, "Subject") or "",
                    sender=_header(headers, "From") or "",
                    snippet=detail.get("snippet", ""),
                    date=_header(headers, "Date"),
                )
            )
        return summaries

    async def read(self, message_id: str) -> EmailMessage:
        svc = self._svc()
        try:
            detail = await asyncio.to_thread(
                lambda: svc.users()
                .messages()
                .get(userId="me", id=message_id, format="full")
                .execute()
            )
        except Exception as e:  # noqa: BLE001 - mapped to stable error codes
            raise _map_http_error(e) from e

        payload = detail.get("payload", {})
        headers = payload.get("headers", [])
        to_value = _header(headers, "To") or ""
        recipients = [r.strip() for r in to_value.split(",") if r.strip()]
        return EmailMessage(
            id=detail.get("id", message_id),
            subject=_header(headers, "Subject") or "",
            sender=_header(headers, "From") or "",
            recipients=recipients,
            body=_decode_body(payload),
            date=_header(headers, "Date"),
        )

    async def draft_reply(self, message_id: str, body: str) -> DraftResult:
        svc = self._svc()
        original = await self.read(message_id)
        raw, thread_id = await self._build_reply(svc, message_id, original, body)
        try:
            draft = await asyncio.to_thread(
                lambda: svc.users()
                .drafts()
                .create(
                    userId="me",
                    body={"message": {"raw": raw, "threadId": thread_id}},
                )
                .execute()
            )
        except Exception as e:  # noqa: BLE001 - mapped to stable error codes
            raise _map_http_error(e) from e
        return DraftResult(
            draft_id=draft.get("id", ""), message_id=message_id, body=body
        )

    async def send_reply(self, message_id: str, body: str) -> dict[str, Any]:
        svc = self._svc()
        original = await self.read(message_id)
        raw, thread_id = await self._build_reply(svc, message_id, original, body)
        try:
            sent = await asyncio.to_thread(
                lambda: svc.users()
                .messages()
                .send(userId="me", body={"raw": raw, "threadId": thread_id})
                .execute()
            )
        except Exception as e:  # noqa: BLE001 - mapped to stable error codes
            raise _map_http_error(e) from e
        return {"id": sent.get("id", "")}

    async def delete(self, message_id: str) -> None:
        svc = self._svc()
        try:
            await asyncio.to_thread(
                lambda: svc.users()
                .messages()
                .trash(userId="me", id=message_id)
                .execute()
            )
        except Exception as e:  # noqa: BLE001 - mapped to stable error codes
            raise _map_http_error(e) from e

    async def _build_reply(
        self,
        svc: Any,
        message_id: str,
        original: EmailMessage,
        body: str,
    ) -> tuple[str, str | None]:
        """Build a base64url MIME reply and resolve the source thread id."""
        try:
            meta = await asyncio.to_thread(
                lambda: svc.users()
                .messages()
                .get(
                    userId="me",
                    id=message_id,
                    format="metadata",
                    metadataHeaders=["Message-ID", "References"],
                )
                .execute()
            )
        except Exception as e:  # noqa: BLE001 - mapped to stable error codes
            raise _map_http_error(e) from e

        headers = meta.get("payload", {}).get("headers", [])
        in_reply_to = _header(headers, "Message-ID")
        references = _header(headers, "References")
        thread_id = meta.get("threadId")

        subject = original.subject or ""
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"

        mime = MIMEText(body, _charset="utf-8")
        mime["To"] = original.sender
        mime["Subject"] = subject
        if in_reply_to:
            mime["In-Reply-To"] = in_reply_to
            mime["References"] = (
                f"{references} {in_reply_to}".strip() if references else in_reply_to
            )

        raw = base64.urlsafe_b64encode(mime.as_bytes()).decode("ascii")
        return raw, thread_id
