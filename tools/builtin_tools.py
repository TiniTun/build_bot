"""Built-in tools for agent capabilities."""

import asyncio
import re
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import yaml

from core.cron_loader import CronDef
from tools.base import tool

if TYPE_CHECKING:
    from core.agent import AgentSession


# Filesystem tools


@tool(
    name="read",
    description="Read the contents of a text file",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file to read"},
        },
        "required": ["path"],
    },
)
async def read_file(path: str, session: "AgentSession") -> str:
    """Read and return the contents of a file at the given path."""
    try:
        return Path(path).read_text()
    except FileNotFoundError:
        return f"Error: File not found: {path}"
    except PermissionError:
        return f"Error: Permission denied reading: {path}"
    except IsADirectoryError:
        return f"Error: Path is a directory, not a file: {path}"
    except Exception as e:
        return f"Error reading file: {e}"
    
@tool(
    name="write",
    description="Write content to a file",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file to write"},
            "content": {
                "type": "string",
                "description": "Content to write to the file",
            },
        },
        "required": ["path", "content"],
    },
)
async def write_file(path: str, content: str, session: "AgentSession") -> str:
    """Write content to a file at the given path."""
    try:
        Path(path).write_text(content)
        return f"Successfully wrote to: {path}"
    except PermissionError:
        return f"Error: Permission denied writing to: {path}"
    except IsADirectoryError:
        return f"Error: Path is a directory, not a file: {path}"
    except Exception as e:
        return f"Error writing file: {e}"
    
@tool(
    name="edit",
    description="Edit a file by replacing a string with new content",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file to edit"},
            "old_text": {"type": "string", "description": "The text to replace"},
            "new_text": {
                "type": "string",
                "description": "The new text to replace with",
            },
        },
        "required": ["path", "old_text", "new_text"],
    },
)
async def edit_file(
    path: str, old_text: str, new_text: str, session: "AgentSession"
) -> str:
    """Edit a file by replacing old_text with new_text."""
    try:
        content = Path(path).read_text()
        if old_text not in content:
            return f"Error: '{old_text}' not found in {path}"
        new_content = content.replace(old_text, new_text)
        Path(path).write_text(new_content)
        return f"Successfully edited {path}"
    except FileNotFoundError:
        return f"Error: File not found: {path}"
    except PermissionError:
        return f"Error: Permission denied editing: {path}"
    except Exception as e:
        return f"Error editing file: {e}"


# Cron tools


def _safe_cron_id(value: str) -> str:
    """Convert a user-facing name into a filesystem-safe cron ID."""
    cron_id = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return cron_id or "cron-job"


def _one_off_schedule(run_at: str, timezone: str | None) -> str:
    """Convert an ISO datetime into a cron expression for a single local run."""
    normalized = run_at.replace("Z", "+00:00")
    run_dt = datetime.fromisoformat(normalized)

    if run_dt.tzinfo is None:
        if not timezone:
            raise ValueError(
                "timezone is required when run_at does not include a UTC offset"
            )
        run_dt = run_dt.replace(tzinfo=ZoneInfo(timezone))

    if timezone:
        run_dt = run_dt.astimezone(ZoneInfo(timezone))
    else:
        run_dt = run_dt.astimezone()

    return f"{run_dt.minute} {run_dt.hour} {run_dt.day} {run_dt.month} *"


@tool(
    name="create_cron_job",
    description=(
        "Create a scheduled cron job. Always use this tool instead of bash/write "
        "when creating CRON.md files."
    ),
    parameters={
        "type": "object",
        "properties": {
            "cron_id": {
                "type": "string",
                "description": (
                    "Optional filesystem-safe cron ID. If omitted, it is generated "
                    "from the name."
                ),
            },
            "name": {
                "type": "string",
                "description": "Human-readable cron name. Prefer this field.",
            },
            "title": {
                "type": "string",
                "description": "Alias for name, accepted for compatibility.",
            },
            "description": {
                "type": "string",
                "description": "Brief description of what the cron does",
            },
            "agent": {
                "type": "string",
                "description": "Agent ID that should execute the cron",
            },
            "schedule": {
                "type": "string",
                "description": (
                    "Cron expression: minute hour day month weekday. Required for "
                    "recurring jobs. For one-off jobs, prefer run_at instead."
                ),
            },
            "run_at": {
                "type": "string",
                "description": (
                    "ISO datetime for a one-off job, for example "
                    "2026-05-26T00:30:00+10:00."
                ),
            },
            "timezone": {
                "type": "string",
                "description": (
                    "Optional IANA timezone for interpreting run_at when it has no "
                    "UTC offset. Defaults to the configured workspace timezone."
                ),
            },
            "prompt": {
                "type": "string",
                "description": "Task prompt the agent should run on schedule",
            },
            "one_off": {
                "type": "boolean",
                "description": "Whether to delete the cron after its first run",
                "default": False,
            },
        },
        "required": ["name", "description", "agent", "prompt"],
    },
)
async def create_cron_job(
    description: str,
    agent: str,
    prompt: str,
    session: "AgentSession",
    name: str | None = None,
    title: str | None = None,
    schedule: str | None = None,
    run_at: str | None = None,
    timezone: str | None = None,
    one_off: bool = False,
    cron_id: str | None = None,
) -> str:
    """Create a CRON.md file under the configured workspace crons path."""
    try:
        session.shared_context.agent_loader.load(agent)
    except Exception as e:
        return f"Error: Agent not found: {agent}: {e}"

    display_name = name or title
    if not display_name:
        return "Error: name is required"

    cron_id = _safe_cron_id(cron_id or display_name)
    crons_path = session.shared_context.config.crons_path.resolve()
    job_dir = (crons_path / cron_id).resolve()
    cron_file = (job_dir / "CRON.md").resolve()

    try:
        cron_file.relative_to(crons_path)
    except ValueError:
        return f"Error: Invalid cron_id outside crons path: {cron_id}"

    if job_dir.exists():
        return f"Error: Cron job already exists: {cron_id}"

    timezone = timezone or session.shared_context.config.timezone
    if one_off and run_at:
        try:
            schedule = _one_off_schedule(run_at, timezone)
        except Exception as e:
            return f"Error: Invalid run_at: {e}"

    if not schedule:
        if one_off:
            return "Error: run_at or schedule is required for one-off cron jobs"
        return "Error: schedule is required for recurring cron jobs"

    try:
        CronDef(
            id=cron_id,
            name=display_name,
            description=description,
            agent=agent,
            schedule=schedule,
            prompt=prompt.strip(),
            one_off=one_off,
        )
    except Exception as e:
        return f"Error: Invalid cron job: {e}"

    frontmatter = {
        "name": display_name,
        "description": description,
        "agent": agent,
        "schedule": schedule,
        "one_off": one_off,
    }
    content = (
        "---\n"
        f"{yaml.safe_dump(frontmatter, sort_keys=False)}"
        "---\n\n"
        f"{prompt.strip()}\n"
    )

    try:
        job_dir.mkdir(parents=True, exist_ok=False)
        cron_file.write_text(content)
        session.shared_context.cron_loader.load(cron_id)
    except Exception as e:
        return f"Error creating cron job: {e}"

    return f"Created cron job `{cron_id}` at {cron_file}"


# Shell tool


@tool(
    name="bash",
    description="Execute a bash shell command",
    parameters={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The bash command to execute"},
        },
        "required": ["command"],
    },
)
async def bash(command: str, session: "AgentSession") -> str:
    """Execute a bash command and return the output."""
    try:
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        output = stdout.decode() if stdout else ""
        error = stderr.decode() if stderr else ""
        if output and error:
            return f"{output}\n{error}"
        return output or error or "Command completed with no output"
    except Exception as e:
        return f"Error executing command: {e}"
