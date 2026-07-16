"""Places capability tool factory (search and nearby recommendations).

The ``places.search`` capability is a read-only lookup that returns place
results with ratings, review evidence, distance, and map links. It is disabled
unless ``config.places`` is configured.
"""

from math import asin, cos, radians, sin, sqrt
from typing import TYPE_CHECKING

from provider.places import build_map_links, get_places_provider
from tools.base import BaseTool, ToolErrorCode, ToolResult, tool
from tools.capabilities import CapabilityDef, ToolRiskLevel
from tools.external_support import provider_exception_to_result

if TYPE_CHECKING:
    from core.agent import AgentSession
    from utils.config import Config


def _distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return the great-circle distance between two coordinates."""
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(
        dlon / 2
    ) ** 2
    return 2 * 6_371_000 * asin(min(1, sqrt(a)))


def _compact_review(text: str, max_chars: int = 320) -> str:
    """Keep review evidence useful without flooding the agent context."""
    compact = " ".join(text.split())
    if len(compact) <= max_chars:
        return compact
    return compact[: max_chars - 3].rstrip() + "..."


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
            "Search Google Places for businesses, landmarks, or addresses. "
            "For nearby recommendations, pass the user's latitude and longitude. "
            "Returns ratings, review excerpts, distance, and map links."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query."},
                "latitude": {
                    "type": "number",
                    "description": "Search-center latitude from the user.",
                },
                "longitude": {
                    "type": "number",
                    "description": "Search-center longitude from the user.",
                },
                "radius_m": {
                    "type": "integer",
                    "description": "Nearby search radius in metres (100-50000).",
                    "default": 3000,
                },
                "limit": {
                    "type": "integer",
                    "description": "Number of places to return (1-10).",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
    )
    async def places_search(
        query: str,
        session: "AgentSession",
        latitude: float | None = None,
        longitude: float | None = None,
        radius_m: int = 3000,
        limit: int = 5,
    ) -> str:
        if (latitude is None) != (longitude is None):
            return ToolResult.error(
                ToolErrorCode.INVALID_ARGS,
                "latitude and longitude must be provided together",
            ).to_tool_content()
        if latitude is not None and not -90 <= latitude <= 90:
            return ToolResult.error(
                ToolErrorCode.INVALID_ARGS, "latitude must be between -90 and 90"
            ).to_tool_content()
        if longitude is not None and not -180 <= longitude <= 180:
            return ToolResult.error(
                ToolErrorCode.INVALID_ARGS,
                "longitude must be between -180 and 180",
            ).to_tool_content()
        if not 100 <= radius_m <= 50_000:
            return ToolResult.error(
                ToolErrorCode.INVALID_ARGS,
                "radius_m must be between 100 and 50000",
            ).to_tool_content()
        if not 1 <= limit <= 10:
            return ToolResult.error(
                ToolErrorCode.INVALID_ARGS, "limit must be between 1 and 10"
            ).to_tool_content()

        try:
            places = await provider.search(
                query,
                latitude=latitude,
                longitude=longitude,
                radius_m=radius_m,
                max_results=min(limit * 2, 20),
                include_reviews=True,
            )
        except Exception as e:  # noqa: BLE001 - mapped to stable error codes
            return provider_exception_to_result(e).to_tool_content()

        places_with_distance = []
        for place in places:
            distance = None
            if latitude is not None and longitude is not None:
                distance = _distance_m(latitude, longitude, place.lat, place.lon)
                if distance > radius_m:
                    continue
            places_with_distance.append((place, distance))

        if not places_with_distance:
            return ToolResult.success("No places found.").to_tool_content()

        blocks = []
        for p, distance in places_with_distance[:limit]:
            lines = [f"**{p.name}**", f"   {p.address}"]
            if p.rating is not None:
                rating = f"   Rating: {p.rating}"
                if p.user_rating_count is not None:
                    rating += f" ({p.user_rating_count} ratings)"
                lines.append(rating)
            if distance is not None:
                lines.append(f"   Distance: {distance / 1000:.1f} km")
            if p.serves_breakfast is not None:
                lines.append(
                    f"   Serves breakfast: {'yes' if p.serves_breakfast else 'no'}"
                )
            if p.reviews:
                lines.append("   Relevant Google reviews:")
                for review in p.reviews[:3]:
                    attribution = review.author_name
                    if review.relative_publish_time:
                        attribution += f", {review.relative_publish_time}"
                    review_rating = (
                        f" {review.rating}/5" if review.rating is not None else ""
                    )
                    lines.append(
                        f"   - {attribution}{review_rating}: "
                        f"{_compact_review(review.text)}"
                    )
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
                description="Search for places, reviews, distance, and map links.",
                risk_level=ToolRiskLevel.READ,
                required_config=["places"],
                enabled_by_default=True,
            ),
            places_search,
        ),
    ]
