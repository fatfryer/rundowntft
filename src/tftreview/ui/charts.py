"""Chart components: embeddable mini chart + standalone charts window using QtCharts."""
from __future__ import annotations

from typing import Optional

from PySide6.QtCharts import QChart, QChartView, QLineSeries, QValueAxis, QCategoryAxis
from PySide6.QtCore import Qt, QPointF, Signal
from PySide6.QtGui import QColor, QPen, QFont, QBrush, QPainter
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QComboBox, QLabel,
                                QPushButton, QCheckBox, QWidget, QSizePolicy, QToolTip)

from .. import analysis
from ..store import Store
from ..models import Game


class ChartWidget(QWidget):
    """Embeddable mini chart showing HP (red) + Gold (gold) for the current game."""

    round_clicked = Signal(int)  # emits timestamp in ms when a data point is clicked

    def __init__(self, store: Store, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.store = store
        self._game_id: Optional[int] = None
        self._round_data: list[tuple[str, Optional[float], Optional[float], int]] = []  # (label, hp, gold, time_ms)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        self.chart_view = QChartView()
        self.chart_view.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.chart_view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.chart_view.setMinimumHeight(200)
        layout.addWidget(self.chart_view)

        # Enable hover tooltips
        self.chart_view.setMouseTracking(True)
        self.chart_view.viewport().setMouseTracking(True)

        self._empty_chart()

    def set_game(self, game_id: Optional[int]) -> None:
        """Update chart for a new game."""
        self._game_id = game_id
        self._update_chart()

    def _empty_chart(self) -> None:
        chart = QChart()
        chart.setTitle("No game loaded")
        chart.legend().hide()
        for axis in chart.axes():
            chart.removeAxis(axis)
        self.chart_view.setChart(chart)

    def _update_chart(self) -> None:
        if self._game_id is None:
            self._empty_chart()
            return

        game = self.store.get_game(self._game_id)
        if not game:
            self._empty_chart()
            return

        rows = analysis.event_table(self.store, game_ids=[game.id])

        round_data = []
        for r in rows:
            hp = r["metrics"].get("hp")
            gold = r["metrics"].get("gold")
            if hp is not None or gold is not None:
                round_data.append((r["label"], hp, gold, r["time_ms"]))

        if not round_data:
            chart = QChart()
            chart.setTitle(f"{game.title} — No HP/Gold data")
            chart.legend().hide()
            for axis in chart.axes():
                chart.removeAxis(axis)
            self.chart_view.setChart(chart)
            return

        self._round_data = round_data

        chart = QChart()
        chart.setTitle(f"{game.title}")
        chart.setAnimationOptions(QChart.AnimationOption.SeriesAnimations)
        chart.legend().setVisible(True)
        chart.legend().setAlignment(Qt.AlignmentFlag.AlignBottom)
        chart.legend().setFont(QFont("sans-serif", 8))

        # X-axis: round labels
        axis_x = QCategoryAxis()
        axis_x.setLabelsPosition(QCategoryAxis.AxisLabelsPosition.AxisLabelsPositionOnValue)
        axis_x.setRange(0, len(round_data) - 1)
        for i, (label, _, _, _) in enumerate(round_data):
            axis_x.append(label, i)
        axis_x.setLabelsFont(QFont("monospace", 7))
        axis_x.setGridLineVisible(False)
        chart.addAxis(axis_x, Qt.AlignmentFlag.AlignBottom)

        # Left Y-axis: Health (red)
        hp_values = [hp for _, hp, _, _ in round_data if hp is not None]
        hp_min = min(hp_values) if hp_values else 0
        hp_max = max(hp_values) if hp_values else 100
        hp_padding = max(5, (hp_max - hp_min) * 0.1)

        axis_hp = QValueAxis()
        axis_hp.setTitleText("HP")
        axis_hp.setTitleBrush(QColor("#e74c3c"))
        axis_hp.setLabelsBrush(QColor("#e74c3c"))
        axis_hp.setLabelFormat("%.0f")
        axis_hp.setRange(max(0, hp_min - hp_padding), hp_max + hp_padding)
        axis_hp.setTickCount(min(6, max(3, len(hp_values))))
        axis_hp.setMinorTickCount(0)
        axis_hp.setGridLinePen(QPen(QColor("#e74c3c"), 0.5, Qt.PenStyle.DotLine))
        chart.addAxis(axis_hp, Qt.AlignmentFlag.AlignLeft)

        # Right Y-axis: Gold (gold)
        gold_values = [g for _, _, g, _ in round_data if g is not None]
        gold_min = min(gold_values) if gold_values else 0
        gold_max = max(gold_values) if gold_values else 50
        gold_padding = max(2, (gold_max - gold_min) * 0.1)

        axis_gold = QValueAxis()
        axis_gold.setTitleText("Gold")
        axis_gold.setTitleBrush(QColor("#f1c40f"))
        axis_gold.setLabelsBrush(QColor("#f1c40f"))
        axis_gold.setLabelFormat("%.0f")
        axis_gold.setRange(max(0, gold_min - gold_padding), gold_max + gold_padding)
        axis_gold.setTickCount(min(7, max(3, len(gold_values))))
        axis_gold.setMinorTickCount(0)
        axis_gold.setGridLinePen(QPen(QColor("#f1c40f"), 0.5, Qt.PenStyle.DotLine))
        chart.addAxis(axis_gold, Qt.AlignmentFlag.AlignRight)

        # Health series (red solid)
        hp_series = QLineSeries()
        hp_series.setName("Health")
        pen_hp = QPen(QColor("#e74c3c"))
        pen_hp.setWidth(2)
        hp_series.setPen(pen_hp)
        hp_series.setPointsVisible(True)
        hp_series.setPointLabelsVisible(False)
        hp_series.setPointLabelsFormat("@yPoint")
        hp_series.setPointLabelsFont(QFont("sans-serif", 7))
        hp_series.setPointLabelsColor(QColor("#e74c3c"))

        # Gold series (gold dashed)
        gold_series = QLineSeries()
        gold_series.setName("Gold")
        pen_gold = QPen(QColor("#f1c40f"))
        pen_gold.setWidth(2)
        pen_gold.setStyle(Qt.PenStyle.DashLine)
        gold_series.setPen(pen_gold)
        gold_series.setPointsVisible(True)
        gold_series.setPointLabelsVisible(False)
        gold_series.setPointLabelsFormat("@yPoint")
        gold_series.setPointLabelsFont(QFont("sans-serif", 7))
        gold_series.setPointLabelsColor(QColor("#f1c40f"))

        for i, (label, hp, gold, time_ms) in enumerate(round_data):
            if hp is not None:
                hp_series.append(QPointF(i, hp))
            if gold is not None:
                gold_series.append(QPointF(i, gold))

        chart.addSeries(hp_series)
        chart.addSeries(gold_series)

        hp_series.attachAxis(axis_x)
        hp_series.attachAxis(axis_hp)
        gold_series.attachAxis(axis_x)
        gold_series.attachAxis(axis_gold)

        # Click navigation: seek to round timestamp
        def on_point_clicked(point: QPointF, series: QLineSeries):
            idx = int(round(point.x()))
            if 0 <= idx < len(round_data):
                label, hp, gold, time_ms = round_data[idx]
                # Verify the clicked point matches the series (hp vs gold)
                if (series is hp_series and hp is not None) or (series is gold_series and gold is not None):
                    self.round_clicked.emit(time_ms)

        hp_series.clicked.connect(lambda pt: on_point_clicked(pt, hp_series))
        gold_series.clicked.connect(lambda pt: on_point_clicked(pt, gold_series))

        # Hover tooltips
        def on_hover(point: QPointF, state: bool, series: QLineSeries):
            if state:
                idx = int(round(point.x()))
                if 0 <= idx < len(round_data):
                    label, hp, gold, time_ms = round_data[idx]
                    if series is hp_series and hp is not None:
                        QToolTip.showText(
                            self.chart_view.mapToGlobal(self.chart_view.mapFromScene(
                                self.chart_view.chart().mapToPosition(point)
                            )),
                            f"{label}\nHP: {hp:.0f}"
                        )
                    elif series is gold_series and gold is not None:
                        QToolTip.showText(
                            self.chart_view.mapToGlobal(self.chart_view.mapFromScene(
                                self.chart_view.chart().mapToPosition(point)
                            )),
                            f"{label}\nGold: {gold:.0f}"
                        )
            else:
                QToolTip.hideText()

        hp_series.hovered.connect(lambda pt, st: on_hover(pt, st, hp_series))
        gold_series.hovered.connect(lambda pt, st: on_hover(pt, st, gold_series))

        self.chart_view.setChart(chart)


class ChartsWindow(QDialog):
    """Standalone window showing health/gold dual-axis charts with game selector and overlay mode."""

    def __init__(self, store: Store, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.store = store
        self.setWindowTitle("TFT Charts — Health & Gold")
        self.resize(1100, 700)

        self._games: list[Game] = []
        self._current_game_idx = 0
        self._show_all_games = False

        self._build_ui()
        self._load_games()
        self._update_chart()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        # Toolbar
        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("Game:"))
        self.game_combo = QComboBox()
        self.game_combo.setMinimumWidth(300)
        self.game_combo.currentIndexChanged.connect(self._on_game_changed)
        toolbar.addWidget(self.game_combo, 1)

        self.all_games_cb = QCheckBox("Overlay all games")
        self.all_games_cb.setToolTip("Show all games on one chart (lighter lines)")
        self.all_games_cb.toggled.connect(self._on_all_games_toggled)
        toolbar.addWidget(self.all_games_cb)

        toolbar.addStretch(1)
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self._load_games)
        toolbar.addWidget(refresh_btn)
        layout.addLayout(toolbar)

        # Chart view
        self.chart_view = QChartView()
        self.chart_view.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.chart_view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout.addWidget(self.chart_view, 1)

        # Legend info
        self.legend_label = QLabel("")
        self.legend_label.setFont(QFont("monospace", 9))
        self.legend_label.setStyleSheet("color: #888; padding: 4px;")
        layout.addWidget(self.legend_label)

    def _load_games(self) -> None:
        all_games = self.store.list_games()
        self._games = []

        for game in all_games:
            rows = analysis.event_table(self.store, game_ids=[game.id])
            has_data = any(
                r["metrics"].get("hp") is not None or r["metrics"].get("gold") is not None
                for r in rows
            )
            if has_data:
                self._games.append(game)

        self._games.sort(key=lambda g: g.id, reverse=True)

        self.game_combo.blockSignals(True)
        self.game_combo.clear()
        for game in self._games:
            place = self._get_final_placement(game.id)
            place_str = f"  #{place}" if place else ""
            self.game_combo.addItem(f"{game.title}{place_str}  ({game.id})", game.id)
        self.game_combo.blockSignals(False)

        if self._games:
            self._current_game_idx = 0
            self.game_combo.setCurrentIndex(0)
        else:
            self._current_game_idx = -1

    def _get_final_placement(self, game_id: int) -> Optional[int]:
        rows = analysis.game_table(self.store, game_ids=[game_id])
        if rows:
            place = rows[0]["metrics"].get("place")
            if place is not None:
                return int(place)
        return None

    def _on_game_changed(self, idx: int) -> None:
        if 0 <= idx < len(self._games):
            self._current_game_idx = idx
            self._update_chart()

    def _on_all_games_toggled(self, checked: bool) -> None:
        self._show_all_games = checked
        self._update_chart()

    def _update_chart(self) -> None:
        chart = QChart()
        chart.setTitle("Health & Gold by Round")
        chart.setAnimationOptions(QChart.AnimationOption.SeriesAnimations)
        chart.legend().setVisible(True)
        chart.legend().setAlignment(Qt.AlignmentFlag.AlignBottom)
        chart.legend().setFont(QFont("sans-serif", 8))

        if self._show_all_games:
            self._build_overlay_chart(chart)
        else:
            self._build_single_game_chart(chart)

        self.chart_view.setChart(chart)
        self._update_legend_label()

    def _build_single_game_chart(self, chart: QChart) -> None:
        if not (0 <= self._current_game_idx < len(self._games)):
            self._set_empty_chart(chart, "No game selected")
            return

        game = self._games[self._current_game_idx]
        rows = analysis.event_table(self.store, game_ids=[game.id])

        round_data = []
        for r in rows:
            hp = r["metrics"].get("hp")
            gold = r["metrics"].get("gold")
            if hp is not None or gold is not None:
                round_data.append((r["label"], hp, gold, r["time_ms"]))

        if not round_data:
            self._set_empty_chart(chart, f"No health/gold data for {game.title}")
            return

        # X-axis: round labels
        axis_x = QCategoryAxis()
        axis_x.setLabelsPosition(QCategoryAxis.AxisLabelsPosition.AxisLabelsPositionOnValue)
        axis_x.setRange(0, len(round_data) - 1)
        for i, (label, _, _, _) in enumerate(round_data):
            axis_x.append(label, i)
        axis_x.setLabelsFont(QFont("monospace", 8))
        axis_x.setGridLineVisible(False)
        chart.addAxis(axis_x, Qt.AlignmentFlag.AlignBottom)

        # Left Y-axis: Health (red)
        hp_values = [hp for _, hp, _, _ in round_data if hp is not None]
        hp_min = min(hp_values) if hp_values else 0
        hp_max = max(hp_values) if hp_values else 100
        hp_padding = max(5, (hp_max - hp_min) * 0.1)

        axis_hp = QValueAxis()
        axis_hp.setTitleText("Health (HP)")
        axis_hp.setTitleBrush(QColor("#e74c3c"))
        axis_hp.setLabelsBrush(QColor("#e74c3c"))
        axis_hp.setLabelFormat("%.0f")
        axis_hp.setRange(max(0, hp_min - hp_padding), hp_max + hp_padding)
        axis_hp.setTickCount(min(6, max(3, len(hp_values))))
        axis_hp.setMinorTickCount(0)
        axis_hp.setGridLinePen(QPen(QColor("#e74c3c"), 0.5, Qt.PenStyle.DotLine))
        chart.addAxis(axis_hp, Qt.AlignmentFlag.AlignLeft)

        # Right Y-axis: Gold (gold)
        gold_values = [g for _, _, g, _ in round_data if g is not None]
        gold_min = min(gold_values) if gold_values else 0
        gold_max = max(gold_values) if gold_values else 60
        gold_padding = max(2, (gold_max - gold_min) * 0.1)

        axis_gold = QValueAxis()
        axis_gold.setTitleText("Gold")
        axis_gold.setTitleBrush(QColor("#f1c40f"))
        axis_gold.setLabelsBrush(QColor("#f1c40f"))
        axis_gold.setLabelFormat("%.0f")
        axis_gold.setRange(max(0, gold_min - gold_padding), gold_max + gold_padding)
        axis_gold.setTickCount(min(7, max(3, len(gold_values))))
        axis_gold.setMinorTickCount(0)
        axis_gold.setGridLinePen(QPen(QColor("#f1c40f"), 0.5, Qt.PenStyle.DotLine))
        chart.addAxis(axis_gold, Qt.AlignmentFlag.AlignRight)

        # Health series (red solid)
        hp_series = QLineSeries()
        hp_series.setName("Health")
        pen_hp = QPen(QColor("#e74c3c"))
        pen_hp.setWidth(2)
        hp_series.setPen(pen_hp)
        hp_series.setPointsVisible(True)

        # Gold series (gold dashed)
        gold_series = QLineSeries()
        gold_series.setName("Gold")
        pen_gold = QPen(QColor("#f1c40f"))
        pen_gold.setWidth(2)
        pen_gold.setStyle(Qt.PenStyle.DashLine)
        gold_series.setPen(pen_gold)
        gold_series.setPointsVisible(True)

        for i, (label, hp, gold, time_ms) in enumerate(round_data):
            if hp is not None:
                hp_series.append(QPointF(i, hp))
            if gold is not None:
                gold_series.append(QPointF(i, gold))

        chart.addSeries(hp_series)
        chart.addSeries(gold_series)

        hp_series.attachAxis(axis_x)
        hp_series.attachAxis(axis_hp)
        gold_series.attachAxis(axis_x)
        gold_series.attachAxis(axis_gold)

        # Click navigation: seek to round timestamp
        def on_point_clicked(point: QPointF, series: QLineSeries):
            idx = int(round(point.x()))
            if 0 <= idx < len(round_data):
                label, hp, gold, time_ms = round_data[idx]
                if (series is hp_series and hp is not None) or (series is gold_series and gold is not None):
                    # Emit signal to parent MainWindow if connected
                    parent = self.parent()
                    if parent and hasattr(parent, 'player'):
                        parent.player.seek(time_ms)

        hp_series.clicked.connect(lambda pt: on_point_clicked(pt, hp_series))
        gold_series.clicked.connect(lambda pt: on_point_clicked(pt, gold_series))

        # Hover tooltips
        def on_hover(point: QPointF, state: bool, series: QLineSeries, metric_name: str):
            if state:
                idx = int(round(point.x()))
                if 0 <= idx < len(round_data):
                    label, hp, gold, time_ms = round_data[idx]
                    val = hp if metric_name == "hp" else gold
                    if val is not None:
                        QToolTip.showText(
                            self.chart_view.mapToGlobal(self.chart_view.mapFromScene(
                                self.chart_view.chart().mapToPosition(point)
                            )),
                            f"{label}\n{metric_name.upper()}: {val:.0f}"
                        )
            else:
                QToolTip.hideText()

        hp_series.hovered.connect(lambda pt, st: on_hover(pt, st, hp_series, "hp"))
        gold_series.hovered.connect(lambda pt, st: on_hover(pt, st, gold_series, "gold"))

        # Placement badge in title
        place = self._get_final_placement(game.id)
        if place:
            chart.setTitle(f"{game.title} — Final: #{place}")

    def _build_overlay_chart(self, chart: QChart) -> None:
        if not self._games:
            self._set_empty_chart(chart, "No games with health/gold data")
            return

        # Use first game's rounds as reference for X-axis
        first_rows = analysis.event_table(self.store, game_ids=[self._games[0].id])
        round_labels = [r["label"] for r in first_rows
                        if r["metrics"].get("hp") is not None or r["metrics"].get("gold") is not None]

        axis_x = QCategoryAxis()
        axis_x.setLabelsPosition(QCategoryAxis.AxisLabelsPosition.AxisLabelsPositionOnValue)
        axis_x.setRange(0, max(0, len(round_labels) - 1))
        for i, label in enumerate(round_labels):
            axis_x.append(label, i)
        axis_x.setLabelsFont(QFont("monospace", 8))
        axis_x.setGridLineVisible(False)
        chart.addAxis(axis_x, Qt.AlignmentFlag.AlignBottom)

        # Left Y-axis: Health
        axis_hp = QValueAxis()
        axis_hp.setTitleText("Health (HP)")
        axis_hp.setTitleBrush(QColor("#e74c3c"))
        axis_hp.setLabelsBrush(QColor("#e74c3c"))
        axis_hp.setLabelFormat("%.0f")
        axis_hp.setRange(0, 110)
        axis_hp.setTickCount(6)
        axis_hp.setGridLinePen(QPen(QColor("#e74c3c"), 0.5, Qt.PenStyle.DotLine))
        chart.addAxis(axis_hp, Qt.AlignmentFlag.AlignLeft)

        # Right Y-axis: Gold
        axis_gold = QValueAxis()
        axis_gold.setTitleText("Gold")
        axis_gold.setTitleBrush(QColor("#f1c40f"))
        axis_gold.setLabelsBrush(QColor("#f1c40f"))
        axis_gold.setLabelFormat("%.0f")
        axis_gold.setRange(0, 60)
        axis_gold.setTickCount(7)
        axis_gold.setGridLinePen(QPen(QColor("#f1c40f"), 0.5, Qt.PenStyle.DotLine))
        chart.addAxis(axis_gold, Qt.AlignmentFlag.AlignRight)

        def placement_color(place: Optional[int]) -> QColor:
            if place is None:
                return QColor("#95a5a6")
            if place == 1:
                return QColor("#f1c40f")
            elif place == 2:
                return QColor("#f39c12")
            elif place == 3:
                return QColor("#e67e22")
            elif place == 4:
                return QColor("#ecf0f1")
            elif place == 5:
                return QColor("#e74c3c")
            elif place == 6:
                return QColor("#c0392b")
            elif place == 7:
                return QColor("#8e44ad")
            else:
                return QColor("#2c3e50")

        for game in self._games:
            rows = analysis.event_table(self.store, game_ids=[game.id])
            place = self._get_final_placement(game.id)
            base_color = placement_color(place)

            round_data = []
            for r in rows:
                hp = r["metrics"].get("hp")
                gold = r["metrics"].get("gold")
                if hp is not None or gold is not None:
                    round_data.append((r["label"], hp, gold, r["time_ms"]))

            if not round_data:
                continue

            hp_series = QLineSeries()
            hp_series.setName(f"{game.title} HP #{place or '?'}")
            pen_hp = QPen(base_color)
            pen_hp.setWidth(1)
            hp_series.setPen(pen_hp)
            hp_series.setPointsVisible(False)
            hp_series.setOpacity(0.6)

            gold_series = QLineSeries()
            gold_series.setName(f"{game.title} Gold #{place or '?'}")
            pen_gold = QPen(base_color)
            pen_gold.setWidth(1)
            pen_gold.setStyle(Qt.PenStyle.DashLine)
            gold_series.setPen(pen_gold)
            gold_series.setPointsVisible(False)
            gold_series.setOpacity(0.6)

            for i, (label, hp, gold, time_ms) in enumerate(round_data):
                if hp is not None:
                    hp_series.append(QPointF(i, hp))
                if gold is not None:
                    gold_series.append(QPointF(i, gold))

            chart.addSeries(hp_series)
            chart.addSeries(gold_series)

            hp_series.attachAxis(axis_x)
            hp_series.attachAxis(axis_hp)
            gold_series.attachAxis(axis_x)
            gold_series.attachAxis(axis_gold)

            # Click navigation for this game's series
            def make_click_handler(series_hp, series_gold, data):
                def on_point_clicked(point: QPointF, series: QLineSeries):
                    idx = int(round(point.x()))
                    if 0 <= idx < len(data):
                        label, hp, gold, time_ms = data[idx]
                        if (series is series_hp and hp is not None) or (series is series_gold and gold is not None):
                            parent = self.parent()
                            if parent and hasattr(parent, 'player'):
                                parent.player.seek(time_ms)
                return on_point_clicked

            click_handler = make_click_handler(hp_series, gold_series, round_data)
            hp_series.clicked.connect(lambda pt: click_handler(pt, hp_series))
            gold_series.clicked.connect(lambda pt: click_handler(pt, gold_series))

        chart.setTitle(f"All Games ({len(self._games)} games) — Solid=HP, Dashed=Gold")

    def _set_empty_chart(self, chart: QChart, message: str) -> None:
        chart.setTitle(message)
        chart.removeAllSeries()
        for axis in chart.axes():
            chart.removeAxis(axis)

    def _update_legend_label(self) -> None:
        if self._show_all_games:
            self.legend_label.setText(
                "Solid lines = Health (left axis)  |  Dashed lines = Gold (right axis)  |  "
                "Color by final placement: 🥇 Gold=1st 🥈 Orange=2nd 🥉 Orange=3rd ⬜ White=4th 🔴 Red=5-6th 🟣 Purple=7-8th"
            )
        else:
            game = self._games[self._current_game_idx] if 0 <= self._current_game_idx < len(self._games) else None
            if game:
                place = self._get_final_placement(game.id)
                place_str = f"  Final placement: #{place}" if place else ""
                self.legend_label.setText(
                    f"Red solid line = Health (left axis)  |  Gold dashed line = Gold (right axis){place_str}"
                )
            else:
                self.legend_label.setText("No game data available")


def open_charts_window(store: Store, parent: Optional[QWidget] = None) -> ChartsWindow:
    """Create and show the charts window. Returns the window instance."""
    win = ChartsWindow(store, parent)
    win.show()
    return win