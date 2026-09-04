"""Turning a DayPlan into calendar writes — idempotently, and only ours.

Pure diffing plus the guards. The provider calls live in a later task;
nothing here performs I/O.
"""

from datetime import datetime
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from core.planning.models import PlannedEvent
    from provider.calendar.base import CalendarEvent

# Stamped into extendedProperties.private on every event we create. An event
# without it is someone else's and is never modified or deleted.
OWNER = "build-bot-daily-planner"


class SyncAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["create", "patch", "delete"]
    slot_key: str
    title: str
    start: str | None = None
    end: str | None = None
    event_id: str | None = None


class SyncPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    creates: list[SyncAction] = Field(default_factory=list)
    patches: list[SyncAction] = Field(default_factory=list)
    deletes: list[SyncAction] = Field(default_factory=list)
    # Ids of events on the planning calendar that are not ours. Reported so an
    # operator can see them; never acted on.
    refused_foreign: list[str] = Field(default_factory=list)
    unchanged: list[str] = Field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.creates or self.patches or self.deletes)


class GuardError(Exception):
    """A safety precondition failed. Aborts the whole batch, never partially."""


def marker(plan_date: str, slot_key: str) -> dict[str, str]:
    """The ownership stamp for one planned block."""
    return {"owner": OWNER, "plan_date": plan_date, "slot_key": slot_key}


def is_owned(event: "CalendarEvent") -> bool:
    """True only for an event this planner created (any date, any slot)."""
    return event.private_properties.get("owner") == OWNER


def _instant(value: str) -> datetime | None:
    """Parse an RFC3339/ISO datetime to an absolute instant.

    Comparison must be on instants, never on strings: Google echoes the
    calendar's own UTC offset, which will not be byte-identical to a locally
    generated ISO string for the same moment. String comparison would classify
    every re-run as a patch and quietly break idempotence.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def diff(
    desired: list["PlannedEvent"],
    existing: list["CalendarEvent"],
    *,
    plan_date: str,
) -> SyncPlan:
    """Compare wanted blocks against what is already on the planning calendar.

    Keyed on (plan_date, slot_key), which is stable across runs. Only events
    carrying our owner marker for *this* date, with a slot_key present, are
    candidates for patch or delete; everything else (foreign events, and our
    own events from other dates) is left untouched. Foreign events are also
    reported in ``refused_foreign``.
    """
    plan = SyncPlan()

    ours: dict[str, "CalendarEvent"] = {}
    for event in existing:
        if not is_owned(event):
            if event.id:
                plan.refused_foreign.append(event.id)
            continue
        if event.private_properties.get("plan_date") != plan_date:
            # Another day's plan. Not this run's business.
            continue
        slot_key = event.private_properties.get("slot_key")
        if not slot_key:
            # A marker missing slot_key cannot be matched to any desired
            # slot safely; skip rather than treat it as a universal match.
            continue
        ours[slot_key] = event

    wanted = {event.slot_key: event for event in desired}

    for slot_key, event in wanted.items():
        current = ours.get(slot_key)
        start, end = event.start.isoformat(), event.end.isoformat()
        if current is None:
            plan.creates.append(SyncAction(
                op="create", slot_key=slot_key, title=event.title,
                start=start, end=end))
            continue
        same_time = (
            _instant(current.start) == event.start
            and _instant(current.end) == event.end
        )
        if same_time and current.title == event.title:
            plan.unchanged.append(slot_key)
            continue
        plan.patches.append(SyncAction(
            op="patch", slot_key=slot_key, title=event.title,
            start=start, end=end, event_id=current.id))

    for slot_key, event in ours.items():
        if slot_key not in wanted:
            plan.deletes.append(SyncAction(
                op="delete", slot_key=slot_key, title=event.title,
                event_id=event.id))

    return plan
