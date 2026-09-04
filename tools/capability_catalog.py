"""Assemble the per-session CapabilityRegistry from context.

This centralizes how tools become capabilities. Builtins, skill, web, messaging,
subagent, and external email/calendar tools are each registered with a
``CapabilityDef``. Policy (``ToolPolicy.from_config``) then decides which are
included. With no ``tools`` config the policy is permissive, so the resulting
tool set matches the legacy ``Agent._build_tools`` behavior.
"""

from typing import TYPE_CHECKING

from tools.builtin_tools import bash, create_cron_job, edit_file, read_file, write_file
from tools.calendar_tools import (
    build_calendar_capabilities,
    build_calendar_confirmed_executors,
)
from tools.capabilities import (
    CapabilityDef,
    CapabilityRegistry,
    ConfirmationRequiredTool,
    ToolRiskLevel,
)
from tools.confirmed_executors import ConfirmedExecutorRegistry
from tools.email_tools import (
    build_email_capabilities,
    build_email_confirmed_executors,
)
from tools.mcp_tools import build_mcp_capabilities
from tools.memory_tools import build_memory_capabilities
from tools.places_tools import build_places_capabilities
from tools.planning_tools import (
    build_planning_capabilities,
    build_planning_confirmed_executors,
)
from tools.post_message_tool import create_post_message_tool
from tools.skill_run_script_tool import create_skill_run_script_tool
from tools.skill_tool import create_skill_tool
from tools.subagent_tool import create_subagent_dispatch_tool
from tools.task_tools import (
    build_task_capabilities,
    build_task_confirmed_executors,
)
from tools.webread_tool import create_webread_tool
from tools.websearch_tool import create_websearch_tool

if TYPE_CHECKING:
    from core.agent_loader import AgentDef
    from core.context import SharedContext
    from utils.config import Config


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
                risk_level=ToolRiskLevel.WRITE,
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

        run_script_tool = create_skill_run_script_tool(context.skill_loader)
        if run_script_tool:
            registry.register(
                CapabilityDef(
                    id="skills.run_script",
                    tool_name="skill_run_script",
                    domain="skills",
                    operation="run_script",
                    description="Run a script declared in a skill's SKILL.md.",
                    risk_level=ToolRiskLevel.WRITE,
                    enabled_by_default=True,
                ),
                run_script_tool,
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
    for capability, tool in build_places_capabilities(config):
        registry.register(capability, tool)
    # Thread the places provider into calendar only when geocoding is opted in.
    calendar_places_provider = None
    if config.external_tools.calendar.geocode_locations and config.places is not None:
        from provider.places import get_places_provider

        calendar_places_provider = get_places_provider(config)
    for capability, tool in build_calendar_capabilities(
        config, calendar_places_provider
    ):
        registry.register(capability, tool)
    for capability, tool in build_task_capabilities(config):
        registry.register(capability, tool)
    for capability, tool in build_memory_capabilities(config):
        registry.register(capability, tool)
    for capability, tool in build_planning_capabilities(config):
        registry.register(capability, tool)

    # MCP tools are gated here rather than by ToolPolicy so they stay fail-closed
    # even in permissive legacy mode (no `tools:` block). Only tools an operator
    # listed by exact name reach this point at all.
    for capability, tool in build_mcp_capabilities(context):
        if capability.risk_level is ToolRiskLevel.CONFIRM_REQUIRED:
            tool = ConfirmationRequiredTool(capability, tool)
        registry.register(capability, tool)

    return registry


def build_confirmed_executor_registry(config: "Config") -> ConfirmedExecutorRegistry:
    """Assemble confirmed executors for mutation capabilities (post-confirm path).

    Keyed by dotted capability id; ``ConfirmCommand`` looks an action's capability
    up here and runs the executor exactly once. Domains with no enabled provider
    contribute nothing, so legacy confirm-required builtins (cron/post_message)
    keep their existing re-execute-by-tool-name path.
    """
    registry = ConfirmedExecutorRegistry()
    registry.merge(build_email_confirmed_executors(config))
    registry.merge(build_calendar_confirmed_executors(config))
    registry.merge(build_task_confirmed_executors(config))
    registry.merge(build_planning_confirmed_executors(config))
    return registry
