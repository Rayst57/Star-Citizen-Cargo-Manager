"""
ZoneDetailDialog — single-zone deep-dive view.

Shows two synchronised renderings of one zone (e.g. F1):
  - Top-down: looking down at the floor (width × length)
  - Side: looking at the port side, FORWARD on the LEFT, ramp on the RIGHT
          (so the pilot reads pallets left-to-right going from the nose
          to the door — matches `ui_interactions.md` request)

Conflict pallets are highlighted with red border + diagonal stripe.
Conflict pallets are also placed at the ramp end (low Y) by the
planner so they are first off when the zone is unloaded.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QComboBox, QDialog, QHBoxLayout, QLabel, QPushButton, QSizePolicy,
    QVBoxLayout, QWidget,
)


class _TopDownView(QWidget):
    """Top-down view of a single zone (width × length)."""

    def __init__(self, zone_meta: dict, pallets: list, parent=None):
        super().__init__(parent)
        self.zone_meta = zone_meta
        self.pallets = pallets
        self.setMinimumSize(160, 280)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

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
        p.setPen(QPen(QColor("#deb447")))
        f = QFont("Segoe UI", 11)
        f.setBold(True)
        p.setFont(f)
        p.drawText(QRect(0, 4, self.width(), title_h),
                   Qt.AlignmentFlag.AlignCenter,
                   f"Top-down — {self.zone_meta['zone_label']}")

        # Floor
        rect = QRect(x0, y0, used_w, used_h)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor("#212e67")))
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

        # Draw dashed 1×1 outlines for unoccupied floor cells so the
        # pilot can see how much space is still available.
        dash_pen = QPen(QColor("#5a6aae"), 1, Qt.PenStyle.DashLine)
        p.setPen(dash_pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        for x in range(zw):
            for y in range(zl):
                if occupied[x][y]:
                    continue
                screen_y = y0 + (zl - 1 - y) * cell
                p.drawRect(x0 + x * cell + 1, screen_y + 1,
                           cell - 2, cell - 2)

        # Pallets — only floor footprint (z=0 layer). For stacked pallets,
        # only the bottom one is drawn here; the side view shows the stack.
        # Zone Y=0 is the ramp end; we flip Y on screen so the ramp ends
        # up at the BOTTOM of the view (matches the "ramp / door" label).
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
            screen_y = y0 + (zl - local_y - pl.cell_l) * cell
            r = QRect(x0 + local_x * cell + 1,
                      screen_y + 1,
                      pl.cell_w * cell - 2,
                      pl.cell_l * cell - 2)
            color = QColor(pl.color)
            p.setBrush(QBrush(color))
            p.setPen(QPen(color.darker(140), 1))
            p.drawRect(r)
            if pl.is_conflicted:
                p.setBrush(QBrush(QColor(255, 48, 48, 90), Qt.BrushStyle.BDiagPattern))
                p.setPen(Qt.PenStyle.NoPen)
                p.drawRect(r)
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setPen(QPen(QColor("#ff3030"), 2))
                p.drawRect(r)
            text_color = QColor("#ffffff") if color.lightness() < 140 else QColor("#212e67")
            p.setPen(QPen(text_color))
            f2 = QFont("Segoe UI", max(7, cell - 8))
            p.setFont(f2)
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, str(pl.pallet_size))

        # Ramp arrow at bottom (low-Y end)
        p.setPen(QPen(QColor("#ffbe20"), 2))
        f3 = QFont("Segoe UI", 9)
        p.setFont(f3)
        p.drawText(QRect(x0, y0 + used_h + 4, used_w, ramp_h),
                   Qt.AlignmentFlag.AlignCenter, "▲ ramp / door")
        # Forward marker at top
        p.setPen(QPen(QColor("#deb447")))
        f4 = QFont("Segoe UI", 8)
        p.setFont(f4)
        p.drawText(QRect(x0, y0 - 16, used_w, 14),
                   Qt.AlignmentFlag.AlignCenter, "forward")

        p.end()


class _SideView(QWidget):
    """Side view: forward on the LEFT, ramp on the RIGHT."""

    def __init__(self, zone_meta: dict, pallets: list, parent=None):
        super().__init__(parent)
        self.zone_meta = zone_meta
        self.pallets = pallets
        self.setMinimumSize(280, 160)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

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
        p.setPen(QPen(QColor("#deb447")))
        f = QFont("Segoe UI", 11)
        f.setBold(True)
        p.setFont(f)
        p.drawText(QRect(0, 4, self.width(), title_h),
                   Qt.AlignmentFlag.AlignCenter,
                   f"Side view — forward to the left")

        # Floor frame
        rect = QRect(x0, y0, used_w, used_h)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor("#212e67")))
        p.drawRect(rect)
        p.setPen(QPen(QColor("#ffffff"), 1.5))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(rect)

        # Per-Y stack columns: track which Z heights are filled.
        #   stacks[y] = list of (pallet, h)
        stacks: dict[int, list] = {}
        for pl in sorted(self.pallets, key=lambda x: (x.cell_x, x.cargo_line_id)):
            local_y = pl.cell_y - self.zone_meta["cube_offset_y"]
            if local_y < 0:
                continue
            stacks.setdefault(local_y, []).append((pl, pl.cell_h))

        # Build (Y, Z) occupancy in zone-local cells so we can dash the
        # empty ones. Each pallet covers cell_l Y-cells × cell_h Z-cells.
        occ = [[False] * zh_units for _ in range(zl)]
        z_used_by_y: dict[int, int] = {}
        for local_y in sorted(stacks):
            current_z = 0
            for pl, h in stacks[local_y]:
                for dy in range(pl.cell_l):
                    yidx = local_y + dy
                    if 0 <= yidx < zl:
                        for dz in range(h):
                            zidx = current_z + dz
                            if 0 <= zidx < zh_units:
                                occ[yidx][zidx] = True
                current_z += h
            z_used_by_y[local_y] = current_z

        # Dashed outlines for unoccupied 1×1 cells
        dash_pen = QPen(QColor("#5a6aae"), 1, Qt.PenStyle.DashLine)
        p.setPen(dash_pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        for y in range(zl):
            for z in range(zh_units):
                if occ[y][z]:
                    continue
                screen_x = x0 + (zl - 1 - y) * cell
                screen_y = y0 + (zh_units - 1 - z) * cell
                p.drawRect(screen_x + 1, screen_y + 1, cell - 2, cell - 2)

        # Pallets — forward on LEFT, ramp on RIGHT.
        for local_y, items in stacks.items():
            current_z = 0
            for pl, h in items:
                screen_x = x0 + (zl - local_y - pl.cell_l) * cell
                screen_y = y0 + (zh_units - current_z - h) * cell
                w_px = pl.cell_l * cell
                h_px = h * cell
                r = QRect(screen_x + 1, screen_y + 1, w_px - 2, h_px - 2)
                color = QColor(pl.color)
                p.setBrush(QBrush(color))
                p.setPen(QPen(color.darker(140), 1))
                p.drawRect(r)
                if pl.is_conflicted:
                    p.setBrush(QBrush(QColor(255, 48, 48, 90), Qt.BrushStyle.BDiagPattern))
                    p.setPen(Qt.PenStyle.NoPen)
                    p.drawRect(r)
                    p.setBrush(Qt.BrushStyle.NoBrush)
                    p.setPen(QPen(QColor("#ff3030"), 2))
                    p.drawRect(r)
                text_color = QColor("#ffffff") if color.lightness() < 140 else QColor("#212e67")
                p.setPen(QPen(text_color))
                f2 = QFont("Segoe UI", max(7, cell - 8))
                p.setFont(f2)
                p.drawText(r, Qt.AlignmentFlag.AlignCenter, str(pl.pallet_size))
                current_z += h

        # Direction labels
        p.setPen(QPen(QColor("#deb447")))
        f3 = QFont("Segoe UI", 9)
        p.setFont(f3)
        p.drawText(QRect(x0, y0 + used_h + 4, used_w // 2, ramp_h),
                   Qt.AlignmentFlag.AlignLeft, "◀ forward")
        p.drawText(QRect(x0 + used_w // 2, y0 + used_h + 4, used_w // 2, ramp_h),
                   Qt.AlignmentFlag.AlignRight, "ramp ▶")

        p.end()


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

        # Header with title + close
        header = QHBoxLayout()
        title = QLabel(f"Zone {zone_label} — detail")
        title.setProperty("heading", True)
        header.addWidget(title)

        # Strip summary text (destinations + SCU)
        self.summary = QLabel("")
        self.summary.setProperty("muted", True)
        header.addWidget(self.summary, 1)

        close_btn = QPushButton("✕")
        close_btn.setProperty("flat", True)
        close_btn.clicked.connect(self.reject)
        header.addWidget(close_btn)
        root.addLayout(header)

        # Resolve zone metadata + pallets in this zone
        zone_meta, pallets, strip = self._fetch(stop_number)
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
        views.addWidget(self.top_view, 2)
        views.addWidget(self.side_view, 3)
        root.addLayout(views, 1)

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
            note.setStyleSheet("color: #ffbe20; font-weight: bold;")
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
        for s in all_strips:
            if s.zone_label == self.zone_label:
                continue
            if s.is_empty:
                text = f"{s.zone_label}  (empty)"
            else:
                names = " + ".join(d.station_name for d in s.destinations)
                text = f"{s.zone_label}  ({names} — will swap)"
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
        self.controller.move_zone_destination(self.zone_label, target)
        self.accept()       # close the dialog so the user sees the updated bay

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
                   z.width_units, z.length_units, z.height_units, z.scu_capacity
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
