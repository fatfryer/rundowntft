"""Headless smoke test of the Qt window: builds it, drives the hotkey actions, checks the panels.

It can't play real video (no display/codecs in CI), so the player's position is faked;
everything else - stamping, navigation, quick entry, refresh, sidecar autosave - is real.
"""
import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["TFTREVIEW_NO_AUDIO"] = "1"   # no audio device in CI

from PySide6.QtWidgets import QApplication  # noqa: E402

from tftreview.export import read_sidecar, sidecar_path  # noqa: E402
from tftreview.keymap import DEFAULT_KEYMAP  # noqa: E402
from tftreview.store import Store  # noqa: E402
from tftreview.ui.main_window import MainWindow  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app, tmp_path):
    store = Store(":memory:")
    win = MainWindow(store)
    clock = {"ms": 0}
    win.player.position = lambda: clock["ms"]                       # type: ignore[method-assign]
    win.player.seek = lambda ms: clock.update(ms=ms)                # type: ignore[method-assign]
    win.player.seek_relative = lambda d: clock.update(ms=max(0, clock["ms"] + d))  # type: ignore[method-assign]
    win.player.is_playing = lambda: False                           # type: ignore[method-assign]
    win.clock = clock
    win.open_video(str(tmp_path / "game1.mp4"))
    yield win
    win._save_now()
    win.close()
    store.close()


def at(win, ms):
    win.clock["ms"] = ms
    win._on_position(ms)


def test_every_keymap_action_has_a_handler(window):
    missing = [a for a in DEFAULT_KEYMAP if a not in window._actions]
    assert missing == []


def test_stamp_navigate_and_annotate(window):
    for ms in (10_000, 40_000, 70_000):
        at(window, ms)
        window.run_action("stamp_next")
    assert [e.label for e in window.session.events()] == ["1-1", "1-2", "1-3"]
    assert window.event_list.count() == 3
    assert "1-4" in window.next_hint.text()

    at(window, 45_000)
    window.run_action("next_event")
    assert window.clock["ms"] == 70_000
    window.run_action("prev_event")                       # sitting on 1-3's start: goes back to 1-2
    assert window.clock["ms"] == 40_000
    window.run_action("prev_event")
    assert window.clock["ms"] == 10_000
    window.run_action("jump_stage_1")
    assert window.clock["ms"] == 10_000

    # quick entry on the current round via the hotkey flow
    at(window, 45_000)
    window.run_action("focus_event_note")                 # begins typing
    window.entry_edit.setText("gold=32 #bad-roll rolled too early")
    window.submit_entry()
    entries = [i.text().strip() for i in (window.entry_list.item(n) for n in range(window.entry_list.count()))]
    assert any("gold=32" in e for e in entries) and any("#bad-roll" in e for e in entries)
    assert window.entry_edit.text() == ""

    # game-level entry
    window.run_action("focus_game_note")
    window.entry_edit.setText("place=3 #fast9")
    window.submit_entry("game")
    assert [e.display() for e in window.session.entries_for(None)] == ["place=3", "#fast9"]


def test_entry_before_any_round_goes_to_game_and_errors_are_shown_not_raised(window):
    at(window, 1_000)
    window.run_action("focus_event_note")
    window.entry_edit.setText("intro thoughts")
    window.submit_entry()
    assert [e.display() for e in window.session.entries_for(None)] == ["intro thoughts"]
    window.run_action("unstamp_last")                     # nothing to remove: must not raise
    assert "Nothing to remove" in window.statusBar().currentMessage()


def test_sequence_exhaustion_is_reported(window):
    window.session.sequence.slots[:] = window.session.sequence.slots[:1]
    window.session.sequence._index = {window.session.sequence.slots[0].label: 0}
    window.run_action("stamp_next")
    window.run_action("stamp_next")
    assert "already marked" in window.statusBar().currentMessage()


def test_autosaves_sidecar(window, tmp_path):
    at(window, 5_000)
    window.run_action("stamp_next")
    window.session.add_entry("hello", ms=6_000)
    window._save_now()
    data = read_sidecar(sidecar_path(tmp_path / "game1.mp4"))
    assert [e["label"] for e in data["events"]] == ["1-1"]
    assert any(en["text"] == "hello" for en in data["entries"])


def test_real_key_events_and_typing_safety(window):
    """Plain-letter hotkeys must not fire while typing in the note box; Enter saves, Esc cancels."""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    window.show()
    window.activateWindow()
    QTest.qWaitForWindowActive(window)

    at(window, 5_000)
    QTest.keyClick(window, Qt.Key.Key_M)                      # hotkey works when not typing
    at(window, 9_000)
    QTest.keyClick(window, Qt.Key.Key_M)
    assert [e.label for e in window.session.events()] == ["1-1", "1-2"]

    QTest.keyClick(window, Qt.Key.Key_Return)                 # Enter starts a note
    assert window.entry_edit.hasFocus()
    QTest.keyClicks(window.entry_edit, "nmpbk gold=5")        # every one of these letters is a hotkey
    assert window.entry_edit.text() == "nmpbk gold=5"
    assert [e.label for e in window.session.events()] == ["1-1", "1-2"]   # nothing fired

    QTest.keyClick(window.entry_edit, Qt.Key.Key_Return)      # Enter again saves
    assert window.entry_edit.text() == "" and not window.entry_edit.hasFocus()
    round_2 = window.session.events()[1]
    assert sorted(e.display() for e in window.session.entries_for(round_2.id)) == ["gold=5", "nmpbk"]

    QTest.keyClick(window, Qt.Key.Key_Return)                 # Escape cancels without saving
    QTest.keyClicks(window.entry_edit, "discard me")
    QTest.keyClick(window.entry_edit, Qt.Key.Key_Escape)
    assert window.entry_edit.text() == ""
    assert len(window.session.entries_for(round_2.id)) == 2

    QTest.keyClick(window, Qt.Key.Key_Return)                 # arrows edit the text, not the video
    QTest.keyClicks(window.entry_edit, "abc")
    before = window.clock["ms"]
    QTest.keyClick(window.entry_edit, Qt.Key.Key_Left)
    assert window.clock["ms"] == before and window.entry_edit.cursorPosition() == 2
    QTest.keyClick(window.entry_edit, Qt.Key.Key_Escape)

    QTest.keyClick(window, Qt.Key.Key_Left)                   # ...but outside the box they seek
    assert window.clock["ms"] == before - 5_000


def test_hotkey_cheat_sheet_lists_everything(window):
    from tftreview.keymap import ACTION_DOCS, describe
    sheet = describe(window.keymap)
    assert all(doc in sheet for doc in ACTION_DOCS.values())


def test_frame_label_and_frame_stepping(window):
    """V/X step exactly one frame and the label follows the frame cursor."""
    assert "—" in window.frame_label.text()            # no fps: no frame info for the dummy file

    window.player.set_fps(60.0)
    window._frame_count = 60 * 120
    at(window, 1_000)
    assert "f 60" in window.frame_label.text() and "(60 fps)" in window.frame_label.text()

    window.run_action("frame_fwd")
    assert window.clock["ms"] == 1_017
    assert "f 61" in window.frame_label.text()

    window.run_action("frame_fwd")                     # rapid presses accumulate
    assert window.clock["ms"] == 1_033
    window.run_action("frame_back")
    window.run_action("frame_back")
    assert window.clock["ms"] == 1_000
    assert "f 60" in window.frame_label.text()

    window.run_action("seek_fwd_1s")
    assert window.clock["ms"] == 2_000
    window.run_action("seek_back_1s")
    assert window.clock["ms"] == 1_000

    assert "frame_fwd" in window._actions and "frame_back" in window._actions


def test_shift_v_is_forward_and_shift_x_is_back(window):
    assert "Shift+V" in window.keymap["seek_fwd_1s"]
    assert "Shift+X" in window.keymap["seek_back_1s"]
    assert "Shift+V" not in window.keymap["seek_back_1s"]
    assert "Shift+X" not in window.keymap["seek_fwd_1s"]


def _focus_window(win):
    from PySide6.QtTest import QTest

    win.show()
    win.activateWindow()
    QTest.qWaitForWindowActive(win)


def test_note_saved_after_seeking_while_focused_uses_the_live_playhead(window):
    """Regression: the timestamp captured when typing started must not win."""
    _focus_window(window)
    for ms in (10_000, 40_000, 70_000):
        at(window, ms)
        window.run_action("stamp_next")

    at(window, 45_000)
    window.run_action("focus_event_note")
    assert window.entry_edit.hasFocus()
    window.entry_edit.setText("gold=2")
    at(window, 75_000)                          # navigate without leaving the box
    assert "round 1-3" in window.scope_label.text()

    window.submit_entry()
    first, _second, third = window.session.events()
    assert [e.display() for e in window.session.entries_for(third.id)] == ["gold=2"]
    assert window.session.entries_for(first.id) == []
    assert window._pending_scope == "auto"


def test_clicking_into_the_note_box_targets_the_playhead(window):
    """Regression: mouse focus bypasses begin_entry, which used to leave _note_ms stale."""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    _focus_window(window)
    for ms in (10_000, 40_000):
        at(window, ms)
        window.run_action("stamp_next")

    at(window, 45_000)
    QTest.mouseClick(window.entry_edit, Qt.MouseButton.LeftButton)
    assert window.entry_edit.hasFocus()
    assert "round 1-2" in window.scope_label.text()

    window.entry_edit.setText("hp=100")
    window.submit_entry()
    _first, second = window.session.events()
    assert [e.display() for e in window.session.entries_for(second.id)] == ["hp=100"]
