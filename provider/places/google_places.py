"""Google Places API (New) provider."""

from typing import TYPE_CHECKING

import httpx

from .base import Place, PlaceReview

if TYPE_CHECKING:
    from utils.config import Config


class GooglePlacesProvider:
    """Places provider using the Google Places API (New) Text Search."""

    BASE_URL = "https://places.googleapis.com/v1/places:searchText"

    def __init__(self, config: "Config"):
        """Initialize Google Places provider."""
        self.api_key = config.places.api_key

    async def search(
        self,
        query: str,
        *,
        latitude: float | None = None,
        longitude: float | None = None,
        radius_m: float = 3000,
        max_results: int = 10,
        include_reviews: bool = False,
    ) -> list[Place]:
        """Search for places using the Google Places API Text Search."""
        fields = [
            "places.displayName",
            "places.formattedAddress",
            "places.location",
            "places.id",
            "places.rating",
            "places.userRatingCount",
        ]
        if include_reviews:
            fields.extend(["places.servesBreakfast", "places.reviews"])

        request_body: dict = {
            "textQuery": query,
            "pageSize": max(1, min(max_results, 20)),
        }
        if latitude is not None and longitude is not None:
            request_body["locationBias"] = {
                "circle": {
                    "center": {
                        "latitude": latitude,
                        "longitude": longitude,
                    },
                    "radius": radius_m,
                }
            }

        async with httpx.AsyncClient() as client:
            response = await client.post(
                self.BASE_URL,
                headers={
                    "Content-Type": "application/json",
                    "X-Goog-Api-Key": self.api_key,
                    "X-Goog-FieldMask": ",".join(fields),
                },
                json=request_body,
                timeout=30.0,
            )
            response.raise_for_status()
            data = response.json()

        results = []
        for item in data.get("places", []):
            location = item.get("location") or {}
            reviews = []
            for review in item.get("reviews") or []:
                text = (review.get("text") or {}).get("text", "").strip()
                if not text:
                    continue
                author = review.get("authorAttribution") or {}
                reviews.append(
                    PlaceReview(
                        text=text,
                        rating=review.get("rating"),
                        author_name=author.get("displayName")
                        or "Google Maps user",
                        relative_publish_time=review.get(
                            "relativePublishTimeDescription"
                        ),
                    )
                )
            results.append(
                Place(
                    name=(item.get("displayName") or {}).get("text", ""),
                    address=item.get("formattedAddress", ""),
                    lat=location.get("latitude", 0.0),
                    lon=location.get("longitude", 0.0),
                    place_id=item.get("id", ""),
                    rating=item.get("rating"),
                    user_rating_count=item.get("userRatingCount"),
                    serves_breakfast=item.get("servesBreakfast"),
                    reviews=reviews,
                )
            )

        return results
