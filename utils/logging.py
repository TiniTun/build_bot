"""Logging configuration for mybot."""

import logging
import sys

import litellm
from utils.config import Config
from logging.handlers import RotatingFileHandler


def _secret_scrub_filter() -> logging.Filter:
    """Return a handler filter that scrubs every secret the MCP layer registered.

    Imported lazily: ``provider.mcp.client`` imports ``utils``, so a module-level
    import here would be circular.
    """
    from provider.mcp.client import make_secret_scrub_filter

    return make_secret_scrub_filter()


def setup_logging(config: Config, console_output: bool = False) -> None:
    """Set up logging for pickle-bot."""
    format_str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    formatter = logging.Formatter(format_str)

    # Console format is simpler (no timestamp)
    console_format = "%(levelname)s - %(name)s - %(message)s"
    console_formatter = logging.Formatter(console_format)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)                                                     
    logging.getLogger("litellm").setLevel(logging.ERROR)
    logging.getLogger("LiteLLM").setLevel(logging.ERROR)
    litellm.set_verbose = False

    logging.getLogger("telegram").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    logging.getLogger("watchdog").setLevel(logging.WARNING)

    config.logging_path.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        config.logging_path / "built-bot.log", maxBytes=256 * 1024 * 128, backupCount=3
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.DEBUG)
    # A filter on a logger misses records propagated from its child loggers (the
    # MCP SDK logs from ``mcp.client.auth.oauth2``), so each handler scrubs known
    # secrets for every logger.
    file_handler.addFilter(_secret_scrub_filter())
    root_logger.addHandler(file_handler)

    # Optionally log to console (for server mode)
    if console_output:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(console_formatter)
        console_handler.setLevel(logging.INFO)
        console_handler.addFilter(_secret_scrub_filter())
        root_logger.addHandler(console_handler)