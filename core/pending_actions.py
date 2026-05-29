"""Persistence for confirmation-required pending actions.

Confirm-required tools (e.g. ``calendar.create_event``) never mutate external
state directly. Instead they record a pending action here and return a
``requires_confirmation`` result. A later confirmation step (``/confirm``) can
read the stored action; ``/reject`` discards it.
"""

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from utils.config import Config

logger = logging.getLogger(__name__)
UUID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{12}$"
)


class PendingActionStore:
    """File-backed store of pending actions under ``<event_path>/pending_actions``."""

    def __init__(self, config: "Config") -> None:
        self._dir = config.event_path / "pending_actions"

    def _path_for_action(self, action_id: str) -> Path:
        """Return the safe storage path for a UUID action id."""
        if not UUID_PATTERN.fullmatch(action_id):
            raise ValueError(f"invalid pending action id: {action_id}")

        base_dir = self._dir.resolve()
        path = (base_dir / f"{action_id}.json").resolve()
        try:
            path.relative_to(base_dir)
        except ValueError as e:
            raise ValueError(f"pending action path escaped store: {action_id}") from e
        return path

    def create(
        self,
        *,
        action_id: str,
        capability_id: str,
        summary: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist a new pending action and return its stored record."""
        record = {
            "id": action_id,
            "capability_id": capability_id,
            "summary": summary,
            "payload": payload,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path_for_action(action_id).write_text(json.dumps(record, indent=2))
        return record

    def get(self, action_id: str) -> dict[str, Any] | None:
        """Return a stored pending action, or None if it does not exist."""
        try:
            path = self._path_for_action(action_id)
        except ValueError:
            return None
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("Failed to read pending action %s: %s", action_id, e)
            return None

    def list_actions(self) -> list[dict[str, Any]]:
        """Return all stored pending actions, newest first."""
        if not self._dir.exists():
            return []
        actions: list[dict[str, Any]] = []
        for path in self._dir.glob("*.json"):
            try:
                actions.append(json.loads(path.read_text()))
            except (OSError, json.JSONDecodeError) as e:
                logger.warning("Skipping unreadable pending action %s: %s", path, e)
        actions.sort(key=lambda a: a.get("created_at", ""), reverse=True)
        return actions

    def delete(self, action_id: str) -> bool:
        """Delete a pending action. Returns True if it existed."""
        try:
            path = self._path_for_action(action_id)
        except ValueError:
            return False
        if not path.is_file():
            return False
        path.unlink()
        return True
