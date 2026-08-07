"""Capability metadata and policy layer over the tool registry.

`ToolRegistry` stays the executor of `BaseTool` instances. `CapabilityRegistry`
is a thin catalog/policy layer: it maps dotted capability ids (used for internal
policy and logs) to tools, applies a `ToolPolicy`, and produces the final
`ToolRegistry` consumed by `Agent._build_tools()`.
"""

import uuid
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from tools.base import BaseTool, ToolResult
from tools.registry import ToolRegistry

if TYPE_CHECKING:
    from core.agent import AgentSession
    from utils.config import Config


class ToolRiskLevel(str, Enum):
    """Risk classification used by the policy layer."""

    READ = "read"
    DRAFT = "draft"
    CONFIRM_REQUIRED = "confirm_required"
    WRITE = "write"


class RiskAction(str, Enum):
    """How the policy treats a given risk level."""

    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_CONFIRMATION = "require_confirmation"


class CapabilityDef(BaseModel):
    """Metadata describing a single tool capability."""

    id: str
    tool_name: str
    domain: str
    operation: str
    description: str
    risk_level: ToolRiskLevel
    required_config: list[str] = Field(default_factory=list)
    required_scopes: list[str] = Field(default_factory=list)
    enabled_by_default: bool = False


# Default risk -> action mapping when a tools policy is configured but a level is
# left unspecified. Confirm-required defaults to gating so mutations stay safe.
DEFAULT_RISK_ACTIONS: dict[ToolRiskLevel, RiskAction] = {
    ToolRiskLevel.READ: RiskAction.ALLOW,
    ToolRiskLevel.DRAFT: RiskAction.ALLOW,
    ToolRiskLevel.CONFIRM_REQUIRED: RiskAction.REQUIRE_CONFIRMATION,
    ToolRiskLevel.WRITE: RiskAction.ALLOW,
}


@dataclass(frozen=True)
class ToolPolicy:
    """Resolves whether a capability is available and how it is gated.

    When ``enabled_capabilities`` is ``None`` the policy is permissive (legacy
    behavior): every registered capability is allowed and nothing is gated. This
    preserves current behavior when no ``tools`` config block is present.

    ``allowed_capabilities`` is an optional per-agent narrowing layer applied on
    top of the global policy. When set, the final availability is the
    intersection of the globally enabled capabilities and the agent's allowed
    list. It is enforced even in permissive mode.
    """

    enabled_capabilities: set[str] | None
    risk_actions: dict[ToolRiskLevel, RiskAction]
    allowed_capabilities: frozenset[str] | None = None

    @classmethod
    def permissive(cls) -> "ToolPolicy":
        return cls(
            enabled_capabilities=None,
            risk_actions=dict(DEFAULT_RISK_ACTIONS),
            allowed_capabilities=None,
        )

    @classmethod
    def from_config(
        cls,
        config: "Config",
        allowed_capabilities: list[str] | None = None,
    ) -> "ToolPolicy":
        agent_allowed = (
            frozenset(allowed_capabilities)
            if allowed_capabilities is not None
            else None
        )

        tools_cfg = getattr(config, "tools", None)
        if tools_cfg is None:
            return cls(
                enabled_capabilities=None,
                risk_actions=dict(DEFAULT_RISK_ACTIONS),
                allowed_capabilities=agent_allowed,
            )

        risk_actions = dict(DEFAULT_RISK_ACTIONS)
        for level, action in tools_cfg.risk_policy.items():
            risk_actions[ToolRiskLevel(level)] = RiskAction(action)

        return cls(
            enabled_capabilities=set(tools_cfg.enabled_capabilities),
            risk_actions=risk_actions,
            allowed_capabilities=agent_allowed,
        )

    def resolve(self, capability: CapabilityDef) -> RiskAction | None:
        """Return the effective action for a capability, or None if excluded."""
        # Per-agent narrowing applies even in permissive mode.
        if (
            self.allowed_capabilities is not None
            and capability.id not in self.allowed_capabilities
        ):
            return None
        if self.enabled_capabilities is None:
            # Legacy/permissive: run everything as-is, no confirmation gating.
            return RiskAction.ALLOW
        if capability.id not in self.enabled_capabilities:
            return None
        action = self.risk_actions.get(capability.risk_level, RiskAction.ALLOW)
        if action is RiskAction.DENY:
            return None
        return action


class ConfirmationRequiredTool(BaseTool):
    """Wraps a tool so that invoking it records a pending action instead of running.

    The wrapped tool is never executed here, which guarantees confirm-required
    capabilities cannot mutate external state without an explicit confirmation
    step.
    """

    def __init__(self, capability: CapabilityDef, wrapped: BaseTool) -> None:
        self.name = wrapped.name
        self.description = wrapped.description
        self.parameters = wrapped.parameters
        self._capability = capability
        self._wrapped = wrapped

    async def execute(self, session: "AgentSession", **kwargs: Any) -> str:
        from core.pending_actions import PendingActionStore

        action_id = str(uuid.uuid4())
        summary = self._capability.description
        store = PendingActionStore(session.shared_context.config)
        store.create(
            action_id=action_id,
            capability_id=self._capability.id,
            summary=summary,
            payload=kwargs,
        )
        return ToolResult.requires_confirmation(
            action_id=action_id,
            capability_id=self._capability.id,
            summary=summary,
            payload=kwargs,
        ).to_tool_content()


class CapabilityRegistry:
    """Catalog of capabilities and their tools, plus policy-driven assembly."""

    def __init__(self) -> None:
        self._defs: dict[str, CapabilityDef] = {}
        self._tools: dict[str, BaseTool] = {}

    def register(self, capability: CapabilityDef, tool: BaseTool) -> None:
        """Register a capability definition with its executable tool."""
        self._defs[capability.id] = capability
        self._tools[capability.id] = tool

    def capabilities(self) -> list[CapabilityDef]:
        """Return all registered capability definitions."""
        return list(self._defs.values())

    def capability_for_tool_name(self, tool_name: str) -> str | None:
        """Map a safe LLM tool name back to its dotted capability id."""
        for cap in self._defs.values():
            if cap.tool_name == tool_name:
                return cap.id
        return None

    def build_tool_registry(self, policy: ToolPolicy) -> ToolRegistry:
        """Apply policy and return a ToolRegistry of allowed (possibly gated) tools."""
        registry = ToolRegistry()
        for cap_id, capability in self._defs.items():
            action = policy.resolve(capability)
            if action is None:
                continue
            tool = self._tools[cap_id]
            if action is RiskAction.REQUIRE_CONFIRMATION and not isinstance(
                tool, ConfirmationRequiredTool
            ):
                # Some tools (MCP) are gated at registration so they stay
                # fail-closed in permissive mode; do not wrap them twice.
                tool = ConfirmationRequiredTool(capability, tool)
            registry.register(tool)
        return registry
