"""Seek bar that shows where the events are."""
from __future__ import annotations

from typing import Sequence

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPalette, QPen, QPolygonF
from PySide6.QtWidgets import QWidget

from ..models import MARKER, Event

TYPE_COLORS = {
    "pvp": "#6c8ebf",
    "pve": "#5fa55a",
    "carousel": "#a06cd5",
    MARKER: "#e0a030",
}
DEFAULT_COLOR = "#999999"
MIN_LABEL_GAP_PX = 30


class TimelineBar(QWidget):
    seekRequested = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(48)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._duration = 0
        self._position = 0
        self._events: Sequence[Event] = []

    # ------------------------------------------------------------------- state
    def set_duration(self, ms: int) -> None:
        self._duration = max(0, int(ms))
        self.update()

    def set_position(self, ms: int) -> None:
        self._position = max(0, int(ms))
        self.update()

    def set_events(self, events: Sequence[Event]) -> None:
        self._events = list(events)
        self.update()

    # ---------------------------------------------------------------- geometry
    def _x_for(self, ms: int) -> float:
        if self._duration <= 0:
            return 0.0
        return min(1.0, max(0.0, ms / self._duration)) * (self.width() - 1)

    def _ms_for(self, x: float) -> int:
        if self._duration <= 0 or self.width() <= 1:
            return 0
        return int(min(1.0, max(0.0, x / (self.width() - 1))) * self._duration)

    # ------------------------------------------------------------------ events
    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.seekRequested.emit(self._ms_for(event.position().x()))

    def mouseMoveEvent(self, event) -> None:
        if event.buttons() & Qt.MouseButton.LeftButton:
            self.seekRequested.emit(self._ms_for(event.position().x()))

    # ---------------------------------------------------------------- painting
    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pal = self.palette()
        track = QRectF(0, 26, self.width(), 12)
        p.fillRect(track, pal.color(QPalette.ColorRole.Mid))
        if self._duration > 0:
            done = QRectF(0, 26, self._x_for(self._position), 12)
            p.fillRect(done, pal.color(QPalette.ColorRole.Highlight))

        small = QFont(self.font())
        small.setPointSizeF(max(7.0, small.pointSizeF() - 2))
        p.setFont(small)
        last_label_x = -1e9
        for ev in self._events:
            x = self._x_for(ev.time_ms)
            kind = MARKER if ev.kind == MARKER else (ev.round_type or "")
            color = QColor(TYPE_COLORS.get(kind, DEFAULT_COLOR))
            if ev.kind == MARKER:
                p.setBrush(color)
                p.setPen(Qt.PenStyle.NoPen)
                p.drawPolygon(QPolygonF([QPointF(x - 4, 44), QPointF(x + 4, 44), QPointF(x, 36)]))
                continue
            p.setPen(QPen(color, 2))
            p.drawLine(QPointF(x, 22), QPointF(x, 42))
            if x - last_label_x >= MIN_LABEL_GAP_PX:
                p.setPen(pal.color(QPalette.ColorRole.WindowText))
                p.drawText(QPointF(x + 3, 18), ev.label)
                last_label_x = x

        if self._duration > 0:  # playhead
            x = self._x_for(self._position)
            p.setPen(QPen(pal.color(QPalette.ColorRole.WindowText), 2))
            p.drawLine(QPointF(x, 20), QPointF(x, 44))
