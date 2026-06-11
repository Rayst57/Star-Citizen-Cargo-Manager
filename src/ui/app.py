"""
QApplication launch sequence.

main.py imports launch() from here and runs it.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QSplashScreen

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


def _build_splash() -> QSplashScreen:
    """Mobiglass-styled boot splash shown while the DB opens and the
    UEX station registry check runs."""
    pm = QPixmap(520, 300)
    grad = QLinearGradient(0, 0, 0, 300)
    grad.setColorAt(0.0, QColor("#06101c"))
    grad.setColorAt(1.0, QColor("#0a1f33"))
    p = QPainter(pm)
    p.fillRect(pm.rect(), grad)
    # Corner accents in the Mobiglass cyan.
    accent = QColor("#36c5e8")
    p.setPen(accent)
    for x0, y0, dx, dy in (
        (10, 10, 36, 0), (10, 10, 0, 36),
        (510, 10, -36, 0), (510, 10, 0, 36),
        (10, 290, 36, 0), (10, 290, 0, -36),
        (510, 290, -36, 0), (510, 290, 0, -36),
    ):
        p.drawLine(x0, y0, x0 + dx, y0 + dy)
    title_font = QFont()
    title_font.setPointSize(18)
    title_font.setBold(True)
    p.setFont(title_font)
    p.setPen(QColor("#dcf3ff"))
    p.drawText(pm.rect().adjusted(0, -40, 0, -40),
               Qt.AlignmentFlag.AlignCenter, "STAR CITIZEN")
    p.drawText(pm.rect().adjusted(0, -4, 0, -4),
               Qt.AlignmentFlag.AlignCenter, "CARGO MANAGER")
    sub_font = QFont()
    sub_font.setPointSize(9)
    p.setFont(sub_font)
    p.setPen(accent)
    p.drawText(pm.rect().adjusted(0, 56, 0, 56),
               Qt.AlignmentFlag.AlignCenter, "— LOADMASTER TERMINAL —")
    p.end()
    splash = QSplashScreen(pm)
    return splash


def _splash_status(app: QApplication, splash: QSplashScreen, text: str) -> None:
    splash.showMessage(
        f"  {text}",
        Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignLeft,
        QColor("#9fd8ec"),
    )
    app.processEvents()


def launch() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Star Citizen Cargo Manager")
    apply_stylesheet(app)
    # Auto-paint Mobiglass corner accents on every QDialog (workday
    # picker, settings, paste contracts, etc.) so the theme is
    # consistent across all windows.
    _dialog_filter = install_dialog_decorator(app)  # noqa: F841 — kept alive

    splash = _build_splash()
    splash.show()
    _splash_status(app, splash, "Opening local database…")

    db_path = _resolve_db_path()
    conn = initialize_database(db_path)

    # Compare the local station registry against the live UEX catalog
    # and pull in anything new (new patch stations, Pyro/Nyx additions).
    # Fail-soft: offline → continue on local data after the timeout.
    _splash_status(app, splash, "Checking UEX station registry…")
    from ..db.uex_sync import sync_stations_from_uex
    summary = sync_stations_from_uex(conn)
    if summary["ok"]:
        n_local = conn.execute(
            "SELECT COUNT(*) AS c FROM stations"
        ).fetchone()["c"]
        if summary["added"]:
            _splash_status(
                app, splash,
                f"Station registry updated — {summary['added']} new, "
                f"{n_local} total.",
            )
        else:
            _splash_status(
                app, splash,
                f"Station registry up to date ({n_local} stations).",
            )
    else:
        _splash_status(
            app, splash,
            "UEX unreachable — using local station registry.",
        )

    controller = AppController(conn, db_path)
    splash.close()

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

    # Wire the Quick Capture global hotkey AFTER MainWindow exists so
    # the queue-badge slot is connected. Stash the handle on the
    # controller so SettingsDialog can rebind it when the user picks
    # a new combo.
    from .quick_capture import install_quick_capture
    controller._quick_capture_hotkey = install_quick_capture(controller, window)

    return app.exec()
