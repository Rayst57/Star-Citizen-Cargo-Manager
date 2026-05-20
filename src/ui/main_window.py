"""
MainWindow — three-panel layout, recompute banner, status bar.

Owns the AppController reference and wires inter-panel signals. Panels
never talk to each other directly.
"""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFileDialog, QHBoxLayout, QLabel, QMainWindow, QMessageBox,
    QStatusBar, QVBoxLayout, QWidget,
)

from ..app_controller import AppController, ToolError
from .dialogs.add_contract import AddContractDialog
from .dialogs.detailed_plan import DetailedPlanDialog
from .dialogs.paste_contracts import PasteContractsDialog
from .dialogs.settings_dialog import SettingsDialog
from .dialogs.zone_detail import ZoneDetailDialog
from .panels.bay_canvas import BayCanvas
from .panels.contracts_panel import ContractsPanel
from .panels.route_panel import RoutePanel
from .widgets.mobiglass_corners import MobiglassCornerOverlay
from .widgets.recompute_banner import RecomputeBanner
from .widgets.status_bar import StatusBarWidget


class MainWindow(QMainWindow):
    def __init__(self, controller: AppController, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.setWindowTitle("Star Citizen Cargo Manager")
        self.resize(1280, 780)

        # Central layout
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Mobiglass content area (topbar + 3-panel row). The recompute
        # banner and status bar sit OUTSIDE this margined region so they
        # span the full window width.
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(10, 10, 10, 10)
        content_layout.setSpacing(12)

        # Topbar — Mobiglass-styled brand strip with its own corner taper.
        self.topbar = self._build_topbar()
        content_layout.addWidget(self.topbar)

        # Three-panel row
        panels = QWidget()
        panel_row = QHBoxLayout(panels)
        # Generous gutter between the three translucent panels so the
        # soft dark backdrop reads through.
        panel_row.setContentsMargins(0, 0, 0, 0)
        panel_row.setSpacing(14)

        self.contracts_panel = ContractsPanel(controller)
        self.bay_canvas = BayCanvas(controller)
        self.route_panel = RoutePanel(controller)

        # Mobiglass styling: object name drives the QSS rule for the
        # translucent charcoal-blue panel background, and the overlay
        # paints the cyan corner-taper accents on top. QWidget subclasses
        # need WA_StyledBackground for the stylesheet background to paint.
        for panel in (self.contracts_panel, self.bay_canvas, self.route_panel):
            panel.setObjectName("mainPanel")
            panel.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
            MobiglassCornerOverlay(panel)

        # The bay canvas has fixed-aspect content and stops growing past
        # ~460 px wide anyway. Side panels are mostly text and benefit
        # from extra width, so they get the stretch when the window grows.
        panel_row.addWidget(self.contracts_panel, 2)   # stretch=2
        panel_row.addWidget(self.bay_canvas, 0)        # stretch=0
        panel_row.addWidget(self.route_panel, 2)       # stretch=2
        content_layout.addWidget(panels, 1)

        outer.addWidget(content, 1)

        # Recompute banner (between panels and status bar) — spans the
        # full window width, outside the Mobiglass content gutter.
        self.banner = RecomputeBanner()
        outer.addWidget(self.banner)

        # Custom status bar
        sb = QStatusBar()
        self.status = StatusBarWidget()
        sb.addPermanentWidget(self.status, 1)
        self.setStatusBar(sb)

        # Wire signals
        self._wire_signals()

        # Initial paint
        self._refresh_all()

    # ── signal wiring ──────────────────────────────────────────────────

    def _wire_signals(self) -> None:
        # Contracts panel
        self.contracts_panel.add_requested.connect(self._open_add_contract)
        self.contracts_panel.paste_requested.connect(self._open_paste_contracts)
        self.contracts_panel.export_requested.connect(self._export_contracts)
        self.contracts_panel.import_requested.connect(self._import_contracts)
        self.contracts_panel.edit_requested.connect(self._open_edit_contract)
        self.contracts_panel.remove_requested.connect(self._on_remove_contract)

        # Route panel
        self.route_panel.recompute_clicked.connect(self.controller.recompute)
        self.route_panel.detailed_plan_requested.connect(self._open_detailed_plan)

        # Bay canvas
        self.bay_canvas.pallet_dropped.connect(self._on_pallet_moved)
        self.bay_canvas.zone_detail_requested.connect(self._open_zone_detail)

        # Recompute banner
        self.banner.recompute_clicked.connect(self.controller.recompute)

        # Status bar
        self.status.settings_clicked.connect(self._open_settings)

        # Controller signals
        self.controller.contracts_changed.connect(self.contracts_panel.refresh)
        self.controller.contracts_changed.connect(self.bay_canvas.refresh)
        self.controller.route_changed.connect(self.route_panel.refresh)
        self.controller.plan_dirty_changed.connect(self.banner.set_dirty)
        self.controller.recompute_started.connect(
            lambda: self.status.set_progress(0, 0)
        )
        self.controller.recompute_done.connect(self._on_recompute_done)
        self.controller.recompute_failed.connect(self._on_recompute_failed)
        self.controller.stop_progress.connect(self.status.set_progress)
        self.controller.scu_usage.connect(self.status.set_scu)
        self.controller.mic_state_changed.connect(self.status.set_mic)
        self.controller.api_health_changed.connect(self.status.set_api_health)

    # ── topbar ─────────────────────────────────────────────────────────

    def _build_topbar(self) -> QWidget:
        """Mobiglass title strip — brand on the left, corner-taper
        accents on all four corners. Matches `.topbar` from the
        HTML mockup."""
        bar = QWidget()
        bar.setObjectName("mainPanel")
        bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        bar.setFixedHeight(48)

        row = QHBoxLayout(bar)
        row.setContentsMargins(22, 0, 22, 0)
        row.setSpacing(18)

        brand = QLabel("◆ STAR CITIZEN CARGO MANAGER")
        brand.setProperty("brand", True)
        row.addWidget(brand)
        row.addStretch(1)

        MobiglassCornerOverlay(bar)
        return bar

    # ── handlers ───────────────────────────────────────────────────────

    def _refresh_all(self) -> None:
        self.contracts_panel.refresh()
        self.bay_canvas.refresh()
        self.route_panel.refresh()
        used = self.controller.total_scu_in_use()
        total = self.controller.total_scu_capacity()
        self.status.set_scu(used, total)

    def _open_add_contract(self) -> None:
        dlg = AddContractDialog(self.controller, parent=self)
        if dlg.exec():
            try:
                self.controller.add_contract(dlg.value())
            except ToolError as e:
                QMessageBox.warning(self, "Add contract failed", str(e))

    def _open_paste_contracts(self) -> None:
        dlg = PasteContractsDialog(self.controller, parent=self)
        dlg.exec()

    def _export_contracts(self) -> None:
        contracts = self.controller.list_contracts()
        if not contracts:
            QMessageBox.information(
                self, "Nothing to export",
                "No contracts in the current workday yet.",
            )
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export contracts", "contracts.json",
            "JSON files (*.json);;All files (*)",
        )
        if not path:
            return
        try:
            payload = self.controller.export_contracts()
            Path(path).write_text(
                json.dumps(payload, indent=2), encoding="utf-8",
            )
        except OSError as e:
            QMessageBox.warning(self, "Export failed", str(e))
            return
        n = len(payload["contracts"])
        self.statusBar().showMessage(
            f"Exported {n} contract{'s' if n != 1 else ''} → {path}", 5000,
        )

    def _import_contracts(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Import contracts", "",
            "JSON files (*.json);;All files (*)",
        )
        if not path:
            return
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            QMessageBox.warning(self, "Import failed",
                                f"Could not read the file:\n{e}")
            return
        try:
            added, errors = self.controller.import_contracts(payload)
        except ToolError as e:
            QMessageBox.warning(self, "Import failed", str(e))
            return
        if errors:
            preview = "\n".join(errors[:8])
            more = f"\n…and {len(errors) - 8} more." if len(errors) > 8 else ""
            QMessageBox.warning(
                self, "Import finished with issues",
                f"Imported {added} contract(s). Skipped "
                f"{len(errors)}:\n\n{preview}{more}",
            )
        else:
            QMessageBox.information(
                self, "Import complete",
                f"Imported {added} contract(s).",
            )

    def _open_edit_contract(self, contract_number: int) -> None:
        c = next(
            (x for x in self.controller.list_contracts()
             if x["contract_number"] == contract_number),
            None,
        )
        if not c:
            return
        dlg = AddContractDialog(self.controller, contract=c, parent=self)
        if dlg.exec():
            try:
                self.controller.edit_contract(contract_number, dlg.value())
            except ToolError as e:
                QMessageBox.warning(self, "Edit failed", str(e))

    def _on_remove_contract(self, contract_number: int) -> None:
        ans = QMessageBox.question(
            self,
            "Remove contract",
            f"Remove contract #{contract_number}?",
        )
        if ans == QMessageBox.StandardButton.Yes:
            try:
                self.controller.remove_contract(contract_number)
            except ToolError as e:
                QMessageBox.warning(self, "Remove failed", str(e))

    def _open_detailed_plan(self) -> None:
        dlg = DetailedPlanDialog(self.controller, parent=self)
        dlg.exec()

    def _open_settings(self) -> None:
        dlg = SettingsDialog(self.controller, parent=self)
        dlg.exec()

    def _on_pallet_moved(self, cargo_line_id: int, target_zone: str) -> None:
        self.controller.move_cargo(cargo_line_id, target_zone)
        self.bay_canvas.refresh()

    def _open_zone_detail(self, zone_label: str) -> None:
        # Open the detail at the SAME stop the user is currently
        # viewing in the main canvas, so loadout / pallet positions
        # match what they just clicked on.
        stop_number = self.bay_canvas.current_stop_number()
        dlg = ZoneDetailDialog(
            self.controller, zone_label,
            stop_number=stop_number, parent=self,
        )
        dlg.exec()

    def _on_recompute_done(self, _result) -> None:
        # The controller already emits contracts_changed / route_changed
        # which are connected to each panel's refresh slot, so the panels
        # rebuild on their own. Nothing else needed here — kept as a hook
        # for future "after compute" UI work (e.g. auto-jumping to the
        # first conflict stop).
        pass

    def _on_recompute_failed(self, msg: str) -> None:
        QMessageBox.warning(self, "Recompute failed", msg)

    # ── close handler ──────────────────────────────────────────────────

    def closeEvent(self, event) -> None:  # noqa: N802
        # Persist any pending state; nothing to cancel for v1 (no voice yet)
        super().closeEvent(event)
