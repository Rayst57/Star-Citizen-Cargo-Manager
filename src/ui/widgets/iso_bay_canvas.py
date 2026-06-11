"""
IsoBayCanvas — orbitable 3D-axonometric view of the cargo bay with
individual-pallet drag-and-drop locking and per-pallet rotation.

The widget now uses a fully orthographic projection parameterised by
camera ``yaw`` (rotation around world-Z) and ``pitch`` (tilt around the
camera-X axis). The default state (yaw=0, pitch=~30°) reproduces the old
fixed axonometric framing; the user can right-mouse-drag to orbit, scroll
to zoom, and press **R** while dragging a pallet to flip its footprint
(swap width and length).

The free-standing ``iso_project`` / ``iso_unproject_ground`` helpers at
module scope are preserved for callers — and for the math round-trip
tests — but the widget itself routes through ``project()`` /
``unproject_ground()`` so changes to the camera state are honored
everywhere.

Drag-drop semantics:
    - left-mouse-down on a pallet "picks it up" and stops drawing it
      solidly.
    - while dragging, a ghost outline follows the cursor projected onto
      the ground plane (z=0) of the hovered zone, snapped to the
      nearest cube cell.
    - pressing R during the drag flips the held pallet's orientation —
      the ghost outline redraws with swapped w↔l. The orientation is
      pre-filled from any existing lock when the pallet is picked up.
    - on release, we call ``controller.lock_pallet(...)`` with the
      current orientation. Any ToolError raised by the controller is
      shown as a warning and the drop is aborted (no UI state change).

This file deliberately does NOT touch the controller's schema or
planner integration. It only consumes the existing
``get_bay_layout()`` / ``get_pallet_rects()`` API plus four new
methods on the controller contract:

    lock_pallet(cargo_line_id, pallet_index, zone, x, y, z, orientation=0) -> None
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
    QBrush, QColor, QFont, QKeyEvent, QMouseEvent, QPainter, QPaintEvent,
    QPen, QPolygonF, QResizeEvent, QWheelEvent,
)
from PySide6.QtWidgets import (
    QComboBox, QDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton,
    QSizePolicy, QVBoxLayout, QWidget,
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

# Camera defaults — yaw=0 / pitch=30° matches the historical iso framing
# closely enough that legacy callers see the same visual result.
DEFAULT_YAW = 0.0
DEFAULT_PITCH = math.radians(30)
MIN_PITCH = 0.10
MAX_PITCH = 1.40
MIN_CELL_W = 8
MAX_CELL_W = 80


def iso_project(
    world_x: float, world_y: float, world_z: float,
    origin_x: float, origin_y: float,
    cell_w: float = DEFAULT_CELL_W, cell_h: float = DEFAULT_CELL_H,
) -> tuple[float, float]:
    """World cube coordinates -> screen pixel coordinates (legacy fixed
    axonometric framing).

    Preserved for the module-level math round-trip tests and for any
    out-of-widget callers; the widget itself uses ``project()`` which
    honors the camera state.
    """
    sx = origin_x + (world_x - world_y) * cell_w * ISO_X
    sy = origin_y + (world_x + world_y) * cell_w * ISO_Y - world_z * cell_h
    return sx, sy


def iso_unproject_ground(
    screen_x: float, screen_y: float,
    origin_x: float, origin_y: float,
    cell_w: float = DEFAULT_CELL_W, cell_h: float = DEFAULT_CELL_H,
) -> tuple[float, float]:
    """Inverse of ``iso_project`` for ground-plane (z=0) points."""
    dx = screen_x - origin_x
    dy = screen_y - origin_y
    u = dx / (cell_w * ISO_X)
    v = dy / (cell_w * ISO_Y)
    world_x = (u + v) / 2.0
    world_y = (v - u) / 2.0
    return world_x, world_y


# ── pallet shape cache ──────────────────────────────────────────────────

@dataclass
class _PalletShape:
    """Cached projected geometry for one pallet (a cuboid).

    Stores the three visible-face polygons (top, side_a, side_b) and
    the silhouette used for hit-testing. ``depth_key`` is the camera-
    relative depth used by the painter's-algorithm sort; smaller values
    are FARTHER from the camera, drawn first.
    """
    rect: object
    top: QPolygonF
    side_a: QPolygonF
    side_b: QPolygonF
    # ``side_a_normal`` / ``side_b_normal`` are the world-space normals
    # of the two visible side faces (each in the set { (1,0,0), (-1,0,0),
    # (0,1,0), (0,-1,0) }). Used by the shader to decide which is the
    # "more perpendicular" face (gets ``darker(118)``) vs the more
    # glancing one (``darker(143)``).
    side_a_normal: tuple[float, float, float]
    side_b_normal: tuple[float, float, float]
    silhouette: QPolygonF
    depth_key: float


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
        # We need keyboard focus during a drag for the R rotation key.
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        # state
        self._bays: list = []
        self._pallets: list = []
        self._shapes: list[_PalletShape] = []
        self._zone_meta: dict[str, dict] = {}
        self._cell_w = DEFAULT_CELL_W
        self._cell_h = DEFAULT_CELL_H
        self._origin = QPointF(0, 0)
        self._stop_number: int | None = None

        # camera state
        self._yaw: float = DEFAULT_YAW
        self._pitch: float = DEFAULT_PITCH

        # interaction state
        self._hover_index: int | None = None
        self._drag_shape: _PalletShape | None = None
        self._drag_target: tuple[str, int, int, int] | None = None
        self._drag_cursor: QPoint | None = None
        # 0 = natural footprint, 1 = swapped w/l (the R key toggle).
        self._drag_orientation: int = 0
        # cached (w, l) of the dragged pallet AT ORIENTATION 0 so R
        # always toggles back and forth between the same two states.
        self._drag_base_wl: tuple[int, int] = (1, 1)

        # right-mouse orbit state
        self._orbit_last: QPoint | None = None

        # locked pallets (cargo_line_id, pallet_index) -> orientation
        self._locks: dict[tuple[int, int], int] = {}

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

        self.reset_view_btn = QPushButton("Reset view")
        self.reset_view_btn.clicked.connect(self._on_reset_view)
        toolbar.addWidget(self.reset_view_btn)

        self.clear_btn = QPushButton("Clear all locks")
        self.clear_btn.clicked.connect(self._on_clear_locks)
        toolbar.addWidget(self.clear_btn)

        self._toolbar_h = 36
        toolbar_w = QWidget()
        toolbar_w.setFixedHeight(self._toolbar_h)
        toolbar_w.setLayout(toolbar)
        root.addWidget(toolbar_w, 0)

        # hint
        self.hint_label = QLabel(
            "Drag a pallet to lock it to a new cell. Right-mouse-drag "
            "to orbit, wheel to zoom. Locked pallets keep a thin gold "
            "outline; the planner will pack other cargo around them."
        )
        self.hint_label.setStyleSheet("color: #5be4ff;")
        self.hint_label.setWordWrap(True)
        root.addWidget(self.hint_label, 0)

        root.addStretch(1)

        # Bottom status hint — empty by default, shows "R to rotate"
        # while a pallet is being dragged.
        self.drag_hint_label = QLabel("")
        self.drag_hint_label.setStyleSheet(
            "color: #ffd24a; font-weight: bold;"
        )
        root.addWidget(self.drag_hint_label, 0)

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

    # ── projection: camera-aware project / unproject ────────────────────

    def project(self, wx: float, wy: float, wz: float) -> QPointF:
        """World cube coords -> screen point under the current camera.

        Rotates the world by ``self._yaw`` around Z, then tilts by
        ``self._pitch`` around the camera-X axis, then drops onto the
        screen plane (orthographic).
        """
        cy = math.cos(self._yaw)
        sy = math.sin(self._yaw)
        cp = math.cos(self._pitch)
        sp = math.sin(self._pitch)
        rx = wx * cy - wy * sy
        ry = wx * sy + wy * cy
        sx = self._origin.x() + rx * self._cell_w
        # World Z points UP, which is -screen_y. Pitch rotates the
        # ground (Y axis) into the view plane.
        sy_out = self._origin.y() + (ry * cp - wz * sp) * self._cell_w
        return QPointF(sx, sy_out)

    def unproject_ground(self, sx: float, sy: float) -> tuple[float, float]:
        """Inverse of ``project`` on the ground plane (wz=0).

        Solves the linear system for (wx, wy) such that
        ``project(wx, wy, 0) == (sx, sy)``.
        """
        cy = math.cos(self._yaw)
        sy_t = math.sin(self._yaw)
        cp = math.cos(self._pitch)
        # Guard against a degenerate top-down pitch (cp → 0).
        cp = cp if abs(cp) > 1e-6 else 1e-6
        dx = (sx - self._origin.x()) / self._cell_w
        dy = (sy - self._origin.y()) / (self._cell_w * cp)
        # Inverse of the yaw rotation: [[cy, -sy], [sy, cy]] applied to
        # (dx, dy). Because the forward rotation maps (wx, wy) -> (rx, ry),
        # the inverse is rx*cy + ry*sy = wx, -rx*sy + ry*cy = wy.
        wx = dx * cy + dy * sy_t
        wy = -dx * sy_t + dy * cy
        return wx, wy

    def _depth_for(
        self, wx: float, wy: float, wz: float,
    ) -> float:
        """View-space depth used for painter's algorithm.

        Larger = closer to the camera; smaller = farther. The yaw'd
        Y-component is rotated into the view plane by pitch, with the
        world-Z component contributing via ``sin(pitch)``.
        """
        cy = math.cos(self._yaw)
        sy = math.sin(self._yaw)
        cp = math.cos(self._pitch)
        sp = math.sin(self._pitch)
        # Nearness = position · toward-camera. The toward-camera axis
        # in the yaw-rotated frame is (0, sp, cp): a displacement along
        # it leaves the screen position unchanged (dy*cp == dz*sp), and
        # its +Z component is positive because the camera looks down.
        # So nearness = ry*sp + wz*cp — bigger ry (lower on screen) and
        # bigger wz (higher stack) are both nearer. Shapes are sorted
        # ASCENDING on this key so far pallets draw first and near
        # pallets paint over them (painter's algorithm).
        ry = wx * sy + wy * cy
        return ry * sp + wz * cp

    # ── camera control ──────────────────────────────────────────────────

    def _on_reset_view(self) -> None:
        self._yaw = DEFAULT_YAW
        self._pitch = DEFAULT_PITCH
        self._cell_w = DEFAULT_CELL_W
        self._cell_h = DEFAULT_CELL_H
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
            try:
                orientation = r["orientation"]
            except (IndexError, KeyError):
                orientation = 0
            self._locks[(cl, idx)] = int(orientation or 0)

    # ── geometry: lay the ship out in world cube coordinates ───────────

    def _zone_world_offset(self, bay_label: str, zone_label: str
                           ) -> tuple[int, int]:
        wx_base = 0
        for b in self._bays:
            if b.bay_label == bay_label:
                for z in b.zones:
                    if z.zone_label == zone_label:
                        return wx_base + z.cube_offset_x, z.cube_offset_y
            wx_base += b.width_cells + 2
        return 0, 0

    def _bay_world_extent(self) -> tuple[int, int]:
        if not self._bays:
            return 0, 0
        mx = 0
        my = 0
        wx = 0
        for b in self._bays:
            mx = wx + b.width_cells
            my = max(my, b.length_cells)
            wx += b.width_cells + 2
        mx = max(0, mx)
        return mx, my

    def _fit_view(self) -> None:
        """Pick origin so the ship sits comfortably in the paint region.

        Cell sizes are driven by the user's zoom — this only chooses the
        origin (which we want to keep stable when the user orbits).
        """
        ex, ey = self._bay_world_extent()
        if ex == 0 or ey == 0:
            self._origin = QPointF(self.width() / 2,
                                   self._toolbar_h + 60 + self.height() / 3)
            return

        # Keep the origin centered horizontally with some vertical room
        # for high stacks above the ground plane.
        ox = self.width() / 2
        oy = self._toolbar_h + 60 + max(80, self.height() / 3)
        self._origin = QPointF(ox, oy)

    def _initial_fit_zoom(self) -> None:
        """Set cell_w on first layout / reset so the ship fits the view.

        Only run when no user zoom has been applied yet (we detect this
        by comparing cell_w against its default — the moment the user
        scrolls the wheel they own the zoom).
        """
        ex, ey = self._bay_world_extent()
        if ex == 0 or ey == 0:
            return
        avail_w = max(40, self.width() - 32)
        # Width of the projected ship at yaw=0 is roughly
        # (ex*cos + ey*sin) which collapses to ex at yaw=0.
        cy = abs(math.cos(self._yaw))
        sy = abs(math.sin(self._yaw))
        proj_w = max(1.0, ex * cy + ey * sy)
        cw_by_w = avail_w / (proj_w + 4)
        cell_w = max(MIN_CELL_W, min(int(cw_by_w), 28))
        self._cell_w = cell_w
        self._cell_h = cell_w

    def _rebuild_geometry(self) -> None:
        """Project every pallet to screen space and cache the polygons."""
        if not getattr(self, "_user_zoomed", False):
            self._initial_fit_zoom()
        self._fit_view()
        self._zone_meta = {}
        for b in self._bays:
            for z in b.zones:
                wx, wy = self._zone_world_offset(b.bay_label, z.zone_label)
                self._zone_meta[z.zone_label] = {
                    "bay": b.bay_label,
                    "bay_label": b.bay_label,
                    "world_x": wx,
                    "world_y": wy,
                    "width": z.width_units,
                    "length": z.length_units,
                    "height": getattr(z, "height_units", 4),
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
            shape = self._build_cuboid_shape(
                wx, wy, wz, r.cell_w, r.cell_l, r.cell_h, r,
            )
            self._shapes.append(shape)
        # back-to-front: smaller depth_key first
        self._shapes.sort(key=lambda s: s.depth_key)

    def _build_cuboid_shape(
        self, wx: int, wy: int, wz: int,
        w: int, l: int, h: int, rect_obj,
    ) -> _PalletShape:
        """Build a _PalletShape for the cuboid (wx..wx+w, wy..wy+l, wz..wz+h).

        Picks the 3 faces facing the camera by checking each face
        normal against the view direction (computed from yaw/pitch).
        """
        x0, x1 = wx, wx + w
        y0, y1 = wy, wy + l
        z0, z1 = wz, wz + h

        # Vertices, named by their (x, y, z) octant within the cuboid.
        def P(x, y, z):
            return self.project(x, y, z)

        v = {
            (0, 0, 0): P(x0, y0, z0),
            (1, 0, 0): P(x1, y0, z0),
            (0, 1, 0): P(x0, y1, z0),
            (1, 1, 0): P(x1, y1, z0),
            (0, 0, 1): P(x0, y0, z1),
            (1, 0, 1): P(x1, y0, z1),
            (0, 1, 1): P(x0, y1, z1),
            (1, 1, 1): P(x1, y1, z1),
        }

        # Top face is always (0,0,1)-(1,0,1)-(1,1,1)-(0,1,1) — pitch is
        # clamped above 0 so the camera always looks down at the top.
        top_poly = QPolygonF([
            v[(0, 0, 1)], v[(1, 0, 1)], v[(1, 1, 1)], v[(0, 1, 1)],
        ])

        # Toward-camera vector in world space. In the yaw-rotated frame
        # the view axis is (0, sp, cp): displacement along it leaves the
        # screen position unchanged (dy*cp == dz*sp from the projection
        # formula screen_y = ry*cp - wz*sp), and its Z component is
        # positive because the camera looks down (top faces always
        # visible). Un-rotating by yaw gives the world-space vector.
        # A face is visible iff its outward normal has a POSITIVE dot
        # with this vector.
        cy = math.cos(self._yaw)
        sy = math.sin(self._yaw)
        cp = math.cos(self._pitch)
        sp = math.sin(self._pitch)
        view = (sy * sp, cy * sp, cp)

        # Each side face has a known normal. Pick the two with the
        # largest positive view dot — those are the two visible sides.
        side_candidates = [
            # (normal, vertex_indices_of_face)
            ((1, 0, 0), [(1, 0, 0), (1, 1, 0), (1, 1, 1), (1, 0, 1)]),
            ((-1, 0, 0), [(0, 0, 0), (0, 1, 0), (0, 1, 1), (0, 0, 1)]),
            ((0, 1, 0), [(0, 1, 0), (1, 1, 0), (1, 1, 1), (0, 1, 1)]),
            ((0, -1, 0), [(0, 0, 0), (1, 0, 0), (1, 0, 1), (0, 0, 1)]),
        ]
        scored = []
        for normal, verts in side_candidates:
            dot = normal[0] * view[0] + normal[1] * view[1] + normal[2] * view[2]
            if dot > 1e-9:
                scored.append((dot, normal, verts))
        # Sort by visibility strength descending; take top 2.
        scored.sort(key=lambda x: -x[0])
        scored = scored[:2]
        while len(scored) < 2:
            # Degenerate fallback — duplicate the first to keep shape
            # invariants; this only happens at extreme camera angles.
            scored.append(scored[0] if scored else (
                0.0, (0, 1, 0),
                [(0, 1, 0), (1, 1, 0), (1, 1, 1), (0, 1, 1)],
            ))

        # The more-perpendicular face has the larger dot.
        (dot_a, normal_a, verts_a), (dot_b, normal_b, verts_b) = scored

        def poly(verts):
            return QPolygonF([v[k] for k in verts])

        side_a = poly(verts_a)
        side_b = poly(verts_b)

        # Silhouette: outer convex hull of all 8 projected vertices.
        # In orthographic projection this is the cuboid outline.
        sil = self._compute_silhouette(v.values())

        # Depth key for painter's algorithm — use the centroid.
        cx = wx + w / 2
        cy_ = wy + l / 2
        cz_ = wz + h / 2
        depth_key = self._depth_for(cx, cy_, cz_)

        return _PalletShape(
            rect=rect_obj,
            top=top_poly,
            side_a=side_a,
            side_b=side_b,
            side_a_normal=normal_a,
            side_b_normal=normal_b,
            silhouette=sil,
            depth_key=depth_key,
        )

    @staticmethod
    def _compute_silhouette(points: Iterable[QPointF]) -> QPolygonF:
        """Convex hull of *points* as a QPolygonF, using Andrew's
        monotone chain. Used for hit-testing the cuboid silhouette."""
        pts = sorted(((p.x(), p.y()) for p in points))
        if len(pts) < 2:
            return QPolygonF([QPointF(x, y) for x, y in pts])

        def cross(o, a, b):
            return ((a[0] - o[0]) * (b[1] - o[1])
                    - (a[1] - o[1]) * (b[0] - o[0]))

        lower: list[tuple[float, float]] = []
        for p in pts:
            while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
                lower.pop()
            lower.append(p)
        upper: list[tuple[float, float]] = []
        for p in reversed(pts):
            while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
                upper.pop()
            upper.append(p)
        hull = lower[:-1] + upper[:-1]
        return QPolygonF([QPointF(x, y) for x, y in hull])

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
        for i in range(len(self._shapes) - 1, -1, -1):
            if self._shapes[i].silhouette.containsPoint(
                ptf, Qt.FillRule.OddEvenFill,
            ):
                return i
        return None

    def _drop_target(self, pos: QPoint) -> tuple[str, int, int, int] | None:
        """Reverse-project *pos* to a (zone, x, y, z) cube.

        The returned origin is CLAMPED so the dragged pallet's full
        footprint stays inside the zone: dropping a 2-wide pallet on a
        zone's last column snaps it flush against the wall instead of
        failing validation. The cursor only needs to land anywhere on
        the zone."""
        wx_f, wy_f = self.unproject_ground(pos.x(), pos.y())
        wx = int(math.floor(wx_f))
        wy = int(math.floor(wy_f))

        # Footprint of the dragged pallet (already reflects the active
        # R-key orientation since the rect's cell_w/cell_l are swapped
        # on rotate).
        fw = fl = 1
        if self._drag_shape is not None:
            fw = max(1, getattr(self._drag_shape.rect, "cell_w", 1))
            fl = max(1, getattr(self._drag_shape.rect, "cell_l", 1))

        # Find which zone (if any) contains this world cell.
        validate_fn = getattr(self.controller, "validate_span", None)
        for zone_label, zm in self._zone_meta.items():
            zx, zy = zm["world_x"], zm["world_y"]
            zw, zl = zm["width"], zm["length"]
            if zx <= wx < zx + zw and zy <= wy < zy + zl:
                local_x = wx - zx
                local_y = wy - zy
                # If the unclamped footprint legally spans into adjacent
                # walled-connected zones, DON'T clamp — let the pallet
                # extend across the wall. Otherwise (truly off-ship or
                # crossing a bulkhead) fall back to the clamp-inside-zone
                # behaviour so the user still gets a snap target.
                spans_ok = False
                if validate_fn is not None:
                    try:
                        spans_ok = bool(
                            validate_fn(zone_label, local_x, local_y, fw, fl)
                        )
                    except Exception:
                        spans_ok = False
                if not spans_ok:
                    # Snap the origin back from the far edges so the whole
                    # footprint fits.
                    local_x = min(local_x, max(0, zw - fw))
                    local_y = min(local_y, max(0, zl - fl))
                z = self._lowest_open_z(zone_label, local_x, local_y, fw, fl)
                # When the stack is already at the zone's height limit,
                # stacking isn't possible — target ground level instead
                # so the drop runs the force-push displacement flow
                # (push the blockers to holding) rather than failing
                # the height validation outright.
                fh = 1
                if self._drag_shape is not None:
                    fh = max(1, getattr(self._drag_shape.rect, "cell_h", 1))
                if z + fh > zm.get("height", 4):
                    z = 0
                return zone_label, local_x, local_y, z
        return None

    def _lowest_open_z(
        self, zone_label: str, local_x: int, local_y: int,
        fw: int = 1, fl: int = 1,
    ) -> int:
        """Stack height at the (local_x, local_y) origin for a pallet
        of footprint fw x fl — the max top across EVERY column the
        footprint covers, so a wide pallet dropped half-on a stack
        rests on the stack instead of interpenetrating it."""
        top = 0
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
            # Rect-overlap between the dragged footprint and pallet p.
            if (p.cell_x < target_cx + fw and target_cx < p.cell_x + p.cell_w
                    and p.cell_y < target_cy + fl
                    and target_cy < p.cell_y + p.cell_l):
                top = max(top, p.cell_z + p.cell_h)
        return top

    # ── events ────────────────────────────────────────────────────────

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        self._rebuild_geometry()
        super().resizeEvent(event)

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        # Mark that the user has taken over zoom; the auto-fit on
        # rebuild_geometry no longer interferes.
        self._user_zoomed = True
        delta = event.angleDelta().y()
        if delta == 0:
            return
        step = 2 if abs(delta) >= 120 else 1
        if delta > 0:
            new_w = min(MAX_CELL_W, self._cell_w + step)
        else:
            new_w = max(MIN_CELL_W, self._cell_w - step)
        if new_w != self._cell_w:
            self._cell_w = new_w
            self._cell_h = new_w
            self._rebuild_geometry()
            self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        pos = event.position().toPoint()

        # Right-mouse-drag orbit
        if self._orbit_last is not None:
            dx = pos.x() - self._orbit_last.x()
            dy = pos.y() - self._orbit_last.y()
            self._orbit_last = pos
            self._yaw = (self._yaw + dx / 100.0) % (2 * math.pi)
            self._pitch = max(MIN_PITCH,
                              min(MAX_PITCH, self._pitch + dy / 100.0))
            self._rebuild_geometry()
            self.update()
            return

        if self._drag_shape is not None:
            self._drag_cursor = pos
            self._drag_target = self._drop_target(pos)
            self.update()
            return
        idx = self._hit_test(pos)
        if idx != self._hover_index:
            self._hover_index = idx
            self._update_hover_tooltip(pos, idx)
            self.update()
        elif idx is not None:
            # Same pallet, but the cursor moved — keep the tooltip
            # anchored to the cursor so it doesn't lag behind.
            self._update_hover_tooltip(pos, idx)

    def _update_hover_tooltip(self, pos: QPoint, idx: int | None) -> None:
        """Show a QToolTip with commodity + Pickup → Destination for
        the pallet currently under the cursor, or hide it when the
        cursor leaves a pallet."""
        from PySide6.QtWidgets import QToolTip
        if idx is None or idx >= len(self._shapes):
            QToolTip.hideText()
            return
        r = self._shapes[idx].rect
        cl = getattr(r, "cargo_line_id", "?")
        pi = getattr(r, "pallet_index", 0)
        pickup = getattr(r, "pickup_station_name", "") or "—"
        dest = getattr(r, "delivery_station_name", "") or "—"
        commodity = getattr(r, "commodity_name", "") or "—"
        size = getattr(r, "pallet_size", "—")
        contract = getattr(r, "contract_number", "—")
        # Rich-text tooltip so we can emphasise the route line and
        # the commodity, with smaller meta text below.
        html = (
            f"<div style='font-size: 11pt;'>"
            f"<b>{commodity}</b><br>"
            f"<span style='color:#5be4ff;'>{pickup}</span>"
            f" &nbsp;→&nbsp; "
            f"<span style='color:#ffd24a;'>{dest}</span><br>"
            f"<span style='color:#8aa;font-size:9pt;'>"
            f"{size} SCU &nbsp;|&nbsp; Contract #{contract} &nbsp;|&nbsp; "
            f"cl#{cl}:p{pi}</span>"
            f"</div>"
        )
        QToolTip.showText(self.mapToGlobal(pos), html, self)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.RightButton:
            self._orbit_last = event.position().toPoint()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        pos = event.position().toPoint()
        idx = self._hit_test(pos)
        if idx is None:
            return
        self._drag_shape = self._shapes[idx]
        self._drag_cursor = pos
        self._drag_target = self._drop_target(pos)
        # Initialise rotation state from the existing lock (if any) so
        # the user can leave the orientation as-is on re-drop.
        rect = self._drag_shape.rect
        cl_id = getattr(rect, "cargo_line_id", None)
        p_idx = getattr(rect, "pallet_index", 0)
        existing = self._locks.get((cl_id, p_idx), 0) if cl_id is not None else 0
        self._drag_orientation = int(existing)
        # Resolve the natural (orientation=0) footprint so toggling is
        # stable. The pallet's currently rendered (cell_w, cell_l) is
        # the *current* orientation; flip when existing==1.
        cw = getattr(rect, "cell_w", 1)
        cl = getattr(rect, "cell_l", 1)
        if existing == 1:
            self._drag_base_wl = (cl, cw)
        else:
            self._drag_base_wl = (cw, cl)
        self.drag_hint_label.setText("R to rotate")
        self.setFocus()
        self.update()

    def _find_displaced_pallets(
        self, zone: str, x: int, y: int, z: int,
        shape, orientation: int, dragged_cl: int, dragged_idx: int,
    ) -> list:
        """Return the PalletRects whose bay-space cubes intersect the
        footprint of the dragged pallet at (x, y, z). Excludes the
        pallet being dragged. The footprint reflects the current
        orientation (swapped w/l when orientation == 1).

        Generalised to walk every PalletRect on board — a spanning
        pallet might displace pallets in any zone its footprint touches,
        and a dragged spanning pallet might collide with pallets in
        adjacent zones. We compare in BAY-SPACE coordinates because the
        PalletRects already carry their cell_x/cell_y in bay space.
        """
        rect = shape.rect
        w = getattr(rect, "cell_w", 1)
        l = getattr(rect, "cell_l", 1)
        h = getattr(rect, "cell_h", 1)
        # Translate the dragged origin (zone-local) to bay-space so the
        # cube comparisons match the PalletRect cell_x/cell_y namespace.
        zm = self._zone_meta.get(zone)
        if zm is None:
            return []
        # The widget's _zone_meta tracks world_x/world_y which are
        # bay-space offsets (see _zone_world_offset for the multi-bay
        # gap fudge). For collision math we want the ship's bay-space
        # coordinates from the underlying ship_zones row, which equals
        # the PalletRect.cell_x/cell_y namespace. Build a quick lookup.
        bay_x = self._zone_local_offset_x(zone, zm["bay"]) + x
        bay_y = self._zone_local_offset_y(zone, zm["bay"]) + y
        target_cubes = {
            (bay_x + dx, bay_y + dy, z + dz)
            for dx in range(w) for dy in range(l) for dz in range(h)
        }
        displaced = []
        for p in self._pallets:
            if (getattr(p, "cargo_line_id", None) == dragged_cl
                    and getattr(p, "pallet_index", 0) == dragged_idx):
                continue
            pw = getattr(p, "cell_w", 1)
            pl = getattr(p, "cell_l", 1)
            ph = getattr(p, "cell_h", 1)
            px = getattr(p, "cell_x", 0)
            py = getattr(p, "cell_y", 0)
            pz = getattr(p, "cell_z", 0)
            p_cubes = {
                (px + dx, py + dy, pz + dz)
                for dx in range(pw) for dy in range(pl) for dz in range(ph)
            }
            if p_cubes & target_cubes:
                displaced.append(p)
        return displaced

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.RightButton:
            self._orbit_last = None
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if self._drag_shape is None:
            return
        shape = self._drag_shape
        target = self._drag_target
        orientation = self._drag_orientation
        self._drag_shape = None
        self._drag_target = None
        self._drag_cursor = None
        self._drag_orientation = 0
        self.drag_hint_label.setText("")
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
        # Force-push: if the target cubes are occupied by OTHER pallets,
        # confirm the displacement before locking. Approved displacees
        # get pushed to the holding table; the operator can place them
        # back later via the holding sidebar.
        displaced = self._find_displaced_pallets(
            zone, x, y, z, shape, orientation, cl_id, p_idx,
        )
        if displaced:
            from ..dialogs.force_push import ForcePushDialog
            incoming = {
                "cargo_line_id": cl_id,
                "pallet_index": p_idx,
                "size": getattr(rect, "pallet_size", 0),
                "destination": getattr(rect, "delivery_station_name", "—"),
            }
            displaced_info = [
                {
                    "cargo_line_id": d.cargo_line_id,
                    "pallet_index": d.pallet_index,
                    "size": d.pallet_size,
                    "destination": d.delivery_station_name,
                    "commodity": d.commodity_name,
                }
                for d in displaced
            ]
            dlg = ForcePushDialog(
                self.controller,
                incoming_pallet=incoming,
                displaced_pallets=displaced_info,
                parent=self,
            )
            if dlg.exec() != QDialog.DialogCode.Accepted:
                self.update()
                return
            confirmed = list(dlg.confirmed_displacements)
            # If the user unchecked any displacee, abort the drop (can't
            # land a pallet that depends on keeping someone else there).
            if len(confirmed) != len(displaced_info):
                QMessageBox.information(
                    self, "Drop aborted",
                    "Some pallets that would block this drop were "
                    "marked 'keep'. The pallet can't land here.",
                )
                self.update()
                return
            push_fn = getattr(self.controller, "push_pallets_to_holding", None)
            if push_fn is not None:
                try:
                    push_fn(confirmed, notes="Displaced by force-push in 3D view")
                except Exception as e:                  # noqa: BLE001
                    QMessageBox.warning(self, "Push failed", str(e))
                    self.update()
                    return
        try:
            # Prefer the orientation-aware signature; fall back to the
            # legacy 6-arg call if a stub controller doesn't accept it.
            try:
                fn(cl_id, p_idx, zone, x, y, z, orientation=orientation)
            except TypeError:
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
        self.refresh()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if (event.key() == Qt.Key.Key_R
                and self._drag_shape is not None):
            self._drag_orientation = 1 - self._drag_orientation
            # Mutate the rect's footprint so the ghost outline redraws
            # rotated. We keep the natural baseline in ``_drag_base_wl``
            # so the toggle is stable across multiple R presses.
            bw, bl = self._drag_base_wl
            rect = self._drag_shape.rect
            if self._drag_orientation == 1:
                rect.cell_w, rect.cell_l = bl, bw
            else:
                rect.cell_w, rect.cell_l = bw, bl
            self.update()
            return
        super().keyPressEvent(event)

    # ── painting ───────────────────────────────────────────────────────

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        self._draw_floor_grid(p)
        self._draw_zone_floors(p)
        self._draw_bay_outlines(p)
        self._draw_pallets(p)
        if self._drag_shape is not None:
            self._draw_drag_ghost(p)
        p.end()

    def _draw_floor_grid(self, p: QPainter) -> None:
        """Thin grid on the ground plane underneath the zones — a few
        cells of margin around the ship footprint. Helps sell the 3D
        scale and matches the look of polished SC cargo viewers."""
        if not self._zone_meta:
            return
        # Compute the world-space bounding box of all zones + margin.
        xs, ys = [], []
        for zm in self._zone_meta.values():
            xs.append(zm["world_x"])
            xs.append(zm["world_x"] + zm["width"])
            ys.append(zm["world_y"])
            ys.append(zm["world_y"] + zm["length"])
        if not xs:
            return
        margin = 4
        x0, x1 = min(xs) - margin, max(xs) + margin
        y0, y1 = min(ys) - margin, max(ys) + margin
        # Project corners + draw a grid of cell-sized parallelograms.
        # Light pen, no fill — the zone floors will overlay on top.
        p.setPen(QPen(QColor(60, 80, 95, 90), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        for gx in range(x0, x1 + 1):
            p.drawLine(self.project(gx, y0, 0), self.project(gx, y1, 0))
        for gy in range(y0, y1 + 1):
            p.drawLine(self.project(x0, gy, 0), self.project(x1, gy, 0))

    def _draw_bay_outlines(self, p: QPainter) -> None:
        """Translucent wireframe box for each bay showing its full
        height limit — the 'opacity guide' so the user sees how much
        head-room remains."""
        if not self._zone_meta:
            return
        # Group zones by bay so we draw one combined box per bay
        # rather than one per zone (looks cleaner on multi-zone bays).
        by_bay: dict[str, dict] = {}
        for zm in self._zone_meta.values():
            bay = zm.get("bay_label", "main")
            box = by_bay.setdefault(bay, {
                "x0": zm["world_x"],
                "x1": zm["world_x"] + zm["width"],
                "y0": zm["world_y"],
                "y1": zm["world_y"] + zm["length"],
                "h":  zm.get("height", 4),
            })
            box["x0"] = min(box["x0"], zm["world_x"])
            box["x1"] = max(box["x1"], zm["world_x"] + zm["width"])
            box["y0"] = min(box["y0"], zm["world_y"])
            box["y1"] = max(box["y1"], zm["world_y"] + zm["length"])
            box["h"]  = max(box["h"],  zm.get("height", 4))

        line_pen = QPen(QColor(38, 182, 212, 90), 1, Qt.PenStyle.DashLine)
        label_pen = QPen(QColor(91, 228, 255, 200))
        label_font = QFont("Segoe UI", 10)
        label_font.setBold(True)
        for bay_name, box in by_bay.items():
            x0, x1, y0, y1, h = box["x0"], box["x1"], box["y0"], box["y1"], box["h"]
            corners_low = [
                self.project(x0, y0, 0), self.project(x1, y0, 0),
                self.project(x1, y1, 0), self.project(x0, y1, 0),
            ]
            corners_high = [
                self.project(x0, y0, h), self.project(x1, y0, h),
                self.project(x1, y1, h), self.project(x0, y1, h),
            ]
            p.setPen(line_pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            for i in range(4):
                p.drawLine(corners_low[i], corners_low[(i + 1) % 4])
                p.drawLine(corners_high[i], corners_high[(i + 1) % 4])
                p.drawLine(corners_low[i], corners_high[i])
            # Bay label on the floor at the centre of the bay.
            cx = (x0 + x1) / 2
            cy = (y0 + y1) / 2
            label_pt = self.project(cx, cy, 0)
            p.setPen(label_pen)
            p.setFont(label_font)
            p.drawText(
                QRectF(label_pt.x() - 80, label_pt.y() + 8, 160, 18),
                Qt.AlignmentFlag.AlignCenter,
                bay_name.title() + " Bay",
            )

    def _draw_zone_floors(self, p: QPainter) -> None:
        """Paint a faint parallelogram for each zone's ground footprint."""
        for zone_label, zm in self._zone_meta.items():
            zx, zy = zm["world_x"], zm["world_y"]
            zw, zl = zm["width"], zm["length"]
            corners = [
                self.project(zx,      zy,      0),
                self.project(zx + zw, zy,      0),
                self.project(zx + zw, zy + zl, 0),
                self.project(zx,      zy + zl, 0),
            ]
            poly = QPolygonF(corners)
            p.setBrush(QBrush(QColor(20, 32, 40)))
            p.setPen(QPen(QColor(38, 182, 212), 1.2))
            p.drawPolygon(poly)

            # zone label at the back-most projected corner
            label_pt = corners[0]
            p.setPen(QPen(QColor("#5be4ff")))
            font = QFont("Segoe UI", 9)
            font.setBold(True)
            p.setFont(font)
            p.drawText(QRectF(label_pt.x() - 30, label_pt.y() - 18, 80, 14),
                       Qt.AlignmentFlag.AlignCenter, zone_label)

            # Drop-target hover highlight
            if (self._drag_target is not None
                    and self._drag_target[0] == zone_label):
                p.setBrush(QBrush(QColor(123, 191, 63, 60)))
                p.setPen(QPen(QColor("#7bbf3f"), 2))
                p.drawPolygon(poly)

    def _draw_pallets(self, p: QPainter) -> None:
        size_font = QFont("Segoe UI", 9)
        size_font.setBold(True)
        label_font = QFont("Segoe UI", 7)
        for i, shape in enumerate(self._shapes):
            if self._drag_shape is shape:
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setPen(QPen(QColor(255, 255, 255, 70), 1, Qt.PenStyle.DashLine))
                p.drawPolygon(shape.silhouette)
                continue

            base = QColor(shape.rect.color)
            # Soft pastel shading — barely-different side faces matching
            # the look of polished SC cargo viewers. Top stays at base,
            # sides are subtle darkenings so the 3D form reads without
            # the heavy contrast we had before.
            top_c = base
            side_a_c = base.darker(106)
            side_b_c = base.darker(112)
            border = base.darker(190)
            border_pen = QPen(border, 0.8)

            p.setPen(border_pen)
            # Paint the more-glancing face first (it's mostly behind
            # the closer one in screen space), then the closer one.
            p.setBrush(QBrush(side_b_c))
            p.drawPolygon(shape.side_b)
            p.setBrush(QBrush(side_a_c))
            p.drawPolygon(shape.side_a)
            p.setBrush(QBrush(top_c))
            p.drawPolygon(shape.top)

            # Pick text color from the top face's lightness — pastel
            # colors get a dark glyph; saturated darks get a light one.
            text_color = (QColor("#ffffff") if top_c.lightness() < 140
                          else QColor("#1a2530"))
            p.setPen(QPen(text_color))
            cx = sum(pt.x() for pt in shape.top) / 4
            cy = sum(pt.y() for pt in shape.top) / 4
            p_idx = getattr(shape.rect, "pallet_index", 0)
            r = shape.rect
            # On big pallets (top face wide enough to read text) print
            # commodity + Pickup → Destination directly on the face.
            # Smaller pallets show just the SCU number — the tooltip
            # has the rest.
            top_screen_w = max(abs(shape.top[1].x() - shape.top[0].x()),
                                abs(shape.top[2].x() - shape.top[3].x()))
            commodity = getattr(r, "commodity_name", "") or ""
            pickup = getattr(r, "pickup_station_name", "") or ""
            dest = getattr(r, "delivery_station_name", "") or ""
            if top_screen_w >= 80 and (commodity or dest):
                p.setFont(size_font)
                p.drawText(QRectF(cx - 30, cy - 18, 60, 14),
                           Qt.AlignmentFlag.AlignCenter,
                           f"{r.label} SCU")
                p.setFont(label_font)
                if commodity:
                    p.drawText(QRectF(cx - 60, cy - 4, 120, 12),
                               Qt.AlignmentFlag.AlignCenter, commodity)
                if dest:
                    route = self._short_route(pickup, dest)
                    p.drawText(QRectF(cx - 70, cy + 6, 140, 12),
                               Qt.AlignmentFlag.AlignCenter, route)
            else:
                p.setFont(size_font)
                p.drawText(QRectF(cx - 18, cy - 8, 36, 16),
                           Qt.AlignmentFlag.AlignCenter,
                           f"{r.label}")

            cl_id = getattr(shape.rect, "cargo_line_id", None)
            if cl_id is not None and (cl_id, p_idx) in self._locks:
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setPen(QPen(QColor("#ffd24a"), 2))
                p.drawPolygon(shape.silhouette)

            if self._hover_index == i and self._drag_shape is None:
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setPen(QPen(QColor("#ff8a3c"), 2))
                p.drawPolygon(shape.silhouette)

    @staticmethod
    def _short_route(pickup: str, dest: str) -> str:
        """Compact route string for pallet labels — drops the
        "Station" suffix and the L-point prefix on Lagrange names so
        e.g. "CRU-L1 Ambitious Dream Station → Baijini Point" fits."""
        def _short(name: str) -> str:
            n = name or ""
            n = n.replace(" Station", "").strip()
            # Drop a leading "XYZ-LN " Lagrange prefix.
            if len(n) > 7 and n[3:5] == "-L" and n[6:7] == " ":
                n = n[7:]
            return n.strip() or "—"
        return f"{_short(pickup)} → {_short(dest)}"

    def _draw_drag_ghost(self, p: QPainter) -> None:
        """Outline of the dragged pallet snapped to the candidate cell."""
        shape = self._drag_shape
        if shape is None:
            return
        if self._drag_target is None:
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
        # Use the (possibly R-toggled) cell_w / cell_l from the rect.
        ghost = self._build_cuboid_shape(
            wx, wy, lz, r.cell_w, r.cell_l, r.cell_h, r,
        )
        base = QColor(r.color)
        base.setAlpha(120)
        p.setBrush(QBrush(base))
        p.setPen(QPen(QColor("#ffffff"), 1.5, Qt.PenStyle.DashLine))
        p.drawPolygon(ghost.silhouette)


# ── dialog wrapper ───────────────────────────────────────────────────────

def open_iso_view(controller, parent=None):
    """Open the IsoBayCanvas in a resizable modeless dialog with the
    holding-table sidebar."""
    from PySide6.QtWidgets import QDialog, QHBoxLayout as _H, QSplitter
    from ..widgets.holding_table import HoldingTableWidget
    dlg = QDialog(parent)
    dlg.setWindowTitle("Cargo Bay — 3D View")
    dlg.resize(1200, 800)
    splitter = QSplitter(Qt.Orientation.Horizontal, dlg)
    canvas = IsoBayCanvas(controller, parent=splitter)
    holding = HoldingTableWidget(controller, parent=splitter)
    # When the user clicks "Pick up" on a holding pallet, refresh the
    # canvas so the next click in 3D can target a cube; the pallet is
    # off any zone until it gets a new lock.
    holding.pallet_picked.connect(lambda _cl, _idx: canvas.refresh())
    splitter.addWidget(canvas)
    splitter.addWidget(holding)
    splitter.setStretchFactor(0, 3)
    splitter.setStretchFactor(1, 1)
    splitter.setSizes([900, 300])
    lay = _H(dlg)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.addWidget(splitter)
    canvas.refresh()
    holding.refresh()
    dlg.show()
    return dlg
