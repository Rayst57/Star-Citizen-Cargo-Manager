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

    # Workday screen — loop until accepted, or exit on close.
    #
    # Three outcomes per iteration:
    #  - User picked Resume or Start New → controller.workday_id is set,
    #    we drop out and open the main window.
    #  - User picked "End and Start New" → workday_id is still None but
    #    the screen sets continue_picking=True; loop again so the user
    #    can fill out the new workday.
    #  - User closed the dialog (X / Cancel) → workday_id None,
    #    continue_picking False → quit the app.
    #
    # The previous version used "is there still an open workday?" as a
    # proxy for that distinction, which failed both ways: closing with
    # a stale open workday reopened the screen, and "End and Start New"
    # (which clears the open workday) accidentally quit.
    while controller.workday_id is None:
        screen = WorkdayScreen(controller)
        screen.exec()
        if controller.workday_id is not None:
            break
        if screen.continue_picking:
            continue
        return 0

    window = MainWindow(controller)
    window.show()

    return app.exec()
