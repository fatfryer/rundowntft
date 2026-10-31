"""Tests for reading gold / level / XP / HP off the HUD.

The geometry and the glyph classifier are tested against painted frames built from a
stand-in template set, so the tests do not depend on the shipped ``glyph_templates.npz``
being any particular shape. One separate test only checks that the shipped file still
covers every digit.

Sampling and validation are tested with a stub frame source: their job is deciding
*which* frame to believe, not decoding pixels.
"""
from __future__ import annotations

from fractions import Fraction

import numpy as np
import pytest

pytest.importorskip("cv2")

from tftreview.vision import glyphs  # noqa: E402
from tftreview.vision.glyphs import FONT12, FONT16, Glyph  # noqa: E402
from tftreview.vision.metrics import (  # noqa: E402
    GOLD_BOX,
    HP_BOX_DY,
    HP_BOX_X,
    HP_ROW_TOPS,
    Metrics,
    MetricsReader,
    RoundCheck,
    RoundSample,
    sample_round,
)
from tftreview.vision.video import VideoMeta  # noqa: E402

from synth import paint_number, paint_slash, slash_mask, synth_templates  # noqa: E402

# Painted frames are 1080p, i.e. the reference geometry the boxes are defined against.
TEMPLATES = synth_templates()


def hud_frame(**values: int) -> np.ndarray:
    """A black 1080p frame with the requested widgets painted in."""
    frame = np.zeros((1080, 1920, 3), np.uint8)
    if "gold" in values:
        paint_number(frame, str(values["gold"]), 1030, 882, TEMPLATES)
    if "level" in values:
        paint_number(frame, str(values["level"]), 395, 882, TEMPLATES)
    if "xp" in values:
        x = paint_number(frame, str(values["xp"]), 473, 888, TEMPLATES, font="font12")
        x = paint_slash(frame, x + 2, 888)
        paint_number(frame, str(values["xp_next"]), x + 2, 888, TEMPLATES, font="font12")
    if "hp" in values:
        row = values.get("hp_row", 3)
        paint_number(frame, str(values["hp"]), 1783, HP_ROW_TOPS[row] + HP_BOX_DY[0],
                     TEMPLATES)
    return frame


def reader() -> MetricsReader:
    return MetricsReader(templates=TEMPLATES)


# ------------------------------------------------------------------- templates
def test_templates_save_and_load_roundtrip(tmp_path):
    path = glyphs.save_templates(tmp_path / "templates.npz", TEMPLATES)
    loaded = glyphs.load_templates(path)
    assert set(loaded) == {"font16", "font12"}
    for font, digits in TEMPLATES.items():
        assert set(loaded[font]) == set(digits)
        for digit, bitmap in digits.items():
            assert np.allclose(loaded[font][digit], bitmap)
    assert glyphs.digit_count(loaded["font16"]) == "0123456789"


def test_load_missing_templates_is_empty(tmp_path):
    assert glyphs.load_templates(tmp_path / "nope.npz") == {}


def test_shipped_templates_cover_the_digits():
    shipped = glyphs.load_templates()
    assert "0123456789" == glyphs.digit_count(shipped.get("font16", {}))
    assert "01234568" == glyphs.digit_count(shipped.get("font12", {}))
    assert shipped["font16"]["7"].shape == FONT16.size[::-1]
    assert shipped["font12"]["6"].shape == FONT12.size[::-1]


# ------------------------------------------------------- segmentation / reading
def test_segment_and_read_number_left_to_right():
    frame = np.zeros((1080, 1920, 3), np.uint8)
    paint_number(frame, "42", 1030, 882, TEMPLATES)
    found = glyphs.segment(frame, GOLD_BOX, FONT16)
    assert [glyph.w for glyph in found] == [12, 12]
    assert [glyph.x for glyph in found] == sorted(glyph.x for glyph in found)
    value, text, score, margin = glyphs.read_number(found, TEMPLATES["font16"], FONT16)
    assert (value, text) == (42, "42")
    assert score > 0.99 and margin > 0


def test_segment_clamps_boxes_to_the_frame():
    frame = np.zeros((1080, 1920, 3), np.uint8)
    assert glyphs.segment(frame, (-50, -50, 20, 20), FONT16) == []
    assert glyphs.segment(frame, (1900, 1060, 2000, 1200), FONT16) == []


def test_segment_max_width_drops_merged_digits():
    frame = np.zeros((1080, 1920, 3), np.uint8)
    paint_number(frame, "8", 1030, 882, TEMPLATES)
    assert len(glyphs.segment(frame, GOLD_BOX, FONT16)) == 1
    assert glyphs.segment(frame, GOLD_BOX, FONT16, max_width=5) == []


def test_unknown_glyph_is_not_guessed():
    # a solid block matches no digit strongly enough
    block = Glyph(0, 0, 12, 16, np.ones((16, 12), bool))
    digit, score, _ = glyphs.classify(block, TEMPLATES["font16"], FONT16)
    assert digit is None and score < FONT16.min_score
    assert glyphs.read_number([block], TEMPLATES["font16"], FONT16)[0] is None


def test_read_all_widgets_from_one_frame():
    frame = hud_frame(gold=42, level=5, xp=3, xp_next=10, hp=87)
    metrics = reader().read(frame)
    assert metrics.complete
    assert metrics.values() == (42, 5, 3, 10, 87)
    assert metrics.own_slot == 4                 # hp_row=3 is slot 4
    assert set(metrics.scores) == {"gold", "level", "xp", "xp_next", "hp"}
    assert all(score > 0.99 for score in metrics.scores.values())


def test_blank_frame_reads_nothing():
    metrics = reader().read(np.zeros((1080, 1920, 3), np.uint8))
    assert metrics.values() == (None, None, None, None, None)
    assert metrics.own_slot is None
    assert not metrics.complete


# --------------------------------------------------------------------- gold
def test_gold_coin_highlight_is_not_a_digit():
    frame = np.zeros((1080, 1920, 3), np.uint8)
    # the spinning coin's white highlight: glyph-sized, but entirely left of the cut-off
    frame[882:898, 1006:1018] = (255, 255, 255)
    assert reader().read(frame).gold is None

    paint_number(frame, "5", 1030, 882, TEMPLATES)
    assert reader().read(frame).gold == 5


def test_gold_merged_blob_is_rejected():
    frame = np.zeros((1080, 1920, 3), np.uint8)
    frame[882:898, 1030:1047] = (255, 255, 255)      # 17 px wide: two digits merged
    assert reader().read(frame).gold is None


def test_gold_box_geometry_matches_the_reference_hud():
    # the coin lives at x<=1021, so the box must start left of it and filter by right edge
    assert GOLD_BOX[0] < 1023 and GOLD_BOX[2] > 1070


# ---------------------------------------------------------------------- XP
def test_find_slash():
    slash = Glyph(30, 0, 5, 12, slash_mask())
    digits = [Glyph(0, 0, 8, 12, TEMPLATES["font12"]["1"] > 0.5),
              Glyph(12, 0, 8, 12, TEMPLATES["font12"]["2"] > 0.5)]
    assert glyphs.find_slash(digits) is None
    assert glyphs.find_slash(digits + [slash]) == 2
    assert glyphs.find_slash([slash] + digits) == 0


def test_xp_reads_current_and_needed():
    frame = hud_frame(xp=12, xp_next=34)
    metrics = reader().read(frame)
    assert (metrics.xp, metrics.xp_next) == (12, 34)


def test_xp_without_slash_is_unreadable():
    frame = hud_frame(xp=0, xp_next=2)
    # erase the slash so only digits remain
    frame[884:906, 470:532] = 0
    paint_number(frame, "02", 473, 888, TEMPLATES, font="font12")
    metrics = reader().read(frame)
    assert (metrics.xp, metrics.xp_next) == (None, None)


# ---------------------------------------------------------------------- HP
def test_hp_locates_the_own_row():
    for row in (0, 3, 7):
        frame = hud_frame(hp=71, hp_row=row)
        metrics = reader().read(frame)
        assert metrics.hp == 71
        assert metrics.own_slot == row + 1


def test_hp_refuses_two_lookalike_rows():
    frame = hud_frame(hp=71, hp_row=2)
    paint_number(frame, "71", 1783, HP_ROW_TOPS[5] + HP_BOX_DY[0], TEMPLATES)
    assert reader().read(frame).hp is None


def test_hp_ignores_small_roster_font():
    frame = np.zeros((1080, 1920, 3), np.uint8)
    # small digits are only 10 px tall, below the 16 px font's 13 px minimum
    for row in HP_ROW_TOPS:
        paint_number(frame, "71", 1790, row + 30, TEMPLATES, font="font12")
    assert reader().read(frame).hp is None


def test_hp_box_geometry_matches_the_reference_hud():
    assert HP_BOX_X[0] >= 1775 and HP_BOX_X[1] <= 1832
    assert len(HP_ROW_TOPS) == 8


# ---------------------------------------------------------------- labelling
def test_label_examples_rebuilds_the_templates():
    frame = hud_frame(gold=42, level=5, xp=3, xp_next=10, hp=87)
    values = {"gold": 42, "level": 5, "xp": 3, "xp_next": 10, "hp": 87}
    examples, skipped = reader().label_examples(frame, values)
    assert skipped == []
    assert len(examples) == 2 + 1 + 1 + 2 + 2          # gold+level+xp+xp_next+hp

    learned = glyphs.templates_from_examples(examples)
    assert set(learned["font16"]) == {"2", "4", "5", "7", "8"}
    assert set(learned["font12"]) == {"0", "1", "3"}
    assert MetricsReader(templates=learned).read(frame).values() == (42, 5, 3, 10, 87)


def test_label_examples_reports_mismatched_digit_counts():
    frame = hud_frame(gold=42, level=5, xp=3, xp_next=10, hp=87)
    values = {"gold": 9, "level": 5, "xp": 3, "xp_next": 10, "hp": 1000}
    _, skipped = reader().label_examples(frame, values)
    assert any(line.startswith("gold: wanted '9'") for line in skipped)
    assert any(line.startswith("hp:") for line in skipped)


# ------------------------------------------------------------------ sampling
def fake_meta(frames: int = 600, fps: int = 60) -> VideoMeta:
    return VideoMeta(width=1920, height=1080, fps=Fraction(fps), frame_count=frames,
                     duration_s=frames / fps)


class StubReader:
    """Returns predetermined metrics per frame; the image is ignored."""

    def __init__(self, value_for):
        self.value_for = value_for

    def read(self, image) -> Metrics:
        return self.value_for(image)


@pytest.fixture
def stub_frames(monkeypatch):
    """Make the decoder yield just the frame numbers in ``pairs`` (values are unused)."""

    def install(pairs, *, end=None):
        by_frame = dict(pairs)

        def iter_frames(video, meta, *, start_frame=0, end_frame=None, **kwargs):
            limit = end if end is not None else end_frame
            for frame in sorted(by_frame):
                if frame < start_frame:
                    continue
                if limit is not None and frame >= limit:
                    continue
                yield frame, frame

        monkeypatch.setattr("tftreview.vision.metrics.iter_frames", iter_frames)
        return by_frame

    return install


def readable(gold: int) -> Metrics:
    """A complete reading: ``sample_round`` ignores partial ones."""
    return Metrics(gold=gold, level=3, xp=2, xp_next=6, hp=100)


def test_sample_round_takes_two_matching_readings(stub_frames):
    values = {0: None, 2: None, 20: 5, 22: 5, 24: 5}

    def metrics_for(frame):
        gold = values.get(frame)
        return Metrics() if gold is None else readable(gold)

    stub_frames({frame: None for frame in values})
    sample = sample_round("unused.mp4", 0, meta=fake_meta(), reader=StubReader(metrics_for),
                          step=2)
    assert sample is not None and sample.stable
    assert sample.frame == 20
    assert sample.metrics.gold == 5


def test_sample_round_rejects_a_single_frame_glitch(stub_frames):
    # 5, 6, 5, 5: the 6 must reset the run, so the answer is the second 5 (frame 24)
    values = {20: 5, 22: 6, 24: 5, 26: 5}

    def metrics_for(frame):
        return readable(values[frame])

    stub_frames({frame: None for frame in values})
    sample = sample_round("unused.mp4", 0, meta=fake_meta(), reader=StubReader(metrics_for),
                          step=2)
    assert sample is not None and sample.stable
    assert sample.frame == 24


def test_sample_round_falls_back_to_the_last_reading(stub_frames):
    values = {frame: frame for frame in range(0, 600, 2)}

    def metrics_for(frame):
        return readable(values[frame])

    stub_frames({frame: None for frame in values})
    sample = sample_round("unused.mp4", 0, meta=fake_meta(), reader=StubReader(metrics_for),
                          step=2, max_wait_s=0.1)
    assert sample is not None and not sample.stable
    assert sample.frame == 4                      # 6 frames at 60 fps -> frames 0..5
    assert sample.metrics.gold == 4


def test_sample_round_returns_none_when_nothing_is_readable(stub_frames):
    stub_frames({frame: None for frame in range(0, 600, 2)})
    sample = sample_round("unused.mp4", 0, meta=fake_meta(),
                          reader=StubReader(lambda image: Metrics()), step=2,
                          max_wait_s=0.1)
    assert sample is None


def test_sample_round_stops_at_the_next_round(stub_frames):
    values = {frame: frame for frame in range(0, 600, 2)}

    def metrics_for(frame):
        return readable(values[frame])

    meta = fake_meta()
    stub_frames({frame: None for frame in values})
    sample = sample_round("unused.mp4", 0, meta.ms_of(100), meta=meta,
                          reader=StubReader(metrics_for), step=2)
    assert sample is not None and not sample.stable
    assert sample.frame == 98                     # frames 0..99 are sampled, evens only


def test_sample_round_returns_none_without_room_for_a_step(stub_frames):
    stub_frames({})
    sample = sample_round("unused.mp4", 0, meta=fake_meta(),
                          reader=StubReader(lambda image: readable(1)), step=10,
                          max_wait_s=0.1)
    assert sample is None                         # only 6 frames fit in 0.1 s


# ----------------------------------------------------------------- validation
def make_sample(metrics: Metrics) -> RoundSample:
    return RoundSample(frame=10, time_ms=160, metrics=metrics, attempts=3, stable=True)


def test_round_check_verdicts():
    metrics = Metrics(gold=3, level=3, xp=2, xp_next=6, hp=100)
    ok = RoundCheck(label="2-1", event_ms=100, sample=make_sample(metrics),
                    reference={"gold": 3, "level": 3, "xp": 2, "hp": 100})
    assert ok.verdict == "ok" and ok.matches(metrics)
    assert ok.as_dict()["verdict"] == "ok"

    late = RoundCheck(label="4-2", event_ms=100, sample=make_sample(metrics),
                      reference={"gold": 31}, late_frame=500, late_ms=8400,
                      late_metrics=Metrics(gold=31))
    assert late.verdict == "late"

    bad = RoundCheck(label="1-4", event_ms=100, sample=make_sample(metrics),
                     reference={"hp": 199})
    assert bad.verdict == "bad"

    unlabelled = RoundCheck(label="1-2", event_ms=100, sample=make_sample(metrics))
    assert unlabelled.verdict == "read"

    unread = RoundCheck(label="1-1", event_ms=100, sample=None, reference={"gold": 1})
    assert unread.verdict == "none"


def test_round_check_matches_only_the_reference_keys():
    metrics = Metrics(gold=3, level=3, xp=2, xp_next=999, hp=100)
    check = RoundCheck(label="2-1", event_ms=0, sample=make_sample(metrics),
                       reference={"gold": 3, "hp": 100})
    assert check.matches(metrics)                 # xp/xp_next are not compared
    assert not check.matches(Metrics(gold=3, level=3, xp=2, xp_next=999, hp=99))
    assert not check.matches(Metrics(gold=4, level=3, xp=2, xp_next=999, hp=100))
    assert not check.matches(None)
