"""Pure event-navigation helpers (no Qt, no database). Events must be sorted by time."""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from typing import Iterable, Optional, Sequence

from .models import Event


def _filtered(events: Sequence[Event], kinds: Optional[Iterable[str]]) -> list[Event]:
    if kinds is None:
        return list(events)
    wanted = set(kinds)
    return [e for e in events if e.kind in wanted]


def current_event(events: Sequence[Event], ms: int, kinds: Optional[Iterable[str]] = None) -> Optional[Event]:
    """The last event at or before ``ms`` (None if the playhead is before the first one)."""
    f = _filtered(events, kinds)
    i = bisect_right([e.time_ms for e in f], ms) - 1
    return f[i] if i >= 0 else None


def next_event(events: Sequence[Event], ms: int, kinds: Optional[Iterable[str]] = None,
               min_gap_ms: int = 100) -> Optional[Event]:
    """First event strictly after the playhead (``min_gap_ms`` avoids re-landing on the current one)."""
    f = _filtered(events, kinds)
    i = bisect_right([e.time_ms for e in f], ms + min_gap_ms)
    return f[i] if i < len(f) else None


def prev_event(events: Sequence[Event], ms: int, kinds: Optional[Iterable[str]] = None,
               restart_ms: int = 1500) -> Optional[Event]:
    """Media-player style "previous": restart the current event unless we just started it.

    More than ``restart_ms`` past an event -> that event (restart it).
    Within ``restart_ms`` of an event's start -> the one before it.
    """
    f = _filtered(events, kinds)
    i = bisect_left([e.time_ms for e in f], ms - restart_ms) - 1
    return f[i] if i >= 0 else None
