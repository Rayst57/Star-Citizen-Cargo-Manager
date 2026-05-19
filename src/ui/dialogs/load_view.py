"""
LoadViewModal — read-only per-stop bay snapshot.

Shows the cargo bay state at a specific route stop. Includes prev/next
navigation so the user can step through stops without closing.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget,
)

from ..panels.bay_canvas import BayCanvasViewport


class LoadViewModal(QDialog):
    def __init__(self, controller, stop_number: int, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.stop_number = stop_number

        self.setWindowTitle("View Load")
        self.setMinimumSize(720, 540)
        self.setModal(False)

        root = QVBoxLayout(self)

        # Header
        header = QHBoxLayout()
        self.title = QLabel("")
        self.title.setProperty("heading", True)
        header.addWidget(self.title)
        header.addStretch(1)
        close_btn = QPushButton("✕")
        close_btn.setProperty("flat", True)
        close_btn.clicked.connect(self.reject)
        header.addWidget(close_btn)
        root.addLayout(header)

        # Legend
        self.legend = QWidget()
        self.legend_layout = QHBoxLayout(self.legend)
        self.legend_layout.setContentsMargins(0, 4, 0, 4)
        root.addWidget(self.legend)

        # Read-only bay viewport
        self.viewport = BayCanvasViewport(controller, interactive=False)
        self.viewport.set_bay_layout(controller.get_bay_layout())
        root.addWidget(self.viewport, 1)

        # Navigation
        nav = QHBoxLayout()
        self.prev_btn = QPushButton("← Prev")
        self.prev_btn.clicked.connect(self._prev)
        self.next_btn = QPushButton("Next →")
        self.next_btn.clicked.connect(self._next)
        self.position_label = QLabel("")
        nav.addWidget(self.prev_btn)
        nav.addStretch(1)
        nav.addWidget(self.position_label)
        nav.addStretch(1)
        nav.addWidget(self.next_btn)
        root.addLayout(nav)

        self._refresh()

    def _total_stops(self) -> int:
        result = self.controller.get_last_result()
        return len(result.route_stops) if result else 0

    def _refresh(self) -> None:
        result = self.controller.get_last_result()
        if not result:
            self.title.setText("No route computed")
            self.viewport.set_pallet_rects([])
            return

        n = len(result.route_stops)
        self.stop_number = max(1, min(self.stop_number, n))
        stop = result.route_stops[self.stop_number - 1]

        self.title.setText(
            f"Stop {stop.stop_number} — {stop.station_name} — {stop.action}"
        )
        self.position_label.setText(f"Stop {self.stop_number} of {n}")
        self.prev_btn.setEnabled(self.stop_number > 1)
        self.next_btn.setEnabled(self.stop_number < n)

        # Build legend from current destinations + colors
        self._rebuild_legend()

        rects = self.controller.get_pallet_rects(stop_number=self.stop_number)
        self.viewport.set_pallet_rects(rects)

    def _rebuild_legend(self) -> None:
        # Clear — properly delete so old chips don't become orphan windows.
        while self.legend_layout.count():
            item = self.legend_layout.takeAt(0)
            if item is None:
                continue
            w = item.widget()
            if w is not None:
                w.hide()
                w.deleteLater()

        # Pull station/color pairs that appear in this workday
        rows = self.controller.conn.execute(
            """
            SELECT DISTINCT s.name, s.color_hex
            FROM stations s
            JOIN cargo_lines cl ON cl.delivery_station_id = s.id
            JOIN contracts ct ON ct.id = cl.contract_id
            WHERE ct.workday_id = ?
              AND s.color_hex IS NOT NULL
            ORDER BY s.name
            """,
            (self.controller.workday_id,),
        ).fetchall()

        for r in rows:
            chip = QLabel(f"  {r['name']}  ")
            chip.setStyleSheet(
                f"background-color: {r['color_hex']}; color: #142028; "
                f"padding: 2px 6px; border-radius: 3px; font-weight: bold;"
            )
            self.legend_layout.addWidget(chip)
        self.legend_layout.addStretch(1)

    def _prev(self) -> None:
        if self.stop_number > 1:
            self.stop_number -= 1
            self._refresh()

    def _next(self) -> None:
        if self.stop_number < self._total_stops():
            self.stop_number += 1
            self._refresh()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Left:
            self._prev()
        elif event.key() == Qt.Key.Key_Right:
            self._next()
        elif event.key() == Qt.Key.Key_Escape:
            self.reject()
        else:
            super().keyPressEvent(event)
