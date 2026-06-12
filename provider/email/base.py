"""Email provider protocol, data models, and a null provider.

Real provider integration (e.g. Gmail) is intentionally out of scope for this
iteration. ``get_email_provider`` returns a ``NullEmailProvider`` that raises
``AuthMissingError`` until a real client is wired in, so tools surface a stable
``AUTH_MISSING`` error instead of failing opaquely.
"""

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from provider.external_errors import AuthMissingError

if TYPE_CHECKING:
    from utils.config import Config


class EmailSummary(BaseModel):
    """Lightweight email metadata returned by search."""

    id: str
    subject: str
    sender: str
    snippet: str = ""
    date: str | None = None


class EmailMessage(BaseModel):
    """Full email message returned by read."""

    id: str
    subject: str
    sender: str
    recipients: list[str] = Field(default_factory=list)
    body: str = ""
    date: str | None = None


class DraftResult(BaseModel):
    """Result of creating a draft reply (not sent)."""

    draft_id: str
    message_id: str
    body: str


@runtime_checkable
class EmailProvider(Protocol):
    """Read/draft email operations plus confirmation-gated send/delete.

    ``send_reply`` and ``delete`` mutate the mailbox and are only reached through
    the confirmed-execution path; the proposal tools never call them directly.
    """

    async def search(self, query: str, limit: int) -> list[EmailSummary]: ...

    async def read(self, message_id: str) -> EmailMessage: ...

    async def draft_reply(self, message_id: str, body: str) -> DraftResult: ...

    async def send_reply(self, message_id: str, body: str) -> dict[str, Any]: ...

    async def delete(self, message_id: str) -> None: ...


class NullEmailProvider:
    """Placeholder provider that reports missing authentication."""

    async def search(self, query: str, limit: int) -> list[EmailSummary]:
        raise AuthMissingError("email provider is not configured")

    async def read(self, message_id: str) -> EmailMessage:
        raise AuthMissingError("email provider is not configured")

    async def draft_reply(self, message_id: str, body: str) -> DraftResult:
        raise AuthMissingError("email provider is not configured")

    async def send_reply(self, message_id: str, body: str) -> dict[str, Any]:
        raise AuthMissingError("email provider is not configured")

    async def delete(self, message_id: str) -> None:
        raise AuthMissingError("email provider is not configured")


def get_email_provider(config: "Config") -> EmailProvider:
    """Return the configured email provider, or a null provider if unavailable."""
    external = config.external_tools.email
    if not external.enabled:
        return NullEmailProvider()
    if external.provider == "gmail":
        from provider.email.gmail import GmailProvider

        return GmailProvider(config, external)
    # Unknown/unspecified provider: behave as auth-missing rather than raising.
    return NullEmailProvider()