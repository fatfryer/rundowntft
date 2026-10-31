"""Computer-vision helpers for reading a TFT VOD.

``video``  - ffmpeg-backed metadata + frame decoding (OpenCV cannot decode AV1)
``hud``    - where the round icons are and how "lit" each one is
``detect``  - the scan that turns per-frame HUD readings into round-start timestamps
``roi``    - named regions from ``rois.json`` (gold, HP, level, ... later)
``metrics`` - HUD number reading (gold, level, XP, HP)
``shop``   - shop slot detection (unit costs, charms)
``spend``  - gold spend classification (roll, xp, unit, charm)

The Qt-free core lives in :mod:`tftreview`; this package only depends on numpy + ffmpeg
(and OpenCV for colour conversion, which is the one thing it is genuinely good at).
"""
from __future__ import annotations

from .detect import Detection, DetectResult, IconTracker, detect
from .hud import HudModel, Observation, observe
from .metrics import Metrics, MetricsReader, RoundCheck, RoundSample, check_round, iter_readings, sample_round
from .shop import ShopModel, ShopReader, UnitInSlot, iter_shop_frames
from .spend import SpendAction, SpendTracker, scan_spends
from .video import VideoError, VideoMeta, frame_at, iter_frames, probe

__all__ = [
    "Detection", "DetectResult", "IconTracker", "detect",
    "HudModel", "Observation", "observe",
    "Metrics", "MetricsReader", "RoundCheck", "RoundSample", "check_round", "iter_readings", "sample_round",
    "ShopModel", "ShopReader", "UnitInSlot", "iter_shop_frames",
    "SpendAction", "SpendTracker", "scan_spends",
    "VideoError", "VideoMeta", "frame_at", "iter_frames", "probe",
]
