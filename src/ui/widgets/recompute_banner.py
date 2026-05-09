"""Persistent dirty-flag banner with a Recompute button."""

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
        self.label = QLabel("⚠  Modifications made — recompute required")
        layout.addWidget(self.label)
        layout.addStretch(1)
        btn = QPushButton("Recompute")
        btn.clicked.connect(self.recompute_clicked.emit)
        layout.addWidget(btn)
        self.hide()

    def set_dirty(self, dirty: bool, n_changes: int = 0) -> None:
        if dirty:
            if n_changes:
                self.label.setText(
                    f"⚠  {n_changes} modifications pending — recompute required"
                )
            else:
                self.label.setText("⚠  Modifications made — recompute required")
            self.show()
        else:
            self.hide()
