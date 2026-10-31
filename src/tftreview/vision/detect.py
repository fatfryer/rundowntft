"""Detect round-start timestamps from the HUD icon row.

The pipeline is a single sequential decode of the video. For every frame we crop the
icon strip, score gold density on each candidate icon box, and feed the reading to a
small state machine that knows the round sequence (:class:`tftreview.rounds.RoundConfig`).

Why a state machine: the HUD is not always readable. The row briefly disappears during
transitions and sometimes for a second mid-fight, and a round counter is monotonic, so a
reading is only accepted when it fits the sequence *and* holds for a few frames. The
timestamp reported is the first frame of the winning run, i.e. the frame the HUD
switches to the new round - not the frame where we became sure about it.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from fractions import Fraction
from pathlib import Path
from typing import Callable, Iterable, Optional

import numpy as np

from ..rounds import RoundConfig, default_config
from . import hud
from .video import DecodeAborted, VideoMeta, VideoError, iter_frames, probe, save_frame

ProgressFn = Callable[[int, int, int], None]


@dataclass(frozen=True)
class Detection:
    """One round start found in the video."""

    label: str
    stage: int
    round: int
    frame: int
    time_ms: int
    confidence: float
    n_icons: int = 0
    round_type: str = ""
    method: str = "hud-icons"

    def meta(self) -> dict:
        return {"frame": self.frame, "confidence": round(self.confidence, 3),
                "method": self.method, "icons": self.n_icons}

    def as_dict(self) -> dict:
        return {"label": self.label, "stage": self.stage, "round": self.round,
                "frame": self.frame, "time_ms": self.time_ms,
                "confidence": round(self.confidence, 3), "n_icons": self.n_icons,
                "round_type": self.round_type, "method": self.method}


@dataclass
class DetectResult:
    meta: VideoMeta
    events: list[Detection] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    frames_scanned: int = 0
    seconds: float = 0.0
    first_decodable_frame: Optional[int] = None

    def as_dict(self) -> dict:
        return {
            "video": {"width": self.meta.width, "height": self.meta.height,
                      "fps": float(self.meta.fps), "frames": self.meta.frame_count,
                      "duration_s": round(self.meta.duration_s, 3)},
            "events": [e.as_dict() for e in self.events],
            "warnings": list(self.warnings),
            "frames_scanned": self.frames_scanned,
            "seconds": round(self.seconds, 2),
        }


@dataclass
class _Pending:
    key: tuple[str, int, int, int]
    first: int
    last: int = 0
    count: int = 1
    conf: float = 0.0
    need: int = 3


class IconTracker:
    """Turns per-frame HUD readings into round-start events, in sequence order."""

    def __init__(self, config: RoundConfig, candidate_counts: Iterable[int], *,
                 fps: float = 60.0, confirm_round: int = 3,
                 confirm_rollover_s: float = 0.5, first_stage: Optional[int] = None):
        self.config = config
        self.stages = dict(config.stages)
        self.candidates = sorted({int(c) for c in candidate_counts})
        self.stage = int(first_stage if first_stage is not None else min(self.stages))
        self.index = -1                     # no round seen yet
        self.confirm_round = max(1, int(confirm_round))
        self.confirm_rollover = max(self.confirm_round, round(confirm_rollover_s * fps))
        self.events: list[Detection] = []
        self.warnings: list[str] = []
        self.finished = False
        self._pending: Optional[_Pending] = None
        self._fps = fps
        self._ms = lambda frame: round(1000.0 * frame / fps)

    # ------------------------------------------------------------------ helpers
    @property
    def expected_n(self) -> int:
        return self.stages[self.stage]

    def _classify(self, obs: hud.Observation) -> Optional[tuple[str, int, int, int]]:
        """Does this reading fit the sequence? ``("round"|"stage"|"finish", stage, idx, n)``."""
        expected = self.stages[self.stage]
        if obs.n_icons == expected:
            if obs.index > self.index:
                return ("round", self.stage, obs.index, obs.n_icons)
            if obs.index == self.index:
                return None
            if obs.index == 0 and self.index == expected - 1:
                nxt = self.stage + 1
                if nxt not in self.stages:
                    return ("finish", self.stage, self.index, obs.n_icons)
                return ("stage", nxt, 0, obs.n_icons)
            return None
        nxt = self.stage + 1
        if self.stages.get(nxt) == obs.n_icons and obs.index == 0:
            return ("stage", nxt, 0, obs.n_icons)
        return None

    # --------------------------------------------------------------------- feed
    def feed(self, frame: int, obs: Optional[hud.Observation]) -> Optional[Detection]:
        if self.finished:
            return None
        if obs is None:
            self._pending = None
            return None

        kind = self._classify(obs)
        if kind is None:
            self._pending = None
            return None

        what, stage, index, n_icons = kind
        key = (what, stage, index, n_icons)
        # A stage change that keeps the same N (e.g. 2-7 -> 3-1, both 7 icons) is
        # indistinguishable from a HUD glitch on a single frame, so demand a long run.
        need = self.confirm_round
        if what == "stage" and n_icons == self.stages[self.stage]:
            need = self.confirm_rollover

        p = self._pending
        if p is None or p.key != key:
            self._pending = _Pending(key=key, first=frame, last=frame, count=1,
                                     conf=obs.confidence, need=need)
            return None
        p.count += 1
        p.last = frame
        p.conf += obs.confidence
        if p.count < p.need:
            return None

        self._pending = None
        if what == "finish":
            self.finished = True
            return None

        previous = (self.stage, self.index)
        self.stage, self.index = stage, index
        label = f"{stage}-{index + 1}"
        det = Detection(
            label=label, stage=stage, round=index + 1, frame=p.first,
            time_ms=self._ms(p.first), confidence=p.conf / max(1, p.count),
            n_icons=n_icons, round_type=self.config.type_for(label),
        )
        if previous[0] == stage and index > previous[1] + 1:
            self.warnings.append(
                f"{label}: jumped from {previous[0]}-{previous[1] + 1} "
                f"(rounds in between were never lit)")
        if previous[0] != stage:
            expected_stage = previous[0] + 1
            if stage != expected_stage:
                self.warnings.append(f"{label}: stage jumped from {previous[0]} to {stage}")
        self.events.append(det)
        return det


def detect(video_path: str | Path, config: Optional[RoundConfig] = None, *,
           start_ms: int = 0, end_ms: Optional[int] = None,
           progress: Optional[ProgressFn] = None, progress_every: int = 600,
           debug_dir: Optional[str | Path] = None,
           confirm_round: int = 3, confirm_rollover_s: float = 0.5,
           first_stage: Optional[int] = None,
           frame_stride: int = 1,
           threads: int = 0,
           ) -> DetectResult:
    """Scan ``video_path`` and return every round start found, in time order."""
    config = config or default_config()
    meta = probe(video_path)
    candidates = sorted(set(config.stages.values())) or [7]
    model = hud.HudModel.for_size(meta.width, meta.height, max_rounds=max(candidates))
    crop = model.strip_box

    start_frame = meta.frame_of_ms(start_ms) if start_ms else 0
    end_frame = meta.frame_of_ms(end_ms) if end_ms else meta.frame_count
    total = max(0, end_frame - start_frame)
    tracker = IconTracker(config, candidates, fps=meta.fps_float,
                          confirm_round=confirm_round,
                          confirm_rollover_s=confirm_rollover_s,
                          first_stage=first_stage)
    result = DetectResult(meta=meta)
    started = time.monotonic()
    done = 0
    cursor = start_frame
    attempts = 0
    reached_eof = False

    while cursor < end_frame and attempts < 64 and not tracker.finished:
        attempts += 1
        try:
            for frame, strip in iter_frames(video_path, meta, crop=crop,
                                            start_frame=cursor, end_frame=end_frame,
                                            frame_stride=frame_stride, threads=threads):
                if result.first_decodable_frame is None:
                    result.first_decodable_frame = frame
                obs = hud.observe(model, strip, candidates)
                det = tracker.feed(frame, obs)
                if det is not None and debug_dir:
                    save_frame(hud.draw_debug(strip, model, observations=candidates),
                               Path(debug_dir) / f"{det.frame:07d}_{det.label}.png")
                done += 1
                cursor = frame + 1
                if tracker.finished:
                    break
                if progress and total and (done % progress_every == 0 or done >= total):
                    progress(done, total, len(tracker.events))
            else:
                cursor = end_frame          # clean end of stream
                reached_eof = True
        except DecodeAborted as exc:
            # ffmpeg aborts outright on some corrupt AV1 packets. Salvage what we have:
            # resume right after the last good frame, or skip a second of lead-in when
            # even the first packet was bad (common for streams cut mid-round).
            if exc.last_frame is not None:
                result.warnings.append(
                    f"undecodable frames after frame {exc.last_frame}; resumed at "
                    f"{exc.last_frame + 1}")
                cursor = exc.last_frame + 1
            else:
                skip = max(1, round(meta.fps_float))
                result.warnings.append(
                    f"frames {cursor}-{cursor + skip - 1} are not decodable; skipped them")
                cursor += skip

    result.events = tracker.events
    result.warnings = tracker.warnings + result.warnings
    result.frames_scanned = done
    result.seconds = time.monotonic() - started

    _clamp_lead_in(result, config, start_frame, confirm_round)

    if total > 0 and done < total and not tracker.finished and not reached_eof:
        result.warnings.append(f"scan stopped early after {done} of {total} frames")
    if done and not result.events:
        result.warnings.append(
            "no round icons were detected - wrong video, HUD not visible, or --from too late")
    return result


def _clamp_lead_in(result: DetectResult, config: RoundConfig, start_frame: int,
                   confirm_round: int) -> None:
    """If the video's head is undecodable, report the round that was already running at 0:00."""
    if not result.events or start_frame != 0:
        return
    first = result.events[0]
    if first.frame == 0 or first.round != 1:
        return
    if result.first_decodable_frame is None or first.frame > result.first_decodable_frame + confirm_round:
        return
    if first.stage != min(config.stages):
        return
    result.events[0] = replace(first, frame=0, time_ms=0)
    result.warnings.append(
        f"{first.label} was already in progress at the first decodable frame; "
        f"reported at 0:00 instead of {first.time_ms / 1000:.3f}s")
