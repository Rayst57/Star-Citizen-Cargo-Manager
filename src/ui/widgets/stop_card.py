"""StopCard — single route stop in RoutePanel."""

from __future__ import annotations

from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout


class StopCard(QFrame):
    """Compact card representing one route stop. No popup; per-stop
    detail lives in the combined DetailedPlanDialog (one popup for all
    stops, opened from the Route panel header)."""

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
        st.setWordWrap(True)
        header.addWidget(st, 1)

        ac = QLabel(f"— {action}")
        ac.setProperty("muted", True)
        header.addWidget(ac)

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
            cn.setStyleSheet("color: #ff8a3c; font-weight: bold;")
            root.addWidget(cn)
