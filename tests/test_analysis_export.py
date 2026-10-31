import io

import pytest

from tftreview import analysis
from tftreview.export import (entries_to_csv, game_to_dict, import_game_dict, open_game, read_sidecar,
                              rows_to_csv, sidecar_path, write_sidecar)
from tftreview.keymap import DEFAULT_KEYMAP, find_conflicts, load_keymap
from tftreview.session import ReviewSession
from tftreview.store import Store


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


def build_game(store, name, gold_by_round, place, tags_by_round=None):
    session = ReviewSession(store, store.add_game(f"/v/{name}.mp4"))
    for i, (label, gold) in enumerate(gold_by_round.items()):
        session.stamp_label(label, i * 30_000)
        text = f"gold={gold}"
        if tags_by_round and label in tags_by_round:
            text += " " + " ".join(f"#{t}" for t in tags_by_round[label])
        session.add_entry(text, ms=i * 30_000 + 1_000)
    session.add_entry(f"place={place} #fast9", scope="game")
    return session


def test_event_table_shape_and_inheritance(store):
    build_game(store, "g1", {"3-1": 40, "3-2": 12}, place=2, tags_by_round={"3-2": ["rolldown"]})
    rows = analysis.event_table(store)
    assert [r["label"] for r in rows] == ["3-1", "3-2"]
    assert rows[1]["metrics"] == {"gold": 12.0, "game:place": 2.0}
    assert rows[1]["tags"] == {"rolldown"} and rows[1]["game_tags"] == {"fast9"}
    assert rows[0]["stage"] == 3 and rows[0]["round"] == 1 and rows[0]["round_type"] == "pvp"


def test_metric_by_label_orders_rounds_and_aggregates(store):
    build_game(store, "g1", {"2-1": 10, "10-1": 99, "2-2": 20}, place=1)
    build_game(store, "g2", {"2-1": 30, "2-2": 40}, place=4)
    result = analysis.metric_by_label(analysis.event_table(store), "gold")
    assert [label for label, _ in result] == ["2-1", "2-2", "10-1"]      # numeric, not lexicographic
    assert result[0][1].mean == 20 and result[0][1].n == 2


def test_gold_is_just_a_metric_so_deltas_and_correlation_work(store):
    build_game(store, "g1", {"3-1": 50, "3-2": 30, "3-3": 10}, place=1)
    build_game(store, "g2", {"3-1": 50, "3-2": 45, "3-3": 40}, place=7)
    rows = analysis.with_deltas(analysis.event_table(store), "gold")
    g1 = [r for r in rows if r["game_id"] == 1]
    assert [r["metrics"].get("gold_delta") for r in g1] == [None, -20, -20]
    # spending more (lower gold left) goes with a better placement here
    r, n = analysis.correlate(rows, "gold", "game:place")
    assert n == 6 and r is not None and r > 0.5


def test_compare_by_tag(store):
    build_game(store, "g1", {"4-1": 10, "4-2": 50}, place=1, tags_by_round={"4-1": ["rolldown"]})
    build_game(store, "g2", {"4-1": 20, "4-2": 60}, place=3, tags_by_round={"4-1": ["rolldown"]})
    c = analysis.compare_by_tag(analysis.event_table(store), "gold", "#rolldown")
    assert c.with_tag.n == 2 and c.with_tag.mean == 15
    assert c.without_tag.n == 2 and c.without_tag.mean == 55
    assert c.diff == -40
    anyscope = analysis.compare_by_tag(analysis.event_table(store), "gold", "fast9", scope="game")
    assert anyscope.with_tag.n == 4 and anyscope.without_tag is None


def test_filter_rows_and_tag_counts(store):
    build_game(store, "g1", {"2-1": 5, "2-4": 6}, place=1, tags_by_round={"2-1": ["scouted"]})
    rows = analysis.event_table(store)
    assert [r["label"] for r in analysis.filter_rows(rows, tags_all=["scouted"])] == ["2-1"]
    assert [r["label"] for r in analysis.filter_rows(rows, tags_none=["scouted"])] == ["2-4"]
    assert [r["label"] for r in analysis.filter_rows(rows, round_types=["carousel"])] == ["2-4"]
    counts = analysis.tag_counts(store)
    assert counts["event"]["scouted"] == 1 and counts["game"]["fast9"] == 1


def test_stats_edge_cases():
    assert analysis.describe([]) is None
    assert analysis.describe([5]).stdev is None
    assert analysis.pearson([1, 1, 1], [1, 2, 3]) is None
    assert analysis.pearson([1], [1]) is None


def test_csv_exports(store):
    build_game(store, "g1", {"3-1": 40}, place=2)
    rows = analysis.event_table(store)
    out = io.StringIO()
    assert rows_to_csv(rows, out) == 1
    header, line = out.getvalue().splitlines()[:2]
    assert header.endswith("game:place,gold") and "3-1" in line and line.endswith("2,40")
    out = io.StringIO()
    assert entries_to_csv(store, out) == 3
    assert "game" in out.getvalue()


def test_sidecar_roundtrip_preserves_everything(store, tmp_path):
    session = build_game(store, "g1", {"3-1": 40, "3-2": 12}, place=2, tags_by_round={"3-2": ["rolldown"]})
    session.add_entry("a note on 3-2", ms=31_000)
    session.add_marker(15_000, "oops")
    video = tmp_path / "g1.mp4"
    out = write_sidecar(store, session.game.id, sidecar_path(video))
    assert out.endswith(".tftreview.json")

    other = Store(":memory:")
    game = import_game_dict(other, read_sidecar(out), video)
    def strip_ids(rows):  # database ids legitimately differ after an import
        return [{k: v for k, v in r.items() if k not in ("event_id", "game_id")} for r in rows]

    assert strip_ids(analysis.event_table(other)) == strip_ids(analysis.event_table(store))
    assert [e.label for e in other.list_events(game.id)] == [e.label for e in store.list_events(session.game.id)]
    assert game_to_dict(other, game.id)["entries"] == game_to_dict(store, session.game.id)["entries"]
    other.close()


def test_import_refuses_to_clobber_without_replace(store, tmp_path):
    session = build_game(store, "g1", {"3-1": 40}, place=2)
    data = game_to_dict(store, session.game.id)
    with pytest.raises(ValueError):
        import_game_dict(store, data, session.game.video_path)
    import_game_dict(store, data, session.game.video_path, replace=True)
    assert len(store.list_events(session.game.id)) == 1


def test_open_game_prefers_library_then_sidecar(store, tmp_path):
    video = tmp_path / "x.mp4"
    session = build_game(store, "x", {"2-1": 1}, place=1)
    write_sidecar(store, session.game.id, sidecar_path(video))

    fresh = Store(":memory:")
    game = open_game(fresh, video)                      # nothing in the library: picks up the sidecar
    assert len(fresh.list_events(game.id)) == 1
    assert open_game(fresh, video).id == game.id        # now it's in the library
    fresh.close()

    (tmp_path / "bad.mp4.tftreview.json").write_text("not json")
    fresh = Store(":memory:")
    assert fresh.list_events(open_game(fresh, tmp_path / "bad.mp4").id) == []   # corrupt sidecar: start clean
    fresh.close()


def test_atomic_sidecar_write_leaves_no_temp_files(store, tmp_path):
    session = build_game(store, "g1", {"3-1": 40}, place=2)
    write_sidecar(store, session.game.id, tmp_path / "a.tftreview.json")
    assert [p.name for p in tmp_path.iterdir()] == ["a.tftreview.json"]


# ------------------------------------------------------------------ keymap
def test_default_keymap_has_no_conflicts():
    assert find_conflicts(DEFAULT_KEYMAP) == {}


def test_keymap_overrides_and_warnings(tmp_path):
    path = tmp_path / "keymap.json"
    path.write_text('{"next_event": ["Down"], "bogus": ["X"], "prev_event": "Up", "play_pause": ["M"]}')
    keymap, warnings = load_keymap(path)
    assert keymap["next_event"] == ["Down"] and keymap["prev_event"] == ["Up"]
    assert any("bogus" in w for w in warnings)
    assert any("several actions" in w and "stamp_next" in w for w in warnings)   # 'M' is now double-bound
    path.write_text("{not json")
    keymap, warnings = load_keymap(path)
    assert keymap == DEFAULT_KEYMAP and warnings
