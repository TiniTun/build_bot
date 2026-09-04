"""Domain models for day planning.

Pure data plus the pattern-file loader. Nothing here performs I/O beyond
reading the pattern file it is handed, and nothing here talks to a provider,
the MCP hub, or the LLM.
"""

from datetime import datetime, time
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

Priority = Literal["essential", "high", "medium", "low"]
Energy = Literal["high", "medium", "low"]
Category = Literal["work", "recovery", "admin", "personal"]
Flexibility = Literal["fixed", "flexible"]

# Ordered so a policy can express "at least this priority" / "at most this
# energy" as an integer comparison instead of a chain of literals.
PRIORITY_RANK: dict[str, int] = {"essential": 0, "high": 1, "medium": 2, "low": 3}
ENERGY_RANK: dict[str, int] = {"low": 0, "medium": 1, "high": 2}

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# The only pattern-file schema this loader understands. A file declaring
# anything else is rejected rather than half-read: guessing at an unknown
# schema is how a planner silently drops half a routine.
SUPPORTED_VERSION = 1


class TimeWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: str
    end: str

    @model_validator(mode="after")
    def validate_order(self) -> "TimeWindow":
        if self.as_times()[0] >= self.as_times()[1]:
            raise ValueError(f"window start must precede end: {self.start}-{self.end}")
        return self

    def as_times(self) -> tuple[time, time]:
        return (
            datetime.strptime(self.start, "%H:%M").time(),
            datetime.strptime(self.end, "%H:%M").time(),
        )


class Interval(BaseModel):
    """A half-open span of free time, [start, end)."""

    model_config = ConfigDict(extra="forbid")
    start: datetime
    end: datetime

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)


class PatternLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schedulable_window: TimeWindow
    buffer_minutes: int = Field(default=10, ge=0, le=120)
    max_scheduled_minutes: int = Field(default=180, gt=0)
    min_free_minutes: int = Field(default=60, ge=0)
    min_block_minutes: int = Field(default=20, gt=0)


class DayPattern(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    duration_minutes: int = Field(gt=0)
    priority: Priority = "medium"
    flexibility: Flexibility = "flexible"
    energy: Energy = "medium"
    category: Category = "work"
    fixed_time: str | None = None
    window: TimeWindow | None = None

    @model_validator(mode="after")
    def validate_placement(self) -> "DayPattern":
        if self.fixed_time is not None and self.window is not None:
            raise ValueError(
                f"{self.name}: give fixed_time or window, never both"
            )
        if self.fixed_time is not None:
            datetime.strptime(self.fixed_time, "%H:%M")
        return self

    def slug(self) -> str:
        """Stable, filename-safe form of the name, used inside ``slot_key``."""
        return "-".join(
            "".join(c for c in part if c.isalnum()) for part in self.name.lower().split()
        ).strip("-")


class DayPatternSet(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int
    defaults: PatternLimits
    weekdays: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_set(self) -> "DayPatternSet":
        if self.version != SUPPORTED_VERSION:
            raise ValueError(
                f"unsupported day_patterns version {self.version}; "
                f"this build understands version {SUPPORTED_VERSION}"
            )
        unknown = sorted(set(self.weekdays) - set(WEEKDAYS))
        if unknown:
            raise ValueError(f"unknown weekday keys: {', '.join(unknown)}")
        # Force both accepted shapes through validation now, so a malformed
        # Thursday fails at load rather than on a Thursday.
        for weekday in self.weekdays:
            self.activities_for(weekday)
            self.limits_for(weekday)
        return self

    def _entry(self, weekday: str) -> Any:
        return self.weekdays.get(weekday)

    def activities_for(self, weekday: str) -> list[DayPattern]:
        """The weekday's activities, in file order (their tie-break order)."""
        entry = self._entry(weekday)
        if entry is None:
            return []
        raw = entry.get("activities", []) if isinstance(entry, dict) else entry
        return [DayPattern.model_validate(item) for item in raw]

    def limits_for(self, weekday: str) -> PatternLimits:
        """Defaults, with any per-weekday overrides applied."""
        entry = self._entry(weekday)
        if not isinstance(entry, dict):
            return self.defaults
        overrides = {k: v for k, v in entry.items() if k != "activities"}
        if not overrides:
            return self.defaults
        return PatternLimits.model_validate(
            {**self.defaults.model_dump(), **overrides}
        )

    @classmethod
    def load(cls, path: Path) -> "DayPatternSet":
        """Load and fully validate a pattern file."""
        return cls.model_validate(yaml.safe_load(path.read_text()) or {})


class Candidate(BaseModel):
    """One thing that may be scheduled, normalized across patterns and tasks."""

    model_config = ConfigDict(extra="forbid")
    slot_key: str
    title: str
    duration_minutes: int
    priority: Priority
    energy: Energy
    category: Category
    flexibility: Flexibility
    fixed_time: str | None = None
    window: TimeWindow | None = None
    # Stable tie-break: file order for patterns, sort position for tasks.
    order: int
    duration_assumed: bool = False


ReadinessVerdict = Literal["green", "yellow", "red", "unknown"]
ReadinessSource = Literal[
    "whoop", "no_cycle_yet", "unavailable", "unauthorised", "date_mismatch"
]


class ReadinessSignal(BaseModel):
    """Everything the planner is allowed to know about the user's readiness.

    This is the whole allowlist. `note` is written by build_bot from
    (verdict, source) — never copied from the server, whose `reasons` embed
    recovery scores and sleep durations in prose.
    """

    model_config = ConfigDict(extra="forbid")
    verdict: ReadinessVerdict
    source: ReadinessSource
    retry_eligible: bool
    note: str
