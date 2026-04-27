"""Server CLI command for worker-based architecture."""

import asyncio
import logging
import typer

from core.context import SharedContext
from server.server import Server
from utils.logging import setup_logging

logger = logging.getLogger(name=__name__)

def server_command(ctx: typer.Context) -> None:
    """Start the 24/7 server for cron and messagebus execution."""
    config = ctx.obj.get("config")

    setup_logging(config, console_output=True)
    logger.info("Logger start...")

    typer.echo("Starting mybot server...")
    typer.echo("Press Ctrl+C to stop")

    try:
        context = SharedContext(config)
        asyncio.run(Server(context).run())
    except KeyboardInterrupt:
        typer.echo("\nServer stopped")