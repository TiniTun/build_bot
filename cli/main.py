"""CLI interface for bot using Typer."""

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from cli.chat import chat_command
from cli.server import server_command
from core.skill_loader import SkillLoader
from utils.config import Config

app = typer.Typer(
    name="build-bot",
    help="build-bot: Personal AI Assistant",
    no_args_is_help=True,
    add_completion=True,
)

console = Console()


def workspace_callback(ctx: typer.Context, workspace: str) -> Path:
    """Store workspace path in context for later use."""
    ctx.ensure_object(dict)
    ctx.obj["workspace"] = Path(workspace)
    return Path(workspace)


@app.callback()
def main(
    ctx: typer.Context,
    workspace: str = typer.Option(
        "./default_workspace",
        "--workspace",
        "-w",
        help="Path to workspace directory",
        callback=workspace_callback,
    )
) -> None:
    """Configuration is loaded from workspace/config.user.yaml by default."""
    workspace_path = ctx.obj["workspace"]
    config_file = workspace_path / "config.user.yaml"

    if not config_file.exists():
        console.print(f"[yellow]No configuration found at {config_file}[/yellow]")
        raise typer.Exit(1)
    
    try:
        cfg = Config.load(workspace_path)
        ctx.obj["config"] = cfg
    except Exception as e:
        console.print(f"[red]Error loading config: {e}[/red]")
        raise typer.Exit(1)
    

@app.command("chat")
def chat(
    ctx: typer.Context,
    agent: Annotated[
        str | None,
        typer.Option(
            "--agent",
            "-a",
            help="Agent ID to use (overrides default_agent from config)",
        )
    ] = None,
) -> None:
    """Start interactive chat session."""
    chat_command(ctx, agent_id=agent)


@app.command("server")
def server(ctx: typer.Context) -> None:
    """Start the 24/7 server for cron and messagebus execution."""
    server_command(ctx)


@app.command("migrate-memory")
def migrate_memory(ctx: typer.Context) -> None:
    """Create canonical memory files and copy old profile data forward.

    Non-destructive: old files (topics/, daily-notes/) are left in place.
    """
    from core.memory_store import MemoryStore

    cfg: Config = ctx.obj["config"]
    actions = MemoryStore(cfg).migrate()

    if not actions:
        console.print("[green]Memory layout already up to date.[/green]")
        return
    for action in actions:
        console.print(f"[green]•[/green] {action}")
    console.print(f"\n[green]Migration complete:[/green] {len(actions)} action(s).")


@app.command("validate-skills")
def validate_skills(ctx: typer.Context) -> None:
    """Validate every SKILL.md in the workspace against the Skills v2 contract."""
    cfg: Config = ctx.obj["config"]
    results = SkillLoader.from_config(cfg).validate_skills()

    if not results:
        console.print("[yellow]No skills found.[/yellow]")
        return

    has_errors = False
    for result in results:
        if result.ok:
            console.print(f"[green]✓[/green] {result.id}")
            continue

        has_errors = True
        console.print(f"[red]✗[/red] {result.id}")
        for error in result.errors:
            console.print(f"    - {error}")

    if has_errors:
        raise typer.Exit(1)
    console.print("\n[green]All skills valid.[/green]")


if __name__ == "__main__":
    app()