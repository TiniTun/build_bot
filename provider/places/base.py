"""Places provider protocol, data models, and shared map-link helpers."""

from typing import TYPE_CHECKING, Protocol, runtime_checkable
from urllib.parse import quote

from pydantic import BaseModel

if TYPE_CHECKING:
    from utils.config import Config


class Place(BaseModel):
    """Normalized place result from any provider."""

    name: str
    address: str
    lat: float
    lon: float
    place_id: str
    rating: float | None = None


class MapLinks(BaseModel):
    """Map deep links for a place location."""

    apple_maps_url: str
    google_maps_url: str


def build_map_links(lat: float, lon: float, name: str, place_id: str) -> MapLinks:
    """Build Apple and Google map deep links for a location.

    Shared utility used by both the places search tool and the calendar
    geocoding path.
    """
    return MapLinks(
        apple_maps_url=f"https://maps.apple.com/?ll={lat},{lon}&q={quote(name)}",
        google_maps_url=(
            f"https://www.google.com/maps/search/?api=1"
            f"&query={lat},{lon}&query_place_id={place_id}"
        ),
    )


@runtime_checkable
class PlacesProvider(Protocol):
    """Text-search place lookup operations."""

    async def search(self, query: str) -> list[Place]: ...


def get_places_provider(config: "Config") -> PlacesProvider:
    """Factory to create the configured places provider."""
    if config.places is None:
        raise ValueError("Places not configured")

    match config.places.provider:
        case "google_places":
            from .google_places import GooglePlacesProvider

            return GooglePlacesProvider(config)
        case _:
            raise ValueError(f"Unknown places provider: {config.places.provider}")
