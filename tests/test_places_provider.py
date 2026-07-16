"""Tests for the Places provider and shared map-link helpers.

All tests are hermetic: no network. ``httpx.AsyncClient`` is patched so the
async context manager's ``post`` returns a fake response.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from provider.places.base import Place, build_map_links
from provider.places.google_places import GooglePlacesProvider


def _fake_config():
    return SimpleNamespace(
        places=SimpleNamespace(provider="google_places", api_key="test")
    )


def _patched_client(payload):
    fake_resp = MagicMock()
    fake_resp.raise_for_status = MagicMock()
    fake_resp.json = MagicMock(return_value=payload)
    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=False)
    fake_client.post = AsyncMock(return_value=fake_resp)
    return fake_client


class BuildMapLinksTests(unittest.TestCase):
    def test_build_map_links_exact_urls(self) -> None:
        links = build_map_links(
            lat=37.7749,
            lon=-122.4194,
            name="Blue Bottle Coffee",
            place_id="ChIJabc123",
        )
        self.assertEqual(
            links.apple_maps_url,
            "https://maps.apple.com/?ll=37.7749,-122.4194&q=Blue%20Bottle%20Coffee",
        )
        self.assertEqual(
            links.google_maps_url,
            "https://www.google.com/maps/search/?api=1"
            "&query=37.7749,-122.4194&query_place_id=ChIJabc123",
        )


class GooglePlacesProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_maps_place(self) -> None:
        payload = {
            "places": [
                {
                    "displayName": {"text": "Blue Bottle Coffee"},
                    "formattedAddress": "66 Mint St, San Francisco, CA",
                    "location": {"latitude": 37.7749, "longitude": -122.4194},
                    "id": "ChIJabc123",
                    "rating": 4.5,
                    "userRatingCount": 321,
                    "servesBreakfast": True,
                    "reviews": [
                        {
                            "rating": 5,
                            "text": {"text": "Excellent flat white and eggs."},
                            "authorAttribution": {"displayName": "Alex"},
                            "relativePublishTimeDescription": "a month ago",
                        }
                    ],
                }
            ]
        }
        fake_client = _patched_client(payload)
        with patch("httpx.AsyncClient", return_value=fake_client):
            provider = GooglePlacesProvider(_fake_config())
            results = await provider.search("blue bottle")

        self.assertEqual(len(results), 1)
        place = results[0]
        self.assertIsInstance(place, Place)
        self.assertEqual(place.name, "Blue Bottle Coffee")
        self.assertEqual(place.address, "66 Mint St, San Francisco, CA")
        self.assertEqual(place.lat, 37.7749)
        self.assertEqual(place.lon, -122.4194)
        self.assertEqual(place.place_id, "ChIJabc123")
        self.assertEqual(place.rating, 4.5)
        self.assertEqual(place.user_rating_count, 321)
        self.assertTrue(place.serves_breakfast)
        self.assertEqual(len(place.reviews), 1)
        self.assertEqual(place.reviews[0].author_name, "Alex")
        self.assertEqual(place.reviews[0].text, "Excellent flat white and eggs.")

    async def test_search_sends_location_bias_and_page_size(self) -> None:
        fake_client = _patched_client({"places": []})
        with patch("httpx.AsyncClient", return_value=fake_client):
            provider = GooglePlacesProvider(_fake_config())
            await provider.search(
                "breakfast cafe",
                latitude=-27.4698,
                longitude=153.0251,
                radius_m=2500,
                max_results=7,
                include_reviews=True,
            )

        request = fake_client.post.await_args.kwargs
        self.assertEqual(request["json"]["textQuery"], "breakfast cafe")
        self.assertEqual(request["json"]["pageSize"], 7)
        self.assertEqual(
            request["json"]["locationBias"],
            {
                "circle": {
                    "center": {
                        "latitude": -27.4698,
                        "longitude": 153.0251,
                    },
                    "radius": 2500,
                }
            },
        )
        self.assertIn("places.reviews", request["headers"]["X-Goog-FieldMask"])

    async def test_search_omits_atmosphere_fields_by_default(self) -> None:
        fake_client = _patched_client({"places": []})
        with patch("httpx.AsyncClient", return_value=fake_client):
            provider = GooglePlacesProvider(_fake_config())
            await provider.search("123 Main Street")

        field_mask = fake_client.post.await_args.kwargs["headers"][
            "X-Goog-FieldMask"
        ]
        self.assertNotIn("places.reviews", field_mask)
        self.assertNotIn("places.servesBreakfast", field_mask)

    async def test_search_missing_rating_and_location(self) -> None:
        payload = {
            "places": [
                {
                    "displayName": {"text": "Mystery Spot"},
                    "formattedAddress": "Unknown",
                    "id": "ChIJxyz",
                }
            ]
        }
        fake_client = _patched_client(payload)
        with patch("httpx.AsyncClient", return_value=fake_client):
            provider = GooglePlacesProvider(_fake_config())
            results = await provider.search("mystery")

        self.assertEqual(len(results), 1)
        place = results[0]
        self.assertIsNone(place.rating)
        self.assertEqual(place.lat, 0.0)
        self.assertEqual(place.lon, 0.0)


if __name__ == "__main__":
    unittest.main()
