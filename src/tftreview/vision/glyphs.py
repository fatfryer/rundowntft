"""Digit segmentation and template matching for the TFT HUD.

The HUD uses two bitmap fonts and no OCR library is needed (or available): glyphs are
cut out by thresholding the white HUD text, normalised to a fixed grid, and compared to
a small set of templates.

Templates are **bootstrapped from labelled examples**, not drawn by hand: run the reader
over frames whose values are known (the reviewer's own notes), assign every segmented
glyph its expected digit from the number's string, and average the glyphs of each digit.
That is how ``glyph_templates.npz`` next to this module was produced; see
``scripts/build_glyph_templates.py``. Keeping the learning step in the code means the
templates can be rebuilt for another resolution or patch without touching a paint program.

Two fonts are used:

``font16``
    gold, level and the own player's HP number. White, 16 px tall, on the dark HUD.
``font12``
    the XP line (``current/needed``). Dimmer and only 12 px tall; it needs a looser
    brightness threshold, which is why the font spec carries its own mask limits.

Masks are stored as boolean ink maps cropped to the glyph's bounding box and resized to
``spec.size``. Comparing two glyphs is then just pixel agreement, which is robust enough
for a fixed, pixel-aligned renderer and needs no training.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Mapping, Optional

import cv2
import numpy as np

TEMPLATE_PATH = Path(__file__).with_name("glyph_templates.npz")


@dataclass(frozen=True)
class FontSpec:
    """How to find and normalise the glyphs of one HUD font."""

    name: str
    size: tuple[int, int]              # normalised (width, height)
    min_h: int
    max_h: int
    min_w: int
    max_w: int
    min_area: int = 6
    v_min: int = 170                   # HSV value floor for "white text"
    s_max: int = 85                    # HSV saturation ceiling
    min_score: float = 0.72            # below this a glyph is "unknown"
    min_margin: float = 0.0            # best-vs-runner-up; 0 keeps every best match

    def scaled(self, sx: float, sy: float) -> "FontSpec":
        """Same font for a frame that is not 1080p tall."""
        scale = min(sx, sy)
        return FontSpec(
            name=self.name, size=self.size,
            min_h=max(1, round(self.min_h * scale)),
            max_h=max(1, round(self.max_h * scale)),
            min_w=max(1, round(self.min_w * scale)),
            max_w=max(1, round(self.max_w * scale)),
            min_area=max(1, round(self.min_area * scale * scale)),
            v_min=self.v_min, s_max=self.s_max,
            min_score=self.min_score, min_margin=self.min_margin,
        )


FONT16 = FontSpec("font16", (12, 16), min_h=13, max_h=20, min_w=2, max_w=13)
FONT12 = FontSpec("font12", (8, 12), min_h=9, max_h=14, min_w=1, max_w=9,
                  v_min=110, s_max=255)


@dataclass(frozen=True)
class Glyph:
    """One connected piece of HUD text, plus its absolute position."""

    x: int
    y: int
    w: int
    h: int
    mask: np.ndarray                   # bool, cropped to (h, w)

    @property
    def right(self) -> int:
        return self.x + self.w

    @property
    def bottom(self) -> int:
        return self.y + self.h

    def normalised(self, size: tuple[int, int]) -> np.ndarray:
        return cv2.resize(self.mask.astype(np.float32), size,
                          interpolation=cv2.INTER_AREA)


def ink_mask(bgr: np.ndarray, spec: FontSpec) -> np.ndarray:
    """Boolean mask of the white-ish HUD text in a BGR crop."""
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    return cv2.inRange(hsv, np.array([0, 0, spec.v_min], np.uint8),
                       np.array([179, spec.s_max, 255], np.uint8)) > 0


def segment(bgr: np.ndarray, box: tuple[int, int, int, int], spec: FontSpec, *,
            max_width: Optional[int] = None) -> list[Glyph]:
    """Cut the glyphs out of ``box`` (``x, y, x1, y1``), left to right.

    ``max_width`` is an extra guard for widgets where a wide blob means two digits have
    merged (blur during combat): rejecting it makes the caller retry a later frame
    instead of reading a wrong number.
    """
    x0, y0, x1, y1 = _clamp_box(box, bgr.shape)
    x1, y1 = max(x0 + 1, x1), max(y0 + 1, y1)
    crop = bgr[y0:y1, x0:x1]
    mask = ink_mask(crop, spec).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    pieces: list[Glyph] = []
    for i in range(1, count):
        x, y, w, h, area = (int(v) for v in stats[i])
        if not (spec.min_h <= h <= spec.max_h):
            continue
        if not (spec.min_w <= w <= spec.max_w):
            continue
        if area < spec.min_area:
            continue
        if max_width is not None and w > max_width:
            continue
        pieces.append(Glyph(x + x0, y + y0, w, h, labels[y:y + h, x:x + w] == i))
    return _merge_fragments(sorted(pieces, key=lambda g: g.x))


def _merge_fragments(glyphs: list[Glyph]) -> list[Glyph]:
    """Rejoin a glyph that anti-aliasing split into vertically stacked pieces."""
    out: list[Glyph] = []
    for g in glyphs:
        if out:
            prev = out[-1]
            overlap = min(prev.right, g.right) - max(prev.x, g.x)
            if overlap >= 0.5 * min(prev.w, g.w) and -2 <= g.y - prev.bottom <= 2:
                y0, y1 = prev.y, max(prev.bottom, g.bottom)
                x0, x1 = min(prev.x, g.x), max(prev.right, g.right)
                merged = np.zeros((y1 - y0, x1 - x0), dtype=bool)
                merged[prev.y - y0:prev.bottom - y0, prev.x - x0:prev.right - x0] |= prev.mask
                merged[g.y - y0:g.bottom - y0, g.x - x0:g.right - x0] |= g.mask
                out[-1] = Glyph(x0, y0, x1 - x0, y1 - y0, merged)
                continue
        out.append(g)
    return out


def _clamp_box(box: tuple[int, int, int, int], shape) -> tuple[int, int, int, int]:
    h, w = shape[:2]
    x0, y0, x1, y1 = box
    return (max(0, min(w, x0)), max(0, min(h, y0)),
            max(0, min(w, x1)), max(0, min(h, y1)))


def similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Fraction of pixels that agree once both are thresholded at 0.5."""
    return float(1.0 - np.mean((a > 0.5) != (b > 0.5)))


def classify(glyph: Glyph, templates: Mapping[str, np.ndarray],
             spec: FontSpec) -> tuple[Optional[str], float, float]:
    """Best digit for ``glyph`` plus ``(score, margin over runner-up)``."""
    if not templates:
        return None, 0.0, 0.0
    probe = glyph.normalised(spec.size)
    scores = sorted(((similarity(probe, t), digit) for digit, t in templates.items()),
                    reverse=True)
    best_score, best = scores[0]
    margin = best_score - scores[1][0] if len(scores) > 1 else best_score
    if best_score < spec.min_score or margin < spec.min_margin:
        return None, best_score, margin
    return best, best_score, margin


def read_number(glyphs: Iterable[Glyph], templates: Mapping[str, np.ndarray],
                spec: FontSpec) -> tuple[Optional[int], str, float, float]:
    """Read left-to-right glyphs as one integer.

    Returns ``(value, text, min_score, min_margin)``. ``value`` is ``None`` when there
    are no glyphs or any glyph is unknown - callers retry on a later frame rather than
    guessing.
    """
    text = ""
    scores: list[float] = []
    margins: list[float] = []
    for glyph in glyphs:
        digit, score, margin = classify(glyph, templates, spec)
        if digit is None:
            return None, text + "?", score, margin
        text += digit
        scores.append(score)
        margins.append(margin)
    if not text:
        return None, "", 0.0, 0.0
    return int(text), text, min(scores), min(margins)


def find_slash(glyphs: Iterable[Glyph]) -> Optional[int]:
    """Index of the ``/`` in an XP line, if one is present.

    The slash is the only narrow glyph whose ink drifts from top-right to bottom-left.
    A ``1`` is a straight vertical stroke; digits are wider and only wobble by a pixel.
    """
    for i, g in enumerate(glyphs):
        if not (4 <= g.w <= 6 and g.h >= 8):
            continue
        rows = [np.flatnonzero(row) for row in g.mask]
        rows = [r for r in rows if len(r)]
        if len(rows) < 6:
            continue
        drift = float(np.mean([r.mean() for r in rows[-3:]])) - \
            float(np.mean([r.mean() for r in rows[:3]]))
        if drift <= -1.5:
            return i
    return None


# --------------------------------------------------------------------- templates
def templates_from_examples(examples: Iterable[tuple[Glyph, str, FontSpec]]
                            ) -> dict[str, dict[str, np.ndarray]]:
    """Average labelled glyphs into one template per digit, per font."""
    acc: dict[str, dict[str, list[np.ndarray]]] = {}
    for glyph, digit, spec in examples:
        acc.setdefault(spec.name, {}).setdefault(digit, []).append(
            glyph.normalised(spec.size))
    out: dict[str, dict[str, np.ndarray]] = {}
    for font, by_digit in acc.items():
        out[font] = {digit: np.mean(stack, axis=0).astype(np.float32)
                     for digit, stack in by_digit.items()}
    return out


def save_templates(path: str | Path, templates: dict[str, dict[str, np.ndarray]]) -> str:
    path = str(path)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    arrays = {f"{font}:{digit}": arr for font, by_digit in templates.items()
              for digit, arr in by_digit.items()}
    np.savez_compressed(path, **arrays)
    return path


def load_templates(path: str | Path = TEMPLATE_PATH
                   ) -> dict[str, dict[str, np.ndarray]]:
    """Load the shipped templates, grouped as ``{font_name: {digit: bitmap}}``."""
    path = Path(path)
    if not path.exists():
        return {}
    out: dict[str, dict[str, np.ndarray]] = {}
    with np.load(path) as data:
        for key in data.files:
            font, _, digit = key.partition(":")
            out.setdefault(font, {})[digit] = data[key].astype(np.float32)
    return out


def digit_count(templates: Mapping[str, np.ndarray]) -> str:
    """Sorted digits available for a font (handy for error messages)."""
    return "".join(sorted(templates))
