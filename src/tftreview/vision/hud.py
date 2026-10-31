"""Where the round icons live, and how "lit" they are.

The TFT HUD draws one small icon per round of the current stage in a row that is
centred horizontally at the top of the screen, and highlights the round in progress in
gold. Two consequences shape everything here:

* The row re-centres when the number of rounds changes, so icon positions depend on
  ``N = len(rounds in this stage)``. Nothing here uses fixed per-icon ROIs.
* Only one icon is gold at a time, and the inactive ones are effectively grey. That
  makes "which round is it" a one-dimensional gold-density measurement, no OCR.

Measured on a 1080p VOD: pitch 37.16 px, row centred at x=960.1, icon band y=6..30.
The whole gold *line* under the icons sits at y=38, outside the band on purpose.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

import cv2
import numpy as np

# Reference geometry, in pixels at 1920x1080.
REF_PITCH = 37.16
REF_ICON_HALF = 8.0            # the icon boxes are 16 px wide
REF_BAND_TOP = 6.0
REF_BAND_BOTTOM = 30.0
REF_MARGIN = 10.0

# Gold of the active-pip, in OpenCV HSV (H 0-179). Measured: active 28-34 % of the box,
# inactive 0.0 %. The old detector's (15,80,80)-(35,255,255) missed the real gold.
GOLD_LO = (10, 100, 110)
GOLD_HI = (45, 255, 255)

LIT_MIN = 0.12                 # a pip counts as lit at >= 12 % gold pixels
CONFIDENT = 0.20               # winner-vs-runner-up gap that makes an observation trustworthy


@dataclass(frozen=True)
class HudModel:
    """Icon-row geometry for one video resolution, scaled from the 1080p reference."""

    width: int
    height: int
    max_rounds: int = 7
    pitch: float = REF_PITCH
    icon_half: float = REF_ICON_HALF
    band_top: float = REF_BAND_TOP
    band_bottom: float = REF_BAND_BOTTOM
    margin: float = REF_MARGIN

    @classmethod
    def for_size(cls, width: int, height: int, max_rounds: int = 7) -> "HudModel":
        sx, sy = width / 1920.0, height / 1080.0
        return cls(
            width=int(width), height=int(height), max_rounds=int(max_rounds),
            pitch=REF_PITCH * sx, icon_half=REF_ICON_HALF * sx,
            band_top=REF_BAND_TOP * sy, band_bottom=REF_BAND_BOTTOM * sy,
            margin=REF_MARGIN * sx,
        )

    # ----------------------------------------------------------------- geometry
    @property
    def strip_box(self) -> tuple[int, int, int, int]:
        """``(x, y, w, h)`` of the one crop that covers every possible icon row."""
        half = ((self.max_rounds - 1) / 2.0) * self.pitch + self.icon_half + self.margin
        x0 = int(round(self.width / 2.0 - half))
        x1 = int(round(self.width / 2.0 + half))
        y0 = int(round(self.band_top))
        y1 = int(round(self.band_bottom))
        x0 = max(0, x0)
        y0 = max(0, y0)
        return (x0, y0, max(1, x1 - x0), max(1, y1 - y0))

    def centers(self, n_icons: int) -> list[float]:
        """Icon centre x for a stage with ``n_icons`` rounds."""
        return [self.width / 2.0 + (i - (n_icons - 1) / 2.0) * self.pitch
                for i in range(n_icons)]

    def slot_boxes(self, n_icons: int) -> list[tuple[int, int, int, int]]:
        """Absolute-pixel ``(x0, y0, x1, y1)`` boxes, one per round icon."""
        top = int(round(self.band_top))
        bottom = int(round(self.band_bottom))
        out = []
        for cx in self.centers(n_icons):
            out.append((int(round(cx - self.icon_half)), top,
                        int(round(cx + self.icon_half)), bottom))
        return out

    # ------------------------------------------------------------------ scoring
    def gold_mask(self, strip: np.ndarray) -> np.ndarray:
        hsv = cv2.cvtColor(strip, cv2.COLOR_BGR2HSV)
        return cv2.inRange(hsv, np.array(GOLD_LO, np.uint8), np.array(GOLD_HI, np.uint8)) > 0

    def scores(self, strip: np.ndarray, n_icons: int) -> list[float]:
        """Fraction of gold pixels in each icon box (0..1). Index = round - 1."""
        mask = self.gold_mask(strip)
        return self.scores_from_mask(mask, n_icons)

    def scores_from_mask(self, mask: np.ndarray, n_icons: int,
                         x_offset: Optional[int] = None) -> list[float]:
        """Same as :meth:`scores` but reuses an already-computed mask (one per frame)."""
        if x_offset is None:
            x_offset = self.strip_box[0]
        cols = mask.sum(axis=0)
        band_h = mask.shape[0]
        out: list[float] = []
        for x0, _y0, x1, _y1 in self.slot_boxes(n_icons):
            a = max(0, x0 - x_offset)
            b = min(mask.shape[1], max(a + 1, x1 - x_offset))
            out.append(float(cols[a:b].sum()) / float((b - a) * band_h))
        return out

    def column_profile(self, strip: np.ndarray) -> np.ndarray:
        """Gold density per column of the strip - for debugging the geometry."""
        mask = self.gold_mask(strip)
        return mask.sum(axis=0) / float(max(1, mask.shape[0]))


@dataclass(frozen=True)
class Observation:
    """What the HUD says at one frame: ``n_icons`` rounds, ``index`` of the lit one."""

    n_icons: int
    index: int
    confidence: float
    scores: tuple[float, ...] = field(default=())

    @property
    def round_number(self) -> int:
        return self.index + 1


def observe(model: HudModel, strip: np.ndarray, candidate_counts: Iterable[int],
            lit_min: float = LIT_MIN) -> Optional[Observation]:
    """Best reading of the icon row, or ``None`` when no row can be read.

    Every plausible ``N`` (stage round count) is scored, because the row position of
    index *i* depends on ``N``; picking the best-separated interpretation is what makes
    stage changes detectable without knowing the stage up front.
    """
    mask = model.gold_mask(strip)
    best: Optional[Observation] = None
    for n in sorted(set(int(c) for c in candidate_counts)):
        if n <= 0:
            continue
        s = model.scores_from_mask(mask, n)
        order = sorted(range(len(s)), key=lambda i: s[i], reverse=True)
        top = s[order[0]]
        if top < lit_min:
            continue
        runner_up = s[order[1]] if len(order) > 1 else 0.0
        conf = top - runner_up
        if best is None or conf > best.confidence:
            best = Observation(n_icons=n, index=order[0], confidence=conf, scores=tuple(s))
    return best


def draw_debug(strip: np.ndarray, model: HudModel, n_icons: Optional[int] = None,
               observations: Sequence[int] = ()) -> np.ndarray:
    """Annotated copy of the strip: icon boxes for each ``N`` plus the lit density."""
    canvas = strip.copy()
    for n in (observations or ((n_icons,) if n_icons else ())):
        scores = model.scores(strip, n)
        x_off = model.strip_box[0]
        y_off = model.strip_box[1]
        for i, (x0, y0, x1, y1) in enumerate(model.slot_boxes(n)):
            x0, x1 = x0 - x_off, x1 - x_off
            y0, y1 = y0 - y_off, min(canvas.shape[0], y1 - y_off)
            colour = (0, 255, 0) if scores[i] >= LIT_MIN else (80, 80, 80)
            cv2.rectangle(canvas, (x0, y0), (x1 - 1, max(y0, y1 - 1)), colour, 1)
            cv2.putText(canvas, f"{scores[i] * 100:.0f}", (x0, max(8, y0 - 1)),
                        cv2.FONT_HERSHEY_PLAIN, 0.6, colour, 1, cv2.LINE_AA)
    return canvas
