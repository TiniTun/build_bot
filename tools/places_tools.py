"""Places capability tool factory (search).

The ``places.search`` capability is a read-only lookup that returns place
results with Apple/Google map deep links. It is disabled unless ``config.places``
is configured.
"""

from typing import TYPE_CHECKING

from provider.places import build_map_links, get_places_provider
from tools.base import BaseTool, ToolResult, tool
from tools.capabilities import CapabilityDef, ToolRiskLevel
from tools.external_support import provider_exception_to_result

if TYPE_CHECKING:
    from core.agent import AgentSession
    from utils.config import Config


def build_places_capabilities(
    config: "Config",
) -> list[tuple[CapabilityDef, BaseTool]]:
    """Build places capability/tool pairs, or [] when places is unconfigured."""
    if config.places is None:
        return []

    provider = get_places_provider(config)

    @tool(
        name="places_search",
        description=(
            "Search for places (businesses, landmarks, addresses) and get "
            "Apple/Google map links."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query."},
            },
            "required": ["query"],
        },
    )
    async def places_search(query: str, session: "AgentSession") -> str:
        try:
            places = await provider.search(query)
        except Exception as e:  # noqa: BLE001 - mapped to stable error codes
            return provider_exception_to_result(e).to_tool_content()
        if not places:
            return ToolResult.success("No places found.").to_tool_content()

        blocks = []
        for p in places[:5]:
            lines = [f"**{p.name}**", f"   {p.address}"]
            if p.rating is not None:
                lines.append(f"   Rating: {p.rating}")
            links = build_map_links(p.lat, p.lon, p.name, p.place_id)
            lines.append(f"   Apple Maps: {links.apple_maps_url}")
            lines.append(f"   Google Maps: {links.google_maps_url}")
            blocks.append("\n".join(lines))
        return ToolResult.success("\n\n".join(blocks)).to_tool_content()

    return [
        (
            CapabilityDef(
                id="places.search",
                tool_name="places_search",
                domain="places",
                operation="search",
                description="Search for places and return map links.",
                risk_level=ToolRiskLevel.READ,
                required_config=["places"],
                enabled_by_default=True,
            ),
            places_search,
        ),
    ]
