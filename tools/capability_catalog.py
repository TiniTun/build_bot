"""Assemble the per-session CapabilityRegistry from context.

This centralizes how tools become capabilities. Builtins, skill, web, messaging,
subagent, and external email/calendar tools are each registered with a
``CapabilityDef``. Policy (``ToolPolicy.from_config``) then decides which are
included. With no ``tools`` config the policy is permissive, so the resulting
tool set matches the legacy ``Agent._build_tools`` behavior.
"""

from typing import TYPE_CHECKING

from tools.builtin_tools import bash, create_cron_job, edit_file, read_file, write_file
from tools.calendar_tools import build_calendar_capabilities
from tools.capabilities import CapabilityDef, CapabilityRegistry, ToolRiskLevel
from tools.email_tools import build_email_capabilities
from tools.post_message_tool import create_post_message_tool
from tools.skill_tool import create_skill_tool
from tools.subagent_tool import create_subagent_dispatch_tool
from tools.webread_tool import create_webread_tool
from tools.websearch_tool import create_websearch_tool

if TYPE_CHECKING:
    from core.agent_loader import AgentDef
    from core.context import SharedContext


def _builtin_capabilities() -> list[tuple[CapabilityDef, object]]:
    """Capability metadata for the always-available builtin tools."""
    return [
        (
            CapabilityDef(
                id="filesystem.read",
                tool_name="read",
                domain="filesystem",
                operation="read",
                description="Read a text file.",
                risk_level=ToolRiskLevel.READ,
                enabled_by_default=True,
            ),
            read_file,
        ),
        (
            CapabilityDef(
                id="filesystem.write",
                tool_name="write",
                domain="filesystem",
                operation="write",
                description="Write a text file.",
                risk_level=ToolRiskLevel.WRITE,
                enabled_by_default=True,
            ),
            write_file,
        ),
        (
            CapabilityDef(
                id="filesystem.edit",
                tool_name="edit",
                domain="filesystem",
                operation="edit",
                description="Edit a text file by string replacement.",
                risk_level=ToolRiskLevel.WRITE,
                enabled_by_default=True,
            ),
            edit_file,
        ),
        (
            CapabilityDef(
                id="cron.create_job",
                tool_name="create_cron_job",
                domain="cron",
                operation="create_job",
                description="Create a scheduled cron job.",
                risk_level=ToolRiskLevel.CONFIRM_REQUIRED,
                enabled_by_default=True,
            ),
            create_cron_job,
        ),
        (
            CapabilityDef(
                id="shell.bash",
                tool_name="bash",
                domain="shell",
                operation="bash",
                description="Execute a bash shell command.",
                risk_level=ToolRiskLevel.WRITE,
                enabled_by_default=True,
            ),
            bash,
        ),
    ]


def build_capability_registry(
    agent_def: "AgentDef",
    context: "SharedContext",
    include_post_message: bool,
) -> CapabilityRegistry:
    """Build the capability catalog for a session (pre-policy)."""
    registry = CapabilityRegistry()
    config = context.config

    for capability, tool in _builtin_capabilities():
        registry.register(capability, tool)

    if agent_def.allow_skills:
        skill_tool = create_skill_tool(context.skill_loader)
        if skill_tool:
            registry.register(
                CapabilityDef(
                    id="skills.invoke",
                    tool_name="skill",
                    domain="skills",
                    operation="invoke",
                    description="Load and invoke a specialized skill.",
                    risk_level=ToolRiskLevel.READ,
                    enabled_by_default=True,
                ),
                skill_tool,
            )

    websearch_tool = create_websearch_tool(config)
    if websearch_tool:
        registry.register(
            CapabilityDef(
                id="web.search",
                tool_name="websearch",
                domain="web",
                operation="search",
                description="Search the web.",
                risk_level=ToolRiskLevel.READ,
                required_config=["websearch"],
                enabled_by_default=True,
            ),
            websearch_tool,
        )

    webread_tool = create_webread_tool(config)
    if webread_tool:
        registry.register(
            CapabilityDef(
                id="web.read",
                tool_name="webread",
                domain="web",
                operation="read",
                description="Read and extract a web page.",
                risk_level=ToolRiskLevel.READ,
                required_config=["webread"],
                enabled_by_default=True,
            ),
            webread_tool,
        )

    if include_post_message:
        post_tool = create_post_message_tool(context)
        if post_tool:
            registry.register(
                CapabilityDef(
                    id="messaging.post_message",
                    tool_name="post_message",
                    domain="messaging",
                    operation="post_message",
                    description="Send a message to the user via the default platform.",
                    risk_level=ToolRiskLevel.CONFIRM_REQUIRED,
                    enabled_by_default=True,
                ),
                post_tool,
            )

    subagent_tool = create_subagent_dispatch_tool(agent_def.id, context)
    if subagent_tool:
        registry.register(
            CapabilityDef(
                id="agent.subagent_dispatch",
                tool_name="subagent_dispatch",
                domain="agent",
                operation="subagent_dispatch",
                description="Dispatch a task to a specialized subagent.",
                risk_level=ToolRiskLevel.READ,
                enabled_by_default=True,
            ),
            subagent_tool,
        )

    for capability, tool in build_email_capabilities(config):
        registry.register(capability, tool)
    for capability, tool in build_calendar_capabilities(config):
        registry.register(capability, tool)

    return registry
