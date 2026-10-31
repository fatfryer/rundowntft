"""Read the numeric HUD widgets: gold, level, XP and the player's HP.

All geometry is expressed at 1920x1080 and scaled to the video, because the HUD is a
fixed overlay that only changes with resolution/UI scale. The numbers themselves are read
by :mod:`tftreview.vision.glyphs`.

The one non-obvious widget is HP. The roster on the right shows every player's HP in a
small font, but the *own* row is rendered with a larger 16 px number at a fixed x. That
size difference is the row locator: no portrait matching or highlight-colour heuristics
(it is the same reason the player's own card is easy to spot by eye). The locator scans
the eight known row positions for 16 px glyphs and takes the unique row that has them.

Sampling a round is a scan, not a single frame: round starts are sometimes covered by an
augment overlay, and combat can tint the text. :func:`sample_round` walks the round in
10-frame steps and returns the first reading that a second reading 10 frames later
confirms.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Iterable, Iterator, Mapping, Optional

import numpy as np

from . import glyphs
from .glyphs import FONT12, FONT16, FontSpec, Glyph
from .video import VideoMeta, iter_frames, probe

# ---------------------------------------------------------------- 1080p geometry
REF_W, REF_H = 1920, 1080

#: gold digits (the coin sits to the left of x=1010 and is not white)
GOLD_BOX = (995, 882, 1095, 912)
#: a gold digit's right edge must be right of this. The coin icon lives at x<=1021 and
#: its white highlight can grow to full glyph height while it spins at round start, so
#: the box starts over the coin and the coin fragment is dropped by this rule instead.
GOLD_MIN_RIGHT = 1023
#: the single level digit, right of the "Lvl" label
LEVEL_BOX = (388, 882, 436, 912)
#: the ``current/needed`` XP line
XP_BOX = (470, 884, 532, 906)
#: horizontal band that contains the own player's large HP number in the roster
HP_BOX_X = (1775, 1832)
#: y offset of that number inside a roster row
HP_BOX_DY = (18, 62)
#: top of each of the eight roster rows
HP_ROW_TOPS = (158, 240, 318, 397, 480, 555, 646, 729)


@dataclass(frozen=True)
class Metrics:
    """One frame's worth of HUD numbers. ``None`` means "not readable here"."""

    gold: Optional[int] = None
    level: Optional[int] = None
    xp: Optional[int] = None
    xp_next: Optional[int] = None
    hp: Optional[int] = None
    own_slot: Optional[int] = None
    scores: Mapping[str, float] = field(default_factory=dict)
    margins: Mapping[str, float] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        return None not in (self.gold, self.level, self.xp, self.xp_next, self.hp)

    def values(self) -> tuple[Optional[int], ...]:
        return (self.gold, self.level, self.xp, self.xp_next, self.hp)

    def as_dict(self) -> dict:
        return {"gold": self.gold, "level": self.level, "xp": self.xp,
                "xp_next": self.xp_next, "hp": self.hp, "own_slot": self.own_slot,
                "scores": {k: round(v, 3) for k, v in self.scores.items()},
                "margins": {k: round(v, 3) for k, v in self.margins.items()}}

    def same_values(self, other: "Metrics") -> bool:
        return self.values() == other.values()


class MetricsReader:
    """Reads one frame of HUD numbers, with glyph templates loaded once."""

    def __init__(self, templates: Optional[Mapping[str, Mapping[str, np.ndarray]]] = None,
                 width: int = REF_W, height: int = REF_H,
                 template_path: str | Path = glyphs.TEMPLATE_PATH):
        self.templates = dict(templates) if templates is not None else glyphs.load_templates(template_path)
        self.width, self.height = int(width), int(height)
        self.sx = self.width / REF_W
        self.sy = self.height / REF_H
        self.font16: FontSpec = FONT16.scaled(self.sx, self.sy)
        self.font12: FontSpec = FONT12.scaled(self.sx, self.sy)
        self.gold_box = _scale_box(GOLD_BOX, self.sx, self.sy)
        self.gold_min_right = round(GOLD_MIN_RIGHT * self.sx)
        self.level_box = _scale_box(LEVEL_BOX, self.sx, self.sy)
        self.xp_box = _scale_box(XP_BOX, self.sx, self.sy)
        self.hp_box_x = (round(HP_BOX_X[0] * self.sx), round(HP_BOX_X[1] * self.sx))
        self.hp_rows = [round(y * self.sy) for y in HP_ROW_TOPS]
        self.hp_dy = (round(HP_BOX_DY[0] * self.sy), round(HP_BOX_DY[1] * self.sy))

    @classmethod
    def for_meta(cls, meta: VideoMeta, **kwargs) -> "MetricsReader":
        return cls(width=meta.width, height=meta.height, **kwargs)

    # ------------------------------------------------------------------ widgets
    def read(self, frame: np.ndarray) -> Metrics:
        scores: dict[str, float] = {}
        margins: dict[str, float] = {}

        gold_glyphs = [g for g in glyphs.segment(frame, self.gold_box, self.font16,
                                                 max_width=self.font16.max_w)
                       if g.right > self.gold_min_right]
        gold, _, gs, gm = glyphs.read_number(gold_glyphs, self.templates.get("font16", {}),
                                             self.font16)
        _record("gold", gold, gs, gm, scores, margins)

        level_glyphs = glyphs.segment(frame, self.level_box, self.font16)
        level, _, ls, lm = glyphs.read_number(level_glyphs, self.templates.get("font16", {}),
                                              self.font16)
        _record("level", level, ls, lm, scores, margins)

        xp, xp_next, xs, xm = self._read_xp(frame)
        _record("xp", xp, xs, xm, scores, margins)
        _record("xp_next", xp_next, xs, xm, scores, margins)

        hp, own_slot, hs, hm = self._read_hp(frame)
        _record("hp", hp, hs, hm, scores, margins)

        return Metrics(gold=gold, level=level, xp=xp, xp_next=xp_next, hp=hp,
                       own_slot=own_slot, scores=scores, margins=margins)

    def _read_xp(self, frame: np.ndarray) -> tuple[Optional[int], Optional[int], float, float]:
        found = glyphs.segment(frame, self.xp_box, self.font12)
        slash = glyphs.find_slash(found)
        if slash is None:
            return None, None, 0.0, 0.0
        templates = self.templates.get("font12", {})
        current, _, cs, cm = glyphs.read_number(found[:slash], templates, self.font12)
        needed, _, ns, nm = glyphs.read_number(found[slash + 1:], templates, self.font12)
        return current, needed, min(cs, ns), min(cm, nm)

    def label_examples(self, frame: np.ndarray, values: Mapping[str, int],
                       widgets: Iterable[str] = ("gold", "level", "xp", "hp"),
                       ) -> tuple[list[tuple[Glyph, str, FontSpec]], list[str]]:
        """Cut labelled glyphs out of a frame whose HUD values are already known.

        This is the mirror of :meth:`read`: instead of classifying, it trusts ``values``
        (the reviewer's notes) and pairs every segmented glyph with the expected digit.
        It returns the examples plus a note for each widget that could not be used, e.g.
        because the glyph count did not match (blur, mid-transition, wrong value).

        Used to rebuild the shipped templates; see ``scripts/build_glyph_templates.py``.
        """
        examples: list[tuple[Glyph, str, FontSpec]] = []
        skipped: list[str] = []

        def take(name: str, found: list[Glyph], text: str, spec: FontSpec) -> None:
            if len(found) != len(text):
                skipped.append(f"{name}: wanted {text!r}, segmented {len(found)} glyphs")
                return
            examples.extend((g, d, spec) for g, d in zip(found, text))

        wanted = set(widgets)
        if "gold" in wanted and values.get("gold") is not None:
            found = [g for g in glyphs.segment(frame, self.gold_box, self.font16,
                                               max_width=self.font16.max_w)
                     if g.right > self.gold_min_right]
            take("gold", found, str(int(values["gold"])), self.font16)
        if "level" in wanted and values.get("level") is not None:
            take("level", glyphs.segment(frame, self.level_box, self.font16),
                 str(int(values["level"])), self.font16)
        if "xp" in wanted and values.get("xp") is not None:
            found = glyphs.segment(frame, self.xp_box, self.font12)
            slash = glyphs.find_slash(found)
            if slash is None:
                skipped.append("xp: no slash")
            else:
                take("xp", found[:slash], str(int(values["xp"])), self.font12)
                if values.get("xp_next") is not None:
                    take("xp_next", found[slash + 1:], str(int(values["xp_next"])),
                         self.font12)
        if "hp" in wanted and values.get("hp") is not None:
            text = str(int(values["hp"]))
            hits = []
            for index, row_top in enumerate(self.hp_rows):
                box = (self.hp_box_x[0], row_top + self.hp_dy[0],
                       self.hp_box_x[1], row_top + self.hp_dy[1])
                found = glyphs.segment(frame, box, self.font16,
                                       max_width=self.font16.max_w)
                if len(found) == len(text):
                    hits.append(found)
            if len(hits) == 1:
                examples.extend((g, d, self.font16) for g, d in zip(hits[0], text))
            else:
                skipped.append(f"hp: {len(hits)} rows match {text!r}")
        return examples, skipped

    def _read_hp(self, frame: np.ndarray) -> tuple[Optional[int], Optional[int], float, float]:
        candidates: list[tuple[int, list[Glyph]]] = []
        for index, row_top in enumerate(self.hp_rows):
            box = (self.hp_box_x[0], row_top + self.hp_dy[0],
                   self.hp_box_x[1], row_top + self.hp_dy[1])
            found = glyphs.segment(frame, box, self.font16, max_width=self.font16.max_w)
            if found:
                candidates.append((index, found))
        if not candidates:
            return None, None, 0.0, 0.0
        candidates.sort(key=lambda item: len(item[1]), reverse=True)
        if len(candidates) > 1 and len(candidates[0][1]) == len(candidates[1][1]):
            return None, None, 0.0, 0.0          # two rows look like the own row: refuse
        index, found = candidates[0]
        templates = self.templates.get("font16", {})
        hp, _, score, margin = glyphs.read_number(found, templates, self.font16)
        return hp, index + 1, score, margin


def _record(name: str, value: Optional[int], score: float, margin: float,
            scores: dict[str, float], margins: dict[str, float]) -> None:
    if value is not None:
        scores[name] = score
        margins[name] = margin


def _scale_box(box: tuple[int, int, int, int], sx: float, sy: float
               ) -> tuple[int, int, int, int]:
    return (round(box[0] * sx), round(box[1] * sy),
            round(box[2] * sx), round(box[3] * sy))


def read_metrics(frame: np.ndarray, reader: Optional[MetricsReader] = None) -> Metrics:
    """Convenience wrapper for a one-off frame."""
    return (reader or MetricsReader()).read(frame)


# ------------------------------------------------------------------- sampling
@dataclass(frozen=True)
class RoundSample:
    """The metrics found for one round, and where they were read."""

    frame: int
    time_ms: int
    metrics: Metrics
    attempts: int
    stable: bool

    def as_dict(self) -> dict:
        return {"frame": self.frame, "time_ms": self.time_ms,
                "attempts": self.attempts, "stable": self.stable,
                "metrics": self.metrics.as_dict()}


def iter_readings(video: str | Path, start_ms: int, end_ms: int, *,
                  meta: Optional[VideoMeta] = None,
                  reader: Optional[MetricsReader] = None,
                  step: int = 10) -> Iterator[tuple[int, Metrics]]:
    """Yield ``(frame, metrics)`` every ``step`` frames between two times."""
    meta = meta or probe(video)
    reader = reader or MetricsReader.for_meta(meta)
    start = max(0, meta.frame_of_ms(start_ms))
    end = min(meta.frame_count, meta.frame_of_ms(end_ms))
    if end <= start:
        return
    step = max(1, int(step))
    for frame, image in iter_frames(video, meta, start_frame=start, end_frame=end):
        if (frame - start) % step == 0:
            yield frame, reader.read(image)


def sample_round(video: str | Path, event_ms: int, next_ms: Optional[int] = None, *,
                 meta: Optional[VideoMeta] = None,
                 reader: Optional[MetricsReader] = None,
                 step: int = 10, max_wait_s: float = 45.0) -> Optional[RoundSample]:
    """Find the first stable, complete HUD reading of a round.

    Starts at the round's first frame and steps forward. A reading is accepted when the
    reading ``step`` frames later has identical values, so a HUD that is still showing
    the previous round (or is half-faded) is never taken. When nothing is stable before
    the next round starts, the last complete reading is returned with ``stable=False``.
    """
    meta = meta or probe(video)
    reader = reader or MetricsReader.for_meta(meta)
    start = max(0, meta.frame_of_ms(event_ms))
    hard_end = meta.frame_count
    if next_ms is not None:
        hard_end = min(hard_end, meta.frame_of_ms(next_ms))
    hard_end = min(hard_end, start + round(max_wait_s * meta.fps_float))
    step = max(1, int(step))
    if hard_end - start < step:
        return None

    previous: Optional[tuple[int, Metrics]] = None
    fallback: Optional[tuple[int, Metrics]] = None
    attempts = 0
    for frame, metrics in iter_readings(video, meta.ms_of(start), meta.ms_of(hard_end),
                                        meta=meta, reader=reader, step=step):
        attempts += 1
        if metrics.complete:
            fallback = (frame, metrics)
            if previous is not None and previous[1].same_values(metrics):
                return RoundSample(frame=previous[0], time_ms=meta.ms_of(previous[0]),
                                   metrics=previous[1], attempts=attempts, stable=True)
            previous = (frame, metrics)
        else:
            previous = None
    if fallback is None:
        return None
    return RoundSample(frame=fallback[0], time_ms=meta.ms_of(fallback[0]),
                       metrics=fallback[1], attempts=attempts, stable=False)


def find_value_frame(video: str | Path, start_ms: int, end_ms: int, key: str,
                     target: int, *, meta: Optional[VideoMeta] = None,
                     reader: Optional[MetricsReader] = None,
                     step: int = 10) -> Optional[int]:
    """First frame in a window where ``key`` reads exactly ``target`` (for validation)."""
    for frame, metrics in iter_readings(video, start_ms, end_ms, meta=meta,
                                        reader=reader, step=step):
        if getattr(metrics, key, None) == target:
            return frame
    return None


# ------------------------------------------------------------------ validation
#: the reference keys the reviewer enters by hand; ``xp_next`` is derived, not entered
REFERENCE_KEYS = ("gold", "level", "xp", "hp")


@dataclass(frozen=True)
class RoundCheck:
    """A round's start reading compared against the reviewer's reference values."""

    label: str
    event_ms: int
    sample: Optional[RoundSample]
    reference: Mapping[str, int] = field(default_factory=dict)
    late_frame: Optional[int] = None
    late_ms: Optional[int] = None
    late_metrics: Optional[Metrics] = None

    def matches(self, metrics: Optional[Metrics]) -> bool:
        if metrics is None:
            return False
        return all(getattr(metrics, key, None) == value
                   for key, value in self.reference.items())

    @property
    def verdict(self) -> str:
        """``ok`` (start reading matches), ``late`` (reference shows up later in the
        round), ``bad`` (never matches) or ``read``/``none`` when there is no reference."""
        if self.sample is None:
            return "none"
        if not self.reference:
            return "read"
        if self.matches(self.sample.metrics):
            return "ok"
        return "late" if self.late_metrics is not None else "bad"

    def as_dict(self) -> dict:
        return {
            "label": self.label, "event_ms": self.event_ms,
            "sample": self.sample.as_dict() if self.sample else None,
            "reference": dict(self.reference),
            "late_frame": self.late_frame, "late_ms": self.late_ms,
            "late_metrics": self.late_metrics.as_dict() if self.late_metrics else None,
            "verdict": self.verdict,
        }


def check_round(video: str | Path, event_ms: int, next_ms: Optional[int] = None, *,
                label: str = "", reference: Optional[Mapping[str, int]] = None,
                meta: Optional[VideoMeta] = None,
                reader: Optional[MetricsReader] = None,
                step: int = 10, max_wait_s: float = 45.0,
                late_wait_s: float = 60.0) -> RoundCheck:
    """Sample a round and, when it does not match the reference, look for the reference
    values later in the same round.

    The second pass exists because some rounds do not start with the values the reviewer
    typed: an augment round can open on the previous level, and gold can be spent right
    after the round starts. Finding the reference later proves the glyphs were read
    correctly and that the HUD simply moved on, which is different from a reading error.
    """
    meta = meta or probe(video)
    reader = reader or MetricsReader.for_meta(meta)
    reference = {k: int(v) for k, v in (reference or {}).items()
                 if k in REFERENCE_KEYS and v is not None}
    sample = sample_round(video, event_ms, next_ms, meta=meta, reader=reader,
                          step=step, max_wait_s=max_wait_s)
    check = RoundCheck(label=label, event_ms=event_ms, sample=sample,
                       reference=reference)
    if sample is None or not reference or check.matches(sample.metrics):
        return check

    start = max(0, meta.frame_of_ms(event_ms))
    end = start + round(late_wait_s * meta.fps_float)
    if next_ms is not None:
        end = min(end, meta.frame_of_ms(next_ms))
    end = min(end, meta.frame_count)
    for frame, metrics in iter_readings(video, meta.ms_of(start), meta.ms_of(end),
                                        meta=meta, reader=reader, step=step):
        if check.matches(metrics):
            return replace(check, late_frame=frame, late_ms=meta.ms_of(frame),
                           late_metrics=metrics)
    return check
