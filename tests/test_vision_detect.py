"""Tests for the round detector: the state machine first, then a real end-to-end scan."""
from __future__ import annotations

import shutil

import pytest

pytest.importorskip("cv2")

from tftreview.rounds import RoundConfig, default_config  # noqa: E402
from tftreview.vision.detect import IconTracker, detect  # noqa: E402
from tftreview.vision.hud import Observation  # noqa: E402

from synth import make_hud_video  # noqa: E402

ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None,
                            reason="ffmpeg is needed to build and scan the test video")


def obs(n_icons: int, index: int, confidence: float = 0.4) -> Observation:
    return Observation(n_icons=n_icons, index=index, confidence=confidence)


def feed_n(tracker: IconTracker, frame: int, reading: Observation, n: int, step: int = 1,
           ) -> list:
    """Feed the same reading ``n`` frames; returns any events that came out."""
    out = []
    for k in range(n):
        det = tracker.feed(frame + k * step, reading)
        if det is not None:
            out.append(det)
    return out


def test_first_round_needs_three_frames_and_reports_the_first_one():
    tracker = IconTracker(default_config(), [4, 7], fps=60)
    assert feed_n(tracker, 100, obs(4, 0), 1) == []
    assert feed_n(tracker, 101, obs(4, 0), 1) == []
    det = tracker.feed(102, obs(4, 0))
    assert det is not None
    assert det.label == "1-1" and det.frame == 100      # first frame of the winning run
    assert det.time_ms == pytest.approx(round(100 * 1000 / 60))


def test_walks_the_whole_stage_in_order():
    tracker = IconTracker(default_config(), [4, 7], fps=60)
    labels, frames = [], []
    for i in range(4):
        events = feed_n(tracker, i * 100, obs(4, i), 3)
        assert len(events) == 1
        labels.append(events[0].label)
        frames.append(events[0].frame)
    assert labels == ["1-1", "1-2", "1-3", "1-4"]
    assert frames == [0, 100, 200, 300]


def test_a_single_frame_glitch_is_ignored():
    tracker = IconTracker(default_config(), [4, 7], fps=60)
    feed_n(tracker, 0, obs(4, 0), 3)
    tracker.feed(10, obs(4, 2))                         # one frame of nonsense
    assert feed_n(tracker, 11, obs(4, 0), 10) == []
    assert [e.label for e in tracker.events] == ["1-1"]
    events = feed_n(tracker, 30, obs(4, 3), 3)          # a stable reading still lands
    assert [e.label for e in events] == ["1-4"]


def test_a_backwards_reading_never_rewinds_the_sequence():
    tracker = IconTracker(default_config(), [4, 7], fps=60)
    for i in range(3):
        feed_n(tracker, i * 50, obs(4, i), 3)
    assert feed_n(tracker, 200, obs(4, 1), 10) == []
    assert [e.label for e in tracker.events] == ["1-1", "1-2", "1-3"]


def test_stage_change_with_a_new_icon_count_needs_three_frames():
    config = RoundConfig("t", {1: 4, 2: 7})
    tracker = IconTracker(config, [4, 7], fps=60)
    for i in range(4):
        feed_n(tracker, i * 50, obs(4, i), 3)
    assert feed_n(tracker, 500, obs(7, 0), 2) == []
    det = tracker.feed(502, obs(7, 0))
    assert det is not None and det.label == "2-1" and det.frame == 500


def test_same_icon_count_rollover_needs_the_long_confirmation():
    """2-7 -> 3-1 looks exactly like a glitch for one frame, so it must hold ~0.5 s."""
    config = RoundConfig("t", {1: 2, 2: 2})
    tracker = IconTracker(config, [2], fps=60, confirm_rollover_s=0.5)
    feed_n(tracker, 0, obs(2, 0), 3)
    feed_n(tracker, 50, obs(2, 1), 3)
    assert feed_n(tracker, 100, obs(2, 0), 29) == []
    det = tracker.feed(129, obs(2, 0))
    assert det is not None and det.label == "2-1" and det.frame == 100


def test_finishing_the_last_round_marks_the_tracker_done():
    config = RoundConfig("t", {1: 2})
    tracker = IconTracker(config, [2], fps=60)
    feed_n(tracker, 0, obs(2, 0), 3)
    feed_n(tracker, 50, obs(2, 1), 3)
    assert feed_n(tracker, 100, obs(2, 0), 3) == []
    assert tracker.finished
    assert tracker.feed(200, obs(2, 0)) is None


# ------------------------------------------------------------- end-to-end scan
@ffmpeg
def test_detect_finds_rounds_in_a_real_video(tmp_path):
    # 20 frames per round: 1-1, 1-2, 1-3 at 0.00 s, 0.33 s, 0.67 s.
    lit = [0] * 20 + [1] * 20 + [2] * 20
    video = make_hud_video(tmp_path / "hud.mp4", lit)

    result = detect(video)
    found = [(e.label, e.frame, e.time_ms) for e in result.events]
    assert found == [("1-1", 0, 0), ("1-2", 20, 333), ("1-3", 40, 667)]
    assert result.frames_scanned == 60
    assert "scan stopped early" not in " ".join(result.warnings)


@ffmpeg
def test_detect_ignores_a_one_frame_glitch_in_a_real_video(tmp_path):
    lit = [0] * 20 + [2] + [0] * 9 + [1] * 20
    video = make_hud_video(tmp_path / "glitch.mp4", lit)

    result = detect(video)
    assert [e.label for e in result.events] == ["1-1", "1-2"]
    assert result.events[1].frame == 30
