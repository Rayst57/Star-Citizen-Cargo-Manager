"""
BayCanvas — top-down C2 Hercules visualization.

The custom QPainter viewport (`BayCanvasViewport`) is the hero widget.
Above the viewport sits a row of zone destination dropdowns (F1-F3 + R1-R4).
Below sits a row of SCU totals.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import (
    QBrush, QColor, QFont, QMouseEvent, QPainter, QPaintEvent,
    QPen, QResizeEvent,
)
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget,
)


# Forward bay: 6 cells wide × 9 cells deep
# Rear bay:    8 cells wide × 15 cells deep
FORWARD_W, FORWARD_L = 6, 9
REAR_W, REAR_L = 8, 15


class BayCanvasViewport(QWidget):
    """Custom-painted top-down view of both bays."""

    pallet_dropped = Signal(int, str)   # cargo_line_id, target_zone

    def __init__(self, controller, *, interactive: bool = True, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.interactive = interactive
        self.setMinimumSize(420, 320)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)

        self._pallet_rects: list = []
        self._cell_px = 14
        self._fwd_origin = QPoint(20, 30)
        self._rear_origin = QPoint(20, 30)

        # Drag state
        self._drag_id: int | None = None
        self._drag_pos: QPoint | None = None
        self._hover_zone: str | None = None

        self.setStyleSheet("background-color: #181f3f;")

    def set_pallet_rects(self, rects: list) -> None:
        self._pallet_rects = list(rects)
        self.update()

    # ── geometry ────────────────────────────────────────────────────────

    def _compute_layout(self) -> None:
        """Pick the largest cell_px that fits both bays side-by-side."""
        margin = 24
        gap = 30
        label_height = 26
        ramp_height = 18

        avail_w = max(0, self.width() - 2 * margin - gap)
        avail_h = max(0, self.height() - label_height - ramp_height - 2 * margin)

        # Total width in cells = forward + rear
        total_w_cells = FORWARD_W + REAR_W
        max_l_cells = max(FORWARD_L, REAR_L)

        cell_by_w = avail_w // total_w_cells if total_w_cells else 0
        cell_by_h = avail_h // max_l_cells if max_l_cells else 0
        self._cell_px = max(6, min(cell_by_w, cell_by_h, 28))

        # Center horizontally
        used_w = total_w_cells * self._cell_px + gap
        x_origin = (self.width() - used_w) // 2

        # Center vertically (account for label + ramp arrow)
        used_h = max_l_cells * self._cell_px + label_height + ramp_height
        y_origin = (self.height() - used_h) // 2 + label_height

        self._fwd_origin = QPoint(x_origin, y_origin)
        self._rear_origin = QPoint(
            x_origin + FORWARD_W * self._cell_px + gap,
            y_origin,
        )

    def _bay_rect(self, bay: str) -> QRect:
        cp = self._cell_px
        if bay == "forward":
            o = self._fwd_origin
            return QRect(o.x(), o.y(), FORWARD_W * cp, FORWARD_L * cp)
        else:
            o = self._rear_origin
            return QRect(o.x(), o.y(), REAR_W * cp, REAR_L * cp)

    def _cell_to_px(self, bay: str, cell_x: int, cell_y: int) -> QPoint:
        cp = self._cell_px
        o = self._fwd_origin if bay == "forward" else self._rear_origin
        return QPoint(o.x() + cell_x * cp, o.y() + cell_y * cp)

    def _hit_test(self, pos: QPoint):
        """Return the PalletRect under *pos*, or None."""
        cp = self._cell_px
        for r in self._pallet_rects:
            o = self._fwd_origin if r.bay == "forward" else self._rear_origin
            rect = QRect(o.x() + r.cell_x * cp, o.y() + r.cell_y * cp,
                         r.cell_w * cp, r.cell_l * cp)
            if rect.contains(pos):
                return r
        return None

    def _zone_at(self, pos: QPoint) -> str | None:
        """Return zone label whose cells contain *pos*."""
        cp = self._cell_px
        for bay, label, x_off, w in (
            ("forward", "F1", 0, 2), ("forward", "F2", 2, 2), ("forward", "F3", 4, 2),
            ("rear",    "R1", 0, 2), ("rear",    "R2", 2, 2),
            ("rear",    "R3", 4, 2), ("rear",    "R4", 6, 2),
        ):
            o = self._fwd_origin if bay == "forward" else self._rear_origin
            length = FORWARD_L if bay == "forward" else REAR_L
            rect = QRect(o.x() + x_off * cp, o.y(), w * cp, length * cp)
            if rect.contains(pos):
                return label
        return None

    # ── events ──────────────────────────────────────────────────────────

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        self._compute_layout()
        super().resizeEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if not self.interactive or event.button() != Qt.MouseButton.LeftButton:
            return
        hit = self._hit_test(event.position().toPoint())
        if hit:
            self._drag_id = hit.cargo_line_id
            self._drag_pos = event.position().toPoint()
            self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if not self.interactive:
            return
        pos = event.position().toPoint()
        if self._drag_id is not None:
            self._drag_pos = pos
            self._hover_zone = self._zone_at(pos)
            self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if not self.interactive or event.button() != Qt.MouseButton.LeftButton:
            return
        if self._drag_id is not None and self._hover_zone:
            # Only allow within-bay moves for v1 — find source bay
            source = next(
                (r for r in self._pallet_rects if r.cargo_line_id == self._drag_id),
                None,
            )
            target_bay = "forward" if self._hover_zone.startswith("F") else "rear"
            if source and source.bay == target_bay:
                self.pallet_dropped.emit(self._drag_id, self._hover_zone)
        self._drag_id = None
        self._drag_pos = None
        self._hover_zone = None
        self.update()

    # ── painting ────────────────────────────────────────────────────────

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        self._compute_layout()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # Background already set via stylesheet
        self._draw_bay(p, "forward", "FORWARD BAY")
        self._draw_bay(p, "rear", "REAR BAY")
        self._draw_zone_dividers(p)
        self._draw_zone_labels(p)
        self._draw_pallets(p)
        self._draw_ramp_arrow(p)
        if self._drag_pos and self._drag_id is not None:
            self._draw_drag_preview(p)

        p.end()

    def _draw_bay(self, p: QPainter, bay: str, label: str) -> None:
        rect = self._bay_rect(bay)
        # Floor
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor("#212e67")))
        p.drawRect(rect)
        # Outline
        p.setPen(QPen(QColor("#ffffff"), 1.5))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(rect)

    def _draw_zone_dividers(self, p: QPainter) -> None:
        cp = self._cell_px
        pen = QPen(QColor("#3a4894"), 1, Qt.PenStyle.DotLine)
        p.setPen(pen)

        # Forward: dividers at x=2, x=4 (in cells)
        o = self._fwd_origin
        for cx in (2, 4):
            x = o.x() + cx * cp
            p.drawLine(x, o.y(), x, o.y() + FORWARD_L * cp)

        # Rear: dividers at x=2, x=4, x=6
        o = self._rear_origin
        for cx in (2, 4, 6):
            x = o.x() + cx * cp
            p.drawLine(x, o.y(), x, o.y() + REAR_L * cp)

    def _draw_zone_labels(self, p: QPainter) -> None:
        cp = self._cell_px
        font = QFont("Segoe UI", max(7, cp - 4))
        font.setBold(True)
        p.setFont(font)
        p.setPen(QPen(QColor("#deb447")))

        for label, x_off in (("F1", 0), ("F2", 2), ("F3", 4)):
            o = self._fwd_origin
            rect = QRect(o.x() + x_off * cp, o.y() - 22, 2 * cp, 18)
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, label)

        for label, x_off in (("R1", 0), ("R2", 2), ("R3", 4), ("R4", 6)):
            o = self._rear_origin
            rect = QRect(o.x() + x_off * cp, o.y() - 22, 2 * cp, 18)
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, label)

    def _draw_pallets(self, p: QPainter) -> None:
        cp = self._cell_px
        font = QFont("Segoe UI", max(6, cp - 6))
        p.setFont(font)

        for r in self._pallet_rects:
            if r.cargo_line_id == self._drag_id:
                continue   # rendered later as preview
            o = self._fwd_origin if r.bay == "forward" else self._rear_origin
            rect = QRect(o.x() + r.cell_x * cp + 1,
                         o.y() + r.cell_y * cp + 1,
                         r.cell_w * cp - 2,
                         r.cell_l * cp - 2)
            color = QColor(r.color)
            p.setBrush(QBrush(color))
            if r.is_conflicted:
                pen = QPen(QColor("#ff3030"), 2)
                p.setPen(pen)
            else:
                p.setPen(QPen(color.darker(140), 1))
            p.drawRect(rect)

            # Diagonal stripe overlay for conflicts
            if r.is_conflicted:
                stripe = QBrush(QColor(255, 48, 48, 70), Qt.BrushStyle.BDiagPattern)
                p.setBrush(stripe)
                p.setPen(Qt.PenStyle.NoPen)
                p.drawRect(rect)

            # Label inside rectangle
            text_color = QColor("#ffffff") if color.lightness() < 140 else QColor("#212e67")
            p.setPen(QPen(text_color))
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, r.label)

    def _draw_ramp_arrow(self, p: QPainter) -> None:
        cp = self._cell_px
        o = self._rear_origin
        x = o.x() + REAR_W * cp // 2
        y = o.y() + REAR_L * cp + 6
        p.setPen(QPen(QColor("#ffbe20"), 2))
        font = QFont("Segoe UI", 10)
        p.setFont(font)
        p.drawText(QRect(x - 60, y, 120, 18),
                   Qt.AlignmentFlag.AlignCenter, "▲ ramp")

    def _draw_drag_preview(self, p: QPainter) -> None:
        cp = self._cell_px
        # Highlight hover zone if any
        if self._hover_zone:
            bay = "forward" if self._hover_zone.startswith("F") else "rear"
            x_off = {"F1": 0, "F2": 2, "F3": 4,
                     "R1": 0, "R2": 2, "R3": 4, "R4": 6}[self._hover_zone]
            o = self._fwd_origin if bay == "forward" else self._rear_origin
            length = FORWARD_L if bay == "forward" else REAR_L
            rect = QRect(o.x() + x_off * cp, o.y(), 2 * cp, length * cp)
            p.setBrush(QBrush(QColor(123, 191, 63, 60)))
            p.setPen(QPen(QColor("#7bbf3f"), 2))
            p.drawRect(rect)

        # Translucent preview at cursor
        source = next(
            (r for r in self._pallet_rects if r.cargo_line_id == self._drag_id),
            None,
        )
        if source and self._drag_pos:
            preview = QRect(
                self._drag_pos.x() - source.cell_w * cp // 2,
                self._drag_pos.y() - source.cell_l * cp // 2,
                source.cell_w * cp, source.cell_l * cp,
            )
            color = QColor(source.color)
            color.setAlpha(180)
            p.setBrush(QBrush(color))
            p.setPen(QPen(QColor("#ffffff"), 1, Qt.PenStyle.DashLine))
            p.drawRect(preview)


class BayCanvas(QWidget):
    zone_destination_changed = Signal(str, object)   # zone_label, station_id|None
    pallet_dropped = Signal(int, str)

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(4)

        # Zone dropdowns row
        dd_row = QHBoxLayout()
        dd_row.setSpacing(4)
        self.zone_combos: dict[str, QComboBox] = {}
        for label in ("F1", "F2", "F3"):
            self._add_zone_combo(dd_row, label)
        dd_row.addSpacing(20)
        for label in ("R1", "R2", "R3", "R4"):
            self._add_zone_combo(dd_row, label)
        dd_row.addStretch(1)
        root.addLayout(dd_row)

        # Viewport
        self.viewport = BayCanvasViewport(controller)
        self.viewport.pallet_dropped.connect(self.pallet_dropped.emit)
        root.addWidget(self.viewport, 1)

        # Totals row
        totals = QHBoxLayout()
        self.fwd_label = QLabel("Forward: 0 / 216 SCU")
        self.fwd_label.setProperty("muted", True)
        self.rear_label = QLabel("Rear: 0 / 480 SCU")
        self.rear_label.setProperty("muted", True)
        totals.addWidget(self.fwd_label)
        totals.addStretch(1)
        totals.addWidget(self.rear_label)
        root.addLayout(totals)

    def _add_zone_combo(self, row: QHBoxLayout, label: str) -> None:
        wrap = QVBoxLayout()
        wrap.setSpacing(2)
        wrap.addWidget(QLabel(label))
        combo = QComboBox()
        combo.addItem("(auto)", userData=None)
        combo.currentIndexChanged.connect(
            lambda _, lbl=label: self._on_combo_changed(lbl)
        )
        wrap.addWidget(combo)
        self.zone_combos[label] = combo
        row.addLayout(wrap)

    def _on_combo_changed(self, zone_label: str) -> None:
        sid = self.zone_combos[zone_label].currentData()
        self.zone_destination_changed.emit(zone_label, sid)

    def populate_zone_destinations(self, destinations: list[tuple[int, str]]) -> None:
        """Refresh the dropdowns with the active workday's destinations."""
        for combo in self.zone_combos.values():
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("(auto)", userData=None)
            for sid, name in destinations:
                combo.addItem(name, userData=sid)
            combo.blockSignals(False)

    def refresh(self, *, stop_number: int | None = None) -> None:
        # If no explicit stop and the user hasn't completed any stops yet,
        # show whichever snapshot has the most cargo onboard so the user
        # immediately sees their planned loadout.
        if stop_number is None and self.controller._current_stop_index == 0:
            result = self.controller.get_last_result()
            if result and result.snapshots:
                stop_number = max(
                    result.snapshots.keys(),
                    key=lambda k: sum(e.scu_amount for e in result.snapshots[k]),
                )
        rects = self.controller.get_pallet_rects(stop_number=stop_number)
        self.viewport.set_pallet_rects(rects)

        # Compute totals per bay
        fwd_used = sum(r.pallet_size for r in rects if r.bay == "forward")
        rear_used = sum(r.pallet_size for r in rects if r.bay == "rear")
        self.fwd_label.setText(f"Forward: {fwd_used} / 216 SCU")
        self.rear_label.setText(f"Rear: {rear_used} / 480 SCU")

        # Refresh dropdowns from contract destinations
        if self.controller.workday_id:
            rows = self.controller.conn.execute(
                """
                SELECT DISTINCT s.id, s.name
                FROM cargo_lines cl
                JOIN contracts ct ON ct.id = cl.contract_id
                JOIN stations s ON s.id = cl.delivery_station_id
                WHERE ct.workday_id = ?
                ORDER BY s.name
                """,
                (self.controller.workday_id,),
            ).fetchall()
            self.populate_zone_destinations([(r["id"], r["name"]) for r in rows])
