import pytest

from tftreview.entry_parser import parse_entry
from tftreview.timeutil import format_time, parse_time


def test_parse_mixed_line():
    p = parse_entry("gold=32 hp=71 #Bad-Roll #comp:fast9 should have slammed the item")
    assert p.metrics == [("gold", 32.0), ("hp", 71.0)]
    assert p.tags == ["bad-roll", "comp:fast9"]
    assert p.note == "should have slammed the item"


def test_note_only_and_empty():
    assert parse_entry("just a note").note == "just a note"
    assert parse_entry("   ").empty


def test_metric_edge_cases():
    p = parse_entry("econ=-4 ratio=1.5 foo=bar 50g")
    assert p.metrics == [("econ", -4.0), ("ratio", 1.5)]
    assert p.note == "foo=bar 50g"  # non-numeric values stay note text


def test_duplicate_tags_collapse_and_lone_hash_is_text():
    p = parse_entry("#a #A # #b")
    assert p.tags == ["a", "b"]
    assert p.note == "#"


def test_commas_separate_metrics_and_tags():
    p = parse_entry("lvl=2,xp=0,gold=2,hp=100")
    assert p.metrics == [("level", 2.0), ("xp", 0.0), ("gold", 2.0), ("hp", 100.0)]
    assert p.note == ""

    p = parse_entry("gold=32, hp=71 , #bad-roll,#comp:fast9")
    assert p.metrics == [("gold", 32.0), ("hp", 71.0)]
    assert p.tags == ["bad-roll", "comp:fast9"]
    assert p.note == ""


def test_prose_commas_are_preserved():
    p = parse_entry("rolled early, should have slammed, then held")
    assert p.note == "rolled early, should have slammed, then held"
    assert not p.metrics and not p.tags

    p = parse_entry("gold=32, then rolled down")
    assert p.metrics == [("gold", 32.0)]
    assert p.note == "then rolled down"


@pytest.mark.parametrize("text,ms", [
    ("1:23:45", 5025000), ("83:12", 4992000), ("12:03", 723000),
    ("723", 723000), ("723000ms", 723000), ("0:01.5", 1500),
])
def test_parse_time(text, ms):
    assert parse_time(text) == ms


@pytest.mark.parametrize("bad", ["", "abc", "1:2:3:4", "-5"])
def test_parse_time_rejects(bad):
    with pytest.raises(ValueError):
        parse_time(bad)


def test_format_time_roundtrip():
    assert format_time(723000) == "12:03"
    assert format_time(5025000) == "1:23:45"
    assert format_time(1500, with_ms=True) == "0:01.500"
    assert parse_time(format_time(723000)) == 723000
