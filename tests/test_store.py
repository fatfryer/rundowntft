import pytest

from tftreview.models import METRIC, NOTE, ROUND, TAG
from tftreview.store import Store


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


def test_add_game_is_idempotent_per_video(store, tmp_path):
    video = tmp_path / "game1.mp4"
    a = store.add_game(video)
    b = store.add_game(str(video))
    assert a.id == b.id
    assert a.title == "game1"
    assert len(store.list_games()) == 1


def test_events_sorted_by_time(store):
    g = store.add_game("/v/a.mp4")
    store.add_event(g.id, ROUND, "2-2", 50_000)
    store.add_event(g.id, ROUND, "2-1", 10_000)
    assert [e.label for e in store.list_events(g.id)] == ["2-1", "2-2"]


def test_tags_dedupe_per_target(store):
    g = store.add_game("/v/a.mp4")
    e = store.add_event(g.id, ROUND, "2-1", 0)
    store.add_tag(g.id, "#Scouted", e.id)
    store.add_tag(g.id, "scouted", e.id)
    store.add_tag(g.id, "scouted")  # game level is a separate target
    assert len(store.list_entries(game_id=g.id, kind=TAG)) == 2
    assert store.remove_tag(g.id, "scouted", e.id) is True
    assert store.remove_tag(g.id, "scouted", e.id) is False


def test_metric_overwrites_instead_of_duplicating(store):
    g = store.add_game("/v/a.mp4")
    e = store.add_event(g.id, ROUND, "2-1", 0)
    store.set_metric(g.id, "Gold", 30, e.id, time_ms=100)
    store.set_metric(g.id, "gold", 34, e.id, time_ms=200)
    entries = store.list_entries(game_id=g.id, kind=METRIC)
    assert len(entries) == 1 and entries[0].num == 34 and entries[0].key == "gold"


def test_scope_filters(store):
    g = store.add_game("/v/a.mp4")
    e = store.add_event(g.id, ROUND, "2-1", 0)
    store.add_note(g.id, "event note", e.id)
    store.add_note(g.id, "game note")
    assert [x.text for x in store.list_entries(game_id=g.id, scope="game")] == ["game note"]
    assert [x.text for x in store.list_entries(game_id=g.id, scope="event")] == ["event note"]
    with pytest.raises(ValueError):
        store.list_entries(scope="nope")


def test_deleting_event_cascades_to_its_entries_only(store):
    g = store.add_game("/v/a.mp4")
    e = store.add_event(g.id, ROUND, "2-1", 0)
    store.add_note(g.id, "on event", e.id)
    store.add_note(g.id, "on game")
    store.delete_event(e.id)
    assert [x.text for x in store.list_entries(game_id=g.id, kind=NOTE)] == ["on game"]


def test_deleting_game_removes_everything(store):
    g = store.add_game("/v/a.mp4")
    e = store.add_event(g.id, ROUND, "2-1", 0)
    store.add_note(g.id, "x", e.id)
    store.delete_game(g.id)
    assert store.list_events(g.id) == [] and store.list_entries() == []


def test_persists_to_disk(tmp_path):
    path = tmp_path / "sub" / "lib.sqlite3"
    with Store(path) as s:
        g = s.add_game("/v/a.mp4")
        s.add_note(g.id, "hello")
    with Store(path) as s:
        assert [e.text for e in s.list_entries()] == ["hello"]
