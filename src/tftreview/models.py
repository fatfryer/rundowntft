"""Plain data objects shared by the store, session, analysis and UI layers.

The whole annotation system rests on one idea: an *entry* is a (key, value) pair
attached to a game or to an event. There are three flavours:

* ``note``   - free text.
* ``tag``    - a label with no value (``#bad-roll``). Namespacing is just a naming
               convention (``#comp:fast9``).
* ``metric`` - a number (``gold=32``). Gold, HP, level, placement... all the same thing.

Because they share one table and one shape, any analysis that works for one metric
works for every metric, and any tag can be used as a filter or a grouping.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

NOTE = "note"
TAG = "tag"
METRIC = "metric"
ENTRY_KINDS = (NOTE, TAG, METRIC)

# Event kinds
ROUND = "round"    # the start of a TFT round ("2-1")
MARKER = "marker"  # free-form bookmark


@dataclass
class Game:
    id: int
    video_path: str
    title: str
    created_at: str
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class Event:
    id: int
    game_id: int
    kind: str
    label: str
    time_ms: int
    source: str = "manual"  # "manual" now; "auto" once detection exists
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def round_type(self) -> Optional[str]:
        return self.meta.get("round_type")


@dataclass
class Entry:
    id: int
    game_id: int
    event_id: Optional[int]  # None = attached to the game itself
    kind: str
    key: str                 # tag name / metric name / "note"
    num: Optional[float] = None
    text: Optional[str] = None
    time_ms: Optional[int] = None  # playhead position when it was written
    created_at: str = ""

    def display(self) -> str:
        if self.kind == TAG:
            return f"#{self.key}"
        if self.kind == METRIC:
            return f"{self.key}={self.num:g}" if self.num is not None else f"{self.key}="
        return self.text or ""
