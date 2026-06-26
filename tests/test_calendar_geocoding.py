"""Tests for the calendar_search geocoding enrichment branch.

All tests are hermetic: no network or Google auth. ``get_calendar_provider`` is
monkeypatched with a stub returning a single located event, and a fake places
provider supplies (or fails) the geocoding lookup.
"""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from provider.calendar.base import CalendarEvent
from provider.places import Place, build_map_links
from tests.helpers import make_context, make_workspace
from tools.calendar_tools import build_calendar_capabilities
from utils.config import ExternalProviderConfig, ExternalToolsConfig


class _StubCalendarProvider:
    """Returns one event with a location, regardless of query."""

    async def search(self, query, time_min=None, time_max=None):
        return [
            CalendarEvent(
                id="e1",
                title="Lunch",
                start="2026-06-01T12:00:00Z",
                end="2026-06-01T13:00:00Z",
                location="123 Main St",
            )
        ]


class _FakePlacesProvider:
    """Returns one match for any query."""

    async def search(self, query):
        return [
            Place(
                name="Cafe",
                address="123 Main St",
                lat=1.0,
                lon=2.0,
                place_id="pid1",
            )
        ]


class _RaisingPlacesProvider:
    """Always raises to exercise the best-effort skip path."""

    async def search(self, query):
        raise RuntimeError("geocode boom")


def _calendar_context(tmp):
    workspace = make_workspace(Path(tmp))
    context = make_context(workspace)
    context.config.external_tools = ExternalToolsConfig(
        calendar=ExternalProviderConfig(
            enabled=True, provider="google_calendar", calendar_id="primary"
        )
    )
    return context


def _session(context):
    return SimpleNamespace(shared_context=context)


def _search_tool(context, places_provider):
    return next(
        pair[1]
        for pair in build_calendar_capabilities(
            context.config, places_provider=places_provider
        )
        if pair[0].id == "calendar.search"
    )


class CalendarGeocodingTests(unittest.IsolatedAsyncioTestCase):
    async def test_geocode_enabled_appends_map_links(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = _calendar_context(tmp)
            context.config.external_tools.calendar.geocode_locations = True
            import tools.calendar_tools as ct

            orig = ct.get_calendar_provider
            ct.get_calendar_provider = lambda config: _StubCalendarProvider()
            try:
                tool_obj = _search_tool(context, _FakePlacesProvider())
                raw = await tool_obj.execute(session=_session(context), query="lunch")
            finally:
                ct.get_calendar_provider = orig
            links = build_map_links(1.0, 2.0, "Cafe", "pid1")
            self.assertIn("Apple Maps:", raw)
            self.assertIn("Google Maps:", raw)
            self.assertIn(links.apple_maps_url, raw)
            self.assertIn(links.google_maps_url, raw)

    async def test_geocode_disabled_returns_plain_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = _calendar_context(tmp)
            # geocode_locations defaults to False.
            import tools.calendar_tools as ct

            orig = ct.get_calendar_provider
            ct.get_calendar_provider = lambda config: _StubCalendarProvider()
            try:
                tool_obj = _search_tool(context, _FakePlacesProvider())
                raw = await tool_obj.execute(session=_session(context), query="lunch")
            finally:
                ct.get_calendar_provider = orig
            self.assertIn("- Lunch (2026-06-01T12:00:00Z → 2026-06-01T13:00:00Z)", raw)
            self.assertNotIn("Apple Maps:", raw)
            self.assertNotIn("Google Maps:", raw)

    async def test_geocode_provider_error_is_best_effort(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = _calendar_context(tmp)
            context.config.external_tools.calendar.geocode_locations = True
            import tools.calendar_tools as ct

            orig = ct.get_calendar_provider
            ct.get_calendar_provider = lambda config: _StubCalendarProvider()
            try:
                tool_obj = _search_tool(context, _RaisingPlacesProvider())
                raw = await tool_obj.execute(session=_session(context), query="lunch")
            finally:
                ct.get_calendar_provider = orig
            self.assertIn("- Lunch (2026-06-01T12:00:00Z → 2026-06-01T13:00:00Z)", raw)
            self.assertNotIn("Apple Maps:", raw)


if __name__ == "__main__":
    unittest.main()
