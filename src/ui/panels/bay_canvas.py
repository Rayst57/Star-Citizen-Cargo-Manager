"""
BayCanvas — top-down cargo-bay visualization (zone-strip overview).

The main window uses a zone-strip rendering: each zone column shows the
destination color (dominant) for the cargo it carries, with an SCU
progress fill indicating how full it is. Mixed-destination zones flash
with two-tone diagonal stripes until the user clicks the zone — the
click opens a `ZoneDetailDialog` showing the granular top-down + side
view of that single zone.

LoadViewModal (per-stop modal) keeps the original individual-pallet
rendering — see `BayCanvasViewport` in this module which is also used
there with `interactive=False`.

Bay layout is fully data-driven: the controller hands over a list of
`BayLayout` objects (one per cargo bay) describing each bay's cell
dimensions, zone columns, and ramp position. This lets the canvas draw
any ship — a two-bay C2 Hercules or a single-bay Starlancer MAX — with
no hardcoded geometry.

Rendering convention: rear-view, top-down — ship-forward is always at
the TOP of the screen, mirroring standard aircraft diagrams. Each bay's
`ship_forward_y` field tells us which local-Y direction points toward
the ship's nose:
  'high' → high local-Y is forward → render normally (Y=0 at the
            bottom of the bay rectangle, matching the C2 R-bay).
  'low'  → low local-Y is forward → render flipped (Y=0 at the top of
            the bay rectangle, matching the C2 F-bay's nose ramp).
The flip lives entirely in the renderer; data layer is unchanged.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtGui import (
    QBrush, QColor, QFont, QMouseEvent, QPainter, QPaintEvent,
    QPen, QResizeEvent,
)
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QPushButton, QSizePolicy,
    QVBoxLayout, QWidget,
)


def _primary_label_zones(zones):
    """Return the subset of zones that should display a label at the
    bay end.

    When two zones share the same (cube_offset_x, width_units) — e.g.
    Starlancer's RB and RBS, both centered on the bulk floor x-range
    but stacked lengthwise — drawing both labels at the bay-end slot
    collides them into garbage like "FREBS". Only the longer of the
    two is labeled; the shorter supplemental zone still shows up
    correctly in tooltips and the Zone Detail dropdown.
    """
    by_slot: dict[tuple[int, int], object] = {}
    for z in zones:
        key = (z.cube_offset_x, z.width_units)
        cur = by_slot.get(key)
        if cur is None or z.length_units > cur.length_units:
            by_slot[key] = z
    primary = set(id(z) for z in by_slot.values())
    return [z for z in zones if id(z) in primary]


def _local_y_to_screen_y(
    bay_top_y: int,
    local_y: int,
    cell_l: int,
    bay_length_cells: int,
    cell_px: int,
    ship_forward_y: str,
) -> int:
    """Convert a zone-local Y cell to a screen Y in pixels.

    The screen always shows ship-forward at the top. For 'high' bays
    (forward at high local-Y, e.g. C2 R-bay) we flip so the high end
    is at the top of the bay rect; for 'low' bays (forward at low
    local-Y, e.g. C2 F-bay nose) the local axis already points the
    same way as screen-Y so no flip is needed.
    """
    if ship_forward_y == "high":
        return bay_top_y + (bay_length_cells - local_y - cell_l) * cell_px
    return bay_top_y + local_y * cell_px


def _compute_bay_layout(
    widget_w: int, widget_h: int, bays: list,
) -> tuple[int, dict[str, QPoint]]:
    """Lay the ship's bays out left-to-right, centered in the widget.

    Returns the chosen cell size in pixels and a {bay_label: origin}
    map. Shared by both viewports so the per-pallet and zone-strip
    views always agree on geometry.
    """
    margin = 16
    gap = 30
    label_height = 42      # gap above the bay so zone labels never
                           # collide with the bay outline
    ramp_height = 22
    if not bays:
        return 14, {}

    n = len(bays)
    avail_w = max(0, widget_w - 2 * margin - gap * (n - 1))
    avail_h = max(0, widget_h - label_height - ramp_height - 2 * margin)
    total_w_cells = sum(b.width_cells for b in bays)
    max_l_cells = max(b.length_cells for b in bays)

    cell_by_w = avail_w // total_w_cells if total_w_cells else 0
    cell_by_h = avail_h // max_l_cells if max_l_cells else 0
    cell_px = max(6, min(cell_by_w, cell_by_h, 28))

    used_w = total_w_cells * cell_px + gap * (n - 1)
    x = (widget_w - used_w) // 2
    used_h = max_l_cells * cell_px + label_height + ramp_height
    # Clamp y so labels never get clipped at the top edge.
    y = max(label_height, (widget_h - used_h) // 2 + label_height)

    origins: dict[str, QPoint] = {}
    for b in bays:
        origins[b.bay_label] = QPoint(x, y)
        x += b.width_cells * cell_px + gap
    return cell_px, origins


# ── per-pallet viewport (used by LoadViewModal) ──────────────────────────

class BayCanvasViewport(QWidget):
    """Detail-view that paints individual pallet rectangles.

    Used by `LoadViewModal` (per-stop snapshot). Not used by the main
    window any more; that uses `ZoneStripsViewport` below.
    """

    pallet_dropped = Signal(int, str)   # cargo_line_id, target_zone

    def __init__(self, controller, *, interactive: bool = True, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.interactive = interactive
        self.setMinimumSize(420, 320)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)

        self._pallet_rects: list = []
        self._bays: list = []
        self._bay_by_label: dict = {}
        self._zone_to_bay: dict[str, str] = {}
        self._bay_origins: dict[str, QPoint] = {}
        self._cell_px = 14

        self._drag_id: int | None = None
        self._drag_pos: QPoint | None = None
        self._hover_zone: str | None = None

        self.setStyleSheet("background-color: #070b10;")

    def set_bay_layout(self, bays: list) -> None:
        self._bays = list(bays)
        self._bay_by_label = {b.bay_label: b for b in self._bays}
        self._zone_to_bay = {
            z.zone_label: b.bay_label
            for b in self._bays for z in b.zones
        }
        self._compute_layout()
        self.update()

    def set_pallet_rects(self, rects: list) -> None:
        self._pallet_rects = list(rects)
        self.update()

    # ── geometry ────────────────────────────────────────────────────────

    def _compute_layout(self) -> None:
        self._cell_px, self._bay_origins = _compute_bay_layout(
            self.width(), self.height(), self._bays,
        )

    def _bay_rect(self, bay_label: str) -> QRect:
        cp = self._cell_px
        o = self._bay_origins.get(bay_label)
        b = self._bay_by_label.get(bay_label)
        if o is None or b is None:
            return QRect()
        return QRect(o.x(), o.y(), b.width_cells * cp, b.length_cells * cp)

    def _zone_rect(self, zone_label: str) -> QRect:
        cp = self._cell_px
        bay_label = self._zone_to_bay.get(zone_label)
        b = self._bay_by_label.get(bay_label)
        o = self._bay_origins.get(bay_label)
        if b is None or o is None:
            return QRect()
        for z in b.zones:
            if z.zone_label == zone_label:
                return QRect(o.x() + z.cube_offset_x * cp, o.y(),
                             z.width_units * cp, b.length_cells * cp)
        return QRect()

    def _hit_test(self, pos: QPoint):
        cp = self._cell_px
        for r in self._pallet_rects:
            o = self._bay_origins.get(r.bay)
            if o is None:
                continue
            rect = QRect(o.x() + r.cell_x * cp, o.y() + r.cell_y * cp,
                         r.cell_w * cp, r.cell_l * cp)
            if rect.contains(pos):
                return r
        return None

    def _zone_at(self, pos: QPoint) -> str | None:
        for b in self._bays:
            for z in b.zones:
                if self._zone_rect(z.zone_label).contains(pos):
                    return z.zone_label
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
        if self._drag_id is not None:
            self._drag_pos = event.position().toPoint()
            self._hover_zone = self._zone_at(self._drag_pos)
            self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if not self.interactive or event.button() != Qt.MouseButton.LeftButton:
            return
        if self._drag_id is not None and self._hover_zone:
            source = next(
                (r for r in self._pallet_rects if r.cargo_line_id == self._drag_id),
                None,
            )
            target_bay = self._zone_to_bay.get(self._hover_zone)
            if source and target_bay and source.bay == target_bay:
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

        for b in self._bays:
            self._draw_bay(p, b.bay_label)
        self._draw_zone_dividers(p)
        self._draw_zone_labels(p)
        self._draw_pallets(p)
        self._draw_ramp_arrows(p)
        if self._drag_pos and self._drag_id is not None:
            self._draw_drag_preview(p)

        p.end()

    def _draw_bay(self, p: QPainter, bay_label: str) -> None:
        rect = self._bay_rect(bay_label)
        if rect.isNull():
            return
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor("#142028")))
        p.drawRect(rect)
        p.setPen(QPen(QColor("#26b6d4"), 1.5))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(rect)

    def _draw_zone_dividers(self, p: QPainter) -> None:
        cp = self._cell_px
        p.setPen(QPen(QColor("#264a5c"), 1, Qt.PenStyle.DotLine))
        for b in self._bays:
            o = self._bay_origins.get(b.bay_label)
            if o is None:
                continue
            for z in b.zones:
                if z.cube_offset_x == 0:
                    continue
                x = o.x() + z.cube_offset_x * cp
                p.drawLine(x, o.y(), x, o.y() + b.length_cells * cp)

    def _draw_zone_labels(self, p: QPainter) -> None:
        # Column labels go on the END OPPOSITE THE RAMP so the ramp
        # arrow has clearance.
        cp = self._cell_px
        font_pt = max(8, min(cp - 6, 12))
        font = QFont("Segoe UI", font_pt)
        font.setBold(True)
        p.setFont(font)
        p.setPen(QPen(QColor("#5be4ff")))
        for b in self._bays:
            o = self._bay_origins.get(b.bay_label)
            if o is None:
                continue
            for z in _primary_label_zones(b.zones):
                if b.ramp_at_top:
                    rect = QRect(o.x() + z.cube_offset_x * cp,
                                 o.y() + b.length_cells * cp + 4,
                                 z.width_units * cp, 24)
                else:
                    rect = QRect(o.x() + z.cube_offset_x * cp,
                                 o.y() - 40, z.width_units * cp, 24)
                p.drawText(rect, Qt.AlignmentFlag.AlignCenter, z.zone_label)

    def _draw_pallets(self, p: QPainter) -> None:
        cp = self._cell_px
        font = QFont("Segoe UI", max(6, cp - 6))
        p.setFont(font)
        for r in self._pallet_rects:
            if r.cargo_line_id == self._drag_id:
                continue
            o = self._bay_origins.get(r.bay)
            b = self._bay_by_label.get(r.bay)
            if o is None or b is None:
                continue
            screen_y = _local_y_to_screen_y(
                o.y(), r.cell_y, r.cell_l, b.length_cells, cp, r.ship_forward_y,
            )
            rect = QRect(o.x() + r.cell_x * cp + 1,
                         screen_y + 1,
                         r.cell_w * cp - 2,
                         r.cell_l * cp - 2)
            color = QColor(r.color)
            p.setBrush(QBrush(color))
            if r.is_conflicted:
                p.setPen(QPen(QColor("#ff3030"), 2))
            else:
                p.setPen(QPen(color.darker(140), 1))
            p.drawRect(rect)
            if r.is_conflicted:
                p.setBrush(QBrush(QColor(255, 48, 48, 70), Qt.BrushStyle.BDiagPattern))
                p.setPen(Qt.PenStyle.NoPen)
                p.drawRect(rect)
            text_color = QColor("#ffffff") if color.lightness() < 140 else QColor("#142028")
            p.setPen(QPen(text_color))
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, r.label)

    def _draw_ramp_arrows(self, p: QPainter) -> None:
        # Rear-view top-down convention: ship-forward at top of screen.
        # A bay's ramp lands either at the top (nose ramp) or the
        # bottom (rear ramp) of its rectangle; the arrow points
        # OFF-SHIP accordingly.
        cp = self._cell_px
        p.setPen(QPen(QColor("#ff8a3c"), 2))
        p.setFont(QFont("Segoe UI", 10))
        for b in self._bays:
            o = self._bay_origins.get(b.bay_label)
            if o is None:
                continue
            x = o.x() + b.width_cells * cp // 2
            if b.ramp_at_top:
                y = o.y() - 22
                text = f"▲ {b.ramp_label}"
            else:
                y = o.y() + b.length_cells * cp + 6
                text = f"▼ {b.ramp_label}"
            p.drawText(QRect(x - 70, y, 140, 18),
                       Qt.AlignmentFlag.AlignCenter, text)

    def _draw_drag_preview(self, p: QPainter) -> None:
        cp = self._cell_px
        if self._hover_zone:
            rect = self._zone_rect(self._hover_zone)
            if not rect.isNull():
                p.setBrush(QBrush(QColor(123, 191, 63, 60)))
                p.setPen(QPen(QColor("#7bbf3f"), 2))
                p.drawRect(rect)

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


# ── zone-strip viewport (used by main window) ────────────────────────────

class ZoneStripsViewport(QWidget):
    """Renders each zone column as a single colored strip.

    Mixed-destination zones flash diagonal stripes of the two destination
    colors until the user clicks them (acknowledgement).
    """

    zone_clicked = Signal(str)   # zone_label

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.setMinimumSize(420, 320)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)

        self._strips: list = []
        self._bays: list = []
        self._bay_origins: dict[str, QPoint] = {}
        self._cell_px = 14
        self._hover_zone: str | None = None
        self._acknowledged: set[str] = set()
        self._flash_on = True

        self._timer = QTimer(self)
        self._timer.setInterval(550)
        self._timer.timeout.connect(self._toggle_flash)
        self._timer.start()

        self.setStyleSheet("background-color: #070b10;")

    def set_bay_layout(self, bays: list) -> None:
        self._bays = list(bays)
        self._compute_layout()
        self.update()

    def set_strips(self, strips: list) -> None:
        # If a zone is no longer mixed, drop its acknowledgement
        valid_labels = {s.zone_label for s in strips}
        self._acknowledged &= valid_labels
        self._strips = list(strips)
        self.update()

    def _toggle_flash(self) -> None:
        self._flash_on = not self._flash_on
        # Only repaint if there's a flashing strip
        if any(s.is_mixed and s.zone_label not in self._acknowledged
               for s in self._strips):
            self.update()

    # ── geometry ────────────────────────────────────────────────────────

    def _compute_layout(self) -> None:
        self._cell_px, self._bay_origins = _compute_bay_layout(
            self.width(), self.height(), self._bays,
        )

    def _strip_rect(self, strip) -> QRect:
        cp = self._cell_px
        o = self._bay_origins.get(strip.bay)
        if o is None:
            return QRect()
        return QRect(
            o.x() + strip.cube_offset_x * cp,
            o.y() + strip.cube_offset_y * cp,
            strip.width_units * cp,
            strip.length_units * cp,
        )

    def _zone_at(self, pos: QPoint) -> str | None:
        for s in self._strips:
            if self._strip_rect(s).contains(pos):
                return s.zone_label
        return None

    # ── events ─────────────────────────────────────────────────────────

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        self._compute_layout()
        super().resizeEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        new_zone = self._zone_at(event.position().toPoint())
        if new_zone != self._hover_zone:
            self._hover_zone = new_zone
            self.update()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            return
        zone = self._zone_at(event.position().toPoint())
        if zone:
            self._acknowledged.add(zone)
            self.zone_clicked.emit(zone)
            self.update()

    # ── painting ───────────────────────────────────────────────────────

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        self._compute_layout()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        self._draw_bay_outlines(p)
        self._draw_zone_labels(p)
        for s in self._strips:
            self._draw_strip(p, s)
        self._draw_ramp_arrows(p)

        p.end()

    def _draw_bay_outlines(self, p: QPainter) -> None:
        cp = self._cell_px
        for b in self._bays:
            o = self._bay_origins.get(b.bay_label)
            if o is None:
                continue
            rect = QRect(o.x(), o.y(), b.width_cells * cp, b.length_cells * cp)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(QColor("#142028")))
            p.drawRect(rect)
            p.setPen(QPen(QColor("#26b6d4"), 1.5))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(rect)

    def _draw_zone_labels(self, p: QPainter) -> None:
        # Column labels go on the END OPPOSITE THE RAMP so the ramp
        # arrow has clearance.
        cp = self._cell_px
        font_pt = max(8, min(cp - 6, 12))
        font = QFont("Segoe UI", font_pt)
        font.setBold(True)
        p.setFont(font)
        p.setPen(QPen(QColor("#5be4ff")))
        for b in self._bays:
            o = self._bay_origins.get(b.bay_label)
            if o is None:
                continue
            for z in _primary_label_zones(b.zones):
                if b.ramp_at_top:
                    rect = QRect(o.x() + z.cube_offset_x * cp,
                                 o.y() + b.length_cells * cp + 4,
                                 z.width_units * cp, 24)
                else:
                    rect = QRect(o.x() + z.cube_offset_x * cp,
                                 o.y() - 40, z.width_units * cp, 24)
                p.drawText(rect, Qt.AlignmentFlag.AlignCenter, z.zone_label)

    def _draw_ramp_arrows(self, p: QPainter) -> None:
        # Rear-view top-down convention: ship-forward at top of screen.
        # The arrow points OFF-SHIP from whichever end the ramp is on.
        cp = self._cell_px
        p.setPen(QPen(QColor("#ff8a3c"), 2))
        p.setFont(QFont("Segoe UI", 10))
        for b in self._bays:
            o = self._bay_origins.get(b.bay_label)
            if o is None:
                continue
            x = o.x() + b.width_cells * cp // 2
            if b.ramp_at_top:
                y = o.y() - 22
                text = f"▲ {b.ramp_label}"
            else:
                y = o.y() + b.length_cells * cp + 6
                text = f"▼ {b.ramp_label}"
            p.drawText(QRect(x - 70, y, 140, 18),
                       Qt.AlignmentFlag.AlignCenter, text)

    def _draw_strip(self, p: QPainter, strip) -> None:
        rect = self._strip_rect(strip)
        if rect.isNull():
            return
        if strip.is_empty:
            self._draw_empty_strip(p, rect, strip)
            return

        # Fill always grows from the RAMP end inward, so the visible
        # block reads as "loaded depth from the door". With ship-forward
        # at top of screen, the F-bay ramp is at the top of the strip
        # and R-bay ramp is at the bottom.
        fill_ratio = min(1.0, strip.used_scu / max(strip.scu_capacity, 1))
        fill_height = int(rect.height() * fill_ratio)
        if strip.ship_forward_y == "low":
            # Ramp at top → fill from top down.
            fill_rect = QRect(rect.x(), rect.y(), rect.width(), fill_height)
        else:
            # Ramp at bottom → fill from bottom up.
            fill_top = rect.bottom() - fill_height + 1
            fill_rect = QRect(rect.x(), fill_top, rect.width(), fill_height)

        is_mixed = strip.is_mixed
        flashing = is_mixed and strip.zone_label not in self._acknowledged

        if is_mixed:
            self._draw_mixed_fill(p, fill_rect, strip, flashing)
        else:
            color = QColor(strip.primary_color)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(color))
            p.drawRect(fill_rect)

        # Conflict overlay — stripes use the conflicting destination(s)'
        # colors so paired conflict zones are visually linked. Red is
        # only used as a fallback when partner colors are missing.
        if strip.is_conflicted:
            partners = strip.conflict_partner_colors or []
            if partners:
                for i, c_str in enumerate(partners[:2]):
                    pattern = (Qt.BrushStyle.BDiagPattern if i % 2 == 0
                               else Qt.BrushStyle.FDiagPattern)
                    c = QColor(c_str)
                    c.setAlpha(140)
                    p.setBrush(QBrush(c, pattern))
                    p.setPen(Qt.PenStyle.NoPen)
                    p.drawRect(fill_rect)
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setPen(QPen(QColor(partners[0]), 2))
            else:
                p.setBrush(QBrush(QColor(255, 48, 48, 90), Qt.BrushStyle.BDiagPattern))
                p.setPen(Qt.PenStyle.NoPen)
                p.drawRect(fill_rect)
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setPen(QPen(QColor("#ff3030"), 2))
            p.drawRect(rect.adjusted(1, 1, -1, -1))

        # Hover ring
        if self._hover_zone == strip.zone_label:
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor("#ff8a3c"), 2))
            p.drawRect(rect.adjusted(1, 1, -1, -1))

        # Strips are tall and narrow — rotate the destination name 90° to
        # read along the length of the strip. SCU usage is shown by the
        # fill ratio plus the bay totals below the canvas, so no number
        # is rendered inside the strip.
        #
        # Skip text rendering entirely for tiny strips (e.g. Starlancer's
        # RBS, the 1-cube ramp-edge stub). At that size the rotated text
        # fragments and overflows into neighbors as "s H"-style garbage.
        # The destination is still visible via hover tooltip + Zone
        # Detail dialog.
        if rect.height() < 40 or rect.width() < 14:
            return

        if is_mixed:
            name_label = "MIXED"
        else:
            name_label = strip.destinations[0].station_name

        font_px = max(9, min(rect.width() - 6, 14))
        font = QFont("Segoe UI", font_px)
        font.setBold(True)
        p.save()
        p.setFont(font)
        p.setPen(QPen(QColor("#ffffff")))
        p.translate(rect.x() + rect.width() // 2, rect.y())
        p.rotate(90)
        rotated_rect = QRect(0, -rect.width() // 2,
                             rect.height(), rect.width())
        p.drawText(rotated_rect,
                   Qt.AlignmentFlag.AlignCenter,
                   name_label)
        p.restore()

    def _draw_empty_strip(self, p: QPainter, rect: QRect, strip) -> None:
        p.setPen(QPen(QColor("#264a5c"), 1, Qt.PenStyle.DashLine))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(rect.adjusted(1, 1, -1, -1))
        if self._hover_zone == strip.zone_label:
            p.setPen(QPen(QColor("#ff8a3c"), 2))
            p.drawRect(rect.adjusted(1, 1, -1, -1))

    def _draw_mixed_fill(self, p: QPainter, rect: QRect, strip, flashing: bool) -> None:
        # Two-tone diagonal stripes — primary destination color
        # alternates with secondary color. While flashing, swap which
        # color is on top each tick (handled by toggling _flash_on).
        c1 = QColor(strip.destinations[0].color)
        c2 = QColor(strip.destinations[1].color) if len(strip.destinations) > 1 else c1
        if flashing and not self._flash_on:
            c1, c2 = c2, c1

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(c1))
        p.drawRect(rect)
        p.setBrush(QBrush(c2, Qt.BrushStyle.FDiagPattern))
        p.drawRect(rect)
        # warning border
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor("#ff8a3c"), 2))
        p.drawRect(rect.adjusted(1, 1, -1, -1))


# ── BayCanvas (main panel that hosts the zone-strips viewport) ───────────

class BayCanvas(QWidget):
    pallet_dropped       = Signal(int, str)
    zone_detail_requested = Signal(str)

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.setMinimumWidth(460)
        self.setMaximumWidth(560)
        self._bay_layout: list = []

        root = QVBoxLayout(self)
        # Inset past the 22 px rounded corners so the stop selector
        # and zone strips don't poke into the cut-out area.
        root.setContentsMargins(18, 22, 18, 18)
        root.setSpacing(6)

        # Stop selector — the bay state changes at every stop (load /
        # unload), so the user picks which moment to inspect.
        stop_row = QHBoxLayout()
        stop_lbl = QLabel("Bay state at:")
        stop_lbl.setProperty("muted", True)
        stop_row.addWidget(stop_lbl)
        self.stop_combo = QComboBox()
        self.stop_combo.currentIndexChanged.connect(self._on_stop_changed)
        stop_row.addWidget(self.stop_combo, 1)
        root.addLayout(stop_row)

        # Hint
        hint = QLabel("Click a zone for the detailed top + side view.")
        hint.setProperty("muted", True)
        hint.setWordWrap(True)
        root.addWidget(hint)

        # Zone strips viewport
        self.viewport = ZoneStripsViewport(controller)
        self.viewport.zone_clicked.connect(self.zone_detail_requested.emit)
        root.addWidget(self.viewport, 1)

        # Per-bay SCU totals + a small Legend button. The full legend
        # doesn't fit horizontally on the main panel (especially with
        # the conflict chips), so it lives in a popup.
        totals = QHBoxLayout()
        self.totals_label = QLabel("")
        self.totals_label.setProperty("muted", True)
        self.totals_label.setWordWrap(True)
        legend_btn = QPushButton("Legend")
        legend_btn.setProperty("flat", True)
        legend_btn.setToolTip(
            "Show the destination color key + any conflict pairs on this stop."
        )
        legend_btn.clicked.connect(self._open_legend)
        totals.addWidget(self.totals_label, 1)
        totals.addWidget(legend_btn)
        root.addLayout(totals)

    def _open_legend(self) -> None:
        from ..dialogs.zone_detail import LegendDialog
        stop_number = self.current_stop_number()
        pallets = self.controller.get_pallet_rects(stop_number=stop_number)
        LegendDialog(self.controller, pallets=pallets, parent=self).exec()

    def current_stop_number(self) -> int | None:
        """Return the stop_number the user has selected in the dropdown,
        or None if no stop is selected (no route)."""
        return self.stop_combo.currentData()

    def refresh(self, *, stop_number: int | None = None) -> None:
        # The active ship's bay structure can change when a new workday
        # starts, so re-fetch it on every refresh.
        self._bay_layout = self.controller.get_bay_layout()
        self.viewport.set_bay_layout(self._bay_layout)

        # Repopulate the stop selector so it stays in sync with the
        # current route.  If the caller didn't pass an explicit stop,
        # use whatever the user has selected (or fall back to busiest).
        self._populate_stop_combo()

        if stop_number is None:
            stop_number = self.stop_combo.currentData()
        if stop_number is None:
            stop_number = self.controller._busiest_stop_number() or None

        self._render_stop(stop_number)

    def _populate_stop_combo(self) -> None:
        result = self.controller.get_last_result()
        prev = self.stop_combo.currentData()
        self.stop_combo.blockSignals(True)
        self.stop_combo.clear()
        if not result or not result.route_stops:
            self.stop_combo.blockSignals(False)
            return
        # Skip the FINAL stop — at the final destination the ship has been
        # offloaded so there's no "departure loadout" to show. The label
        # is just "Departure N: <station>" — the action (Arrival/Load/
        # Unload/etc) is redundant context here since the dropdown is
        # about the loadout you're carrying when you leave that stop.
        n_total = len(result.route_stops)
        for i, stop in enumerate(result.route_stops):
            if i == n_total - 1:
                continue
            label = f"Departure {i + 1}: {stop.station_name}"
            self.stop_combo.addItem(label, userData=stop.stop_number)
        # Default to STOP 1 (Initial Departure) on first compute; keep
        # whatever the user picked across recomputes if it's still valid.
        target = prev if prev is not None else 1
        for i in range(self.stop_combo.count()):
            if self.stop_combo.itemData(i) == target:
                self.stop_combo.setCurrentIndex(i)
                break
        else:
            # The previously-selected stop disappeared (route changed).
            # Fall back to Stop 1.
            for i in range(self.stop_combo.count()):
                if self.stop_combo.itemData(i) == 1:
                    self.stop_combo.setCurrentIndex(i)
                    break
        self.stop_combo.blockSignals(False)

    def _on_stop_changed(self, _idx: int) -> None:
        stop_number = self.stop_combo.currentData()
        self._render_stop(stop_number)

    def _render_stop(self, stop_number: int | None) -> None:
        strips = self.controller.get_zone_strips(stop_number=stop_number)
        self.viewport.set_strips(strips)

        # Per-bay SCU usage — one "Bay: used / capacity" chunk per bay
        # so the totals row adapts to any ship's bay count.
        used_by_bay: dict[str, int] = {}
        for s in strips:
            used_by_bay[s.bay] = used_by_bay.get(s.bay, 0) + s.used_scu
        parts = [
            f"{b.bay_label.capitalize()}: "
            f"{used_by_bay.get(b.bay_label, 0)} / {b.scu_capacity} SCU"
            for b in self._bay_layout
        ]
        self.totals_label.setText("      ".join(parts))
