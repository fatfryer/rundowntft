"""Thin wrapper over QMediaPlayer.

The rest of the app only talks to this small interface (load / play / pause / toggle /
seek / position / duration / rate / frame / step), so swapping in libmpv later - for
true frame-accurate seeking - means replacing this one file.

Frame stepping: QMediaPlayer has no "step one frame" call, so a step is a seek by
``1000/fps`` milliseconds. Qt's reported position is taken at face value, but while
stepping we keep our own frame cursor for a moment, because the backend may keep
reporting the pre-seek position for a frame or two and a second keypress would then
land on the same frame again.
"""
from __future__ import annotations

import os
import time
from typing import Optional

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget

RATES = [0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0, 4.0]
PENDING_MS_WINDOW = 0.4      # how long a seek counts as "the truth" for position()
FRAME_WINDOW = 1.5           # how long a stepped frame counts as "the truth" for frame()


class Player(QObject):
    positionChanged = Signal(int)
    durationChanged = Signal(int)
    playingChanged = Signal(bool)
    errorOccurred = Signal(str)

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._player = QMediaPlayer(self)
        # Set TFTREVIEW_NO_AUDIO=1 to run without audio (headless machines, or audio
        # stacks where Qt's audio output misbehaves). Playback is otherwise unaffected.
        self._audio: Optional[QAudioOutput] = None
        if os.environ.get("TFTREVIEW_NO_AUDIO") != "1":
            self._audio = QAudioOutput(self)
            self._player.setAudioOutput(self._audio)
        self.video_widget = QVideoWidget()
        self._player.setVideoOutput(self.video_widget)

        # Qt reports 64-bit ints; relay as plain ints (a direct signal-to-signal connect fails).
        self._player.positionChanged.connect(lambda ms: self.positionChanged.emit(int(ms)))
        self._player.durationChanged.connect(lambda ms: self.durationChanged.emit(int(ms)))
        self._player.playbackStateChanged.connect(
            lambda state: self.playingChanged.emit(state == QMediaPlayer.PlaybackState.PlayingState)
        )
        self._player.errorOccurred.connect(lambda _err, msg: self.errorOccurred.emit(msg))

        # While a seek is in flight the backend still reports the old position for a moment,
        # which would make two quick "next event" presses land on the same event.
        self._pending_ms: Optional[int] = None
        self._pending_at = 0.0
        self._fps = 0.0
        self._frame: Optional[int] = None
        self._frame_at = 0.0

    # ---------------------------------------------------------------- loading
    def load(self, path: str) -> None:
        self._pending_ms = None
        self._frame = None
        self._player.setSource(QUrl.fromLocalFile(path))
        self._player.pause()  # show the first frame, don't autoplay

    def set_fps(self, fps: float) -> None:
        self._fps = float(fps or 0.0)
        self._frame = None

    @property
    def fps(self) -> float:
        return self._fps

    # --------------------------------------------------------------- transport
    def play(self) -> None:
        self._frame = None
        self._player.play()

    def pause(self) -> None:
        self._player.pause()

    def toggle(self) -> None:
        if self.is_playing():
            self.pause()
        else:
            self.play()

    def is_playing(self) -> bool:
        return self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState

    # ----------------------------------------------------------------- seeking
    def duration(self) -> int:
        return int(self._player.duration())

    def position(self) -> int:
        if self._pending_ms is not None:
            if time.monotonic() - self._pending_at < 0.4:
                return self._pending_ms
            self._pending_ms = None
        return int(self._player.position())

    def seek(self, ms: int) -> None:
        ms = max(0, int(ms))
        dur = self.duration()
        if dur > 0:
            ms = min(ms, dur)
        self._pending_ms, self._pending_at = ms, time.monotonic()
        self._frame = None
        self._player.setPosition(ms)

    def seek_relative(self, delta_ms: int) -> None:
        self.seek(self.position() + delta_ms)

    # ----------------------------------------------------------------- frames
    def frame(self) -> int:
        """Current frame number, or -1 when the frame rate is unknown."""
        if self._fps <= 0:
            return -1
        if self._frame is not None:
            if time.monotonic() - self._frame_at < FRAME_WINDOW:
                return self._frame
            self._frame = None
        return round(self.position() * self._fps / 1000.0)

    def ms_of_frame(self, frame: int) -> int:
        return round(int(frame) * 1000.0 / self._fps) if self._fps > 0 else 0

    def step_frames(self, count: int = 1) -> int:
        """Pause and move ``count`` frames (negative = backwards). Returns the new frame."""
        if self._fps <= 0:
            return -1
        self.pause()
        target = max(0, self.frame() + int(count))
        dur = self.duration()
        if dur > 0:
            target = min(target, round(dur * self._fps / 1000.0) - 1)
        self.seek(self.ms_of_frame(target))
        # The seek above reset the cursor; a step is authoritative for a moment so that
        # rapid keypresses accumulate instead of re-reading a stale backend position.
        self._frame, self._frame_at = target, time.monotonic()
        return target

    # ------------------------------------------------------------------- speed
    def rate(self) -> float:
        return float(self._player.playbackRate())

    def set_rate(self, rate: float) -> None:
        self._player.setPlaybackRate(rate)

    def step_rate(self, direction: int) -> float:
        """Move to the next faster (+1) or slower (-1) preset. Returns the new rate."""
        current = self.rate()
        idx = min(range(len(RATES)), key=lambda i: abs(RATES[i] - current))
        idx = max(0, min(len(RATES) - 1, idx + direction))
        self.set_rate(RATES[idx])
        return RATES[idx]
