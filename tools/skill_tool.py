"""Skill tool factory for creating dynamic skill tool."""

from typing import TYPE_CHECKING

from tools.base import tool

if TYPE_CHECKING:
    from core.agent import AgentSession
    from core.skill_loader import SkillLoader


def create_skill_tool(skill_loader: "SkillLoader"):
    """Factory function to create skill tool with dynamic schema."""
    skill_metadata = skill_loader.discover_skills()

    if not skill_metadata:
        return None
    
    # Build XML description of available skills (metadata only - progressive loading)
    skills_xml = "<skills>\n"
    for meta in skill_metadata:
        skills_xml += f'  <skill name="{meta.name}">\n'
        skills_xml += f"    <description>{meta.description}</description>\n"
        for trigger in meta.when_to_use:
            skills_xml += f"    <when_to_use>{trigger}</when_to_use>\n"
        skills_xml += "  </skill>\n"
    skills_xml += "</skills>"

    # Build enum of skill IDs
    skill_enum = [meta.id for meta in skill_metadata]

    @tool(
        name="skill",
        description=f"Load and invoke a specialized skill. {skills_xml}",
        parameters={
            "type": "object",
            "properties": {
                "skill_name": {
                    "type": "string",
                    "enum": skill_enum,
                    "description": "The name of the skill to load",
                }
            },
            "required": ["skill_name"],
        },
    )
    async def skill_tool(skill_name: str, session: "AgentSession") -> str:
        """Load and return the full skill body plus a manifest of bundled files.

        Reference/script file *contents* are not loaded here: only their paths and
        guidance are surfaced so they can be read or run on demand.
        """
        try:
            skill_def = skill_loader.load_skill(skill_name)
        except Exception:
            return f"Error: Skill '{skill_name}' not found. It may have been removed or is unavailable."

        parts = [skill_def.content]
        skill_dir = (skill_loader.config.skills_path / skill_def.id).resolve()

        if skill_def.references:
            parts.append("\n## References (read these files on demand with the read tool)")
            for ref in skill_def.references:
                path = (skill_dir / ref.path).resolve()
                parts.append(f"- `{path}` — {ref.description} (load {ref.when_to_load})")

        if skill_def.scripts:
            parts.append(
                "\n## Scripts (run these on demand with the skill_run_script tool)"
            )
            for script in skill_def.scripts:
                path = (skill_dir / script.path).resolve()
                parts.append(f"- `{path}` — {script.description} (run {script.when_to_run})")

        return "\n".join(parts)

    return skill_tool
