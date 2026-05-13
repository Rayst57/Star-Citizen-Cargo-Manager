"""
QApplication launch sequence.

main.py imports launch() from here and runs it.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

from ..app_controller import AppController
from ..db.init_db import DEFAULT_DB_PATH, initialize_database
from .main_window import MainWindow
from .style import apply_stylesheet
from .widgets.mobiglass_corners import install_dialog_decorator
from .workday_screen import WorkdayScreen


def _resolve_db_path() -> Path:
    """DB lives next to the .exe in frozen builds, repo root in dev."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent / "cargo_manager.db"
    return DEFAULT_DB_PATH


def launch() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Star Citizen Cargo Manager")
    apply_stylesheet(app)
    # Auto-paint Mobiglass corner accents on every QDialog (workday
    # picker, settings, paste contracts, etc.) so the theme is
    # consistent across all windows.
    _dialog_filter = install_dialog_decorator(app)  # noqa: F841 — kept alive

    db_path = _resolve_db_path()
    conn = initialize_database(db_path)
    controller = AppController(conn, db_path)

    # Workday screen — loop until accepted or user quits
    while controller.workday_id is None:
        screen = WorkdayScreen(controller)
        result = screen.exec()
        if result == 0 and controller.workday_id is None:
            # rejected and not because the user picked "End and Start New"
            # The "End and Start New" path also calls reject() but the
            # inner loop continues so the user sees the New card live.
            wd = controller.find_open_workday()
            if wd is None and controller.workday_id is None:
                # If there's still no open workday and user closed, exit
                return 0
        # If accepted, controller.workday_id is set and we drop out of the loop

    window = MainWindow(controller)
    window.show()

    return app.exec()
