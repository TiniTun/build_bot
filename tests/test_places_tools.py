"""Tests for the places search capability tool.

All tests are hermetic: no network. A fake provider is injected by swapping the
module-global ``get_places_provider`` in ``tools.places_tools``.
"""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import tools.places_tools as pt
from provider.places import Place, build_map_links
from tests.helpers import make_context, make_workspace
from tools.capabilities import ToolPolicy
from tools.capability_catalog import build_capability_registry
from tools.places_tools import build_places_capabilities
from utils.config import GooglePlacesConfig


class _FakeProvider:
    def __init__(self, places):
        self._places = places

    async def search(self, query):
        return self._places


def _session(context):
    return SimpleNamespace(shared_context=context)


def _places_tool(config):
    cap, tool_obj = next(
        pair
        for pair in build_places_capabilities(config)
        if pair[0].id == "places.search"
    )
    return tool_obj


def _agent_def(context):
    return context.agent_loader.load("pickle")


def _tool_names(context):
    caps = build_capability_registry(
        _agent_def(context), context, include_post_message=False
    )
    registry = caps.build_tool_registry(ToolPolicy.permissive())
    return {s["function"]["name"] for s in registry.get_tool_schemas()}


class PlacesSearchToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_returns_formatted_text_with_both_urls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = make_context(make_workspace(Path(tmp)))
            context.config.places = GooglePlacesConfig(api_key="test")
            place = Place(
                name="Blue Bottle Coffee",
                address="1 Ferry Building, San Francisco",
                lat=37.7955,
                lon=-122.3937,
                place_id="abc123",
                rating=4.5,
            )
            orig = pt.get_places_provider
            pt.get_places_provider = lambda config: _FakeProvider([place])
            try:
                tool_obj = _places_tool(context.config)
                output = await tool_obj.execute(session=_session(context), query="coffee")
            finally:
                pt.get_places_provider = orig

            links = build_map_links(
                place.lat, place.lon, place.name, place.place_id
            )
            self.assertIn(place.name, output)
            self.assertIn(place.address, output)
            self.assertIn("Apple Maps:", output)
            self.assertIn("Google Maps:", output)
            self.assertIn(links.apple_maps_url, output)
            self.assertIn(links.google_maps_url, output)

    async def test_place_without_rating_and_zero_coords_renders(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = make_context(make_workspace(Path(tmp)))
            context.config.places = GooglePlacesConfig(api_key="test")
            place = Place(
                name="Null Island",
                address="Somewhere",
                lat=0.0,
                lon=0.0,
                place_id="zero",
                rating=None,
            )
            orig = pt.get_places_provider
            pt.get_places_provider = lambda config: _FakeProvider([place])
            try:
                tool_obj = _places_tool(context.config)
                output = await tool_obj.execute(session=_session(context), query="coffee")
            finally:
                pt.get_places_provider = orig

            self.assertIn(place.name, output)
            self.assertIn("Apple Maps:", output)
            self.assertIn("Google Maps:", output)
            self.assertNotIn("Rating:", output)

    def test_disabled_when_no_places_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = make_context(make_workspace(Path(tmp)))
            context.config.places = None
            self.assertEqual(build_places_capabilities(context.config), [])

    def test_registered_in_capability_registry_when_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = make_context(make_workspace(Path(tmp)))
            context.config.places = GooglePlacesConfig(api_key="test")
            self.assertIn("places_search", _tool_names(context))

    def test_not_registered_when_unconfigured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = make_context(make_workspace(Path(tmp)))
            context.config.places = None
            self.assertNotIn("places_search", _tool_names(context))


if __name__ == "__main__":
    unittest.main()
