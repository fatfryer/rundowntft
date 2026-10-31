"""Where the app keeps its own files (library DB, keymap and round-config overrides)."""
from __future__ import annotations

import os
import sys
from pathlib import Path


def app_home() -> Path:
    override = os.environ.get("TFTREVIEW_HOME")
    if override:
        return Path(override)
    if sys.platform.startswith("win"):
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "tft-vod-review"


def db_path() -> Path:
    return app_home() / "library.sqlite3"


def keymap_path() -> Path:
    return app_home() / "keymap.json"


def rounds_path() -> Path:
    return app_home() / "rounds.json"
