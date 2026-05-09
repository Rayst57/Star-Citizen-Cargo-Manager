"""StopCard — single route stop in RoutePanel."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
)


class StopCard(QFrame):
    view_load_requested = Signal(int)   # stop_number

    def __init__(
        self,
        stop_number: int,
        station_name: str,
        action: str,
        *,
        unload_summary: str = "",
        load_summary: str = "",
        conflict_note: str = "",
        is_current: bool = False,
        parent=None,
    ):
        super().__init__(parent)
        self.stop_number = stop_number
        self.setObjectName("card")
        self.setProperty("conflict", bool(conflict_note))

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(4)

        # Header row
        header = QHBoxLayout()
        marker = "▶ " if is_current else ""
        n_lbl = QLabel(f"{marker}Stop {stop_number}")
        n_lbl.setProperty("heading", True)
        header.addWidget(n_lbl)

        st = QLabel(station_name)
        header.addWidget(st)

        ac = QLabel(f"— {action}")
        ac.setProperty("muted", True)
        header.addWidget(ac)
        header.addStretch(1)

        view_btn = QPushButton("↗")
        view_btn.setProperty("flat", True)
        view_btn.setToolTip("View load — full bay snapshot at this stop")
        view_btn.setFixedWidth(28)
        view_btn.clicked.connect(lambda: self.view_load_requested.emit(stop_number))
        header.addWidget(view_btn)

        root.addLayout(header)

        # Unload
        if unload_summary:
            ul = QLabel(unload_summary)
            ul.setWordWrap(True)
            root.addWidget(ul)

        # Load
        if load_summary:
            ll = QLabel(load_summary)
            ll.setProperty("muted", True)
            ll.setWordWrap(True)
            root.addWidget(ll)

        # Conflict note
        if conflict_note:
            cn = QLabel(f"⚠ {conflict_note}")
            cn.setWordWrap(True)
            cn.setStyleSheet("color: #ffbe20; font-weight: bold;")
            root.addWidget(cn)
