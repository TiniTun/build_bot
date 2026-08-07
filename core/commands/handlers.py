"""Built-in slash command handlers."""

import re
from datetime import datetime
from typing import TYPE_CHECKING

from core.commands.base import Command
from core.pending_actions import PendingActionStore
from tools.capabilities import ToolPolicy
from tools.capability_catalog import (
    build_capability_registry,
    build_confirmed_executor_registry,
)
from utils.def_loader import DefNotFoundError

if TYPE_CHECKING:
    from core.agent import AgentSession


class SessionCommand(Command):
    """Show current session details."""

    name = "session"
    description = "Show current session details"

    async def execute(self, args: str, session: "AgentSession") -> str:
        info = session.shared_context.history_store.get_session_info(session.session_id)

        created_str = info.created_at if info else "Unknown"

        lines = [
            f"**Session ID:** `{session.session_id}`",
            f"**Agent:** {session.agent.agent_def.name} (`{session.agent.agent_def.id}`)",
            f"**Created:** {created_str}",
            f"**Messages:** {len(session.state.messages)}",
        ]
        return "\n".join(lines)
    

class HelpCommand(Command):
    """Show available commands."""

    name = "help"
    aliases = ["?"]
    description = "Show available commands"

    async def execute(self, args: str, session: "AgentSession") -> str:
        lines = ["**Available Commands:**"]
        for cmd in session.shared_context.command_registry.list_commands():
            names = [f"/{cmd.name}"] + [f"/{a}" for a in cmd.aliases]
            lines.append(f"{', '.join(names)} - {cmd.description}")
        return "\n".join(lines)


class CompactCommand(Command):
    """Trigger manual context compaction."""

    name = "compact"
    description = "Compact conversation context manually"

    async def execute(self, args: str, session: "AgentSession") -> str:
        # Force compaction regardless of threshold
        await session.context_guard._build_compacted_messages(session.state)
        msg_count = len(session.state.messages)
        return f"✓ Context compacted. {msg_count} messages retained."


class ContextCommand(Command):
    """Show session context information."""

    name = "context"
    description = "Show session context information"

    async def execute(self, args: str, session: "AgentSession") -> str:
        token_count = session.context_guard.estimate_tokens(session.state)
        threshold = session.context_guard.token_threshold
        usage_pct = (token_count / threshold) * 100 if threshold > 0 else 0

        lines = [
            f"**Messages:** {len(session.state.messages)}",
            f"**Tokens:** {token_count:,} ({usage_pct:.1f}% of {threshold:,} threshold)",
        ]
        return "\n".join(lines)


class ClearCommand(Command):
    """Clear conversation and start fresh."""

    name = "clear"
    description = "Clear conversation and start fresh"

    async def execute(self, args: str, session: "AgentSession") -> str:
        source_str = str(session.state.source)

        session.shared_context.routing_table.config_source_session_cache(source_str, None)

        return "✓ Conversation cleared. Next message starts fresh."


class AgentCommand(Command):
    """List agents or show agent details."""

    name = "agent"
    aliases = ["agents"]
    description = "List agents or show agent details"

    async def execute(self, args: str, session: "AgentSession") -> str:
        if not args:
            # List agents
            agents = session.shared_context.agent_loader.discover_agents()
            lines = ["**Agents:**"]
            for agent in agents:
                marker = " (current)" if agent.id == session.agent.agent_def.id else ""
                lines.append(f"- `{agent.id}`: {agent.description}{marker}")
            return "\n".join(lines)

        # Show specific agent details
        agent_id = args.strip()
        try:
            agent_def = session.shared_context.agent_loader.load(agent_id)
        except ValueError:
            return f"✗ Agent `{agent_id}` not found."

        lines = [
            f"**Agent:** `{agent_def.id}`",
            f"**Name:** {agent_def.name}",
            f"**Description:** {agent_def.description}",
            f"**LLM:** {agent_def.llm.model}",
        ]

        # Add content sections
        lines.append(f"\n---\n\n**AGENT.md:**\n```\n{agent_def.agent_md}\n```")

        if agent_def.soul_md:
            lines.append(f"\n**SOUL.md:**\n```\n{agent_def.soul_md}\n```")

        return "\n".join(lines)


class SkillsCommand(Command):
    """List all skills or show skill details."""

    name = "skills"
    description = "List all skills or show skill details"

    async def execute(self, args: str, session: "AgentSession") -> str:
        if not args:
            skills = session.shared_context.skill_loader.discover_skills()
            if not skills:
                return "No skills configured."

            lines = ["**Skills:**"]
            for skill in skills:
                lines.append(f"- `{skill.id}`: {skill.description}")
                if skill.when_to_use:
                    lines.append(f"  - _when:_ {'; '.join(skill.when_to_use)}")
            return "\n".join(lines)

        # Show specific skill details (full contract, no reference/script contents)
        skill_id = args.strip()
        try:
            skill = session.shared_context.skill_loader.load_skill(skill_id)
        except DefNotFoundError:
            return f"✗ Skill `{skill_id}` not found."

        lines = [
            f"**Skill:** `{skill.id}`",
            f"**Name:** {skill.name}",
            f"**Description:** {skill.description}",
            "**When to use:**",
        ]
        lines.extend(f"- {trigger}" for trigger in skill.when_to_use)

        if skill.required_tools:
            lines.append(f"**Required tools:** {', '.join(skill.required_tools)}")
        if skill.permissions:
            lines.append(f"**Permissions:** `{skill.permissions}`")
        if skill.references:
            lines.append("**References:**")
            lines.extend(
                f"- `{ref.path}` — {ref.description} (load {ref.when_to_load})"
                for ref in skill.references
            )
        if skill.scripts:
            lines.append("**Scripts:**")
            lines.extend(
                f"- `{s.path}` — {s.description} (run {s.when_to_run})"
                for s in skill.scripts
            )

        lines.append(f"\n---\n\n**SKILL.md:**\n```\n{skill.content}\n```")
        return "\n".join(lines)


class CronsCommand(Command):
    """List all cron jobs or show cron details."""

    name = "crons"
    description = "List all cron jobs or show cron details"

    async def execute(self, args: str, session: "AgentSession") -> str:
        if not args:
            crons = session.shared_context.cron_loader.discover_crons()
            if not crons:
                return "No cron jobs configured."

            lines = ["**Cron Jobs:**"]
            for cron in crons:
                lines.append(f"- `{cron.id}`: {cron.schedule}")
            return "\n".join(lines)

        # Show specific cron details
        cron_id = args.strip()
        try:
            cron = session.shared_context.cron_loader.load(cron_id)
        except DefNotFoundError:
            return f"✗ Cron `{cron_id}` not found."

        lines = [
            f"**Cron:** `{cron.id}`",
            f"**Name:** {cron.name}",
            f"**Schedule:** `{cron.schedule}`",
            f"**Agent:** {cron.agent}",
            f"\n---\n\n**CRON.md:**\n```\n{cron.prompt}\n```",
        ]
        return "\n".join(lines)


class McpCommand(Command):
    """Read-only diagnostics for configured MCP servers.

    Output is deliberately compact and credential-free: it never contains a
    token, a URL that could carry one, tool arguments, or raw results.
    """

    name = "mcp"
    description = "Show MCP server status (/mcp [server_id])"

    async def execute(self, args: str, session: "AgentSession") -> str:
        hub = session.shared_context.mcp_hub
        statuses = hub.statuses()
        if not statuses:
            return "No MCP servers configured."

        server_id = args.strip()
        if server_id:
            status = hub.status(server_id)
            if status is None:
                known = ", ".join(f"`{s.server_id}`" for s in statuses)
                return f"✗ MCP server `{server_id}` not found. Configured: {known}"
            return self._detail(status, hub)

        lines = ["**MCP servers:**"]
        for status in statuses:
            flags = "required" if status.required else "optional"
            if not status.enabled:
                flags += ", disabled"
            summary = (
                f"- `{status.server_id}` — {status.state.value} ({flags}); "
                f"tools {status.enabled_tools}/{status.discovered_tools} enabled, "
                f"{status.quarantined_tools} quarantined"
            )
            if status.last_error:
                summary += f"; last error: {_short(status.last_error)}"
            lines.append(summary)
        return "\n".join(lines)

    def _detail(self, status, hub) -> str:
        lines = [
            f"**MCP server:** `{status.server_id}`",
            f"**State:** {status.state.value}",
            f"**Enabled:** {status.enabled}",
            f"**Required:** {status.required}",
        ]
        if status.server_name:
            lines.append(f"**Server:** {status.server_name} {status.server_version or ''}".strip())
        if status.last_discovery_at is not None:
            stamp = datetime.fromtimestamp(status.last_discovery_at).isoformat(
                timespec="seconds"
            )
            lines.append(f"**Last discovery:** {stamp}")
        lines.append(
            f"**Tools:** {status.discovered_tools} discovered, "
            f"{status.enabled_tools} enabled, {status.quarantined_tools} quarantined"
        )

        snapshot = hub.snapshot(status.server_id)
        server_config = hub.server_config(status.server_id)
        if snapshot is not None and server_config is not None:
            enabled = [
                t.name for t in snapshot.tools if server_config.is_tool_allowed(t.name)
            ]
            quarantined = [
                t.name
                for t in snapshot.tools
                if not server_config.is_tool_allowed(t.name)
            ]
            if enabled:
                lines.append("**Enabled tools:** " + ", ".join(f"`{n}`" for n in enabled))
            if quarantined:
                lines.append(
                    "**Quarantined:** " + ", ".join(f"`{n}`" for n in quarantined)
                )
        if status.last_error:
            lines.append(f"**Last error:** {_short(status.last_error)}")
        return "\n".join(lines)


def _short(text: str, limit: int = 200) -> str:
    """Keep an error summary to one readable line."""
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


class RouteCommand(Command):
    """Create a routing binding."""

    name = "route"
    description = "Create a routing binding (persists to config)"

    async def execute(self, args: str, session: "AgentSession") -> str:
        parts = args.strip().split(None, 1)
        if len(parts) != 2:
            return "**Usage:** `/route <source_pattern> <agent_id>`\n\nExample: `/route platform-telegram:.* pickle`"

        pattern, agent_id = parts

        # Validate regex pattern
        try:
            re.compile(f"^{pattern}$")
        except re.error as e:
            return f"✗ Invalid regex pattern: {e}"

        # Verify agent exists
        try:
            session.shared_context.agent_loader.load(agent_id)
        except ValueError:
            return f"✗ Agent `{agent_id}` not found."

        # Create and persist binding
        session.shared_context.routing_table.persist_binding(pattern, agent_id)

        return f"✓ Route bound: `{pattern}` → `{agent_id}`"


class BindingsCommand(Command):
    """Show all routing bindings."""

    name = "bindings"
    description = "Show all routing bindings"

    async def execute(self, args: str, session: "AgentSession") -> str:
        bindings = session.shared_context.config.routing.get("bindings", [])

        if not bindings:
            return "No routing bindings configured."

        lines = ["**Routing Bindings:**"]
        for binding in bindings:
            lines.append(f"- `{binding['value']}` → `{binding['agent']}`")

        return "\n".join(lines)


class ConfirmCommand(Command):
    """Confirm a pending confirmation-required action."""

    name = "confirm"
    description = "Confirm a pending action by id (/confirm <action_id>)"

    async def execute(self, args: str, session: "AgentSession") -> str:
        action_id = args.strip()
        if not action_id:
            return "**Usage:** `/confirm <action_id>`"

        store = PendingActionStore(session.shared_context.config)
        action = store.get(action_id)
        if action is None:
            return f"✗ Pending action `{action_id}` not found."

        capability_id = action["capability_id"]
        payload = action.get("payload", {})
        if not isinstance(payload, dict):
            return f"✗ Pending action `{action_id}` has an invalid payload."

        config = session.shared_context.config

        # Prefer a dedicated confirmed executor for provider mutations. It runs
        # the real mutation directly, so /confirm never re-invokes the proposal
        # tool (which would only record another pending action).
        executor = build_confirmed_executor_registry(config).get(capability_id)
        if executor is not None:
            # Delete before executing: a provider call that may have mutated must
            # not remain confirmable and risk a second mutation.
            store.delete(action_id)
            result = await executor(session, payload)
            return f"✓ Confirmed `{capability_id}` — {action['summary']}.\n{result}"

        # Legacy path: confirm-required builtins (cron/post_message) whose tools
        # execute for real under a permissive registry.
        capabilities = build_capability_registry(
            session.agent.agent_def,
            session.shared_context,
            include_post_message=session.state.source.is_cron,
        )
        capability = next(
            (cap for cap in capabilities.capabilities() if cap.id == capability_id),
            None,
        )
        if capability is None:
            return f"✗ Capability `{capability_id}` is not available."

        registry = capabilities.build_tool_registry(ToolPolicy.permissive())
        store.delete(action_id)
        result = await registry.execute_tool(capability.tool_name, session, **payload)
        return f"✓ Confirmed `{capability_id}` — {action['summary']}.\n{result}"


class RejectCommand(Command):
    """Discard a pending confirmation-required action."""

    name = "reject"
    description = "Reject a pending action by id (/reject <action_id>)"

    async def execute(self, args: str, session: "AgentSession") -> str:
        action_id = args.strip()
        if not action_id:
            return "**Usage:** `/reject <action_id>`"

        store = PendingActionStore(session.shared_context.config)
        if not store.delete(action_id):
            return f"✗ Pending action `{action_id}` not found."
        return f"✓ Rejected and discarded pending action `{action_id}`."
