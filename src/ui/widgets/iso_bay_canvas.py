"""
IsoBayCanvas — birds-eye axonometric (isometric) view of the cargo bay
with individual-pallet drag-and-drop locking.

Each 1.25 m cube projects to a parallelogram via a fixed 30 degree
axonometric projection (cos30/sin30). Pallets render as cuboids with
three visible faces (top, left, right) shaded from the destination
color. Painting walks pallets back-to-front so foreground occludes
background; hit-testing walks the same list reversed so the front-most
pallet under the cursor wins.

Drag-drop semantics:
    - mouse-down on a pallet "picks it up" and stops drawing it solidly.
    - while dragging, a ghost outline follows the cursor projected onto
      the ground plane (z=0) of the hovered zone, snapped to the
      nearest cube cell.
    - on release, we call ``controller.lock_pallet(...)``. Any
      ToolError raised by the controller is shown as a warning and the
      drop is aborted (no UI state change).

This file deliberately does NOT touch the controller's schema or
planner integration. It only consumes the existing
``get_bay_layout()`` / ``get_pallet_rects()`` API plus four new
methods on the controller contract:

    lock_pallet(cargo_line_id, pallet_index, zone, x, y, z) -> None
    unlock_pallet(cargo_line_id, pallet_index) -> None
    clear_pallet_locks() -> None
    list_pallet_locks() -> list[sqlite3.Row]

All four are tolerated as ``AttributeError`` at runtime — the widget
still renders, the lock UI just becomes a no-op until the foundation
agent ships their half.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush, QColor, QFont, QMouseEvent, QPainter, QPaintEvent, QPen,
    QPolygonF, QResizeEvent,
)
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QMessageBox, QPushButton, QSizePolicy,
    QVBoxLayout, QWidget,
)

try:                                                # optional dep
    from ..app_controller import ToolError  # type: ignore
except Exception:  # pragma: no cover - import via package root
    try:
        from src.app_controller import ToolError  # type: ignore
    except Exception:                                # last-ditch fallback
        class ToolError(Exception):
            """Fallback used only when app_controller hasn't been imported
            (e.g. very early test loaders). Real code always uses the
            controller's ToolError."""


# ── projection constants ─────────────────────────────────────────────────

ISO_X = math.cos(math.radians(30))      # 0.8660254...
ISO_Y = math.sin(math.radians(30))      # 0.5
DEFAULT_CELL_W = 22                     # px per cube along world-X
DEFAULT_CELL_H = 18                     # px per cube along world-Z (height)


def iso_project(
    world_x: float, world_y: float, world_z: float,
    origin_x: float, origin_y: float,
    cell_w: float = DEFAULT_CELL_W, cell_h: float = DEFAULT_CELL_H,
) -> tuple[float, float]:
    """World cube coordinates -> screen pixel coordinates.

    +X points down-and-right on screen, +Y points down-and-left,
    +Z points straight UP (negative screen-Y) — matches the SC
    Cargo Grid Reference Guide axonometric framing.
    """
    sx = origin_x + (world_x - world_y) * cell_w * ISO_X
    sy = origin_y + (world_x + world_y) * cell_w * ISO_Y - world_z * cell_h
    return sx, sy


def iso_unproject_ground(
    screen_x: float, screen_y: float,
    origin_x: float, origin_y: float,
    cell_w: float = DEFAULT_CELL_W, cell_h: float = DEFAULT_CELL_H,
) -> tuple[float, float]:
    """Inverse projection onto the ground plane (z=0).

    Returns (world_x, world_y) as floats. Snap to ``int(floor())`` to
    get a cell index.
    """
    dx = screen_x - origin_x
    dy = screen_y - origin_y
    # screen_x = (X - Y) * cell_w * ISO_X   => X - Y = dx / (cell_w*ISO_X)
    # screen_y = (X + Y) * cell_w * ISO_Y   => X + Y = dy / (cell_w*ISO_Y)
    u = dx / (cell_w * ISO_X)
    v = dy / (cell_w * ISO_Y)
    world_x = (u + v) / 2.0
    world_y = (v - u) / 2.0
    return world_x, world_y


# ── pallet shape cache ──────────────────────────────────────────────────

@dataclass
class _PalletShape:
    """Cached projected geometry for one pallet (a cuboid).

    Stores the three visible-face polygons (top, left, right) and the
    6-vertex hexagonal silhouette used for hit-testing.
    """
    rect: object                         # the PalletRect this came from
    top: QPolygonF
    left: QPolygonF
    right: QPolygonF
    silhouette: QPolygonF
    depth_key: float                     # smaller = farther from camera

    # The "depth" of a cuboid for back-to-front sort is the sum of its
    # far-corner world coords (max-X + max-Y) minus its z; smaller values
    # are FARTHER from the camera, so we draw them first.


def _cuboid_faces(
    wx: int, wy: int, wz: int,
    w: int, l: int, h: int,
    origin_x: float, origin_y: float,
    cell_w: float, cell_h: float,
) -> tuple[QPolygonF, QPolygonF, QPolygonF, QPolygonF]:
    """Return (top, left, right, silhouette) polygons for a cuboid
    spanning world cells [wx, wx+w) x [wy, wy+l) x [wz, wz+h)."""
    def P(x, y, z):
        sx, sy = iso_project(x, y, z, origin_x, origin_y, cell_w, cell_h)
        return QPointF(sx, sy)

    x0, x1 = wx, wx + w
    y0, y1 = wy, wy + l
    z0, z1 = wz, wz + h

    # Top face — z = z1 (top of pallet)
    top_quad = [P(x0, y0, z1), P(x1, y0, z1),
                P(x1, y1, z1), P(x0, y1, z1)]
    # Right face — y = y1 (the "down-right of top" face on screen for
    # iso-X axis pointing down-right). Vertices: top-back, top-front,
    # bottom-front, bottom-back.
    right_quad = [P(x1, y0, z1), P(x1, y1, z1),
                  P(x1, y1, z0), P(x1, y0, z0)]
    # Left face — x = ... mapping to "down-left of top" on screen.
    left_quad = [P(x0, y1, z1), P(x1, y1, z1),
                 P(x1, y1, z0), P(x0, y1, z0)]

    # Actually with our axes (+X down-right, +Y down-left), the two
    # visible side faces are X=x1 (down-right side) and Y=y1 (down-left
    # side). Compute both fresh and discard the placeholder above so
    # the polygons match the picture even when w != l.
    right_quad = [P(x1, y0, z1), P(x1, y1, z1),
                  P(x1, y1, z0), P(x1, y0, z0)]
    left_quad = [P(x0, y1, z1), P(x1, y1, z1),
                 P(x1, y1, z0), P(x0, y1, z0)]

    top_poly = QPolygonF(top_quad)
    right_poly = QPolygonF(right_quad)
    left_poly = QPolygonF(left_quad)

    # Silhouette — outer hexagon of the cuboid in iso, walking the
    # 6 visible corners clockwise from the top corner.
    sil = QPolygonF([
        P(x0, y0, z1),     # back-top
        P(x1, y0, z1),     # right-top-back
        P(x1, y0, z0),     # right-bot-back
        P(x1, y1, z0),     # front-bot
        P(x0, y1, z0),     # left-bot-front
        P(x0, y1, z1),     # left-top-front
    ])
    return top_poly, left_poly, right_poly, sil


def _shade(color: QColor, factor: int) -> QColor:
    """Return a color brightened (>100) or darkened (<100) by *factor*
    percent — wraps QColor.lighter/darker into one branchless helper."""
    if factor >= 100:
        return color.lighter(factor)
    return color.darker(int(100 * 100 / max(factor, 1)))


# ── widget ───────────────────────────────────────────────────────────────

class IsoBayCanvas(QWidget):
    """Standalone widget — usable as a panel or inside a QDialog."""

    pallet_locked = Signal(int, int, str, int, int, int)   # cl_id, p_idx, zone, x, y, z

    def __init__(self, controller, *, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.setMinimumSize(640, 480)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self.setStyleSheet("background-color: #050a10;")

        # state
        self._bays: list = []
        self._pallets: list = []
        self._shapes: list[_PalletShape] = []
        self._zone_meta: dict[str, dict] = {}
        self._cell_w = DEFAULT_CELL_W
        self._cell_h = DEFAULT_CELL_H
        self._origin = QPointF(0, 0)
        self._stop_number: int | None = None

        # interaction state
        self._hover_index: int | None = None       # index into _shapes
        self._drag_shape: _PalletShape | None = None
        self._drag_target: tuple[str, int, int, int] | None = None
        self._drag_cursor: QPoint | None = None

        # locked pallets (cargo_line_id, pallet_index) -> True
        self._locks: set[tuple[int, int]] = set()

        # ── top toolbar ─────────────────────────────────────────────
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)

        toolbar.addWidget(QLabel("Bay state at:"))
        self.stop_combo = QComboBox()
        self.stop_combo.currentIndexChanged.connect(self._on_stop_changed)
        toolbar.addWidget(self.stop_combo, 1)

        self.clear_btn = QPushButton("Clear all locks")
        self.clear_btn.clicked.connect(self._on_clear_locks)
        toolbar.addWidget(self.clear_btn)

        # The actual canvas paints onto `self` after the toolbar row.
        # We use a spacer widget to push content down — toolbar items
        # sit above the paint region but the QPainter target is the
        # whole widget. Reserve a fixed band for the toolbar.
        self._toolbar_h = 36
        toolbar_w = QWidget()
        toolbar_w.setFixedHeight(self._toolbar_h)
        toolbar_w.setLayout(toolbar)
        root.addWidget(toolbar_w, 0)

        # hint
        self.hint_label = QLabel(
            "Drag a pallet to lock it to a new cell. Locked pallets keep a "
            "thin gold outline. The planner will pack other cargo around "
            "them on the next recompute."
        )
        self.hint_label.setStyleSheet("color: #5be4ff;")
        self.hint_label.setWordWrap(True)
        root.addWidget(self.hint_label, 0)

        # Spacer so the paint region below toolbar+hint is the rest of
        # the widget. The viewport draws onto `self` directly, not into
        # a child widget, because we want crisp paint coordinates.
        root.addStretch(1)

    # ── public API ──────────────────────────────────────────────────────

    def refresh(self) -> None:
        """Reload bay layout + pallets from the controller and repaint."""
        try:
            self._bays = list(self.controller.get_bay_layout() or [])
        except Exception:
            self._bays = []

        self._populate_stop_combo()
        stop = self.stop_combo.currentData()
        self._stop_number = stop
        try:
            self._pallets = list(self.controller.get_pallet_rects(stop_number=stop) or [])
        except Exception:
            self._pallets = []

        self._reload_locks()
        self._rebuild_geometry()
        self.update()

    # ── internal: stop selector ────────────────────────────────────────

    def _populate_stop_combo(self) -> None:
        prev = self.stop_combo.currentData()
        self.stop_combo.blockSignals(True)
        self.stop_combo.clear()
        result = getattr(self.controller, "get_last_result", lambda: None)()
        if not result or not getattr(result, "route_stops", None):
            self.stop_combo.addItem("Initial loadout", userData=1)
            self.stop_combo.blockSignals(False)
            return
        n = len(result.route_stops)
        for i, stop in enumerate(result.route_stops):
            if i == n - 1:
                continue
            label = f"Departure {i+1}: {stop.station_name}"
            self.stop_combo.addItem(label, userData=stop.stop_number)
        target = prev if prev is not None else 1
        for i in range(self.stop_combo.count()):
            if self.stop_combo.itemData(i) == target:
                self.stop_combo.setCurrentIndex(i)
                break
        self.stop_combo.blockSignals(False)

    def _on_stop_changed(self, _idx: int) -> None:
        stop = self.stop_combo.currentData()
        self._stop_number = stop
        try:
            self._pallets = list(self.controller.get_pallet_rects(stop_number=stop) or [])
        except Exception:
            self._pallets = []
        self._rebuild_geometry()
        self.update()

    def _on_clear_locks(self) -> None:
        fn = getattr(self.controller, "clear_pallet_locks", None)
        if fn is None:
            return
        try:
            fn()
        except Exception as e:                      # noqa: BLE001
            QMessageBox.warning(self, "Clear locks failed", str(e))
            return
        self._reload_locks()
        self.update()

    def _reload_locks(self) -> None:
        self._locks.clear()
        fn = getattr(self.controller, "list_pallet_locks", None)
        if fn is None:
            return
        try:
            rows = fn() or []
        except Exception:
            return
        for r in rows:
            try:
                cl = r["cargo_line_id"]
                idx = r["pallet_index"]
            except Exception:
                continue
            self._locks.add((cl, idx))

    # ── geometry: lay the ship out in world cube coordinates ───────────

    def _zone_world_offset(self, bay_label: str, zone_label: str
                           ) -> tuple[int, int]:
        """Return (world_x, world_y) offset of a zone's (0,0) cube.

        Bays are laid out left-to-right by stacking their X extents.
        Within a bay we use the zone's cube_offset_x / cube_offset_y as
        local offsets.
        """
        wx_base = 0
        for b in self._bays:
            if b.bay_label == bay_label:
                for z in b.zones:
                    if z.zone_label == zone_label:
                        return wx_base + z.cube_offset_x, z.cube_offset_y
            wx_base += b.width_cells + 2     # +2 cube gap between bays
        return 0, 0

    def _bay_world_extent(self) -> tuple[int, int]:
        """Total (max_x, max_y) world cube extent across all bays."""
        if not self._bays:
            return 0, 0
        mx = 0
        my = 0
        wx = 0
        for b in self._bays:
            mx = wx + b.width_cells
            my = max(my, b.length_cells)
            wx += b.width_cells + 2
        # subtract the trailing gap from the last bay's right edge
        mx = max(0, mx)
        return mx, my

    def _fit_view(self) -> None:
        """Pick cell_w/cell_h and origin so the whole ship + a 12-cube
        stack height fits inside the paint region."""
        avail_w = max(40, self.width() - 32)
        avail_h = max(40, self.height() - self._toolbar_h - 40 - 32)
        ex, ey = self._bay_world_extent()
        if ex == 0 or ey == 0:
            self._cell_w = DEFAULT_CELL_W
            self._cell_h = DEFAULT_CELL_H
            self._origin = QPointF(self.width() / 2,
                                   self._toolbar_h + 60)
            return

        max_stack = 12          # design budget for tallest stack
        # Iso footprint width = (ex + ey) * cell_w * ISO_X
        # Iso footprint height = (ex + ey) * cell_w * ISO_Y + max_stack * cell_h
        cw_by_w = avail_w / ((ex + ey) * ISO_X) if (ex + ey) else DEFAULT_CELL_W
        cw_by_h = avail_h / ((ex + ey) * ISO_Y + max_stack * 0.8) if (ex + ey) else DEFAULT_CELL_H
        cell_w = max(6, min(cw_by_w, cw_by_h, 28))
        self._cell_w = cell_w
        self._cell_h = cell_w * 0.8           # keep nice cube proportions

        # Origin: horizontally center the ship; vertically place its top
        # cell row below the hint label.
        footprint_w = (ex + ey) * cell_w * ISO_X
        ox = (self.width() - footprint_w) / 2 + ey * cell_w * ISO_X
        oy = self._toolbar_h + 60 + max_stack * self._cell_h
        self._origin = QPointF(ox, oy)

    def _rebuild_geometry(self) -> None:
        """Project every pallet to screen space and cache the polygons.

        Also caches a zone_label -> zone_meta dict (with world offsets)
        for use by drop reverse-projection and zone-ground rendering.
        """
        self._fit_view()
        # zone meta with world offsets
        self._zone_meta = {}
        for b in self._bays:
            for z in b.zones:
                wx, wy = self._zone_world_offset(b.bay_label, z.zone_label)
                self._zone_meta[z.zone_label] = {
                    "bay": b.bay_label,
                    "world_x": wx,
                    "world_y": wy,
                    "width": z.width_units,
                    "length": z.length_units,
                }

        self._shapes = []
        for r in self._pallets:
            zm = self._zone_meta.get(r.zone_label)
            if not zm:
                continue
            local_x = r.cell_x - self._zone_local_offset_x(r.zone_label, r.bay)
            local_y = r.cell_y - self._zone_local_offset_y(r.zone_label, r.bay)
            wx = zm["world_x"] + local_x
            wy = zm["world_y"] + local_y
            wz = r.cell_z
            top, left, right, sil = _cuboid_faces(
                wx, wy, wz, r.cell_w, r.cell_l, r.cell_h,
                self._origin.x(), self._origin.y(),
                self._cell_w, self._cell_h,
            )
            depth = (wx + r.cell_w - 1) + (wy + r.cell_l - 1) - wz * 0.001
            self._shapes.append(_PalletShape(
                rect=r, top=top, left=left, right=right,
                silhouette=sil, depth_key=depth,
            ))
        # back-to-front: smaller depth first
        self._shapes.sort(key=lambda s: s.depth_key)

    def _zone_local_offset_x(self, zone_label: str, bay_label: str) -> int:
        for b in self._bays:
            if b.bay_label != bay_label:
                continue
            for z in b.zones:
                if z.zone_label == zone_label:
                    return z.cube_offset_x
        return 0

    def _zone_local_offset_y(self, zone_label: str, bay_label: str) -> int:
        for b in self._bays:
            if b.bay_label != bay_label:
                continue
            for z in b.zones:
                if z.zone_label == zone_label:
                    return z.cube_offset_y
        return 0

    # ── hit testing ────────────────────────────────────────────────────

    def _hit_test(self, pos: QPoint) -> int | None:
        """Return the index of the front-most shape under *pos*, or None."""
        ptf = QPointF(pos)
        # Iterate in REVERSE draw order so foreground wins.
        for i in range(len(self._shapes) - 1, -1, -1):
            if self._shapes[i].silhouette.containsPoint(
                ptf, Qt.FillRule.OddEvenFill,
            ):
                return i
        return None

    def _drop_target(self, pos: QPoint) -> tuple[str, int, int, int] | None:
        """Reverse-project *pos* to a (zone, x, y, z) cube. The Z is the
        lowest open level for the chosen (x, y) inside that zone, given
        the zone's current contents.

        Returns None when the cursor doesn't land on any zone footprint.
        """
        wx_f, wy_f = iso_unproject_ground(
            pos.x(), pos.y(),
            self._origin.x(), self._origin.y(),
            self._cell_w, self._cell_h,
        )
        # snap
        wx = int(math.floor(wx_f))
        wy = int(math.floor(wy_f))

        # Find which zone (if any) contains this world cell.
        for zone_label, zm in self._zone_meta.items():
            zx, zy = zm["world_x"], zm["world_y"]
            zw, zl = zm["width"], zm["length"]
            if zx <= wx < zx + zw and zy <= wy < zy + zl:
                local_x = wx - zx
                local_y = wy - zy
                # Z = highest top among existing pallets at this footprint
                z = self._lowest_open_z(zone_label, local_x, local_y)
                return zone_label, local_x, local_y, z
        return None

    def _lowest_open_z(self, zone_label: str, local_x: int, local_y: int) -> int:
        """Return the lowest open Z in (zone_label, local_x, local_y).

        Cheap occupancy scan over the cached pallet list — we don't go
        through the physical_packer because that requires a full grid
        rebuild and we only need a column query.
        """
        top = 0
        # Find the zone's bay-local origin so we can match pallet cell_x.
        zm = self._zone_meta.get(zone_label)
        if not zm:
            return 0
        zone_local_x_off = self._zone_local_offset_x(zone_label, zm["bay"])
        zone_local_y_off = self._zone_local_offset_y(zone_label, zm["bay"])
        target_cx = zone_local_x_off + local_x
        target_cy = zone_local_y_off + local_y
        for p in self._pallets:
            if p.zone_label != zone_label:
                continue
            if self._drag_shape and p is self._drag_shape.rect:
                continue
            if (p.cell_x <= target_cx < p.cell_x + p.cell_w
                    and p.cell_y <= target_cy < p.cell_y + p.cell_l):
                top = max(top, p.cell_z + p.cell_h)
        return top

    # ── events ────────────────────────────────────────────────────────

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        self._rebuild_geometry()
        super().resizeEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        pos = event.position().toPoint()
        if self._drag_shape is not None:
            self._drag_cursor = pos
            self._drag_target = self._drop_target(pos)
            self.update()
            return
        idx = self._hit_test(pos)
        if idx != self._hover_index:
            self._hover_index = idx
            self.update()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            return
        pos = event.position().toPoint()
        idx = self._hit_test(pos)
        if idx is None:
            return
        self._drag_shape = self._shapes[idx]
        self._drag_cursor = pos
        self._drag_target = self._drop_target(pos)
        self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if self._drag_shape is None:
            return
        shape = self._drag_shape
        target = self._drag_target
        self._drag_shape = None
        self._drag_target = None
        self._drag_cursor = None
        if target is None:
            self.update()
            return
        zone, x, y, z = target
        rect = shape.rect
        cl_id = getattr(rect, "cargo_line_id", None)
        p_idx = getattr(rect, "pallet_index", 0)
        if cl_id is None:
            self.update()
            return
        fn = getattr(self.controller, "lock_pallet", None)
        if fn is None:
            QMessageBox.information(
                self, "Lock unavailable",
                "Pallet-lock support hasn't been wired into the "
                "controller yet — drop ignored.",
            )
            self.update()
            return
        try:
            fn(cl_id, p_idx, zone, x, y, z)
        except ToolError as e:
            QMessageBox.warning(self, "Invalid placement", str(e))
            self.update()
            return
        except Exception as e:                      # noqa: BLE001
            QMessageBox.warning(self, "Lock failed", str(e))
            self.update()
            return
        self.pallet_locked.emit(cl_id, p_idx, zone, x, y, z)
        # Refresh so the new lock + repositioned pallet show up.
        self.refresh()

    # ── painting ───────────────────────────────────────────────────────

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        self._draw_zone_floors(p)
        self._draw_pallets(p)
        if self._drag_shape is not None:
            self._draw_drag_ghost(p)
        p.end()

    def _draw_zone_floors(self, p: QPainter) -> None:
        """Paint a faint parallelogram for each zone's ground footprint."""
        for zone_label, zm in self._zone_meta.items():
            zx, zy = zm["world_x"], zm["world_y"]
            zw, zl = zm["width"], zm["length"]
            corners = [
                iso_project(zx, zy, 0, self._origin.x(), self._origin.y(),
                            self._cell_w, self._cell_h),
                iso_project(zx + zw, zy, 0, self._origin.x(), self._origin.y(),
                            self._cell_w, self._cell_h),
                iso_project(zx + zw, zy + zl, 0, self._origin.x(), self._origin.y(),
                            self._cell_w, self._cell_h),
                iso_project(zx, zy + zl, 0, self._origin.x(), self._origin.y(),
                            self._cell_w, self._cell_h),
            ]
            poly = QPolygonF([QPointF(x, y) for x, y in corners])
            p.setBrush(QBrush(QColor(20, 32, 40)))
            p.setPen(QPen(QColor(38, 182, 212), 1.2))
            p.drawPolygon(poly)

            # zone label at the back-most corner
            label_x, label_y = corners[0]
            p.setPen(QPen(QColor("#5be4ff")))
            font = QFont("Segoe UI", 9)
            font.setBold(True)
            p.setFont(font)
            p.drawText(QRectF(label_x - 30, label_y - 18, 80, 14),
                       Qt.AlignmentFlag.AlignCenter, zone_label)

            # Drop-target hover highlight
            if (self._drag_target is not None
                    and self._drag_target[0] == zone_label):
                p.setBrush(QBrush(QColor(123, 191, 63, 60)))
                p.setPen(QPen(QColor("#7bbf3f"), 2))
                p.drawPolygon(poly)

    def _draw_pallets(self, p: QPainter) -> None:
        font = QFont("Segoe UI", 8)
        font.setBold(True)
        for i, shape in enumerate(self._shapes):
            if self._drag_shape is shape:
                # Draw a faint outline at the original location only.
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setPen(QPen(QColor(255, 255, 255, 70), 1, Qt.PenStyle.DashLine))
                p.drawPolygon(shape.silhouette)
                continue

            base = QColor(shape.rect.color)
            top_c = _shade(base, 130)               # brightest
            left_c = _shade(base, 85)
            right_c = _shade(base, 70)              # darkest
            border = base.darker(180)
            border_pen = QPen(border, 1)

            p.setPen(border_pen)
            p.setBrush(QBrush(right_c))
            p.drawPolygon(shape.right)
            p.setBrush(QBrush(left_c))
            p.drawPolygon(shape.left)
            p.setBrush(QBrush(top_c))
            p.drawPolygon(shape.top)

            # Top-face label: SCU and pallet_index
            p.setFont(font)
            text_color = (QColor("#ffffff") if top_c.lightness() < 150
                          else QColor("#142028"))
            p.setPen(QPen(text_color))
            cx = sum(pt.x() for pt in shape.top) / 4
            cy = sum(pt.y() for pt in shape.top) / 4
            p_idx = getattr(shape.rect, "pallet_index", 0)
            p.drawText(QRectF(cx - 24, cy - 8, 48, 16),
                       Qt.AlignmentFlag.AlignCenter,
                       f"{shape.rect.label}#{p_idx}")

            # Lock indicator
            cl_id = getattr(shape.rect, "cargo_line_id", None)
            if cl_id is not None and (cl_id, p_idx) in self._locks:
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setPen(QPen(QColor("#ffd24a"), 2))
                p.drawPolygon(shape.silhouette)

            # Hover ring
            if self._hover_index == i and self._drag_shape is None:
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setPen(QPen(QColor("#ff8a3c"), 2))
                p.drawPolygon(shape.silhouette)

    def _draw_drag_ghost(self, p: QPainter) -> None:
        """Outline of the dragged pallet snapped to the candidate cell."""
        shape = self._drag_shape
        if shape is None:
            return
        if self._drag_target is None:
            # No valid drop — show outline at the cursor for feedback.
            if self._drag_cursor is None:
                return
            p.setBrush(QBrush(QColor(255, 60, 60, 80)))
            p.setPen(QPen(QColor("#ff4040"), 1, Qt.PenStyle.DashLine))
            r = QRectF(self._drag_cursor.x() - 20, self._drag_cursor.y() - 14,
                       40, 28)
            p.drawRect(r)
            return
        zone, lx, ly, lz = self._drag_target
        zm = self._zone_meta[zone]
        wx = zm["world_x"] + lx
        wy = zm["world_y"] + ly
        r = shape.rect
        top, left, right, sil = _cuboid_faces(
            wx, wy, lz, r.cell_w, r.cell_l, r.cell_h,
            self._origin.x(), self._origin.y(),
            self._cell_w, self._cell_h,
        )
        base = QColor(r.color)
        base.setAlpha(120)
        p.setBrush(QBrush(base))
        p.setPen(QPen(QColor("#ffffff"), 1.5, Qt.PenStyle.DashLine))
        p.drawPolygon(sil)


# ── dialog wrapper ───────────────────────────────────────────────────────

def open_iso_view(controller, parent=None):
    """Open the IsoBayCanvas in a resizable modeless dialog."""
    from PySide6.QtWidgets import QDialog, QVBoxLayout as _V
    dlg = QDialog(parent)
    dlg.setWindowTitle("Cargo Bay — 3D View")
    dlg.resize(900, 700)
    lay = _V(dlg)
    lay.setContentsMargins(0, 0, 0, 0)
    canvas = IsoBayCanvas(controller, parent=dlg)
    lay.addWidget(canvas)
    canvas.refresh()
    dlg.show()
    return dlg
