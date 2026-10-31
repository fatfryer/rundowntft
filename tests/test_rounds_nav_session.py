import pytest

from tftreview import nav
from tftreview.models import MARKER, ROUND, Event
from tftreview.rounds import CAROUSEL, PVE, PVP, RoundConfig, RoundSequence, default_config, parse_label
from tftreview.session import NoEventError, ReviewSession, SequenceExhausted
from tftreview.store import Store


def ev(i, label, t, kind=ROUND):
    return Event(i, 1, kind, label, t)


# ------------------------------------------------------------------ rounds
def test_parse_label():
    assert parse_label("3-2") == (3, 2)
    assert parse_label("carousel") is None


def test_default_config_types():
    c = default_config()
    assert c.type_for("1-1") == CAROUSEL
    assert c.type_for("1-4") == PVE          # exact override beats the *-4 carousel wildcard
    assert c.type_for("2-4") == CAROUSEL
    assert c.type_for("3-7") == PVE
    assert c.type_for("3-2") == PVP


def test_sequence_order_and_after():
    seq = RoundSequence(default_config())
    assert seq.slots[0].label == "1-1" and seq.slots[3].label == "1-4"
    assert seq.slots[4].label == "2-1"
    assert seq.after(None).label == "1-1"
    assert seq.after("1-4").label == "2-1"
    assert seq.after("7-7") is None
    assert seq.slot("9-3").round_type == PVP      # off-config labels are still accepted
    with pytest.raises(ValueError):
        seq.slot("nope")


def test_config_roundtrip():
    c = default_config()
    assert RoundConfig.from_dict(c.to_dict()).slots() == c.slots()


# --------------------------------------------------------------------- nav
EVENTS = [ev(1, "2-1", 10_000), ev(2, "2-2", 40_000), ev(3, "2-3", 80_000)]


def test_current_event():
    assert nav.current_event(EVENTS, 5_000) is None
    assert nav.current_event(EVENTS, 10_000).label == "2-1"
    assert nav.current_event(EVENTS, 79_999).label == "2-2"


def test_next_event():
    assert nav.next_event(EVENTS, 0).label == "2-1"
    assert nav.next_event(EVENTS, 10_000).label == "2-2"   # not the one we're sitting on
    assert nav.next_event(EVENTS, 80_000) is None


def test_prev_event_restarts_then_goes_back():
    assert nav.prev_event(EVENTS, 60_000).label == "2-2"    # well into 2-2: restart it
    assert nav.prev_event(EVENTS, 40_800).label == "2-1"    # just started 2-2: go to 2-1
    assert nav.prev_event(EVENTS, 10_500) is None            # just started the first event


def test_kind_filter():
    mixed = EVENTS + [ev(4, "oops", 60_000, MARKER)]
    mixed.sort(key=lambda e: e.time_ms)
    assert nav.next_event(mixed, 41_000).label == "oops"
    assert nav.next_event(mixed, 41_000, kinds=(ROUND,)).label == "2-3"


# ----------------------------------------------------------------- session
@pytest.fixture
def session():
    store = Store(":memory:")
    s = ReviewSession(store, store.add_game("/v/a.mp4"))
    yield s
    store.close()


def test_stamp_next_follows_sequence(session):
    labels = [session.stamp_next(t).label for t in (0, 30_000, 60_000, 90_000, 120_000)]
    assert labels == ["1-1", "1-2", "1-3", "1-4", "2-1"]
    assert session.next_expected().label == "2-2"
    assert session.events()[0].round_type == CAROUSEL


def test_stamp_label_moves_existing_and_advances_cursor(session):
    session.stamp_label("2-1", 100_000)
    assert session.next_expected().label == "2-2"        # cursor jumped past the skipped rounds
    session.stamp_label("2-1", 105_000)                  # re-mark: moves, doesn't duplicate
    assert [e.time_ms for e in session.events()] == [105_000]


def test_unstamp_last(session):
    session.stamp_next(0)
    session.stamp_next(1000)
    assert session.unstamp_last().label == "1-2"
    assert session.next_expected().label == "1-2"
    assert session.unstamp_last().label == "1-1"
    assert session.unstamp_last() is None


def test_sequence_exhausted():
    store = Store(":memory:")
    config = RoundConfig("tiny", {1: 1})
    s = ReviewSession(store, store.add_game("/v/a.mp4"), config)
    s.stamp_next(0)
    with pytest.raises(SequenceExhausted):
        s.stamp_next(1000)


def test_add_entry_auto_scope(session):
    session.stamp_label("3-2", 100_000)
    r = session.add_entry("gold=32 #bad-roll rolled too early", ms=130_000)
    assert r.scope == "event" and r.event.label == "3-2"
    assert sorted(e.display() for e in r.created) == ["#bad-roll", "gold=32", "rolled too early"]
    # before any round has started: auto falls back to the game, explicit "event" errors
    assert session.add_entry("#intro", ms=5_000).scope == "game"
    with pytest.raises(NoEventError):
        session.add_entry("x", ms=5_000, scope="event")


def test_add_entry_game_scope_and_event_id(session):
    e = session.stamp_label("2-1", 0)
    assert session.add_entry("place=3", ms=500_000, scope="game").event is None
    assert session.add_entry("gold=50", event_id=e.id).event.id == e.id
    assert [x.display() for x in session.entries_for(None)] == ["place=3"]
    assert [x.display() for x in session.entries_for(e.id)] == ["gold=50"]


def test_markers_are_navigable_but_not_note_targets(session):
    session.stamp_label("2-1", 0)
    session.add_marker(20_000, "bad scout")
    assert session.next_event(1_000).label == "bad scout"
    assert session.add_entry("gold=1", ms=25_000).event.label == "2-1"   # still the round, not the marker


def test_change_listeners_fire(session):
    calls = []
    session.on_change(lambda: calls.append(1))
    session.stamp_next(0)
    session.add_entry("hi", ms=10)
    assert len(calls) == 2


def test_jump_stage(session):
    session.stamp_label("2-1", 10)
    session.stamp_label("3-1", 20)
    assert session.jump_stage(3).label == "3-1"
    assert session.jump_stage(4) is None
