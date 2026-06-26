"""Google Places API (New) provider."""

from typing import TYPE_CHECKING

import httpx

from .base import Place

if TYPE_CHECKING:
    from utils.config import Config


class GooglePlacesProvider:
    """Places provider using the Google Places API (New) Text Search."""

    BASE_URL = "https://places.googleapis.com/v1/places:searchText"

    def __init__(self, config: "Config"):
        """Initialize Google Places provider."""
        self.api_key = config.places.api_key

    async def search(self, query: str) -> list[Place]:
        """Search for places using the Google Places API Text Search."""
        async with httpx.AsyncClient() as client:
            response = await client.post(
                self.BASE_URL,
                headers={
                    "Content-Type": "application/json",
                    "X-Goog-Api-Key": self.api_key,
                    "X-Goog-FieldMask": (
                        "places.displayName,places.formattedAddress,"
                        "places.location,places.id,places.rating"
                    ),
                },
                json={"textQuery": query},
                timeout=30.0,
            )
            response.raise_for_status()
            data = response.json()

        results = []
        for item in data.get("places", []):
            location = item.get("location", {})
            results.append(
                Place(
                    name=item.get("displayName", {}).get("text", ""),
                    address=item.get("formattedAddress", ""),
                    lat=location.get("latitude", 0.0),
                    lon=location.get("longitude", 0.0),
                    place_id=item.get("id", ""),
                    rating=item.get("rating"),
                )
            )

        return results
