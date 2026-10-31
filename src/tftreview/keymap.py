"""Default hotkeys. Pure data so it can be validated and overridden without Qt.

Users override any action by dropping a ``keymap.json`` in the app data folder:

    {"next_event": ["Down"], "prev_event": ["Up"]}

Key names are Qt key-sequence strings ("Space", "Shift+Left", "Ctrl+O", "N").
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

ACTION_DOCS: dict[str, str] = {
    "play_pause": "Play / pause",
    "frame_back": "Back one frame",
    "frame_fwd": "Forward one frame",
    "seek_back_1s": "Back 1 second",
    "seek_fwd_1s": "Forward 1 second",
    "seek_back_5s": "Back 5 seconds",
    "seek_fwd_5s": "Forward 5 seconds",
    "seek_back_30s": "Back 30 seconds",
    "seek_fwd_30s": "Forward 30 seconds",
    "next_event": "Jump to next event (round)",
    "prev_event": "Jump to previous event (restarts current if you're past its start)",
    "stamp_next": "Mark the next expected round at the playhead",
    "stamp_label": "Mark a specific round label at the playhead",
    "unstamp_last": "Remove the most recently marked round",
    "add_marker": "Add a free-form bookmark at the playhead",
    "focus_event_note": "Write a note/tag/metric on the current round (Enter again to save)",
    "focus_game_note": "Write a note/tag/metric on the whole game (Shift+Enter again to save)",
    "cancel_entry": "Cancel what you're typing",
    "speed_up": "Faster playback",
    "speed_down": "Slower playback",
    "speed_reset": "Normal speed",
    "open_video": "Open a video",
    "show_hotkeys": "Show this list",
    **{f"jump_stage_{n}": f"Jump to the start of stage {n}" for n in range(1, 8)},
}

DEFAULT_KEYMAP: dict[str, list[str]] = {
    "play_pause": ["Space", "C", "K"],
    "frame_back": ["X"],
    "frame_fwd": ["V"],
    "seek_back_1s": ["Shift+X", "Shift+Left"],
    "seek_fwd_1s": ["Shift+V", "Shift+Right"],
    "seek_back_5s": ["Left"],
    "seek_fwd_5s": ["Right"],
    "seek_back_30s": ["Ctrl+Left"],
    "seek_fwd_30s": ["Ctrl+Right"],
    "next_event": ["F", "N", "]"],
    "prev_event": ["D", "P", "["],
    "stamp_next": ["M"],
    "stamp_label": ["Shift+M"],
    "unstamp_last": ["Ctrl+Backspace"],
    "add_marker": ["B"],
    "focus_event_note": ["Return", "Enter"],
    "focus_game_note": ["Shift+Return", "Shift+Enter"],
    "cancel_entry": ["Escape"],
    "speed_up": ["="],
    "speed_down": ["-"],
    "speed_reset": ["0"],
    "open_video": ["Ctrl+O"],
    "show_hotkeys": ["F1"],
    **{f"jump_stage_{n}": [str(n)] for n in range(1, 8)},
}

SEEK_DELTAS_MS: dict[str, int] = {
    "seek_back_1s": -1000,
    "seek_fwd_1s": 1000,
    "seek_back_5s": -5000,
    "seek_fwd_5s": 5000,
    "seek_back_30s": -30000,
    "seek_fwd_30s": 30000,
}


def _norm(key: str) -> str:
    return key.replace(" ", "").lower()


def find_conflicts(keymap: dict[str, list[str]]) -> dict[str, list[str]]:
    """Keys bound to more than one action: ``{key: [action, ...]}``."""
    seen: dict[str, list[str]] = {}
    for action, keys in keymap.items():
        for key in keys:
            seen.setdefault(_norm(key), []).append(action)
    return {k: acts for k, acts in seen.items() if len(acts) > 1}


def load_keymap(path: Optional[str | Path] = None) -> tuple[dict[str, list[str]], list[str]]:
    """Defaults merged with the user's overrides. Returns ``(keymap, warnings)``; never raises on bad input."""
    keymap = {a: list(k) for a, k in DEFAULT_KEYMAP.items()}
    warnings: list[str] = []
    if path is not None and Path(path).exists():
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("top level must be an object")
        except (ValueError, OSError) as exc:
            return keymap, [f"ignored keymap file ({exc})"]
        for action, keys in data.items():
            if action not in DEFAULT_KEYMAP:
                warnings.append(f"unknown action {action!r} ignored")
                continue
            if isinstance(keys, str):
                keys = [keys]
            if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
                warnings.append(f"bad keys for {action!r} ignored")
                continue
            keymap[action] = keys
    for key, actions in find_conflicts(keymap).items():
        warnings.append(f"key {key!r} is bound to several actions: {', '.join(actions)}")
    return keymap, warnings


def describe(keymap: dict[str, list[str]]) -> str:
    """Human-readable cheat sheet."""
    lines = []
    for action, doc in ACTION_DOCS.items():
        keys = ", ".join(keymap.get(action, [])) or "(unbound)"
        lines.append(f"{keys:<22} {doc}")
    return "\n".join(lines)
