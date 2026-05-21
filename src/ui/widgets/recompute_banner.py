"""Persistent dirty-flag banner with a Compute/Recompute button.

Replaces the old per-panel Recompute button as the single place to
trigger a plan compute. Visible whenever a workday is active so the
button is reachable on Resume (when the saved plan is already clean
but the user may want a fresh recompute anyway).

States:
  - first_run (never computed): bright invite + "Compute"
  - dirty (modifications pending): warning glyph + "↻ Recompute"
  - clean (plan up to date): muted text + "↻ Recompute"
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton


class RecomputeBanner(QFrame):
    recompute_clicked = Signal()
    export_clicked    = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("recompute_banner")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 6, 12, 6)
        self.label = QLabel("")
        layout.addWidget(self.label)
        layout.addStretch(1)
        # Export PDF button — visible only when the plan is clean
        # (no pending modifications). A computed-and-clean plan is
        # the only state where a PDF would be meaningful.
        self.export_btn = QPushButton("📄 Export Plan as PDF")
        self.export_btn.clicked.connect(self.export_clicked.emit)
        self.export_btn.hide()
        layout.addWidget(self.export_btn)
        self.btn = QPushButton("Compute")
        self.btn.clicked.connect(self.recompute_clicked.emit)
        layout.addWidget(self.btn)
        self.hide()

    def set_dirty(
        self, dirty: bool, n_changes: int = 0, first_run: bool = False,
    ) -> None:
        """Show / hide the banner and adapt its wording.

        Hidden only when there's no active workday at all (caller
        passes dirty=False AND first_run=False AND we have no plan
        history — handled via set_no_workday). With a workday active,
        the banner stays visible so Recompute is always reachable.
        """
        if first_run:
            self.label.setText("Ready to compute the plan.")
            self.btn.setText("Compute")
            self.setProperty("state", "first_run")
            self.export_btn.hide()
        elif dirty:
            if n_changes:
                self.label.setText(
                    f"⚠  {n_changes} modifications pending — recompute required"
                )
            else:
                self.label.setText("⚠  Modifications made — recompute required")
            self.btn.setText("↻ Recompute")
            self.setProperty("state", "dirty")
            self.export_btn.hide()
        else:
            # Plan is current; banner stays visible so Recompute is
            # always reachable (esp. after Resume Day). Export PDF
            # is only available in this state — if a compute is
            # pending the exported plan would be stale.
            self.label.setText("Plan is current.")
            self.btn.setText("↻ Recompute")
            self.setProperty("state", "clean")
            self.export_btn.show()
        # Re-polish to pick up the [state] style hook if the theme
        # uses it; harmless if not.
        self.style().unpolish(self)
        self.style().polish(self)
        self.show()

    def set_no_workday(self) -> None:
        """Hide the banner when there's no active workday."""
        self.hide()
