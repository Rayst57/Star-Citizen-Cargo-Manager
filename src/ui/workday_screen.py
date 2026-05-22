"""
Start/Resume workday dialog.

First screen the user sees on launch. Returns a workday_id (existing or new)
when accepted; cancelled = exit app.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QCompleter, QDialog, QFrame, QHBoxLayout, QLabel,
    QMessageBox, QPushButton, QVBoxLayout,
)

from ..app_controller import AppController


class WorkdayScreen(QDialog):
    def __init__(self, controller: AppController, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.workday_id: int | None = None
        self.open_settings = False
        # Set to True when the user picks "End and Start New" — tells
        # the launch loop to keep iterating (show the screen again so
        # they can fill out the new workday) instead of treating the
        # dialog close as an app-quit.
        self.continue_picking = False

        self.setWindowTitle("Star Citizen Cargo Manager")
        self.setMinimumWidth(440)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(14)

        title = QLabel("Star Citizen Cargo Manager")
        title.setProperty("heading", True)
        subtitle = QLabel("Giant Loadmaster")
        subtitle.setProperty("muted", True)
        root.addWidget(title)
        root.addWidget(subtitle)

        self._add_resume_card(root)
        self._add_new_card(root)

        bottom = QHBoxLayout()
        bottom.addStretch(1)
        settings_btn = QPushButton("Settings")
        settings_btn.setProperty("flat", True)
        settings_btn.clicked.connect(self._open_settings)
        bottom.addWidget(settings_btn)
        root.addLayout(bottom)

    # ── resume card ────────────────────────────────────────────────────

    def _add_resume_card(self, root: QVBoxLayout) -> None:
        wd = self.controller.find_open_workday()

        card = QFrame()
        card.setObjectName("card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(14, 12, 14, 12)

        header = QLabel("Resume Workday")
        header.setProperty("heading", True)
        layout.addWidget(header)

        if wd:
            info = QLabel(
                f"Started: {wd['started_at'][:16].replace('T', ' ')}\n"
                f"Ship: {wd['ship_name']}\n"
                f"Origin: {wd['origin_name']}\n"
                f"Contracts: {wd['n_contracts']}"
            )
            layout.addWidget(info)
            row = QHBoxLayout()
            resume_btn = QPushButton("Resume")
            resume_btn.clicked.connect(lambda: self._on_resume(wd["id"]))
            end_btn = QPushButton("End and Start New")
            end_btn.setProperty("flat", True)
            end_btn.clicked.connect(self._on_end_old)
            row.addWidget(resume_btn)
            row.addWidget(end_btn)
            row.addStretch(1)
            layout.addLayout(row)
        else:
            empty = QLabel("No open workday.")
            empty.setProperty("muted", True)
            layout.addWidget(empty)
            card.setEnabled(False)

        root.addWidget(card)

    # ── new workday card ────────────────────────────────────────────────

    def _add_new_card(self, root: QVBoxLayout) -> None:
        card = QFrame()
        card.setObjectName("card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(14, 12, 14, 12)

        header = QLabel("Start New Workday")
        header.setProperty("heading", True)
        layout.addWidget(header)

        # Ship combo — which ship is this workday flown in.
        ship_row = QHBoxLayout()
        ship_row.addWidget(QLabel("Ship"))
        self.ship_combo = QComboBox()
        for s in self.controller.list_ships():
            self.ship_combo.addItem(
                f"{s['name']}  ({s['total_scu']} SCU)", userData=s["id"]
            )
        ship_row.addWidget(self.ship_combo, 1)
        layout.addLayout(ship_row)

        # Origin combo — starts blank so the user must consciously
        # pick a departure facility (no silent default).
        origin_row = QHBoxLayout()
        origin_row.addWidget(QLabel("Origin"))
        self.origin_combo = QComboBox()
        self._populate_stations(
            self.origin_combo, include_round_robin=False, blank_first=True,
        )
        origin_row.addWidget(self.origin_combo, 1)
        layout.addLayout(origin_row)

        # Final destination combo. The first entry is "Round Robin" — picking
        # it means "return to origin after the last delivery" (no fixed final
        # station), which replaces the old separate round-robin checkbox.
        final_row = QHBoxLayout()
        final_row.addWidget(QLabel("Final dest."))
        self.final_combo = QComboBox()
        self._populate_stations(self.final_combo, include_round_robin=True)
        final_row.addWidget(self.final_combo, 1)
        layout.addLayout(final_row)

        # Start button
        start_row = QHBoxLayout()
        start_row.addStretch(1)
        start_btn = QPushButton("Start Workday")
        start_btn.clicked.connect(self._on_start_new)
        start_row.addWidget(start_btn)
        layout.addLayout(start_row)

        root.addWidget(card)

    def _populate_stations(
        self, combo: QComboBox, *,
        include_round_robin: bool,
        blank_first: bool = False,
    ) -> None:
        # Editable + type-to-filter: with 300+ stations a plain
        # scrolling combo is unusable. A QCompleter in MatchContains
        # mode narrows the popup as the user types — same behaviour
        # as the contract dialog's station pickers.
        combo.setEditable(True)
        combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        if include_round_robin:
            combo.addItem("Round Robin (return to origin)", userData=None)
        rows = self.controller.conn.execute(
            """
            SELECT id, name FROM stations
            WHERE is_active = 1 AND is_gateway = 0
            ORDER BY name COLLATE NOCASE
            """
        ).fetchall()
        for r in rows:
            combo.addItem(r["name"], userData=r["id"])

        completer = combo.completer()
        completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)

        if blank_first:
            # Origin opens empty so the user must consciously pick a
            # departure facility — no silent default.
            combo.setCurrentIndex(-1)
            combo.lineEdit().setPlaceholderText(
                "Pick or type a departure facility…"
            )

    def _resolve_station_id(self, combo: QComboBox):
        """An editable combo's currentData() can lag the typed text —
        match the text to an item first, then fall back."""
        idx = combo.findText(
            combo.currentText().strip(), Qt.MatchFlag.MatchFixedString,
        )
        if idx >= 0:
            return combo.itemData(idx)
        return combo.currentData()

    # ── actions ────────────────────────────────────────────────────────

    def _on_resume(self, wid: int) -> None:
        self.controller.resume_workday(wid)
        self.workday_id = wid
        self.accept()

    def _on_end_old(self) -> None:
        self.controller.end_workday()
        # Rebuild UI: simplest is to close and reopen, but for v1 we just
        # re-enable the new-workday card and disable the resume card.
        self.continue_picking = True
        self.reject()  # caller can re-show; see app.py

    def _on_start_new(self) -> None:
        origin_id = self._resolve_station_id(self.origin_combo)
        if origin_id is None:
            QMessageBox.warning(
                self, "Pick a departure facility",
                "Select the station you're departing from before "
                "starting the workday.",
            )
            return
        final_id = self._resolve_station_id(self.final_combo)
        ship_id = self.ship_combo.currentData()
        # Round Robin is the dropdown entry whose userData is None.
        rr = final_id is None
        wid = self.controller.start_workday(origin_id, final_id, rr, ship_id=ship_id)
        self.workday_id = wid
        self.accept()

    def _open_settings(self) -> None:
        from .dialogs.settings_dialog import SettingsDialog
        dlg = SettingsDialog(self.controller, parent=self)
        dlg.exec()
