"""Persistent dirty-flag banner with a Compute/Recompute button.

Replaces the old per-panel Recompute button as the single place to
trigger a plan compute. Hidden when the workday is clean; shown when
plan_dirty flips True with whatever contracts/changes have piled up.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton


class RecomputeBanner(QFrame):
    recompute_clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("recompute_banner")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 6, 12, 6)
        self.label = QLabel("")
        layout.addWidget(self.label)
        layout.addStretch(1)
        self.btn = QPushButton("Compute")
        self.btn.clicked.connect(self.recompute_clicked.emit)
        layout.addWidget(self.btn)
        self.hide()

    def set_dirty(
        self, dirty: bool, n_changes: int = 0, first_run: bool = False,
    ) -> None:
        """Show / hide the banner and adapt its wording.

        first_run = True means the active workday has never been computed
        yet, so the button reads "Compute" and the label is a friendly
        invite. After the first successful compute it shifts to
        "Recompute" with the warning glyph the user already knows.
        """
        if not dirty:
            self.hide()
            return
        if first_run:
            self.label.setText("Ready to compute the plan.")
            self.btn.setText("Compute")
        else:
            if n_changes:
                self.label.setText(
                    f"⚠  {n_changes} modifications pending — recompute required"
                )
            else:
                self.label.setText("⚠  Modifications made — recompute required")
            self.btn.setText("↻ Recompute")
        self.show()
