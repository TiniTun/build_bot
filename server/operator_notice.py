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
from core.events import EventSource

from .delivery_worker import find_channel, resolve_default_delivery_source

if TYPE_CHECKING:
    from core.context import SharedContext

logger = logging.getLogger(__name__)

# A notice raised during startup can arrive before the Telegram channel is up, and
# bakeryd's Telegram path can be down for a while: retry 10 s doubling to 10 min
# for at least an hour. The hub re-sends an undelivered notice later.
NOTICE_RETRY_INITIAL_SECONDS = 10.0
NOTICE_RETRY_MAX_SECONDS = 600.0
NOTICE_RETRY_TOTAL_SECONDS = 3600.0


def _notice_source(context: "SharedContext") -> EventSource | None:
    """The platform source operator notices go to, or None if none is configured."""
    source = resolve_default_delivery_source(context.config.default_delivery_source)
    if source is None or source.platform_name is None:
        return None
    return source


async def send_operator_notice(context: "SharedContext", text: str) -> bool:
    """Send ``text`` to the default delivery source. Returns False (logged) if it cannot."""
    source = _notice_source(context)
    if source is None:
        logger.warning("Operator notice not sent: no usable default_delivery_source")
        return False
    assert source.platform_name is not None
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
    command = f"На bakeryd: build-bot --workspace ./default_workspace mcp-login {server_id}"
    if kind == NOTICE_LOGIN_REQUIRED:
        return f"Notion отключён: нужен повторный вход. {command}"
    if kind == NOTICE_REFRESH_CAP_SOON:
        return f"Доступ Notion истечёт через ~14 дней. {command}"
    raise ValueError(f"unknown MCP notice kind: {kind}")


def make_mcp_notice_callback(
    context: "SharedContext",
    *,
    retry_initial_seconds: float = NOTICE_RETRY_INITIAL_SECONDS,
    retry_max_seconds: float = NOTICE_RETRY_MAX_SECONDS,
    retry_total_seconds: float = NOTICE_RETRY_TOTAL_SECONDS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> NoticeCallback:
    """Build the hub's notice callback for server mode.

    Returns True once delivered. Without a usable ``default_delivery_source`` it
    returns False at once (a config problem, not a transient one) and logs that
    only once per process.
    """
    warned_no_source = False

    async def notify(server_id: str, kind: str) -> bool:
        nonlocal warned_no_source
        if _notice_source(context) is None:
            if not warned_no_source:
                warned_no_source = True
                logger.error(f"MCP notice {kind} for {server_id} not sent: no usable default_delivery_source")
            return False
        text = mcp_notice_text(server_id, kind)
        waited = 0.0
        delay = retry_initial_seconds
        while True:
            if await send_operator_notice(context, text):
                return True
            if waited >= retry_total_seconds:
                break
            await sleep(delay)
            waited += delay
            delay = min(delay * 2, retry_max_seconds)
        logger.error(f"MCP notice {kind} for {server_id} not delivered after {waited:.0f}s of retries")
        return False

    return notify
