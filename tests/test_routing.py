import tempfile
import unittest
from pathlib import Path

from core.events import CliEventSource, WebSocketEventSource
from utils.config import SourceSessionConfig

from tests.helpers import make_context, make_workspace


class SessionRoutingTests(unittest.TestCase):
    def test_routing_creates_and_reuses_session_for_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = make_context(
                make_workspace(
                    Path(tmp),
                    routing_bindings=[
                        {"agent": "cookie", "value": "platform-ws:.*"},
                    ],
                )
            )
            source = WebSocketEventSource(user_id="alice")

            first_session_id = context.routing_table.get_or_create_session_id(source)
            second_session_id = context.routing_table.get_or_create_session_id(source)
            session_info = context.history_store.get_session_info(first_session_id)

            self.assertEqual(second_session_id, first_session_id)
            self.assertIsNotNone(session_info)
            assert session_info is not None
            self.assertEqual(session_info.agent_id, "cookie")
            self.assertEqual(
                context.config.sources[str(source)].session_id,
                first_session_id,
            )

    def test_routing_clears_stale_cached_session_and_creates_new_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = make_context(make_workspace(Path(tmp)))
            source = CliEventSource()
            context.config.sources[str(source)] = SourceSessionConfig(
                session_id="missing-session"
            )

            with self.assertLogs("core.routing", level="WARNING"):
                session_id = context.routing_table.get_or_create_session_id(source)

            self.assertNotEqual(session_id, "missing-session")
            self.assertEqual(
                context.config.sources[str(source)].session_id,
                session_id,
            )
            self.assertIsNotNone(context.history_store.get_session_info(session_id))
