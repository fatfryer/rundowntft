"""Video decoding for round detection.

Everything here shells out to ``ffmpeg``/``ffprobe``. OpenCV's ``VideoCapture`` is not
used: it cannot decode the AV1 VODs this tool is aimed at (its bundled ffmpeg has no
AV1 decoder), and its frame seeking is approximate anyway.

The one subtlety worth remembering: convert to ``bgr24`` *before* cropping. Cropping a
yuv420p frame first lets ffmpeg round the odd chroma width (259 -> 258), and the raw
pipe would then desync from the buffer size we expect.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Iterator, Optional

import numpy as np

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"


class VideoError(RuntimeError):
    """ffmpeg/ffprobe is missing or the file could not be read."""


class DecodeAborted(VideoError):
    """ffmpeg died while decoding (it aborts on some corrupt AV1 packets).

    ``last_frame`` is the last frame that came out intact, or ``None`` when the very
    first packet was the problem. Callers that can tolerate a hole should resume after
    ``last_frame`` (or past the broken lead-in when it is ``None``).
    """

    def __init__(self, message: str, last_frame: Optional[int] = None, returncode: int = 0):
        super().__init__(message)
        self.last_frame = last_frame
        self.returncode = returncode


@dataclass(frozen=True)
class VideoMeta:
    width: int
    height: int
    fps: Fraction
    frame_count: int
    duration_s: float
    codec: str = ""

    @property
    def fps_float(self) -> float:
        return float(self.fps)

    def frame_of_ms(self, ms: int | float) -> int:
        """Frame index that contains ``ms`` (round-to-nearest, matching ``time_of``)."""
        return round(float(ms) * self.fps / 1000.0)

    def time_of(self, frame: int) -> float:
        return float(frame) / self.fps_float

    def ms_of(self, frame: int) -> int:
        return round(1000.0 * float(frame) / self.fps_float)


def _require(tool: str) -> str:
    path = shutil.which(tool)
    if not path:
        raise VideoError(f"{tool} not found on PATH; install ffmpeg to use video analysis")
    return path


def probe(path: str | os.PathLike) -> VideoMeta:
    """Read stream metadata with ffprobe. Raises :class:`VideoError` if there is no video stream."""
    _require(FFPROBE)
    path = str(path)
    if not Path(path).exists():
        raise VideoError(f"no such video: {path}")
    cmd = [_require(FFPROBE), "-v", "error", "-select_streams", "v:0",
           "-show_streams", "-show_format", "-of", "json", path]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except OSError as exc:  # pragma: no cover - unplugged ffmpeg
        raise VideoError(str(exc)) from exc
    if out.returncode != 0:
        raise VideoError(out.stderr.strip() or f"ffprobe failed on {path}")
    data = json.loads(out.stdout or "{}")
    streams = data.get("streams") or []
    if not streams:
        raise VideoError(f"no video stream in {path}")
    st = streams[0]

    rate = st.get("avg_frame_rate") or st.get("r_frame_rate") or "0/1"
    try:
        fps = Fraction(rate)
    except (ValueError, ZeroDivisionError):
        fps = Fraction(0)
    if fps <= 0:
        raise VideoError(f"could not read a frame rate from {path}")

    duration = float(st.get("duration") or data.get("format", {}).get("duration") or 0.0)
    frames = int(st.get("nb_frames") or 0)
    if frames <= 0 and duration > 0:
        frames = round(duration * float(fps))

    return VideoMeta(
        width=int(st.get("width") or 0),
        height=int(st.get("height") or 0),
        fps=fps,
        frame_count=frames,
        duration_s=duration,
        codec=str(st.get("codec_name") or ""),
    )


def _filters(crop: Optional[tuple[int, int, int, int]] = None,
             scale: Optional[tuple[int, int]] = None) -> str:
    parts = ["format=bgr24"]                      # must come first - see module docstring
    if crop is not None:
        x, y, w, h = crop
        parts.append(f"crop={int(w)}:{int(h)}:{int(x)}:{int(y)}")
    if scale is not None:
        sw, sh = scale
        parts.append(f"scale={int(sw)}:{int(sh)}:flags=area")
    return ",".join(parts)


def iter_frames(path: str | os.PathLike, meta: Optional[VideoMeta] = None, *,
                crop: Optional[tuple[int, int, int, int]] = None,
                scale: Optional[tuple[int, int]] = None,
                start_frame: int = 0, end_frame: Optional[int] = None,
                frame_stride: int = 1,
                threads: int = 0,
                ) -> Iterator[tuple[int, np.ndarray]]:
    """Yield ``(absolute_frame_index, bgr_array)`` for a frame range.

    Frames are decoded in order. With ``frame_stride > 1`` only every N-th frame is
    yielded (useful for speeding up detection when single-frame precision is not needed).

    ``threads`` controls ffmpeg decode threads (0 = auto, 1 = single-threaded).
    Falls back to single-threaded if multi-threaded decode fails.
    """
    _require(FFMPEG)
    path = str(path)
    meta = meta or probe(path)
    total = meta.frame_count or 0
    start = max(0, int(start_frame))
    end = total if end_frame is None else min(int(end_frame), total or int(end_frame))
    if end <= start:
        return

    stride = max(1, int(frame_stride))

    if crop is not None:
        w, h = int(crop[2]), int(crop[3])
    else:
        w, h = meta.width, meta.height
    if scale is not None:
        w, h = int(scale[0]), int(scale[1])

    # Build base command without thread count (we'll try with threads first, then fallback)
    base_cmd = [_require(FFMPEG), "-nostdin", "-v", "error"]
    if start > 0:
        # Accurate seek: ffmpeg seeks to the keyframe before `start` and decodes to it.
        # Nudge half a frame forward so rounding can never land on the previous frame.
        base_cmd += ["-ss", f"{(start + 0.5) / meta.fps_float:.9f}"]
    base_cmd += ["-i", path, "-map", "0:v:0", "-an", "-sn", "-dn", "-vf", _filters(crop, scale),
                 "-fps_mode", "cfr",
                 "-frames:v", str(end - start), "-pix_fmt", "bgr24", "-f", "rawvideo", "-"]

    frame_bytes = w * h * 3

    def _run_decode(thread_count: int) -> Iterator[tuple[int, np.ndarray]]:
        """Run ffmpeg decode with given thread count, yield frames with stride."""
        cmd = base_cmd.copy()
        # Insert -threads after -v error (index 3)
        cmd.insert(4, "-threads")
        cmd.insert(5, str(thread_count))

        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                bufsize=frame_bytes * 8)
        assert proc.stdout is not None
        errors: list[bytes] = []

        def _pump_stderr() -> None:
            assert proc.stderr is not None
            try:
                for line in iter(proc.stderr.readline, b""):
                    errors.append(line)
            except (OSError, ValueError):
                pass

        pump = threading.Thread(target=_pump_stderr, daemon=True)
        pump.start()
        idx = start
        short_read = False
        returncode = 0
        yielded = 0
        try:
            while idx < end:
                buf = proc.stdout.read(frame_bytes)
                if len(buf) < frame_bytes:
                    short_read = True
                    break
                if yielded % stride == 0:
                    yield idx, np.frombuffer(buf, np.uint8).reshape(h, w, 3)
                idx += 1
                yielded += 1
        finally:
            if proc.stdout is not None:
                proc.stdout.close()
            if proc.poll() is None:
                proc.kill()
            returncode = proc.wait()
            pump.join(timeout=1.0)
            if proc.stderr is not None:
                proc.stderr.close()

        if short_read and returncode != 0:
            last = idx - 1 if idx > start else None
            detail = b"".join(errors).decode("utf-8", "replace").strip().splitlines()
            message = (f"ffmpeg stopped decoding {path} at frame "
                       f"{last if last is not None else start} (exit {returncode})")
            if detail:
                message += f": {detail[-1]}"
            raise DecodeAborted(message, last_frame=last, returncode=returncode)

    # Try with requested thread count, fall back to single-threaded on decode failure
    try:
        yield from _run_decode(threads)
    except DecodeAborted:
        if threads != 1:
            # Retry with single-threaded decode
            yield from _run_decode(1)
        else:
            raise


def frame_at(path: str | os.PathLike, frame: int, meta: Optional[VideoMeta] = None,
             ) -> np.ndarray:
    """Decode one exact frame (full resolution, BGR). Used for debug dumps and verification."""
    meta = meta or probe(path)
    got = list(iter_frames(path, meta, start_frame=max(0, int(frame)),
                           end_frame=max(0, int(frame)) + 1))
    if not got:
        raise VideoError(f"could not decode frame {frame} of {path}")
    return got[0][1]


def save_frame(frame: np.ndarray, path: str | os.PathLike) -> str:
    """Write a BGR frame as PNG/JPEG (chosen by suffix). Returns the path."""
    import cv2  # local import: keeps ffmpeg-only use of this module free of cv2

    path = str(path)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(path, frame):
        raise VideoError(f"could not write {path}")
    return path
