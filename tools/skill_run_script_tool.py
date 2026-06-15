"""Constrained runner for scripts declared in a skill's SKILL.md.

This is a deliberately narrow alternative to raw ``bash``: an agent may only run
a script that the skill explicitly declares under its ``scripts:`` list, the path
is resolved strictly inside the skill directory, and execution happens via
``exec`` (no shell) with the skill directory as the working directory.
"""

import asyncio
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from core.skill_loader import DefNotFoundError
from tools.base import ToolErrorCode, ToolResult, tool

if TYPE_CHECKING:
    from core.agent import AgentSession
    from core.skill_loader import SkillDef, SkillLoader


def _resolve_declared_script(skill_dir: Path, skill_def: "SkillDef", script: str) -> Path:
    """Return the absolute path of a declared script, or raise ValueError.

    Rejects undeclared scripts, absolute paths, parent traversal, paths that
    escape the skill directory, and missing files.
    """
    declared = {s.path for s in skill_def.scripts}
    if script not in declared:
        raise ValueError(f"script not declared in skill '{skill_def.id}': {script}")

    pure = Path(script)
    if pure.is_absolute() or ".." in pure.parts:
        raise ValueError(f"unsafe script path: {script}")

    skill_root = skill_dir.resolve()
    resolved = (skill_root / script).resolve()
    try:
        resolved.relative_to(skill_root)
    except ValueError:
        raise ValueError(f"script path escapes skill directory: {script}")

    if not resolved.is_file():
        raise ValueError(f"script file not found: {script}")
    return resolved


def _build_command(script_path: Path, args: list[str]) -> list[str]:
    """Build an argv list for the script based on its extension (no shell)."""
    suffix = script_path.suffix
    if suffix == ".py":
        return [sys.executable, str(script_path), *args]
    if suffix == ".sh":
        return ["bash", str(script_path), *args]
    return [str(script_path), *args]


def create_skill_run_script_tool(skill_loader: "SkillLoader"):
    """Factory for the constrained skill script runner; None if no skills exist."""
    skill_metadata = skill_loader.discover_skills()
    if not skill_metadata:
        return None

    skill_enum = [meta.id for meta in skill_metadata]

    @tool(
        name="skill_run_script",
        description=(
            "Run a script that a skill declares in its SKILL.md `scripts:` list. "
            "Only declared scripts can run; paths are confined to the skill "
            "directory and execute without a shell. Prefer this over bash for "
            "skill scripts."
        ),
        parameters={
            "type": "object",
            "properties": {
                "skill_name": {
                    "type": "string",
                    "enum": skill_enum,
                    "description": "The skill that owns the script.",
                },
                "script": {
                    "type": "string",
                    "description": (
                        "Relative path of the script as declared in the skill's "
                        "`scripts:` list, e.g. scripts/get_weather.py."
                    ),
                },
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Arguments passed to the script, in order.",
                },
            },
            "required": ["skill_name", "script"],
        },
    )
    async def skill_run_script(
        skill_name: str,
        script: str,
        session: "AgentSession",
        args: list[str] | None = None,
    ) -> str:
        try:
            skill_def = skill_loader.load_skill(skill_name)
        except DefNotFoundError:
            return ToolResult.error(
                ToolErrorCode.NOT_FOUND,
                f"Skill '{skill_name}' not found.",
                user_action="Use a skill id from the available skills.",
            )

        skill_dir = skill_loader.config.skills_path / skill_def.id
        try:
            script_path = _resolve_declared_script(skill_dir, skill_def, script)
        except ValueError as e:
            return ToolResult.error(
                ToolErrorCode.PERMISSION_DENIED,
                str(e),
                user_action="Run only a script declared in the skill's scripts list.",
            )

        argv = [str(a) for a in (args or [])]
        command = _build_command(script_path, argv)

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(skill_dir.resolve()),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()
        except Exception as e:
            return ToolResult.error(
                ToolErrorCode.PROVIDER_ERROR,
                f"Error running script: {e}",
                retryable=True,
            )

        output = stdout.decode() if stdout else ""
        error = stderr.decode() if stderr else ""
        if output and error:
            return f"{output}\n{error}"
        return output or error or "Script completed with no output"

    return skill_run_script
