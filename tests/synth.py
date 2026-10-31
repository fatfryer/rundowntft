"""Helpers for vision tests: paint a fake TFT icon row and encode it with ffmpeg."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Iterable, Sequence

import cv2
import numpy as np

from tftreview.vision.hud import HudModel

# Pure gold; comfortably inside the detector's HSV window.
GOLD_BGR = (0, 215, 255)


def hud_frames(lit_rounds: Sequence[int], *, n_icons: int = 4,
               size: tuple[int, int] = (960, 540),
               max_rounds: int = 7) -> Iterable[np.ndarray]:
    """Yield black frames with one lit round icon, following ``lit_rounds`` (0-based)."""
    width, height = size
    model = HudModel.for_size(width, height, max_rounds=max_rounds)
    boxes = model.slot_boxes(n_icons)
    for index in lit_rounds:
        frame = np.zeros((height, width, 3), np.uint8)
        x0, y0, x1, y1 = boxes[index]
        cv2.rectangle(frame, (x0, y0), (x1 - 1, y1 - 1), GOLD_BGR, -1)
        yield frame


def make_hud_video(path: str | Path, lit_rounds: Sequence[int], *, n_icons: int = 4,
                   fps: int = 60, size: tuple[int, int] = (960, 540),
                   max_rounds: int = 7) -> str:
    """Write a lossless video of the fake icon row. Returns the path.

    The encoding is lossless yuv444p on purpose: chroma subsampling would blur the tiny
    8-pixel icon boxes and could push their gold out of the detector's colour window.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required to build the test video")
    path = Path(path)
    frames_dir = path.parent / (path.stem + "_frames")
    frames_dir.mkdir(parents=True, exist_ok=True)
    for i, frame in enumerate(hud_frames(lit_rounds, n_icons=n_icons, size=size,
                                        max_rounds=max_rounds)):
        cv2.imwrite(str(frames_dir / f"{i:05d}.png"), frame)
    cmd = [ffmpeg, "-nostdin", "-v", "error", "-y", "-framerate", str(fps),
           "-i", str(frames_dir / "%05d.png"),
           "-c:v", "libx264", "-preset", "ultrafast", "-crf", "0",
           "-pix_fmt", "yuv444p", str(path)]
    subprocess.run(cmd, check=True, capture_output=True)
    shutil.rmtree(frames_dir, ignore_errors=True)
    return str(path)


# ------------------------------------------------------- fake metric HUD frames
def synth_templates() -> dict[str, dict[str, np.ndarray]]:
    """Tiny stand-in for ``glyph_templates.npz``: ten unambiguous bitmaps per font.

    Every digit is a filled border plus one horizontal bar in a digit-specific row, so
    each bitmap touches all four edges (its bounding box equals the template size) and
    the classifying reader can be exercised without depending on shipped artwork.
    """
    out: dict[str, dict[str, np.ndarray]] = {}
    for font, (width, height) in (("font16", (12, 16)), ("font12", (8, 12))):
        digits: dict[str, np.ndarray] = {}
        for digit in range(10):
            mask = np.zeros((height, width), bool)
            mask[0, :] = True
            mask[-1, :] = True
            mask[:, 0] = True
            mask[:, -1] = True
            mask[1 + digit, 1:-1] = True
            digits[str(digit)] = mask.astype(np.float32)
        out[font] = digits
    return out


def paint_number(frame: np.ndarray, text: str, x: int, y: int,
                 templates: dict[str, dict[str, np.ndarray]], font: str = "font16",
                 gap: int = 2) -> int:
    """Draw ``text`` as white glyphs; returns the x just past the last digit."""
    for char in text:
        mask = templates[font][char] > 0.5
        height, width = mask.shape
        frame[y:y + height, x:x + width][mask] = (255, 255, 255)
        x += width + gap
    return x


def slash_mask(width: int = 5, height: int = 12) -> np.ndarray:
    """A narrow top-right to bottom-left stroke, like the XP line's ``/``."""
    mask = np.zeros((height, width), bool)
    for row in range(height):
        column = round((height - 1 - row) * (width - 1) / (height - 1))
        mask[row, column] = True
        if column + 1 < width:
            mask[row, column + 1] = True
    return mask


def paint_slash(frame: np.ndarray, x: int, y: int, *, width: int = 5,
                height: int = 12) -> int:
    """Draw a slash at ``(x, y)``; returns the x just past it."""
    mask = slash_mask(width, height)
    frame[y:y + height, x:x + width][mask] = (255, 255, 255)
    return x + width

