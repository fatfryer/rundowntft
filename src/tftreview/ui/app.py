"""Application entry point for the Qt UI."""
from __future__ import annotations

import sys
from typing import Optional

from PySide6.QtWidgets import QApplication

from .. import paths
from ..keymap import load_keymap
from ..rounds import load_config_or_default
from ..store import Store
from .main_window import MainWindow


def run(video: Optional[str] = None, db: Optional[str] = None) -> int:
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("TFT VOD Review")
    store = Store(db or paths.db_path())
    keymap, warnings = load_keymap(paths.keymap_path())
    window = MainWindow(store, keymap=keymap, config=load_config_or_default(paths.rounds_path()), video=video)
    for warning in warnings:
        print(f"keymap warning: {warning}", file=sys.stderr)
    window.show()
    try:
        return app.exec()
    finally:
        store.close()
