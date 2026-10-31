"""ReviewSession: the headless brain of the reviewer.

The Qt window is a thin shell over this class. Everything that matters - stamping
rounds in sequence, jumping between events, turning typed text into notes/tags/metrics -
lives here, so it is testable without a display or a video.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

from . import nav
from .entry_parser import ParsedEntry, parse_entry
from .export import write_sidecar
from .models import MARKER, ROUND, Entry, Event, Game
from .rounds import RoundConfig, RoundSequence, RoundSlot, default_config, parse_label
from .store import Store


class SequenceExhausted(Exception):
    """Every round in the configured sequence has already been marked."""


class NoEventError(Exception):
    """An entry was addressed to a round but there is no round at the playhead."""


@dataclass
class EntryResult:
    scope: str                      # "event" or "game": where the entry actually went
    event: Optional[Event]
    created: list[Entry] = field(default_factory=list)
    parsed: ParsedEntry = field(default_factory=ParsedEntry)


class ReviewSession:
    def __init__(self, store: Store, game: Game, config: Optional[RoundConfig] = None):
        self.store = store
        self.game = game
        self.config = config or default_config()
        self.sequence = RoundSequence(self.config)
        self._listeners: list[Callable[[], None]] = []

    # ------------------------------------------------------------ change events
    def on_change(self, callback: Callable[[], None]) -> None:
        self._listeners.append(callback)

    def _changed(self) -> None:
        for cb in list(self._listeners):
            cb()

    # ------------------------------------------------------------------ events
    def events(self, kinds: Optional[Iterable[str]] = None) -> list[Event]:
        return self.store.list_events(self.game.id, kinds=kinds)

    def current_event(self, ms: int, kinds: Optional[Iterable[str]] = None) -> Optional[Event]:
        return nav.current_event(self.events(), ms, kinds)

    def next_event(self, ms: int, kinds: Optional[Iterable[str]] = None) -> Optional[Event]:
        return nav.next_event(self.events(), ms, kinds)

    def prev_event(self, ms: int, kinds: Optional[Iterable[str]] = None) -> Optional[Event]:
        return nav.prev_event(self.events(), ms, kinds)

    def jump_stage(self, stage: int) -> Optional[Event]:
        """First marked round of a stage."""
        for event in self.events(kinds=(ROUND,)):
            parsed = parse_label(event.label)
            if parsed and parsed[0] == stage:
                return event
        return None

    # --------------------------------------------------------- round sequencing
    def _latest_slot_label(self) -> Optional[str]:
        """Label of the furthest-along marked round (sequence order, not time order)."""
        best_index, best_label = -1, None
        for event in self.events(kinds=(ROUND,)):
            i = self.sequence.index(event.label)
            if i is not None and i > best_index:
                best_index, best_label = i, event.label
        return best_label

    def next_expected(self) -> Optional[RoundSlot]:
        """The round the next ``stamp_next`` will mark (None when the sequence is complete)."""
        return self.sequence.after(self._latest_slot_label())

    def stamp_label(self, label: str, ms: int) -> Event:
        """Mark round ``label`` at ``ms``. Marking a label that already exists moves it."""
        slot = self.sequence.slot(label)
        meta = {"round_type": slot.round_type}
        existing = self.store.find_event(self.game.id, ROUND, slot.label)
        if existing:
            event = self.store.update_event(existing.id, time_ms=ms, meta=meta)
        else:
            event = self.store.add_event(self.game.id, ROUND, slot.label, ms, meta=meta)
        self._changed()
        return event

    def stamp_next(self, ms: int) -> Event:
        slot = self.next_expected()
        if slot is None:
            raise SequenceExhausted("every round in the sequence is already marked")
        return self.stamp_label(slot.label, ms)

    def unstamp_last(self) -> Optional[Event]:
        """Remove the furthest-along marked round (and any entries attached to it)."""
        label = self._latest_slot_label()
        if label is None:
            return None
        event = self.store.find_event(self.game.id, ROUND, label)
        if event is None:
            return None
        self.store.delete_event(event.id)
        self._changed()
        return event

    def move_event(self, event_id: int, ms: int) -> Event:
        event = self.store.update_event(event_id, time_ms=ms)
        self._changed()
        return event

    def relabel_event(self, event_id: int, label: str) -> Event:
        event = self.store.get_event(event_id)
        if event is None:
            raise KeyError(f"no event {event_id}")
        meta = dict(event.meta)
        if event.kind == ROUND:
            slot = self.sequence.slot(label)
            label = slot.label
            meta["round_type"] = slot.round_type
        updated = self.store.update_event(event_id, label=label, meta=meta)
        self._changed()
        return updated

    def delete_event(self, event_id: int) -> None:
        self.store.delete_event(event_id)
        self._changed()

    def add_marker(self, ms: int, label: str = "mark") -> Event:
        event = self.store.add_event(self.game.id, MARKER, label or "mark", ms, meta={})
        self._changed()
        return event

    # ------------------------------------------------------------------ entries
    def add_entry(self, text: str, ms: Optional[int] = None, scope: str = "auto",
                  event_id: Optional[int] = None) -> EntryResult:
        """Turn a line of quick-entry text into notes/tags/metrics and store them.

        scope: ``"auto"``  - the round at the playhead if there is one, else the game;
               ``"event"`` - the round at the playhead (error if none);
               ``"game"``  - the game itself.
        ``event_id`` overrides everything and targets that event directly.
        """
        parsed = parse_entry(text)
        if parsed.empty:
            return EntryResult("game", None, [], parsed)

        event: Optional[Event] = None
        if event_id is not None:
            event = self.store.get_event(event_id)
            if event is None or event.game_id != self.game.id:
                raise NoEventError(f"event {event_id} is not part of this game")
        elif scope == "game":
            event = None
        elif scope in ("auto", "event"):
            if ms is not None:
                event = self.current_event(ms, kinds=(ROUND,))
            if event is None and scope == "event":
                raise NoEventError("no round has started at the playhead yet")
        else:
            raise ValueError(f"bad scope {scope!r}")

        eid = event.id if event else None
        gid = self.game.id
        created: list[Entry] = []
        for key, value in parsed.metrics:
            created.append(self.store.set_metric(gid, key, value, event_id=eid, time_ms=ms))
        for tag in parsed.tags:
            created.append(self.store.add_tag(gid, tag, event_id=eid, time_ms=ms))
        if parsed.note:
            created.append(self.store.add_note(gid, parsed.note, event_id=eid, time_ms=ms))
        self._changed()
        return EntryResult("event" if event else "game", event, created, parsed)

    def delete_entry(self, entry_id: int) -> None:
        self.store.delete_entry(entry_id)
        self._changed()

    def entries_for(self, event_id: Optional[int]) -> list[Entry]:
        """Entries of one event, or the game-level entries when ``event_id`` is None."""
        if event_id is None:
            return self.store.list_entries(game_id=self.game.id, scope="game")
        return self.store.list_entries(game_id=self.game.id, event_id=event_id)

    # ------------------------------------------------------------------ persist
    def save_sidecar(self) -> str:
        return write_sidecar(self.store, self.game.id)
