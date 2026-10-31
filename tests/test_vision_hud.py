"""Unit tests for the HUD geometry/scoring and the ROI helper - no video needed."""
from __future__ import annotations

import json

import pytest

pytest.importorskip("cv2")
import numpy as np

from tftreview.vision.hud import HudModel, observe
from tftreview.vision.roi import ROI, ROIManager

from synth import hud_frames


@pytest.fixture(scope="module")
def model():
    return HudModel.for_size(1920, 1080, max_rounds=7)


@pytest.fixture(scope="module")
def small():
    return HudModel.for_size(960, 540, max_rounds=7)      # matches synth.hud_frames defaults


def test_geometry_scales_and_covers_every_icon_row(model):
    assert model.pitch == pytest.approx(37.16)
    assert model.icon_half == pytest.approx(8.0)
    assert model.strip_box == (831, 6, 258, 24)

    x, _y, w, _h = model.strip_box
    for n in (4, 7):
        boxes = model.slot_boxes(n)
        assert len(boxes) == n
        assert boxes[0][0] >= x and boxes[-1][2] <= x + w
        centres = model.centers(n)
        assert sum(centres) / len(centres) == pytest.approx(960.0)
        for a, b in zip(centres, centres[1:]):
            assert b - a == pytest.approx(model.pitch)


def test_smaller_video_scales_the_same_geometry():
    small = HudModel.for_size(960, 540, max_rounds=7)
    assert small.pitch == pytest.approx(37.16 / 2)
    assert small.icon_half == pytest.approx(4.0)
    assert small.strip_box[0] == pytest.approx(831 / 2, abs=1)


def test_scores_lit_icon_only(small):
    for lit in range(4):
        frame = next(iter(hud_frames([lit])))
        x, y, w, h = small.strip_box
        strip = frame[y:y + h, x:x + w]
        scores = small.scores(strip, 4)
        assert scores[lit] > 0.9
        assert sum(scores) - scores[lit] < 0.01


def test_observe_picks_the_right_round_and_icon_count(small):
    for lit in range(4):
        frame = next(iter(hud_frames([lit])))
        x, y, w, h = small.strip_box
        obs = observe(small, frame[y:y + h, x:x + w], [4, 7])
        assert obs is not None
        assert obs.n_icons == 4
        assert obs.index == lit
        assert obs.confidence > 0.5


def test_observe_returns_none_on_an_empty_strip(small):
    x, y, w, h = small.strip_box
    assert observe(small, np.zeros((h, w, 3), np.uint8), [4, 7]) is None


def test_a_seven_icon_row_is_read_as_seven(small):
    frame = next(iter(hud_frames([4], n_icons=7)))
    x, y, w, h = small.strip_box
    obs = observe(small, frame[y:y + h, x:x + w], [4, 7])
    assert obs is not None and obs.n_icons == 7 and obs.index == 4


# ------------------------------------------------------------------------ ROI
def _gradient(w=200, h=100):
    img = np.zeros((h, w, 3), np.uint8)
    img[:, :, 0] = np.arange(w, dtype=np.uint8)[None, :]
    img[:, :, 1] = np.arange(h, dtype=np.uint8)[:, None]
    return img


def test_roi_corner_click_order_does_not_matter():
    points = [(0.75, 0.75), (0.25, 0.25), (0.25, 0.75), (0.75, 0.25)]  # scrambled on purpose
    roi = ROI("gold", points)
    assert roi.ordered(200, 100) == [(50, 25), (150, 25), (150, 75), (50, 75)]
    assert roi.bounding_box(200, 100) == (50, 25, 100, 50)
    assert roi.is_axis_aligned(200, 100)


def test_roi_extract_matches_a_plain_crop_for_axis_aligned_quads():
    frame = _gradient()
    roi = ROI("gold", [(0.25, 0.25), (0.75, 0.25), (0.75, 0.75), (0.25, 0.75)])
    got = roi.extract(frame)
    assert got.shape == (50, 100, 3)
    assert np.array_equal(got, frame[25:75, 50:150])


def test_roi_extract_warps_a_non_axis_aligned_quad():
    frame = _gradient()
    diamond = ROI("hp", [(0.5, 0.1), (0.9, 0.5), (0.5, 0.9), (0.1, 0.5)])
    assert not diamond.is_axis_aligned(200, 100)
    assert diamond.extract(frame).shape == (80, 160, 3)


def test_roi_manager_loads_named_and_ordered_json(tmp_path):
    named = tmp_path / "named.json"
    named.write_text(json.dumps({"rois": {
        "gold": [{"x": 0.1, "y": 0.1}, {"x": 0.3, "y": 0.1},
                 {"x": 0.3, "y": 0.2}, {"x": 0.1, "y": 0.2}],
    }}), encoding="utf-8")
    manager = ROIManager.load(named)
    assert manager.get("gold") is not None
    assert manager.extract_all(_gradient() * 0 + 40)["gold"].shape == (10, 40, 3)

    ordered = tmp_path / "ordered.json"
    ordered.write_text(json.dumps([[[0.1, 0.1], [0.2, 0.1], [0.2, 0.2], [0.1, 0.2]]]),
                       encoding="utf-8")
    manager = ROIManager.load(ordered)
    assert manager.get_by_index(0).name == "roi_0"
