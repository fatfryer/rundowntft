"""Main review window: video, timeline, events list, notes panel and the hotkey wiring."""
from __future__ import annotations

import os
from typing import Callable, Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QAction, QFocusEvent, QFontDatabase, QKeySequence, QShortcut
from PySide6.QtWidgets import (QFileDialog, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPushButton, QSplitter,
                               QTabWidget, QVBoxLayout, QWidget)

from .. import analysis, paths
from ..export import open_game, rows_to_csv
from ..keymap import ACTION_DOCS, DEFAULT_KEYMAP, SEEK_DELTAS_MS, describe
from ..models import MARKER, METRIC, NOTE, ROUND, TAG, Entry, Event
from ..rounds import RoundConfig, default_config
from ..session import NoEventError, ReviewSession, SequenceExhausted
from ..store import Store
from ..timeutil import format_time
from ..vision.video import VideoError, probe
from .player import Player
from .timeline import TimelineBar
from .charts import ChartWidget, open_charts_window

VIDEO_FILTER = "Videos (*.mp4 *.mkv *.mov *.webm *.avi);;All files (*)"


class NoteEdit(QLineEdit):
    """Note box that reports when it gains focus, however it was focused.

    Clicking into the box bypasses the Enter hotkey, so the window needs this signal
    to pause playback and point the entry at the playhead it was clicked on.
    """

    focused = Signal()

    def focusInEvent(self, event: QFocusEvent) -> None:  # noqa: N802 (Qt naming)
        super().focusInEvent(event)
        self.focused.emit()


class MainWindow(QMainWindow):
    def __init__(self, store: Store, keymap: Optional[dict[str, list[str]]] = None,
                 config: Optional[RoundConfig] = None, video: Optional[str] = None):
        super().__init__()
        self.store = store
        self.keymap = keymap or {a: list(k) for a, k in DEFAULT_KEYMAP.items()}
        self.config = config or default_config()
        self.session: Optional[ReviewSession] = None
        self.player = Player(self)

        self._events: list[Event] = []
        self._current_round_id: Optional[int] = None
        self._pending_scope = "auto"
        self._note_ms = 0
        self._resume_after_note = False
        self._starting_entry = False
        self._frame_count = 0

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(1500)
        self._save_timer.timeout.connect(self._save_now)

        self.setWindowTitle("TFT VOD Review")
        self.resize(1360, 780)
        self._build_ui()
        self._build_menu()
        self._actions = self._build_actions()
        self._build_shortcuts()

        self.player.positionChanged.connect(self._on_position)
        self.player.durationChanged.connect(self.timeline.set_duration)
        self.player.playingChanged.connect(lambda playing: self.play_btn.setText("Pause" if playing else "Play"))
        self.player.errorOccurred.connect(lambda msg: self.statusBar().showMessage(f"Player error: {msg}", 8000))
        self.timeline.seekRequested.connect(self.player.seek)

        self._refresh_all()
        if video:
            self.open_video(video)

    # ---------------------------------------------------------------- layout
    def _build_ui(self) -> None:
        mono = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)

        # Main horizontal splitter: video (left) vs right panel (right)
        main_split = QSplitter(Qt.Orientation.Horizontal)

        # ===== LEFT: Video player =====
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        self.player.video_widget.setMinimumSize(480, 270)
        lv.addWidget(self.player.video_widget, 1)
        self.timeline = TimelineBar()
        lv.addWidget(self.timeline)
        row = QHBoxLayout()
        self.play_btn = QPushButton("Play")
        self.play_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.play_btn.clicked.connect(self.player.toggle)
        self.time_label = QLabel("0:00 / 0:00")
        self.frame_label = QLabel("f —")
        self.frame_label.setFont(mono)
        self.frame_label.setToolTip("Frame number at the playhead (for debugging detection)")
        self.frame_label.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.rate_label = QLabel("1.0x")
        self.next_hint = QLabel("")
        row.addWidget(self.play_btn)
        row.addWidget(self.time_label)
        row.addWidget(self.frame_label)
        row.addWidget(self.rate_label)
        row.addStretch(1)
        row.addWidget(self.next_hint)
        lv.addLayout(row)
        main_split.addWidget(left)

        # ===== RIGHT: Vertical splitter with tabbed panel (top) + chart (bottom) =====
        right_split = QSplitter(Qt.Orientation.Vertical)

        # --- Top: Tabbed panel [Rounds] [Game Notes] [Round Notes] ---
        self.tab_widget = QTabWidget()
        self.tab_widget.setTabPosition(QTabWidget.TabPosition.North)

        # Tab 1: Rounds (events list)
        self.rounds_tab = QWidget()
        rounds_layout = QVBoxLayout(self.rounds_tab)
        rounds_layout.setContentsMargins(4, 4, 4, 4)
        rounds_layout.addWidget(QLabel("Rounds  (double-click to jump, right-click to edit)"))
        self.event_list = QListWidget()
        self.event_list.setFont(mono)
        self.event_list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.event_list.itemDoubleClicked.connect(self._on_event_double_clicked)
        self.event_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.event_list.customContextMenuRequested.connect(self._event_menu)
        rounds_layout.addWidget(self.event_list, 1)
        self.tab_widget.addTab(self.rounds_tab, "Rounds")

        # Tab 2: Game Notes
        self.game_notes_tab = QWidget()
        game_notes_layout = QVBoxLayout(self.game_notes_tab)
        game_notes_layout.setContentsMargins(4, 4, 4, 4)
        game_notes_layout.addWidget(QLabel("Game-level notes, tags & metrics  (right-click to delete)"))
        self.game_entry_list = QListWidget()
        self.game_entry_list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.game_entry_list.setWordWrap(True)
        self.game_entry_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.game_entry_list.customContextMenuRequested.connect(self._game_entry_menu)
        game_notes_layout.addWidget(self.game_entry_list, 1)
        self.tab_widget.addTab(self.game_notes_tab, "Game Notes")

        # Tab 3: Round Notes (context-aware for current round)
        self.round_notes_tab = QWidget()
        round_notes_layout = QVBoxLayout(self.round_notes_tab)
        round_notes_layout.setContentsMargins(4, 4, 4, 4)
        self.round_notes_label = QLabel("Round notes  (select a round in Rounds tab)")
        round_notes_layout.addWidget(self.round_notes_label)
        self.round_entry_list = QListWidget()
        self.round_entry_list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.round_entry_list.setWordWrap(True)
        self.round_entry_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.round_entry_list.customContextMenuRequested.connect(self._round_entry_menu)
        round_notes_layout.addWidget(self.round_entry_list, 1)
        self.tab_widget.addTab(self.round_notes_tab, "Round Notes")

        right_split.addWidget(self.tab_widget)

        # --- Bottom: Chart widget ---
        self.chart_widget = ChartWidget(self.store)
        right_split.addWidget(self.chart_widget)

        # Set splitter sizes: tabbed panel 60%, chart 40%
        right_split.setStretchFactor(0, 6)
        right_split.setStretchFactor(1, 4)
        right_split.setSizes([600, 400])

        main_split.addWidget(right_split)

        # Main splitter: video 55%, right panel 45%
        main_split.setStretchFactor(0, 55)
        main_split.setStretchFactor(1, 45)
        main_split.setSizes([800, 600])

        self.setCentralWidget(main_split)

        # Entry editor (shared across tabs) - placed at bottom of window
        self.scope_label = QLabel("")
        self.entry_edit = NoteEdit()
        self.entry_edit.setPlaceholderText("gold=32 #bad-roll note text   (Enter saves, Esc cancels)")
        self.entry_edit.focused.connect(self._on_note_focus)

        # Add entry editor to a bottom dock or just below the main splitter
        # For now, add it to the status bar area or as a persistent bottom widget
        # We'll use a simple approach: add a bottom widget to the main window
        bottom_widget = QWidget()
        bottom_layout = QVBoxLayout(bottom_widget)
        bottom_layout.setContentsMargins(4, 4, 4, 4)
        bottom_layout.addWidget(self.scope_label)
        bottom_layout.addWidget(self.entry_edit)
        bottom_widget.setMaximumHeight(80)

        # Use a vertical splitter for the whole central area to accommodate bottom widget
        central_split = QSplitter(Qt.Orientation.Vertical)
        central_split.addWidget(main_split)
        central_split.addWidget(bottom_widget)
        central_split.setStretchFactor(0, 1)
        central_split.setStretchFactor(1, 0)
        central_split.setSizes([700, 80])
        central_split.setChildrenCollapsible(False)
        self.setCentralWidget(central_split)

        self.statusBar().showMessage(
            "Open a video with Ctrl+O. F/D jump between rounds, V/X step a frame. F1 shows all hotkeys.")

        # Connect tab change to update round notes context
        self.tab_widget.currentChanged.connect(self._on_tab_changed)

        # Connect chart click navigation
        self.chart_widget.round_clicked.connect(self.player.seek)

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        for text, slot, shortcut in (
            ("&Open video…", self.choose_video, None),
            ("Export round table as &CSV…", self.export_csv, None),
            ("&Save sidecar now", self._save_now, "Ctrl+S"),
        ):
            act = QAction(text, self)
            act.triggered.connect(slot)
            if shortcut:
                act.setShortcut(QKeySequence(shortcut))
            file_menu.addAction(act)
        file_menu.addSeparator()
        quit_act = QAction("&Quit", self)
        quit_act.triggered.connect(self.close)
        file_menu.addAction(quit_act)

        view_menu = self.menuBar().addMenu("&View")
        charts_act = QAction("&Charts", self)
        charts_act.setShortcut(QKeySequence("Ctrl+Shift+C"))
        charts_act.triggered.connect(self._open_charts_window)
        view_menu.addAction(charts_act)

        help_menu = self.menuBar().addMenu("&Help")
        hk = QAction("&Hotkeys", self)
        hk.triggered.connect(self.show_hotkeys)
        help_menu.addAction(hk)

    # ------------------------------------------------------------- hotkeys
    def _build_actions(self) -> dict[str, Callable[[], None]]:
        acts: dict[str, Callable[[], None]] = {
            "play_pause": self.player.toggle,
            "frame_back": lambda: self._step_frames(-1),
            "frame_fwd": lambda: self._step_frames(+1),
            "next_event": self.go_next_event,
            "prev_event": self.go_prev_event,
            "stamp_next": self.stamp_next,
            "stamp_label": self.stamp_label,
            "unstamp_last": self.unstamp_last,
            "add_marker": self.add_marker,
            "focus_event_note": lambda: self._note_key("auto"),
            "focus_game_note": lambda: self._note_key("game"),
            "cancel_entry": self.cancel_entry,
            "speed_up": lambda: self._set_speed(+1),
            "speed_down": lambda: self._set_speed(-1),
            "speed_reset": lambda: (self.player.set_rate(1.0), self._show_rate()),
            "open_video": self.choose_video,
            "show_hotkeys": self.show_hotkeys,
        }
        for name, delta in SEEK_DELTAS_MS.items():
            acts[name] = lambda d=delta: self.player.seek_relative(d)
        for stage in range(1, 8):
            acts[f"jump_stage_{stage}"] = lambda s=stage: self.jump_stage(s)
        return acts

    def _build_shortcuts(self) -> None:
        self._shortcuts: list[QShortcut] = []
        for action, keys in self.keymap.items():
            if action not in self._actions:
                continue
            for key in keys:
                sc = QShortcut(QKeySequence(key), self)
                sc.setContext(Qt.ShortcutContext.WindowShortcut)
                sc.activated.connect(lambda a=action: self.run_action(a))
                self._shortcuts.append(sc)

    def _open_charts_window(self) -> None:
        """Open the standalone charts window."""
        open_charts_window(self.store, self)

    def _on_tab_changed(self, index: int) -> None:
        """Handle tab change - update round notes label when switching to Round Notes tab."""
        if index == 2 and self.session:  # Round Notes tab
            rnd = self.session.current_event(self._pos(), kinds=(ROUND,))
            if rnd:
                self.round_notes_label.setText(f"Round {rnd.label} ({rnd.round_type}) notes  (right-click to delete)")
            else:
                self.round_notes_label.setText("No round at playhead — select a round in Rounds tab")
        self._refresh_entries()

    # ---------------------------------------------------------------- video
    def run_action(self, action: str) -> None:
        """Run a hotkey action by name (also what the tests call instead of faking key presses)."""
        handler = self._actions.get(action)
        if handler is None:
            return
        try:
            handler()
        except (SequenceExhausted, NoEventError, ValueError) as exc:
            self.statusBar().showMessage(str(exc), 5000)

    # ---------------------------------------------------------------- video
    def choose_video(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open VOD", "", VIDEO_FILTER)
        if path:
            self.open_video(path)

    def open_video(self, path: str) -> None:
        self._save_now()
        self.player.load(path)
        self._load_video_meta(path)
        game = open_game(self.store, path)
        self.session = ReviewSession(self.store, game, self.config)
        self.session.on_change(self._on_session_changed)
        self.setWindowTitle(f"TFT VOD Review — {game.title}")
        self._current_round_id = None
        self._refresh_all()
        # Update chart with current game
        self.chart_widget.set_game(game.id)
        self.statusBar().showMessage(f"Opened {os.path.basename(path)}", 5000)

    def _load_video_meta(self, path: str) -> None:
        """Read fps/frame count so the playhead can be labelled with a frame number."""
        try:
            meta = probe(path)
        except VideoError as exc:
            self.player.set_fps(0.0)
            self._frame_count = 0
            self._update_frame_label()
            self.statusBar().showMessage(f"No frame info for this video: {exc}", 8000)
            return
        self.player.set_fps(meta.fps_float)
        self._frame_count = meta.frame_count
        self._update_frame_label()

    def _step_frames(self, count: int) -> None:
        if self.player.fps <= 0:
            self.statusBar().showMessage("Frame stepping needs a video with a known frame rate", 3000)
            return
        self.player.step_frames(count)
        self._on_position(self.player.position())

    def _update_frame_label(self) -> None:
        fps = self.player.fps
        if fps <= 0:
            self.frame_label.setText("f —")
            return
        frame = self.player.frame()
        total = f" / {self._frame_count:,}" if self._frame_count else ""
        self.frame_label.setText(f"f {frame:,}{total}  ({fps:g} fps)")

    # ------------------------------------------------------- event actions
    def _pos(self) -> int:
        return self.player.position()

    def go_next_event(self) -> None:
        if not self.session:
            return
        ev = self.session.next_event(self._pos())
        if ev:
            self.player.seek(ev.time_ms)
        else:
            self.statusBar().showMessage("No later event", 2000)

    def go_prev_event(self) -> None:
        if not self.session:
            return
        ev = self.session.prev_event(self._pos())
        self.player.seek(ev.time_ms if ev else 0)

    def jump_stage(self, stage: int) -> None:
        if not self.session:
            return
        ev = self.session.jump_stage(stage)
        if ev:
            self.player.seek(ev.time_ms)
        else:
            self.statusBar().showMessage(f"Stage {stage} isn't marked yet", 3000)

    def stamp_next(self) -> None:
        if not self.session:
            return
        ev = self.session.stamp_next(self._pos())
        self.statusBar().showMessage(f"Marked {ev.label} ({ev.round_type}) at {format_time(ev.time_ms)}", 3000)

    def stamp_label(self) -> None:
        if not self.session:
            return
        nxt = self.session.next_expected()
        text, ok = QInputDialog.getText(self, "Mark round", "Round label (e.g. 3-2):", text=nxt.label if nxt else "")
        if ok and text.strip():
            ev = self.session.stamp_label(text.strip(), self._pos())
            self.statusBar().showMessage(f"Marked {ev.label} at {format_time(ev.time_ms)}", 3000)

    def unstamp_last(self) -> None:
        if not self.session:
            return
        nxt_label = self.session._latest_slot_label()
        if nxt_label is None:
            self.statusBar().showMessage("Nothing to remove", 2000)
            return
        event = self.store.find_event(self.session.game.id, ROUND, nxt_label)
        if event and self.session.entries_for(event.id):
            answer = QMessageBox.question(
                self, "Remove round",
                f"Round {event.label} has notes/tags/metrics attached. Removing it deletes them too. Continue?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        removed = self.session.unstamp_last()
        if removed:
            self.statusBar().showMessage(f"Removed {removed.label}", 3000)

    def add_marker(self) -> None:
        if not self.session:
            return
        text, ok = QInputDialog.getText(self, "Bookmark", "Label:", text="mark")
        if ok:
            self.session.add_marker(self._pos(), text.strip() or "mark")

    # ------------------------------------------------------------- entries
    def _note_key(self, scope: str) -> None:
        """Enter / Shift+Enter: start typing, or - if already typing - save."""
        if self.entry_edit.hasFocus():
            self.submit_entry(scope)
        else:
            self.begin_entry(scope)

    def _on_note_focus(self) -> None:
        """Focus arrived (usually a mouse click): point the entry at the live playhead."""
        if not self.session or self._starting_entry:
            return
        self._pending_scope = "auto"
        self._note_ms = self._pos()
        self._resume_after_note = self.player.is_playing()
        if self._resume_after_note:
            self.player.pause()
        self._update_scope_label()

    def begin_entry(self, scope: str) -> None:
        if not self.session:
            self.statusBar().showMessage("Open a video first", 3000)
            return
        self._pending_scope = scope
        self._note_ms = self._pos()
        self._resume_after_note = self.player.is_playing()
        if self._resume_after_note:
            self.player.pause()
        self._update_scope_label()
        self._starting_entry = True
        try:
            self.entry_edit.setFocus()
        finally:
            self._starting_entry = False

    def submit_entry(self, scope: Optional[str] = None) -> None:
        text = self.entry_edit.text().strip()
        scope = scope or self._pending_scope
        if not text or not self.session:
            self.cancel_entry()
            return
        # The playhead is the truth: the note box can gain focus by mouse or survive a
        # seek, so a timestamp captured when typing started may be stale by minutes.
        ms = self._pos()
        try:
            result = self.session.add_entry(text, ms=ms, scope=scope)
        except NoEventError as exc:
            self.statusBar().showMessage(str(exc), 5000)
            return
        target = f"round {result.event.label}" if result.event else "game"
        self.statusBar().showMessage(
            f"Saved to {target}: " + ", ".join(e.display() for e in result.created), 4000)
        self._finish_entry()

    def cancel_entry(self) -> None:
        if self.entry_edit.hasFocus() or self.entry_edit.text():
            self._finish_entry()

    def _finish_entry(self) -> None:
        self.entry_edit.clear()
        self.entry_edit.clearFocus()
        self.scope_label.setText("")
        self._pending_scope = "auto"
        self._note_ms = self._pos()
        if self._resume_after_note:
            self.player.play()
        self._resume_after_note = False

    def _update_scope_label(self) -> None:
        if not self.session:
            return
        event = self.session.current_event(self._note_ms, kinds=(ROUND,))
        if self._pending_scope == "game":
            target = f"the whole game ({self.session.game.title})"
        elif event:
            target = f"round {event.label} at {format_time(self._note_ms)}"
        else:
            target = "the game (no round has started yet)"
        self.scope_label.setText(f"Writing to {target}   —   Enter: save · Shift+Enter: save to game · Esc: cancel")

    # ------------------------------------------------------------ refreshing
    def _on_session_changed(self) -> None:
        self._refresh_all()
        self._save_timer.start()
        # Update chart widget with current game
        if self.session:
            self.chart_widget.set_game(self.session.game.id)

    def _refresh_all(self) -> None:
        self._refresh_events()
        self._refresh_entries()
        self._refresh_hint()

    def _refresh_events(self) -> None:
        self.event_list.clear()
        self._events = self.session.events() if self.session else []
        self.timeline.set_events(self._events)
        by_event: dict[int, list[Entry]] = {}
        if self.session:
            for entry in self.store.list_entries(game_id=self.session.game.id, scope="event"):
                by_event.setdefault(entry.event_id, []).append(entry)  # type: ignore[arg-type]
        for ev in self._events:
            entries = by_event.get(ev.id, [])
            parts = [e.display() for e in entries if e.kind in (METRIC, TAG)]
            notes = sum(1 for e in entries if e.kind == NOTE)
            if notes:
                parts.append(f"{notes} note{'s' if notes != 1 else ''}")
            kind = "mark" if ev.kind == MARKER else (ev.round_type or "")
            item = QListWidgetItem(f"{ev.label:<8}{kind:<9}{format_time(ev.time_ms):>8}  {' '.join(parts)}")
            item.setData(Qt.ItemDataRole.UserRole, ev.id)
            self.event_list.addItem(item)
        self._highlight_current()

    def _highlight_current(self) -> None:
        if not self.session:
            return
        cur = self.session.current_event(self._pos())
        self.event_list.blockSignals(True)
        if cur is None:
            self.event_list.setCurrentRow(-1)
        else:
            for row, ev in enumerate(self._events):
                if ev.id == cur.id:
                    self.event_list.setCurrentRow(row)
                    break
        self.event_list.blockSignals(False)

    def _refresh_entries(self) -> None:
        if not self.session:
            self.game_entry_list.clear()
            self.game_entry_list.addItem("(no video open)")
            self.round_entry_list.clear()
            self.round_entry_list.addItem("(no video open)")
            return

        game = self.session.game

        # Game Notes tab - game-level entries
        self.game_entry_list.clear()
        game_entries = self.session.entries_for(None)
        if game_entries:
            for entry in game_entries:
                suffix = f"   @{format_time(entry.time_ms)}" if entry.kind == NOTE and entry.time_ms is not None else ""
                item = QListWidgetItem(f"   {entry.display()}{suffix}")
                item.setData(Qt.ItemDataRole.UserRole, entry.id)
                self.game_entry_list.addItem(item)
        else:
            self.game_entry_list.addItem("   — (no game-level entries)")

        # Round Notes tab - current round entries (based on playhead or selected round)
        self.round_entry_list.clear()
        current_tab = self.tab_widget.currentIndex()
        if current_tab == 2:  # Round Notes tab
            rnd = self.session.current_event(self._pos(), kinds=(ROUND,))
        else:
            # When not on Round Notes tab, show entries for the round at playhead
            rnd = self.session.current_event(self._pos(), kinds=(ROUND,))

        self._current_round_id = rnd.id if rnd else None
        if rnd:
            self.round_notes_label.setText(f"Round {rnd.label} ({rnd.round_type}) notes  (right-click to delete)")
            round_entries = self.session.entries_for(rnd.id)
            if round_entries:
                for entry in round_entries:
                    suffix = f"   @{format_time(entry.time_ms)}" if entry.kind == NOTE and entry.time_ms is not None else ""
                    item = QListWidgetItem(f"   {entry.display()}{suffix}")
                    item.setData(Qt.ItemDataRole.UserRole, entry.id)
                    self.round_entry_list.addItem(item)
            else:
                self.round_entry_list.addItem("   — (no entries for this round)")
        else:
            self.round_notes_label.setText("No round at playhead — select a round in Rounds tab")
            self.round_entry_list.addItem("   — (select a round in Rounds tab)")

    def _refresh_hint(self) -> None:
        if not self.session:
            self.next_hint.setText("")
            return
        nxt = self.session.next_expected()
        keys = "/".join(self.keymap.get("stamp_next", []))
        self.next_hint.setText(
            f"Next round to mark: {nxt.label} ({nxt.round_type})  [{keys}]" if nxt else "All rounds marked")

    def _show_rate(self) -> None:
        self.rate_label.setText(f"{self.player.rate():g}x")

    def _set_speed(self, direction: int) -> None:
        self.player.step_rate(direction)
        self._show_rate()

    def _on_position(self, ms: int) -> None:
        self.timeline.set_position(ms)
        self.time_label.setText(f"{format_time(ms)} / {format_time(self.player.duration())}")
        self._update_frame_label()
        if not self.session:
            return
        if self.entry_edit.hasFocus():
            self._note_ms = ms
            self._update_scope_label()
        self._highlight_current()
        rnd = self.session.current_event(ms, kinds=(ROUND,))
        if (rnd.id if rnd else None) != self._current_round_id:
            self._refresh_entries()

    # ---------------------------------------------------------------- menus
    def _on_event_double_clicked(self, item: QListWidgetItem) -> None:
        event_id = item.data(Qt.ItemDataRole.UserRole)
        for ev in self._events:
            if ev.id == event_id:
                self.player.seek(ev.time_ms)
                return

    def _event_menu(self, pos) -> None:
        item = self.event_list.itemAt(pos)
        if item is None or not self.session:
            return
        event_id = item.data(Qt.ItemDataRole.UserRole)
        menu = QMenu(self)
        move = menu.addAction("Move to playhead")
        rename = menu.addAction("Rename…")
        delete = menu.addAction("Delete…")
        chosen = menu.exec(self.event_list.viewport().mapToGlobal(pos))
        if chosen is move:
            self.session.move_event(event_id, self._pos())
        elif chosen is rename:
            ev = self.store.get_event(event_id)
            text, ok = QInputDialog.getText(self, "Rename event", "Label:", text=ev.label if ev else "")
            if ok and text.strip():
                try:
                    self.session.relabel_event(event_id, text.strip())
                except ValueError as exc:
                    self.statusBar().showMessage(str(exc), 5000)
        elif chosen is delete:
            has = bool(self.session.entries_for(event_id))
            msg = "Delete this event" + (" and everything attached to it?" if has else "?")
            if QMessageBox.question(self, "Delete event", msg) == QMessageBox.StandardButton.Yes:
                self.session.delete_event(event_id)

    def _game_entry_menu(self, pos) -> None:
        item = self.game_entry_list.itemAt(pos)
        entry_id = item.data(Qt.ItemDataRole.UserRole) if item else None
        if entry_id is None or not self.session:
            return
        menu = QMenu(self)
        delete = menu.addAction("Delete entry")
        if menu.exec(self.game_entry_list.viewport().mapToGlobal(pos)) is delete:
            self.session.delete_entry(entry_id)

    def _round_entry_menu(self, pos) -> None:
        item = self.round_entry_list.itemAt(pos)
        entry_id = item.data(Qt.ItemDataRole.UserRole) if item else None
        if entry_id is None or not self.session:
            return
        menu = QMenu(self)
        delete = menu.addAction("Delete entry")
        if menu.exec(self.round_entry_list.viewport().mapToGlobal(pos)) is delete:
            self.session.delete_entry(entry_id)

    # ----------------------------------------------------- export / persist
    def export_csv(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Export round table", "rounds.csv", "CSV (*.csv)")
        if not path:
            return
        rows = analysis.event_table(self.store)
        with open(path, "w", newline="", encoding="utf-8") as fh:
            n = rows_to_csv(rows, fh)
        self.statusBar().showMessage(f"Exported {n} rounds to {path}", 5000)

    def _save_now(self) -> None:
        self._save_timer.stop()
        if not self.session:
            return
        try:
            self.session.save_sidecar()
        except OSError as exc:
            self.statusBar().showMessage(f"Could not write sidecar next to the video: {exc}", 8000)

    def show_hotkeys(self) -> None:
        QMessageBox.information(self, "Hotkeys", describe(self.keymap))

    def closeEvent(self, event) -> None:
        self._save_now()
        self.player.pause()
        super().closeEvent(event)
