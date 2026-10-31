"""Region-of-interest definitions loaded from ``rois.json`` (future gold/HP/level work).

Coordinates are normalised (0..1) so one file works at any resolution. Corners are
stored in click order by the labeler, which used to produce garbage: the old code fed
those four points straight into ``getPerspectiveTransform``, and a wrong winding yields a
folded image. :meth:`ROI.extract` now just crops the bounding box when the quad is
axis-aligned (which every HUD region is), so click order cannot matter.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

import cv2
import numpy as np

AXIS_ALIGNED_TOLERANCE_PX = 1.5


@dataclass
class ROI:
    """A (usually rectangular) region of interest, normalised to the frame size."""

    name: str
    points: list[tuple[float, float]]

    def to_pixel_coords(self, frame_w: int, frame_h: int) -> list[tuple[int, int]]:
        return [(int(round(x * frame_w)), int(round(y * frame_h))) for x, y in self.points]

    def ordered(self, frame_w: int, frame_h: int) -> list[tuple[int, int]]:
        """Corners as top-left, top-right, bottom-right, bottom-left (whatever the click order)."""
        pts = self.to_pixel_coords(frame_w, frame_h)
        if len(pts) != 4:
            return pts
        by_y = sorted(pts, key=lambda p: (p[1], p[0]))
        top = sorted(by_y[:2], key=lambda p: p[0])
        bottom = sorted(by_y[2:], key=lambda p: p[0])
        return [top[0], top[1], bottom[1], bottom[0]]

    def bounding_box(self, frame_w: int, frame_h: int) -> tuple[int, int, int, int]:
        """``(x, y, w, h)`` in pixels, clamped to the frame."""
        xs = [p[0] for p in self.points]
        ys = [p[1] for p in self.points]
        x0 = max(0, min(frame_w, int(round(min(xs) * frame_w))))
        y0 = max(0, min(frame_h, int(round(min(ys) * frame_h))))
        x1 = max(0, min(frame_w, int(round(max(xs) * frame_w))))
        y1 = max(0, min(frame_h, int(round(max(ys) * frame_h))))
        return (x0, y0, max(1, x1 - x0), max(1, y1 - y0))

    def is_axis_aligned(self, frame_w: int, frame_h: int) -> bool:
        if len(self.points) != 4:
            return False
        tl, tr, br, bl = self.ordered(frame_w, frame_h)
        tol = AXIS_ALIGNED_TOLERANCE_PX
        return (abs(tl[1] - tr[1]) <= tol and abs(bl[1] - br[1]) <= tol
                and abs(tl[0] - bl[0]) <= tol and abs(tr[0] - br[0]) <= tol)

    def extract(self, frame: np.ndarray) -> np.ndarray:
        """Cut the ROI out of ``frame`` (BGR)."""
        h, w = frame.shape[:2]
        x, y, bw, bh = self.bounding_box(w, h)
        if self.is_axis_aligned(w, h):
            return frame[y:y + bh, x:x + bw].copy()
        src = np.array(self.ordered(w, h), dtype=np.float32)
        dst = np.array([[0, 0], [bw, 0], [bw, bh], [0, bh]], dtype=np.float32)
        matrix = cv2.getPerspectiveTransform(src, dst)
        return cv2.warpPerspective(frame, matrix, (bw, bh))


class ROIManager:
    """A named (or ordered) bag of ROIs for one video resolution."""

    def __init__(self, rois: dict[str, ROI] | Iterable[ROI]):
        if isinstance(rois, dict):
            self.rois: dict[str, ROI] = dict(rois)
        else:
            self.rois = {roi.name: roi for roi in rois}
        self.roi_order: list[str] = list(self.rois)

    @classmethod
    def load(cls, path: str | Path) -> "ROIManager":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        raw = data.get("rois", data) if isinstance(data, dict) else data
        if isinstance(raw, dict):
            return cls({name: ROI(name, _points(points)) for name, points in raw.items()})
        return cls(ROI(f"roi_{i}", _points(points)) for i, points in enumerate(raw))

    def get(self, name: str) -> Optional[ROI]:
        return self.rois.get(name)

    def get_by_index(self, index: int) -> Optional[ROI]:
        if 0 <= index < len(self.roi_order):
            return self.rois[self.roi_order[index]]
        return None

    def extract_all(self, frame: np.ndarray) -> dict[str, np.ndarray]:
        return {name: roi.extract(frame) for name, roi in self.rois.items()}

    def validate(self, required: Iterable[str]) -> list[str]:
        return [name for name in required if name not in self.rois]


def _points(raw) -> list[tuple[float, float]]:
    """Accepts ``[{"x": .., "y": ..}, ...]`` and ``[[x, y], ...]``."""
    out = []
    for p in raw:
        if isinstance(p, dict):
            out.append((float(p["x"]), float(p["y"])))
        else:
            out.append((float(p[0]), float(p[1])))
    return out
