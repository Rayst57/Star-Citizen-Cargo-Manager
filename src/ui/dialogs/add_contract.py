"""
AddContractDialog — Add or edit a contract.

One pickup station + max pallet size, plus 1-N delivery rows.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QFrame,
    QHBoxLayout, QLabel, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)


PALLET_SIZES = [1, 2, 4, 8, 16, 24, 32]


class DeliveryRow(QFrame):
    """One delivery line inside the contract dialog."""

    def __init__(self, controller, *, on_remove, parent=None):
        super().__init__(parent)
        self.controller = controller
        self._on_remove = on_remove

        self.setObjectName("card")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(6)

        self.station_combo = QComboBox()
        self.commodity_combo = QComboBox()
        self.scu_spin = QSpinBox()
        self.scu_spin.setRange(1, 696)
        self.scu_spin.setValue(8)
        self.scu_spin.setSuffix(" SCU")

        self._populate_stations()
        self._populate_commodities()

        layout.addWidget(QLabel("→"))
        layout.addWidget(self.station_combo, 2)
        layout.addWidget(self.commodity_combo, 2)
        layout.addWidget(self.scu_spin, 1)

        rm = QPushButton("✕")
        rm.setProperty("flat", True)
        rm.setFixedWidth(28)
        rm.clicked.connect(lambda: self._on_remove(self))
        layout.addWidget(rm)

    def _populate_stations(self) -> None:
        rows = self.controller.conn.execute(
            """
            SELECT id, name FROM stations
            WHERE is_active = 1 AND is_gateway = 0
            ORDER BY sort_order
            """
        ).fetchall()
        for r in rows:
            self.station_combo.addItem(r["name"], userData=r["id"])

    def _populate_commodities(self) -> None:
        rows = self.controller.conn.execute(
            "SELECT id, name FROM commodities WHERE is_active = 1 ORDER BY name"
        ).fetchall()
        for r in rows:
            self.commodity_combo.addItem(r["name"], userData=r["id"])

    def value(self) -> dict:
        return {
            "destination": self.station_combo.currentData(),
            "commodity":   self.commodity_combo.currentData(),
            "scu":         self.scu_spin.value(),
        }

    def set_value(self, dest_id: int, commodity_id: int, scu: int) -> None:
        for i in range(self.station_combo.count()):
            if self.station_combo.itemData(i) == dest_id:
                self.station_combo.setCurrentIndex(i)
                break
        for i in range(self.commodity_combo.count()):
            if self.commodity_combo.itemData(i) == commodity_id:
                self.commodity_combo.setCurrentIndex(i)
                break
        self.scu_spin.setValue(scu)


class AddContractDialog(QDialog):
    def __init__(
        self,
        controller,
        *,
        contract: dict | None = None,
        parsed_text: str | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.controller = controller
        self.contract = contract
        self.setWindowTitle("Edit Contract" if contract else "New Contract")
        self.setMinimumWidth(560)

        root = QVBoxLayout(self)

        # Top form
        form = QFormLayout()
        self.pickup_combo = QComboBox()
        self._populate_stations(self.pickup_combo)
        form.addRow("Pickup station", self.pickup_combo)

        self.max_combo = QComboBox()
        for s in PALLET_SIZES:
            self.max_combo.addItem(str(s), userData=s)
        self.max_combo.setCurrentText("8")
        form.addRow("Max pallet size", self.max_combo)
        root.addLayout(form)

        # Cargo lines section
        lines_label = QLabel("Cargo deliveries")
        lines_label.setProperty("heading", True)
        root.addWidget(lines_label)

        self.rows_widget = QWidget()
        self.rows_layout = QVBoxLayout(self.rows_widget)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(4)
        root.addWidget(self.rows_widget)

        add_line_btn = QPushButton("+ Add line")
        add_line_btn.setProperty("flat", True)
        add_line_btn.clicked.connect(lambda: self._add_row())
        root.addWidget(add_line_btn, 0, Qt.AlignmentFlag.AlignLeft)

        # Voice transcript
        if parsed_text:
            tr = QLabel(f"Parsed: \"{parsed_text}\"")
            tr.setProperty("muted", True)
            tr.setWordWrap(True)
            root.addWidget(tr)

        # Buttons
        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)

        # Pre-populate when editing
        if contract:
            self._load_contract(contract)
        else:
            self._add_row()

    def _populate_stations(self, combo: QComboBox) -> None:
        rows = self.controller.conn.execute(
            """
            SELECT id, name FROM stations
            WHERE is_active = 1 AND is_gateway = 0
            ORDER BY sort_order
            """
        ).fetchall()
        for r in rows:
            combo.addItem(r["name"], userData=r["id"])

    def _add_row(self) -> DeliveryRow:
        row = DeliveryRow(self.controller, on_remove=self._remove_row)
        self.rows_layout.addWidget(row)
        return row

    def _remove_row(self, row: DeliveryRow) -> None:
        if self.rows_layout.count() <= 1:
            return  # always keep at least one
        row.setParent(None)

    def _load_contract(self, contract: dict) -> None:
        # Pickup station
        for i in range(self.pickup_combo.count()):
            if self.pickup_combo.itemData(i) == contract["pickup_station_id"]:
                self.pickup_combo.setCurrentIndex(i)
                break
        # Max pallet
        self.max_combo.setCurrentText(str(contract["max_pallet_size"]))
        # Deliveries
        for d in contract["deliveries"]:
            row = self._add_row()
            row.set_value(d["delivery_station_id"], d["commodity_id"], d["scu_amount"])

    # ── results ────────────────────────────────────────────────────────

    def value(self) -> dict:
        rows: list[DeliveryRow] = [
            self.rows_layout.itemAt(i).widget()
            for i in range(self.rows_layout.count())
            if isinstance(self.rows_layout.itemAt(i).widget(), DeliveryRow)
        ]
        return {
            "pickup_station":  self.pickup_combo.currentData(),
            "max_pallet_size": self.max_combo.currentData(),
            "deliveries":      [r.value() for r in rows],
        }
