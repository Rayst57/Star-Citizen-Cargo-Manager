"""
ManualStopDialog — insert an unscheduled stop into the route.

Lets the operator force an extra stop between the planner's
contract-driven stops, e.g. "go to Baijini and unload" once the
ship is filling up. Existing manual stops are listed at the bottom
with per-row Remove buttons so the user can manage them in one place.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QFrame, QHBoxLayout,
    QLabel, QLineEdit, QMessageBox, QPushButton, QScrollArea, QVBoxLayout,
    QWidget,
)

from .add_contract import _make_station_combo, _station_id
from ...app_controller import AppController


class ManualStopDialog(QDialog):
    """Add / remove user-injected route stops."""

    def __init__(self, controller: AppController, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.setWindowTitle("Add Manual Stop")
        self.setMinimumWidth(520)

        root = QVBoxLayout(self)

        intro = QLabel(
            "Insert an extra stop into the current route. Any onboard "
            "cargo bound for that station will unload automatically."
        )
        intro.setProperty("muted", True)
        intro.setWordWrap(True)
        root.addWidget(intro)

        # ── New-stop form ────────────────────────────────────────────────
        form = QFormLayout()
        self.station_combo = _make_station_combo(controller)
        form.addRow("Stop at", self.station_combo)

        self.after_combo = QComboBox()
        self._populate_after_combo()
        form.addRow("Insert after", self.after_combo)

        self.notes_edit = QLineEdit()
        self.notes_edit.setPlaceholderText("Optional notes (e.g. 'getting full')")
        form.addRow("Notes", self.notes_edit)
        root.addLayout(form)

        # ── Existing manual stops ────────────────────────────────────────
        existing_label = QLabel("Existing manual stops")
        existing_label.setProperty("heading", True)
        root.addWidget(existing_label)

        self.existing_widget = QWidget()
        self.existing_layout = QVBoxLayout(self.existing_widget)
        self.existing_layout.setContentsMargins(0, 0, 0, 0)
        self.existing_layout.setSpacing(4)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setMinimumHeight(120)
        scroll.setWidget(self.existing_widget)
        root.addWidget(scroll, 1)

        # ── Buttons ──────────────────────────────────────────────────────
        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Close
        )
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("Add stop")
        bb.accepted.connect(self._on_add_clicked)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)

        self._refresh_existing()

    # ── helpers ─────────────────────────────────────────────────────────

    def _populate_after_combo(self) -> None:
        """Fill the 'Insert after' combo with the current route's stops."""
        self.after_combo.clear()
        # First option always = insert at start.
        self.after_combo.addItem("At the start of the route", userData=None)
        result = self.controller.get_last_result()
        if not result or not result.route_stops:
            return
        for stop in result.route_stops:
            label = f"After stop {stop.stop_number}: {stop.station_name}"
            self.after_combo.addItem(label, userData=stop.station_id)

    def _refresh_existing(self) -> None:
        # Clear current rows
        while self.existing_layout.count():
            item = self.existing_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.hide()
                w.deleteLater()
        rows = self.controller.list_manual_stops()
        if not rows:
            empty = QLabel("No manual stops yet.")
            empty.setProperty("muted", True)
            self.existing_layout.addWidget(empty)
            return
        for r in rows:
            self.existing_layout.addWidget(self._build_row(r))
        self.existing_layout.addStretch(1)

    def _build_row(self, row) -> QFrame:
        frame = QFrame()
        frame.setObjectName("card")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(6)

        after_text = (
            row["after_station_name"]
            if row["after_station_name"]
            else "the start"
        )
        label = QLabel(
            f"<b>{row['station_name']}</b>  "
            f"<span style='color:#888'>after {after_text}</span>"
        )
        if row["notes"]:
            label.setToolTip(row["notes"])
        layout.addWidget(label, 1)

        remove_btn = QPushButton("Remove")
        remove_btn.setProperty("flat", True)
        ms_id = row["id"]
        remove_btn.clicked.connect(lambda _checked=False, mid=ms_id: self._remove(mid))
        layout.addWidget(remove_btn)
        return frame

    def _remove(self, manual_stop_id: int) -> None:
        self.controller.remove_manual_stop(manual_stop_id)
        # The route's stop list changes when manual stops change, so
        # refresh the 'Insert after' combo to keep it in sync too.
        self._populate_after_combo()
        self._refresh_existing()

    # ── add action ───────────────────────────────────────────────────────

    def _on_add_clicked(self) -> None:
        station_id = _station_id(self.station_combo)
        if station_id is None:
            QMessageBox.warning(
                self, "Missing station",
                "Pick or type a station for the manual stop.",
            )
            self.station_combo.setFocus()
            return
        after_id = self.after_combo.currentData()
        # currentData() returns None for 'At the start of the route'
        # — that's intentional (matches the schema's NULL semantics).
        notes = self.notes_edit.text().strip()
        ms_id = self.controller.add_manual_stop(station_id, after_id)
        if notes:
            self.controller.conn.execute(
                "UPDATE manual_stops SET notes = ? WHERE id = ?",
                (notes, ms_id),
            )
            self.controller.conn.commit()
        # Reset the form for another add and refresh the list.
        self.station_combo.setCurrentIndex(-1)
        self.notes_edit.clear()
        self._refresh_existing()
