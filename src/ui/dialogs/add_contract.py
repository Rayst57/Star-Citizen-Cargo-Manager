"""
AddContractDialog — Add or edit a contract.

One pickup station + max pallet size, plus 1-N delivery rows.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QCompleter, QDialog, QDialogButtonBox, QFormLayout, QFrame,
    QHBoxLayout, QLabel, QMessageBox, QPushButton, QSpinBox, QVBoxLayout,
    QWidget,
)


PALLET_SIZES = [1, 2, 4, 8, 16, 24, 32]


def _make_station_combo(controller) -> QComboBox:
    """Station picker — alphabetical and type-to-filter.

    With ~290 stations a plain scrolling combo is unusable, so the
    combo is editable and its completer does a case-insensitive
    substring match: the user can open it and start typing to narrow
    the list down to whatever they want.

    Starts with no selection (empty line edit) so an accidental
    default never gets submitted — the user has to explicitly pick or
    type a station. The save path catches the missing pick.
    """
    combo = QComboBox()
    combo.setEditable(True)
    combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
    rows = controller.conn.execute(
        """
        SELECT id, name FROM stations
        WHERE is_active = 1 AND is_gateway = 0
        ORDER BY name COLLATE NOCASE
        """
    ).fetchall()
    for r in rows:
        combo.addItem(r["name"], userData=r["id"])
    combo.setCurrentIndex(-1)
    combo.lineEdit().setPlaceholderText("Pick or type a station…")
    completer = combo.completer()
    completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
    completer.setFilterMode(Qt.MatchFlag.MatchContains)
    completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
    return combo


def _station_id(combo: QComboBox):
    """Resolve a station combo's selected id. An editable combo's
    currentIndex can lag the typed text, so match the text to an item
    first, then fall back to currentData()."""
    idx = combo.findText(combo.currentText().strip(),
                         Qt.MatchFlag.MatchFixedString)
    if idx >= 0:
        return combo.itemData(idx)
    return combo.currentData()


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

        self.station_combo = _make_station_combo(controller)
        self.commodity_combo = QComboBox()
        self.scu_spin = QSpinBox()
        # Upper bound is intentionally generous — a single delivery
        # can legitimately exceed any one ship's capacity (the user
        # may pick up & drop off intermediates), and contract sizes
        # are creeping into the thousands as larger ships arrive.
        self.scu_spin.setRange(1, 99999)
        self.scu_spin.setValue(8)
        self.scu_spin.setSuffix(" SCU")

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

    def _populate_commodities(self) -> None:
        rows = self.controller.conn.execute(
            "SELECT id, name FROM commodities WHERE is_active = 1 ORDER BY name"
        ).fetchall()
        for r in rows:
            self.commodity_combo.addItem(r["name"], userData=r["id"])

    def value(self) -> dict:
        return {
            "destination": _station_id(self.station_combo),
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
        self.pickup_combo = _make_station_combo(controller)
        form.addRow("Pickup station", self.pickup_combo)

        # Additional pickup candidates — stations where the cargo MIGHT
        # be. The pilot visits each on arrival to find out. Empty by
        # default (single-pickup is the legacy path).
        self.candidates_widget = QWidget()
        self.candidates_layout = QVBoxLayout(self.candidates_widget)
        self.candidates_layout.setContentsMargins(0, 0, 0, 0)
        self.candidates_layout.setSpacing(4)
        form.addRow("Pickup candidates", self.candidates_widget)

        self.add_candidate_btn = QPushButton("+ Add Pickup Candidate")
        self.add_candidate_btn.setProperty("flat", True)
        self.add_candidate_btn.clicked.connect(lambda: self._add_candidate())
        form.addRow("", self.add_candidate_btn)

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

        # Buttons — From Screenshot on the left, OK/Cancel on the right.
        # The screenshot button opens ScreenCaptureDialog, parses a
        # screenshot via the vision API, and prefills the fields above
        # so the user can review/edit before saving.
        button_row = QHBoxLayout()
        self.screenshot_btn = QPushButton("📷 From Screenshot")
        self.screenshot_btn.setProperty("flat", True)
        self.screenshot_btn.setToolTip(
            "Capture a region of your screen (Star Citizen window, "
            "monitor, or any open window) and let GPT-4o vision extract "
            "the contract data."
        )
        self.screenshot_btn.clicked.connect(self._on_screenshot_clicked)
        button_row.addWidget(self.screenshot_btn)
        button_row.addStretch(1)

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        button_row.addWidget(bb)
        root.addLayout(button_row)

        # Pre-populate when editing
        if contract:
            self._load_contract(contract)
        else:
            self._add_row()

    def _add_candidate(self) -> QComboBox:
        """Append an extra pickup-candidate combo row.

        Each candidate row carries its own remove button so the user
        can prune a misclick without resetting the dialog.
        """
        row = QFrame()
        row.setObjectName("card")
        layout = QHBoxLayout(row)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(6)

        combo = _make_station_combo(self.controller)
        layout.addWidget(QLabel("alt:"))
        layout.addWidget(combo, 1)

        rm = QPushButton("✕")
        rm.setProperty("flat", True)
        rm.setFixedWidth(28)
        rm.clicked.connect(lambda: self._remove_candidate(row))
        layout.addWidget(rm)

        # Stash the combo on the row for retrieval in value().
        row._candidate_combo = combo  # type: ignore[attr-defined]
        self.candidates_layout.addWidget(row)
        return combo

    def _remove_candidate(self, row: QFrame) -> None:
        self.candidates_layout.removeWidget(row)
        row.hide()
        row.deleteLater()

    def _candidate_combos(self) -> list[QComboBox]:
        out: list[QComboBox] = []
        for i in range(self.candidates_layout.count()):
            w = self.candidates_layout.itemAt(i).widget()
            if w is None:
                continue
            combo = getattr(w, "_candidate_combo", None)
            if combo is not None:
                out.append(combo)
        return out

    def _add_row(self) -> DeliveryRow:
        row = DeliveryRow(self.controller, on_remove=self._remove_row)
        self.rows_layout.addWidget(row)
        return row

    def _remove_row(self, row: DeliveryRow) -> None:
        if self.rows_layout.count() <= 1:
            return  # always keep at least one
        self.rows_layout.removeWidget(row)
        row.hide()
        row.deleteLater()

    # ── screenshot → vision prefill ────────────────────────────────────

    def _on_screenshot_clicked(self) -> None:
        # Local import so the rest of the app doesn't pay the cost (or
        # take a hard dep on mss) when the user never opens the dialog.
        from .screen_capture import ScreenCaptureDialog
        dlg = ScreenCaptureDialog(self.controller, parent=self)
        dlg.contract_parsed.connect(self._apply_parsed_contract)
        dlg.exec()

    def _apply_parsed_contract(self, data: dict) -> None:
        """Prefill the dialog fields from a vision-parsed contract dict.

        Stations and commodities come back as *names* (the model has no
        access to our DB ids), so we set them through the editable
        line-edit and rely on the existing _station_id() resolver. If a
        name doesn't match anything in the combo's list the user sees
        the typed text and can fix it before saving.
        """
        if not isinstance(data, dict):
            QMessageBox.warning(
                self, "Parse failed",
                f"Unexpected parse result: {data!r}",
            )
            return
        if "error" in data:
            QMessageBox.warning(
                self, "No contract found", str(data["error"]),
            )
            return

        # Pickup station — set the editable text; _station_id() resolves
        # it back to an id by case-insensitive text match.
        pickup = (data.get("pickup_station") or "").strip()
        if pickup:
            self._set_station_combo_text(self.pickup_combo, pickup)

        # Max pallet size — clamp to a valid choice.
        max_size = data.get("max_pallet_size")
        if isinstance(max_size, int) and max_size in PALLET_SIZES:
            self.max_combo.setCurrentText(str(max_size))

        # Pickup candidates (multi-pickup variant). Clear any existing
        # candidate rows first so a re-parse doesn't pile up.
        candidates = data.get("pickup_candidates") or []
        if candidates:
            for row in list(self._candidate_combos()):
                # _candidate_combos returns the inner combos; the parent
                # row widget owns the remove button.
                parent_row = row.parentWidget()
                if parent_row is not None:
                    self._remove_candidate(parent_row)
            for cand in candidates:
                cand = (cand or "").strip()
                if not cand:
                    continue
                combo = self._add_candidate()
                self._set_station_combo_text(combo, cand)

        # Deliveries — clear all existing rows, then add one row per
        # parsed delivery. Always keep at least one row.
        for r in list(self._delivery_rows()):
            self.rows_layout.removeWidget(r)
            r.hide()
            r.deleteLater()

        deliveries = data.get("deliveries") or []
        if not deliveries:
            self._add_row()
            return

        for d in deliveries:
            row = self._add_row()
            dest = (d.get("destination") or "").strip()
            commodity = (d.get("commodity") or "").strip()
            scu = d.get("scu")
            if dest:
                self._set_station_combo_text(row.station_combo, dest)
            if commodity:
                self._set_commodity_combo_text(row.commodity_combo, commodity)
            if isinstance(scu, (int, float)) and scu > 0:
                row.scu_spin.setValue(int(scu))

    @staticmethod
    def _set_station_combo_text(combo: QComboBox, name: str) -> None:
        """Set an editable station combo to `name`. Prefer an exact
        case-insensitive match against the combo's existing items so
        currentData() picks up the right id; otherwise fall back to
        the raw typed text and let the user fix it."""
        idx = combo.findText(name, Qt.MatchFlag.MatchFixedString)
        if idx >= 0:
            combo.setCurrentIndex(idx)
            return
        combo.setCurrentIndex(-1)
        combo.setEditText(name)

    @staticmethod
    def _set_commodity_combo_text(combo: QComboBox, name: str) -> None:
        idx = combo.findText(name, Qt.MatchFlag.MatchFixedString)
        if idx >= 0:
            combo.setCurrentIndex(idx)

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

    def _delivery_rows(self) -> list[DeliveryRow]:
        return [
            self.rows_layout.itemAt(i).widget()
            for i in range(self.rows_layout.count())
            if isinstance(self.rows_layout.itemAt(i).widget(), DeliveryRow)
        ]

    def accept(self) -> None:  # noqa: D401
        # Blank-by-default combos mean the user has to explicitly pick;
        # catch the missing pick here so it never reaches the DB as a
        # cryptic NOT NULL failure.
        if _station_id(self.pickup_combo) is None:
            QMessageBox.warning(
                self, "Missing pickup station",
                "Pick or type a pickup station before saving.",
            )
            self.pickup_combo.setFocus()
            return
        for r in self._delivery_rows():
            if _station_id(r.station_combo) is None:
                QMessageBox.warning(
                    self, "Missing delivery station",
                    "Pick or type a delivery station for every line.",
                )
                r.station_combo.setFocus()
                return
        super().accept()

    def value(self) -> dict:
        candidates = []
        for combo in self._candidate_combos():
            sid = _station_id(combo)
            if sid is not None:
                candidates.append(sid)
        return {
            "pickup_station":   _station_id(self.pickup_combo),
            "pickup_candidates": candidates,
            "max_pallet_size":  self.max_combo.currentData(),
            "deliveries":       [r.value() for r in self._delivery_rows()],
        }
