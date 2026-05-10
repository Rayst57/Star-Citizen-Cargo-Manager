"""
ZoneDetailDialog — single-zone deep-dive view.

Shows two synchronised renderings of one zone (e.g. F1):
  - Top-down: looking down at the floor (width × length)
  - Side: ramp on the RIGHT (low-Y end), interior on the LEFT.
          For F-bay zones the ramp is the *nose ramp* and the
          interior is the ship middle; for R-bay zones the ramp is
          the *rear ramp* and the interior is forward (toward the
          cockpit). The pilot reads pallets left-to-right going from
          the interior to the door.

Conflict pallets are highlighted with red border + diagonal stripe.
Conflict pallets are also placed at the ramp end (low Y) by the
planner so they are first off when the zone is unloaded.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QMouseEvent, QPainter, QPen
from PySide6.QtWidgets import (
    QComboBox, QDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton,
    QSizePolicy, QVBoxLayout, QWidget,
)


def _ramp_label(bay_label: str) -> str:
    """User-visible name for the ramp on this zone's bay.

    The C2 has TWO ramps: forward bay loads via the nose ramp, rear
    bay loads via the rear ramp. Other ships may load both bays from
    a single ramp; in that case the bay_label drives the wording.
    """
    if bay_label == "forward":
        return "nose ramp"
    if bay_label == "rear":
        return "rear ramp"
    return "ramp"


def _interior_label(bay_label: str) -> str:
    """High-Y (non-ramp) end label.

    For the rear bay, high-Y is toward the cockpit, so 'forward'
    is the directionally accurate label. For the forward bay,
    high-Y is the ship interior (mid-fuselage), so 'interior' is
    more accurate than 'forward' (which would suggest the nose,
    where the F-bay ramp actually is).
    """
    if bay_label == "forward":
        return "interior"
    return "forward"


class _ConflictChip(QWidget):
    """A chip drawn with diagonal stripes in two destination colors,
    matching the BDiagPattern overlay used on conflict zones in the
    bay canvas. QLabel + stylesheet can't actually render a hatch, so
    this paints the chip directly."""

    def __init__(self, color_a: str, color_b: str, label: str, parent=None):
        super().__init__(parent)
        self._color_a = color_a
        self._color_b = color_b
        self._label = label
        self._font = QFont("Segoe UI", 9)
        self._font.setBold(True)
        # Size hint based on text width + horizontal padding.
        from PySide6.QtGui import QFontMetrics
        fm = QFontMetrics(self._font)
        self._w = fm.horizontalAdvance(label) + 16
        self._h = fm.height() + 6
        self.setFixedSize(self._w, self._h)

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = self.rect().adjusted(0, 0, -1, -1)
        # Base color
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(self._color_a)))
        p.drawRoundedRect(rect, 3, 3)
        # Diagonal-stripe overlay in partner color
        partner = QColor(self._color_b)
        partner.setAlpha(170)
        p.setBrush(QBrush(partner, Qt.BrushStyle.BDiagPattern))
        p.drawRoundedRect(rect, 3, 3)
        # Red dashed outline to flag the chip as a conflict marker.
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor("#ff3030"), 1, Qt.PenStyle.DashLine))
        p.drawRoundedRect(rect, 3, 3)
        # Label — use whichever foreground reads well against color_a.
        text_color = (QColor("#ffffff")
                      if QColor(self._color_a).lightness() < 140
                      else QColor("#142028"))
        p.setPen(QPen(text_color))
        p.setFont(self._font)
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, self._label)
        p.end()


def build_workday_color_legend(
    controller,
    pallets: list | None = None,
    active_stations: set[str] | None = None,
) -> QWidget | None:
    """Build a horizontal legend of destination colors for the workday.

    Each chip shows the station's color block followed by its name. If
    *pallets* is given and any are conflicted, an extra striped chip is
    appended per conflict pair: the partner colors are rendered as
    diagonal stripes (matching the bay's conflict overlay) and labelled
    "conflict <X> / <Y>" so the pilot can match the bay rendering
    against the destinations they belong to.

    *active_stations* filters the legend to a specific set of station
    names — used by the Zone Detail popup so the legend lists only the
    destinations whose cargo is in THAT zone, not every workday-wide
    destination. The conflict chips and partner names still resolve
    against the full workday-wide color map (so a striped chip can
    reference a partner that lives in a different zone).

    Returns None when there's nothing to legend.
    """
    all_rows = controller.conn.execute(
        """
        SELECT DISTINCT s.name, s.color_hex
        FROM stations s
        JOIN cargo_lines cl ON cl.delivery_station_id = s.id
        JOIN contracts ct ON ct.id = cl.contract_id
        WHERE ct.workday_id = ?
          AND s.color_hex IS NOT NULL
        ORDER BY s.name
        """,
        (controller.workday_id,),
    ).fetchall()
    if not all_rows:
        return None

    if active_stations is not None:
        rows = [r for r in all_rows if r["name"] in active_stations]
    else:
        rows = all_rows
    if not rows and not pallets:
        return None

    holder = QWidget()
    row = QHBoxLayout(holder)
    row.setContentsMargins(0, 4, 0, 4)
    row.setSpacing(6)

    title = QLabel("Legend:")
    title.setProperty("muted", True)
    row.addWidget(title)

    for r in rows:
        chip = QLabel(f"  {r['name']}  ")
        chip.setStyleSheet(
            f"background-color: {r['color_hex']}; color: #142028; "
            f"padding: 2px 6px; border-radius: 3px; font-weight: bold;"
        )
        row.addWidget(chip)

    if pallets:
        seen_pairs: set[frozenset[str]] = set()
        # Use the full workday color map for partner resolution — a
        # conflict's partner might not have any cargo in this zone but
        # we still want to spell out their name on the striped chip.
        color_to_name = {rr["color_hex"]: rr["name"] for rr in all_rows}
        for pl in pallets:
            if not pl.is_conflicted or not pl.conflict_partner_colors:
                continue
            for partner in pl.conflict_partner_colors[:2]:
                pair = frozenset({pl.color, partner})
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                a = color_to_name.get(pl.color, "?")
                b = color_to_name.get(partner, "?")
                row.addWidget(_ConflictChip(pl.color, partner, f"conflict {a} / {b}"))

    row.addStretch(1)
    return holder


class LegendDialog(QDialog):
    """Standalone popup showing the workday color legend.

    Used by the main bay panel where the legend doesn't fit in the
    horizontal space. Embedded versions (e.g. Zone Detail) use the
    `build_workday_color_legend` helper directly.
    """

    def __init__(
        self,
        controller,
        *,
        pallets: list | None = None,
        active_stations: set[str] | None = None,
        title: str = "Color legend",
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(420)
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(8)

        legend = build_workday_color_legend(
            controller, pallets=pallets, active_stations=active_stations,
        )
        if legend is None:
            root.addWidget(QLabel("No destinations on this workday yet."))
        else:
            root.addWidget(legend)

        # Brief explainer so the user knows what the striped chip means.
        explainer = QLabel(
            "Striped chips mark a conflict pair — pallets of those sizes "
            "are visually identical between the two destinations on the "
            "elevator, and the bay rendering uses the same diagonal "
            "stripe to flag the affected zone."
        )
        explainer.setProperty("muted", True)
        explainer.setWordWrap(True)
        root.addWidget(explainer)

        from PySide6.QtWidgets import QDialogButtonBox
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        bb.accepted.connect(self.accept)
        root.addWidget(bb)


def _draw_conflict_stripes(p: QPainter, rect: QRect, partner_colors: list[str]) -> None:
    """Stripe a conflicted pallet in the conflicting destination(s)' colors.

    For a 2-way conflict there is one partner — diagonal stripes in that
    color plus a border. For 3+ ways we alternate two patterns so both
    partners are visible. Falls back to a generic red overlay only if no
    partner colors are provided (shouldn't normally happen).
    """
    if not partner_colors:
        p.setBrush(QBrush(QColor(255, 48, 48, 100), Qt.BrushStyle.BDiagPattern))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRect(rect)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor("#ff3030"), 2))
        p.drawRect(rect)
        return
    for i, c_str in enumerate(partner_colors[:2]):
        pattern = (Qt.BrushStyle.BDiagPattern if i % 2 == 0
                   else Qt.BrushStyle.FDiagPattern)
        c = QColor(c_str)
        c.setAlpha(160)
        p.setBrush(QBrush(c, pattern))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRect(rect)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(QColor(partner_colors[0]), 2))
    p.drawRect(rect)


class _TopDownView(QWidget):
    """Top-down view of a single zone (width × length)."""

    pallet_hovered = Signal(object)   # PalletRect or None (no hover)
    pallet_clicked = Signal(object)   # PalletRect — opens the move-pallet dialog

    def __init__(self, zone_meta: dict, pallets: list, parent=None):
        super().__init__(parent)
        self.zone_meta = zone_meta
        self.pallets = pallets
        self.setMinimumSize(160, 280)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        # mouseMoveEvent fires without a button held only when tracking
        # is enabled — needed for hover-to-inspect.
        self.setMouseTracking(True)
        # (QRect on screen, PalletRect) — repopulated on every paint so
        # mouseMoveEvent can hit-test against the same rectangles the
        # user is looking at.
        self._hit_rects: list[tuple[QRect, object]] = []
        # cargo_line_id of the currently-highlighted pallet, or None.
        # The highlighted one gets a bright cyan ring on the next paint.
        self._selected_cl_id: int | None = None
        self._last_hover_cl_id: int | None = None

    def set_selected(self, cargo_line_id: int | None) -> None:
        if self._selected_cl_id == cargo_line_id:
            return
        self._selected_cl_id = cargo_line_id
        self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        pos = event.position().toPoint()
        # Iterate in reverse so the topmost-painted pallet wins on
        # overlap (top-down stacks share the same X/Y).
        hit_pl = None
        for r, pl in reversed(self._hit_rects):
            if r.contains(pos):
                hit_pl = pl
                break
        new_id = hit_pl.cargo_line_id if hit_pl is not None else None
        if new_id != self._last_hover_cl_id:
            self._last_hover_cl_id = new_id
            self.pallet_hovered.emit(hit_pl)

    def leaveEvent(self, _event) -> None:  # noqa: N802
        # Clear the highlight when the cursor leaves the widget entirely.
        if self._last_hover_cl_id is not None:
            self._last_hover_cl_id = None
            self.pallet_hovered.emit(None)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        pos = event.position().toPoint()
        for r, pl in reversed(self._hit_rects):
            if r.contains(pos):
                self.pallet_clicked.emit(pl)
                return

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        margin = 24
        title_h = 22
        ramp_h = 18
        avail_w = max(0, self.width() - 2 * margin)
        avail_h = max(0, self.height() - title_h - ramp_h - 2 * margin)
        zw = self.zone_meta["width_units"]
        zl = self.zone_meta["length_units"]
        cell = min(avail_w // zw, avail_h // zl, 36) if zw and zl else 16
        cell = max(cell, 10)
        used_w = zw * cell
        used_h = zl * cell
        x0 = (self.width() - used_w) // 2
        y0 = title_h + (self.height() - title_h - ramp_h - used_h) // 2

        # Title
        p.setPen(QPen(QColor("#5be4ff")))
        f = QFont("Segoe UI", 11)
        f.setBold(True)
        p.setFont(f)
        p.drawText(QRect(0, 4, self.width(), title_h),
                   Qt.AlignmentFlag.AlignCenter,
                   f"Top-down — {self.zone_meta['zone_label']}")

        # Floor
        rect = QRect(x0, y0, used_w, used_h)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor("#142028")))
        p.drawRect(rect)
        p.setPen(QPen(QColor("#ffffff"), 1.5))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(rect)

        # Build occupancy grid in zone-local cells. occupied[x][y] = True
        # if any pallet's footprint covers that floor cell.
        occupied = [[False] * zl for _ in range(zw)]
        for pl in self.pallets:
            lx = pl.cell_x - self.zone_meta["cube_offset_x"]
            ly = pl.cell_y - self.zone_meta["cube_offset_y"]
            for dx in range(pl.cell_w):
                for dy in range(pl.cell_l):
                    if 0 <= lx + dx < zw and 0 <= ly + dy < zl:
                        occupied[lx + dx][ly + dy] = True

        # Rear-view top-down: ship-forward at TOP of screen.
        #   ship_forward_y='high' → forward is at high local-Y → flip
        #     so high local-Y ends up at the top of the rect (R-bay).
        #   ship_forward_y='low'  → forward is at low local-Y → no
        #     flip; low local-Y is already drawn at the top (F-bay).
        forward_y = self.zone_meta.get("ship_forward_y", "high")

        def _local_y_to_screen(local_y: int, span: int = 1) -> int:
            if forward_y == "high":
                return y0 + (zl - local_y - span) * cell
            return y0 + local_y * cell

        # Draw dashed 1×1 outlines for unoccupied floor cells so the
        # pilot can see how much space is still available.
        dash_pen = QPen(QColor("#264a5c"), 1, Qt.PenStyle.DashLine)
        p.setPen(dash_pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        for x in range(zw):
            for y in range(zl):
                if occupied[x][y]:
                    continue
                screen_y = _local_y_to_screen(y)
                p.drawRect(x0 + x * cell + 1, screen_y + 1,
                           cell - 2, cell - 2)

        # Pallets — only floor footprint (z=0 layer). For stacked pallets,
        # only the bottom one is drawn here; the side view shows the stack.
        self._hit_rects = []
        already = set()
        for pl in self.pallets:
            local_x = pl.cell_x - self.zone_meta["cube_offset_x"]
            local_y = pl.cell_y - self.zone_meta["cube_offset_y"]
            if local_x < 0 or local_y < 0:
                continue
            key = (pl.cargo_line_id, local_x, local_y)
            if key in already:
                continue
            already.add(key)
            screen_y = _local_y_to_screen(local_y, pl.cell_l)
            r = QRect(x0 + local_x * cell + 1,
                      screen_y + 1,
                      pl.cell_w * cell - 2,
                      pl.cell_l * cell - 2)
            self._hit_rects.append((r, pl))
            color = QColor(pl.color)
            p.setBrush(QBrush(color))
            p.setPen(QPen(color.darker(140), 1))
            p.drawRect(r)
            if pl.is_conflicted:
                _draw_conflict_stripes(p, r, pl.conflict_partner_colors)
            text_color = QColor("#ffffff") if color.lightness() < 140 else QColor("#142028")
            p.setPen(QPen(text_color))
            f2 = QFont("Segoe UI", max(7, cell - 8))
            p.setFont(f2)
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, str(pl.pallet_size))
            if pl.cargo_line_id == self._selected_cl_id:
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setPen(QPen(QColor("#ffd45e"), 3))
                p.drawRect(r.adjusted(-1, -1, 1, 1))

        # Ramp + interior labels follow the rear-view convention:
        # ship-forward (interior side) is always at the TOP of the
        # rectangle; the ramp is at the OPPOSITE end. So for 'high'
        # bays (R-bay) the ramp is at the bottom; for 'low' bays
        # (F-bay) the ramp is at the top.
        bay = self.zone_meta.get("bay_label", "")
        ramp_at_top = forward_y == "low"
        ramp_label_text = _ramp_label(bay)
        interior_label_text = _interior_label(bay)

        if ramp_at_top:
            ramp_y, ramp_arrow = y0 - 22, "▼"   # arrow points off-ship
            interior_y = y0 + used_h + 6
        else:
            ramp_y, ramp_arrow = y0 + used_h + 4, "▲"
            interior_y = y0 - 16

        p.setPen(QPen(QColor("#ff8a3c"), 2))
        f3 = QFont("Segoe UI", 9)
        p.setFont(f3)
        p.drawText(QRect(x0, ramp_y, used_w, ramp_h),
                   Qt.AlignmentFlag.AlignCenter,
                   f"{ramp_arrow} {ramp_label_text} / door")

        p.setPen(QPen(QColor("#5be4ff")))
        f4 = QFont("Segoe UI", 8)
        p.setFont(f4)
        p.drawText(QRect(x0, interior_y, used_w, 14),
                   Qt.AlignmentFlag.AlignCenter, interior_label_text)

        p.end()


class _SideView(QWidget):
    """Side view: interior on the LEFT, ramp on the RIGHT.

    For F-bay zones the interior is mid-fuselage (the F-bay's ramp is
    the nose ramp, at low-Y). For R-bay zones the interior is forward
    of the bay, toward the cockpit.
    """

    pallet_hovered = Signal(object)   # PalletRect or None
    pallet_clicked = Signal(object)   # PalletRect — opens the move-pallet dialog

    def __init__(self, zone_meta: dict, pallets: list, parent=None):
        super().__init__(parent)
        self.zone_meta = zone_meta
        self.pallets = pallets
        self.setMinimumSize(280, 160)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self._hit_rects: list[tuple[QRect, object]] = []
        self._selected_cl_id: int | None = None
        self._last_hover_cl_id: int | None = None

    def set_selected(self, cargo_line_id: int | None) -> None:
        if self._selected_cl_id == cargo_line_id:
            return
        self._selected_cl_id = cargo_line_id
        self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        pos = event.position().toPoint()
        hit_pl = None
        for r, pl in reversed(self._hit_rects):
            if r.contains(pos):
                hit_pl = pl
                break
        new_id = hit_pl.cargo_line_id if hit_pl is not None else None
        if new_id != self._last_hover_cl_id:
            self._last_hover_cl_id = new_id
            self.pallet_hovered.emit(hit_pl)

    def leaveEvent(self, _event) -> None:  # noqa: N802
        if self._last_hover_cl_id is not None:
            self._last_hover_cl_id = None
            self.pallet_hovered.emit(None)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        pos = event.position().toPoint()
        for r, pl in reversed(self._hit_rects):
            if r.contains(pos):
                self.pallet_clicked.emit(pl)
                return

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        margin = 24
        title_h = 22
        ramp_h = 18
        zl = self.zone_meta["length_units"]
        zh_units = max(1, self.zone_meta["scu_capacity"] // (
            self.zone_meta["width_units"] * self.zone_meta["length_units"]
        )) if self.zone_meta["width_units"] * self.zone_meta["length_units"] else 4

        avail_w = max(0, self.width() - 2 * margin)
        avail_h = max(0, self.height() - title_h - ramp_h - 2 * margin)
        cell = min(avail_w // zl, avail_h // zh_units, 36) if zl and zh_units else 16
        cell = max(cell, 10)
        used_w = zl * cell
        used_h = zh_units * cell
        x0 = (self.width() - used_w) // 2
        y0 = title_h + (self.height() - title_h - ramp_h - used_h) // 2

        # Title
        bay = self.zone_meta.get("bay_label", "")
        p.setPen(QPen(QColor("#5be4ff")))
        f = QFont("Segoe UI", 11)
        f.setBold(True)
        p.setFont(f)
        p.drawText(QRect(0, 4, self.width(), title_h),
                   Qt.AlignmentFlag.AlignCenter,
                   f"Side view — {_interior_label(bay)} to the left")

        # Floor frame
        rect = QRect(x0, y0, used_w, used_h)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor("#142028")))
        p.drawRect(rect)
        p.setPen(QPen(QColor("#ffffff"), 1.5))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(rect)

        # Build (Y, Z) occupancy in zone-local cells so we can dash the
        # empty ones. Each pallet covers cell_l Y-cells × cell_h Z-cells
        # at its actual cell_z (never cumulative — two pallets at the
        # same Y but different X are NOT vertically stacked).
        occ = [[False] * zh_units for _ in range(zl)]
        for pl in self.pallets:
            local_y = pl.cell_y - self.zone_meta["cube_offset_y"]
            local_z = pl.cell_z
            if local_y < 0 or local_z < 0:
                continue
            for dy in range(pl.cell_l):
                yidx = local_y + dy
                if 0 <= yidx < zl:
                    for dz in range(pl.cell_h):
                        zidx = local_z + dz
                        if 0 <= zidx < zh_units:
                            occ[yidx][zidx] = True

        # Dashed outlines for unoccupied 1×1 cells
        dash_pen = QPen(QColor("#264a5c"), 1, Qt.PenStyle.DashLine)
        p.setPen(dash_pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        for y in range(zl):
            for z in range(zh_units):
                if occ[y][z]:
                    continue
                screen_x = x0 + (zl - 1 - y) * cell
                screen_y = y0 + (zh_units - 1 - z) * cell
                p.drawRect(screen_x + 1, screen_y + 1, cell - 2, cell - 2)

        # Pallets — forward on LEFT, ramp on RIGHT. Each pallet is drawn
        # at its actual (cell_y, cell_z) instead of guessing from the
        # cumulative stack-order; this prevents two side-by-side pallets
        # at the same Y from being rendered as if they were vertically
        # stacked.
        self._hit_rects = []
        for pl in sorted(self.pallets,
                         key=lambda p: (p.cell_y, p.cell_z, p.cell_x)):
            local_y = pl.cell_y - self.zone_meta["cube_offset_y"]
            local_z = pl.cell_z
            if local_y < 0 or local_z + pl.cell_h > zh_units:
                continue
            screen_x = x0 + (zl - local_y - pl.cell_l) * cell
            screen_y = y0 + (zh_units - local_z - pl.cell_h) * cell
            w_px = pl.cell_l * cell
            h_px = pl.cell_h * cell
            r = QRect(screen_x + 1, screen_y + 1, w_px - 2, h_px - 2)
            self._hit_rects.append((r, pl))
            color = QColor(pl.color)
            p.setBrush(QBrush(color))
            p.setPen(QPen(color.darker(140), 1))
            p.drawRect(r)
            if pl.is_conflicted:
                _draw_conflict_stripes(p, r, pl.conflict_partner_colors)
            text_color = QColor("#ffffff") if color.lightness() < 140 else QColor("#142028")
            p.setPen(QPen(text_color))
            f2 = QFont("Segoe UI", max(7, cell - 8))
            p.setFont(f2)
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, str(pl.pallet_size))
            if pl.cargo_line_id == self._selected_cl_id:
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setPen(QPen(QColor("#ffd45e"), 3))
                p.drawRect(r.adjusted(-1, -1, 1, 1))

        # Direction labels
        p.setPen(QPen(QColor("#5be4ff")))
        f3 = QFont("Segoe UI", 9)
        p.setFont(f3)
        p.drawText(QRect(x0, y0 + used_h + 4, used_w // 2, ramp_h),
                   Qt.AlignmentFlag.AlignLeft,
                   f"◀ {_interior_label(bay)}")
        p.drawText(QRect(x0 + used_w // 2, y0 + used_h + 4, used_w // 2, ramp_h),
                   Qt.AlignmentFlag.AlignRight,
                   f"{_ramp_label(bay)} ▶")

        p.end()


class MovePalletDialog(QDialog):
    """Pops up when the user clicks a pallet in Zone Detail.

    The user picks a target zone for the clicked pallet's entire cargo
    line (the planner tracks placements per cargo line, so the move
    relocates the line as a whole — every pallet that shares the
    cargo_line_id moves together). The move is committed via
    controller.move_cargo() which marks the row is_manual_override=1
    so it survives a recompute.
    """

    def __init__(self, controller, pallet, current_zone: str,
                 *, stop_number: int | None = None, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.pallet = pallet
        self.current_zone = current_zone
        self.setWindowTitle("Move pallet")
        self.setMinimumWidth(420)

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(8)

        # What we're moving.
        title = QLabel(
            f"<b>Move:</b> {pallet.pallet_size} SCU "
            f"{pallet.commodity_name} → {pallet.delivery_station_name}<br>"
            f"<span style='color:#7e96a8;'>Contract "
            f"{pallet.contract_number}, currently in {current_zone}</span>"
        )
        title.setTextFormat(Qt.TextFormat.RichText)
        title.setWordWrap(True)
        root.addWidget(title)

        # Note: this moves the WHOLE cargo line (zone_assignments
        # tracks by cargo_line_id). Make the user aware.
        scu_amount = self.controller.conn.execute(
            "SELECT scu_amount FROM cargo_lines WHERE id = ?",
            (pallet.cargo_line_id,),
        ).fetchone()
        full_scu = scu_amount["scu_amount"] if scu_amount else pallet.pallet_size
        if full_scu != pallet.pallet_size:
            note = QLabel(
                f"<span style='color:#ffcc00;'>⚠ This will move the entire "
                f"cargo line ({full_scu} SCU) — every pallet that shares "
                f"this contract line moves together.</span>"
            )
            note.setTextFormat(Qt.TextFormat.RichText)
            note.setWordWrap(True)
            root.addWidget(note)

        # Target zone picker.
        row = QHBoxLayout()
        row.addWidget(QLabel("Move cargo to:"))
        self.combo = QComboBox()
        strips = self.controller.get_zone_strips(stop_number=stop_number)
        for s in sorted(strips, key=lambda s: s.zone_label):
            if s.zone_label == current_zone:
                continue
            if s.is_empty:
                text = f"{s.zone_label}  (empty)"
            else:
                names = " + ".join(d.station_name for d in s.destinations)
                free = s.scu_capacity - s.used_scu
                text = f"{s.zone_label}  ({names}, {free} SCU free)"
            self.combo.addItem(text, userData=s.zone_label)
        row.addWidget(self.combo, 1)
        root.addLayout(row)

        # Confirm / Cancel buttons.
        from PySide6.QtWidgets import QDialogButtonBox
        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("Confirm move")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)

    def selected_zone(self) -> str | None:
        return self.combo.currentData()


class ZoneDetailDialog(QDialog):
    """Modal dialog showing one zone's loadout in top-down + side views."""

    def __init__(self, controller, zone_label: str, *, stop_number: int | None = None, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.zone_label = zone_label
        self.setWindowTitle(f"Zone {zone_label}")
        self.setMinimumSize(820, 560)

        root = QVBoxLayout(self)
        root.setSpacing(8)

        # Header with title + Legend link + close
        header = QHBoxLayout()
        title = QLabel(f"Zone {zone_label} — detail")
        title.setProperty("heading", True)
        header.addWidget(title)

        # Strip summary text (destinations + SCU)
        self.summary = QLabel("")
        self.summary.setProperty("muted", True)
        header.addWidget(self.summary, 1)

        legend_btn = QPushButton("Legend")
        legend_btn.setProperty("flat", True)
        legend_btn.setToolTip(
            "Show color key for the destinations in this zone "
            "(plus any conflict pairs that touch it)."
        )
        legend_btn.clicked.connect(self._open_legend)
        header.addWidget(legend_btn)

        close_btn = QPushButton("✕")
        close_btn.setProperty("flat", True)
        close_btn.clicked.connect(self.reject)
        header.addWidget(close_btn)
        root.addLayout(header)

        # Resolve zone metadata + pallets in this zone
        zone_meta, pallets, strip = self._fetch(stop_number)
        # Keep the pallets around for _open_legend so the popup knows
        # which destinations are actually IN this zone and which
        # conflict pairs to highlight.
        self._zone_pallets = pallets or []
        self._zone_strip = strip
        if not zone_meta:
            root.addWidget(QLabel("Zone not found."))
            return

        # Summary text
        if strip and strip.destinations:
            parts = [
                f"{d.station_name} ({d.scu_amount} SCU)"
                for d in strip.destinations
            ]
            self.summary.setText("  ·  ".join(parts))

        # Two views side by side
        views = QHBoxLayout()
        views.setSpacing(12)
        self.top_view = _TopDownView(zone_meta, pallets)
        self.side_view = _SideView(zone_meta, pallets)
        # Hover a pallet in either view → both views highlight it and
        # the pallet-info label below the views fills in. Hovering off
        # any pallet clears the highlight.
        self.top_view.pallet_hovered.connect(self._on_pallet_hovered)
        self.side_view.pallet_hovered.connect(self._on_pallet_hovered)
        # Click → open the move-pallet popup.
        self._stop_number_for_dialog = stop_number
        self.top_view.pallet_clicked.connect(self._on_pallet_clicked)
        self.side_view.pallet_clicked.connect(self._on_pallet_clicked)
        views.addWidget(self.top_view, 2)
        views.addWidget(self.side_view, 3)
        root.addLayout(views, 1)

        # Pallet-info status line — updates on hover. For a conflict
        # pallet the destination flashes between this contract's
        # destination and the partner destination(s) so the pilot can
        # see what could-be-mistaken-for-what at the elevator.
        self.pallet_info = QLabel(
            "Mouse over a pallet to see its destination + commodity."
        )
        self.pallet_info.setProperty("muted", True)
        self.pallet_info.setWordWrap(True)
        self.pallet_info.setStyleSheet(
            "color: #5be4ff; padding: 4px 8px; background-color: #0c1620; "
            "border: 1px solid #1f3242; border-radius: 3px;"
        )
        root.addWidget(self.pallet_info)

        # Workday color→station name map, used to translate a conflict
        # pallet's partner_colors into the destination names the label
        # should flash between.
        self._color_to_name: dict[str, str] = {
            r["color_hex"]: r["name"]
            for r in self.controller.conn.execute(
                "SELECT name, color_hex FROM stations "
                "WHERE color_hex IS NOT NULL"
            ).fetchall()
        }
        # Hover/flash state for conflict pallets.
        self._hovered_pallet = None
        self._flash_on = True
        self._flash_timer = QTimer(self)
        self._flash_timer.setInterval(550)
        self._flash_timer.timeout.connect(self._tick_flash)

        # Capacity bar text
        if strip:
            cap = QLabel(
                f"{strip.used_scu} / {strip.scu_capacity} SCU  ·  "
                f"{strip.scu_capacity - strip.used_scu} SCU free"
            )
            cap.setProperty("muted", True)
            cap.setAlignment(Qt.AlignmentFlag.AlignCenter)
            root.addWidget(cap)

        # Note about conflicts at ramp side
        any_conflict = any(p.is_conflicted for p in pallets)
        if any_conflict:
            note = QLabel(
                "⚠ Conflict pallets staged at the ramp end (right side of "
                "side view) for fast deconfliction."
            )
            note.setStyleSheet("color: #ff8a3c; font-weight: bold;")
            note.setWordWrap(True)
            root.addWidget(note)


        # ── Move cargo to a different zone ────────────────────────
        # Only show when this zone has cargo to move
        if strip and not strip.is_empty:
            self._add_move_controls(root, stop_number)

    def _add_move_controls(self, root: QVBoxLayout, stop_number: int | None) -> None:
        row = QHBoxLayout()
        row.addWidget(QLabel("Move cargo to:"))
        self._move_combo = QComboBox()
        self._move_combo.addItem("(stay in this zone)", userData=None)

        all_strips = self.controller.get_zone_strips(stop_number=stop_number)
        # Cache occupancy info so _apply_move can show the right prompt
        # without re-querying.
        self._target_summaries: dict[str, str] = {}
        for s in all_strips:
            if s.zone_label == self.zone_label:
                continue
            if s.is_empty:
                text = f"{s.zone_label}  (empty)"
                self._target_summaries[s.zone_label] = ""
            else:
                names = " + ".join(d.station_name for d in s.destinations)
                text = f"{s.zone_label}  ({names})"
                self._target_summaries[s.zone_label] = names
            self._move_combo.addItem(text, userData=s.zone_label)
        row.addWidget(self._move_combo, 1)

        apply_btn = QPushButton("Apply move")
        apply_btn.clicked.connect(self._apply_move)
        row.addWidget(apply_btn)
        root.addLayout(row)

    def _apply_move(self) -> None:
        target = self._move_combo.currentData()
        if not target:
            return

        existing = self._target_summaries.get(target, "")
        if not existing:
            # Empty target — nothing to disambiguate, just relocate.
            self.controller.move_zone_destination(self.zone_label, target)
            self.accept()
            return

        # Target occupied: ask the user how to resolve.
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Target zone is occupied")
        box.setText(
            f"Zone {target} already has cargo for {existing}.\n\n"
            f"How should this contract's cargo be moved into {target}?"
        )
        merge_btn = box.addButton("Merge", QMessageBox.ButtonRole.AcceptRole)
        merge_btn.setToolTip(
            f"Move this zone's cargo INTO {target} alongside the existing "
            f"cargo. Both end up sharing the zone (mixed)."
        )
        swap_btn = box.addButton("Swap", QMessageBox.ButtonRole.AcceptRole)
        swap_btn.setToolTip(
            f"Exchange the contents of {self.zone_label} and {target}. "
            f"Each zone keeps a single destination."
        )
        cancel_btn = box.addButton("Discard", QMessageBox.ButtonRole.RejectRole)
        cancel_btn.setToolTip("Don't change anything; close this dialog.")
        box.exec()

        clicked = box.clickedButton()
        if clicked is merge_btn:
            self.controller.merge_zone_into(self.zone_label, target)
            self.accept()
        elif clicked is swap_btn:
            self.controller.move_zone_destination(self.zone_label, target)
            self.accept()
        else:
            # Discard: leave the dialog open so the user can pick another
            # target without losing the rest of their context.
            return

    def _on_pallet_clicked(self, pallet) -> None:
        """Open the move-pallet popup for the clicked pallet's cargo line."""
        dlg = MovePalletDialog(
            self.controller,
            pallet,
            current_zone=self.zone_label,
            stop_number=self._stop_number_for_dialog,
            parent=self,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        target = dlg.selected_zone()
        if not target or target == self.zone_label:
            return
        self.controller.move_cargo(pallet.cargo_line_id, target)
        # The cargo line is now in a different zone — close so the user
        # sees the updated bay. (Re-rendering in-place would also need
        # the pallet positions to recompute, which the controller's
        # snapshot patch doesn't do.)
        self.accept()

    def _on_pallet_hovered(self, pallet) -> None:
        """Update the pallet-info label and highlight the hover target
        in BOTH views (so hovering the top-down also rings the matching
        pallet on the side view, and vice versa). Pass None to clear.

        For a conflict pallet we start a flash timer that alternates
        the destination between this contract's destination and the
        partner destination(s) — see _tick_flash.
        """
        if pallet is None:
            self._hovered_pallet = None
            self._flash_timer.stop()
            self.top_view.set_selected(None)
            self.side_view.set_selected(None)
            self.pallet_info.setText(
                "Mouse over a pallet to see its destination + commodity."
            )
            return

        self._hovered_pallet = pallet
        self._flash_on = True
        self.top_view.set_selected(pallet.cargo_line_id)
        self.side_view.set_selected(pallet.cargo_line_id)
        # Conflict pallets flash between the partner destinations.
        if pallet.is_conflicted and pallet.conflict_partner_colors:
            self._flash_timer.start()
        else:
            self._flash_timer.stop()
        self._render_hover_label()

    def _tick_flash(self) -> None:
        self._flash_on = not self._flash_on
        self._render_hover_label()

    def _render_hover_label(self) -> None:
        pallet = self._hovered_pallet
        if pallet is None:
            return
        commodity = pallet.commodity_name
        if not pallet.is_conflicted or not pallet.conflict_partner_colors:
            self.pallet_info.setText(
                f"{pallet.delivery_station_name}  —  {commodity}"
            )
            return
        # Conflict: alternate the destination cell between this dest
        # and the first partner. (For multi-partner groups, partners[1+]
        # are listed alongside in the steady portion.)
        partners = [
            self._color_to_name.get(c, "?")
            for c in pallet.conflict_partner_colors
        ]
        partner_summary = " / ".join(partners) if partners else "?"
        if self._flash_on:
            shown_dest = pallet.delivery_station_name
        else:
            shown_dest = partners[0] if partners else pallet.delivery_station_name
        self.pallet_info.setText(
            f"⚠ {shown_dest}  —  {commodity}  "
            f"(conflicts with {partner_summary})"
        )

    def _open_legend(self) -> None:
        # Only the destinations whose cargo is actually IN this zone.
        active = {
            d.station_name for d in (self._zone_strip.destinations
                                     if self._zone_strip else [])
        }
        LegendDialog(
            self.controller,
            pallets=self._zone_pallets,
            active_stations=active,
            title=f"Legend — Zone {self.zone_label}",
            parent=self,
        ).exec()

    def _fetch(self, stop_number: int | None):
        # Default to whichever stop has the most cargo onboard so the
        # dialog shows a useful loadout instead of the empty "Depart" stop.
        if stop_number is None:
            stop_number = self.controller._busiest_stop_number() or None

        strips = self.controller.get_zone_strips(stop_number=stop_number)
        strip = next((s for s in strips if s.zone_label == self.zone_label), None)

        row = self.controller.conn.execute(
            """
            SELECT z.zone_label, z.bay_label, z.cube_offset_x, z.cube_offset_y,
                   z.width_units, z.length_units, z.height_units, z.scu_capacity,
                   z.ship_forward_y
            FROM ship_zones z
            JOIN workdays w ON w.ship_id = z.ship_id
            WHERE w.id = ? AND z.zone_label = ?
            """,
            (self.controller.workday_id, self.zone_label),
        ).fetchone()
        zone_meta = dict(row) if row else None

        rects = self.controller.get_pallet_rects(stop_number=stop_number)
        pallets = [r for r in rects if r.zone_label == self.zone_label]
        return zone_meta, pallets, strip
