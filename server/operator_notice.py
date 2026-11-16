"""Operator notices sent straight to the default delivery source.

These are system messages to the bot's operator (for example "log in to Notion
again"), not replies in a session, so they bypass the event bus and session
routing and go directly to the platform channel of
``config.default_delivery_source``.
"""

import asyncio
import logging
from typing import TYPE_CHECKING, Awaitable, Callable

from provider.mcp.hub import NOTICE_LOGIN_REQUIRED, NOTICE_REFRESH_CAP_SOON, NoticeCallback
from .delivery_worker import find_channel, resolve_default_delivery_source

if TYPE_CHECKING:
    from core.context import SharedContext

logger = logging.getLogger(__name__)

# A notice raised during startup can arrive before the Telegram channel is up.
NOTICE_ATTEMPTS = 6
NOTICE_RETRY_SECONDS = 10.0


async def send_operator_notice(context: "SharedContext", text: str) -> bool:
    """Send ``text`` to the default delivery source. Returns False (logged) if it cannot."""
    source = resolve_default_delivery_source(context.config.default_delivery_source)
    if source is None or source.platform_name is None:
        logger.warning("Operator notice not sent: no usable default_delivery_source")
        return False
    channel = find_channel(context, source.platform_name)
    if channel is None:
        logger.warning(f"Operator notice not sent: no channel for platform {source.platform_name}")
        return False
    try:
        await channel.reply(text, source)
    except Exception as e:
        logger.warning(f"Operator notice to {source.platform_name} failed: {e}")
        return False
    return True


def mcp_notice_text(server_id: str, kind: str) -> str:
    """Russian operator text for an MCP hub notice kind (OAuth servers are Notion today)."""
    command = f"На bakeryd: build-bot mcp-login {server_id}"
    if kind == NOTICE_LOGIN_REQUIRED:
        return f"Notion отключён: нужен повторный вход. {command}"
    if kind == NOTICE_REFRESH_CAP_SOON:
        return f"Доступ Notion истечёт через ~14 дней. {command}"
    raise ValueError(f"unknown MCP notice kind: {kind}")


def make_mcp_notice_callback(
    context: "SharedContext",
    *,
    attempts: int = NOTICE_ATTEMPTS,
    retry_seconds: float = NOTICE_RETRY_SECONDS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> NoticeCallback:
    """Build the hub's notice callback for server mode."""

    async def notify(server_id: str, kind: str) -> None:
        text = mcp_notice_text(server_id, kind)
        for attempt in range(1, attempts + 1):
            if await send_operator_notice(context, text):
                return
            if attempt < attempts:
                await sleep(retry_seconds)
        logger.error(f"MCP notice {kind} for {server_id} dropped after {attempts} attempts")

    return notify
