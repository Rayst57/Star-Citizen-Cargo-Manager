"""Tests for the isometric 3D-view widget.

Run under ``QT_QPA_PLATFORM=offscreen`` like the other widget tests.
The tests cover three things:
    1. Pure-math round-trip of iso_project / iso_unproject_ground.
    2. The widget instantiates and paints on a tiny workday without
       raising.
    3. A synthesised press-drag-release sequence calls
       ``controller.lock_pallet`` with the expected arguments.

The third test uses a hand-rolled fake controller so it doesn't depend
on the foundation agent's lock_pallet landing first.
"""

from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from dataclasses import dataclass, field

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint, QPointF, Qt, QEvent
from PySide6.QtGui import QKeyEvent, QMouseEvent, QPixmap
from PySide6.QtWidgets import QApplication

from src.app_controller import AppController
from src.db.init_db import initialize_database
from src.palletizer_color import assign_destination_colors
from src.planner.recompute import recompute as run_recompute
from src.ui.widgets.iso_bay_canvas import (
    IsoBayCanvas, iso_project, iso_unproject_ground,
)


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def controller(qapp, tmp_path):
    db_path = tmp_path / "test.db"
    conn = initialize_database(db_path)
    return AppController(conn, db_path)


def _seraphim(controller) -> int:
    return controller.conn.execute(
        "SELECT id FROM stations WHERE name = 'Seraphim Station'"
    ).fetchone()["id"]


def _seed_tiny_workday(controller) -> int:
    wid = controller.start_workday(_seraphim(controller), None, False)
    controller.add_contract({
        "pickup_station": "Yellow Core",
        "max_pallet_size": 8,
        "deliveries": [
            {"destination": "Everus Harbor", "commodity": "Tungsten", "scu": 32},
        ],
    })
    controller.add_contract({
        "pickup_station": "Yellow Core",
        "max_pallet_size": 8,
        "deliveries": [
            {"destination": "Baijini Point", "commodity": "Tungsten", "scu": 24},
        ],
    })
    result = run_recompute(wid, controller.conn)
    controller._last_result = result
    assign_destination_colors(wid, controller.conn)
    return wid


# ── 1. Pure math ──────────────────────────────────────────────────────

def test_iso_projection_round_trip():
    """Projecting a ground-plane (z=0) cell to screen and reversing
    must round-trip exactly (within 1 px of float error)."""
    origin = (400.0, 300.0)
    cell_w = 22.0
    cell_h = 18.0
    for wx, wy in [(0, 0), (1, 0), (0, 1), (3, 5), (10, 2), (7, 9)]:
        sx, sy = iso_project(wx, wy, 0, *origin, cell_w, cell_h)
        rx, ry = iso_unproject_ground(sx, sy, *origin, cell_w, cell_h)
        assert abs(rx - wx) < 1e-6, f"({wx},{wy}) round-trip rx={rx}"
        assert abs(ry - wy) < 1e-6, f"({wx},{wy}) round-trip ry={ry}"


def test_iso_projection_z_goes_up_on_screen():
    """Higher world_z must give SMALLER screen_y (axis points up)."""
    origin = (200.0, 200.0)
    _, sy0 = iso_project(0, 0, 0, *origin)
    _, sy1 = iso_project(0, 0, 1, *origin)
    _, sy2 = iso_project(0, 0, 2, *origin)
    assert sy0 > sy1 > sy2


# ── 2. Render ─────────────────────────────────────────────────────────

def test_iso_canvas_renders_without_crashing(controller):
    _seed_tiny_workday(controller)
    canvas = IsoBayCanvas(controller)
    canvas.resize(800, 600)
    canvas.refresh()

    pm = QPixmap(canvas.size())
    pm.fill(Qt.GlobalColor.black)
    canvas.render(pm)
    # If we get here, no exception was raised during paint.
    assert not pm.isNull()


def test_iso_canvas_empty_workday_no_crash(qapp, tmp_path):
    db_path = tmp_path / "empty.db"
    conn = initialize_database(db_path)
    ctrl = AppController(conn, db_path)
    canvas = IsoBayCanvas(ctrl)
    canvas.resize(600, 400)
    canvas.refresh()
    pm = QPixmap(canvas.size())
    pm.fill(Qt.GlobalColor.black)
    canvas.render(pm)
    assert not pm.isNull()


# ── 3. Drag-and-drop ──────────────────────────────────────────────────

@dataclass
class _FakeRect:
    cargo_line_id: int
    zone_label: str
    bay: str
    cell_x: int
    cell_y: int
    cell_z: int
    cell_w: int
    cell_l: int
    cell_h: int
    pallet_size: int = 8
    color: str = "#3a7bd5"
    is_conflicted: bool = False
    label: str = "8"
    delivery_station_name: str = "Everus Harbor"
    commodity_name: str = "Tungsten"
    contract_number: int = 1
    ship_forward_y: str = "high"
    conflict_partner_colors: list = field(default_factory=list)
    pallet_index: int = 0


@dataclass
class _FakeZone:
    zone_label: str
    cube_offset_x: int = 0
    cube_offset_y: int = 0
    width_units: int = 4
    length_units: int = 6


@dataclass
class _FakeBay:
    bay_label: str = "main"
    width_cells: int = 4
    length_cells: int = 6
    scu_capacity: int = 96
    ship_forward_y: str = "high"
    ramp_at_top: bool = False
    ramp_label: str = "rear ramp"
    zones: list = field(default_factory=list)


class _FakeController:
    """Minimal stand-in that exercises lock_pallet."""

    def __init__(self):
        self.bay = _FakeBay(zones=[
            _FakeZone("RM", 0, 0, 4, 6),
        ])
        self.rect = _FakeRect(
            cargo_line_id=42, zone_label="RM", bay="main",
            cell_x=0, cell_y=0, cell_z=0,
            cell_w=2, cell_l=2, cell_h=1,
            pallet_index=3,
        )
        self.lock_calls: list[tuple] = []
        self.unlock_calls: list[tuple] = []
        self.clear_calls = 0

    def get_bay_layout(self):
        return [self.bay]

    def get_pallet_rects(self, stop_number=None):
        return [self.rect]

    def get_last_result(self):
        return None

    def lock_pallet(self, cargo_line_id, pallet_index, zone, x, y, z):
        self.lock_calls.append((cargo_line_id, pallet_index, zone, x, y, z))

    def unlock_pallet(self, cargo_line_id, pallet_index):
        self.unlock_calls.append((cargo_line_id, pallet_index))

    def clear_pallet_locks(self):
        self.clear_calls += 1

    def list_pallet_locks(self):
        return []


def _make_mouse_event(kind, pos: QPoint) -> QMouseEvent:
    return QMouseEvent(
        kind,
        QPointF(pos),
        QPointF(pos),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton if kind != QEvent.Type.MouseMove
        else Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )


def test_drag_drop_calls_lock_pallet(qapp):
    """Click the pallet at its known projected center, drag to another
    cell within the same zone, release — controller.lock_pallet must
    fire with the new (zone, x, y, z) coords."""
    ctrl = _FakeController()
    canvas = IsoBayCanvas(ctrl)
    canvas.resize(800, 600)
    canvas.refresh()
    assert canvas._shapes, "geometry should have at least the seeded pallet"

    shape = canvas._shapes[0]
    # Pick the center of the top face of the only pallet as press pos.
    cx = sum(pt.x() for pt in shape.top) / 4
    cy = sum(pt.y() for pt in shape.top) / 4
    press_pos = QPoint(int(cx), int(cy))

    # Drop on a different ground cell within the same zone (RM is 4x6).
    # Use the camera-aware projection at (cell 2, cell 3, 0) — well
    # inside the zone and not under any other pallet.
    drop_pt = canvas.project(2.5, 3.5, 0)
    drop_pos = QPoint(int(drop_pt.x()), int(drop_pt.y()))

    canvas.mousePressEvent(_make_mouse_event(QEvent.Type.MouseButtonPress, press_pos))
    assert canvas._drag_shape is not None, "press should pick up pallet"

    canvas.mouseMoveEvent(_make_mouse_event(QEvent.Type.MouseMove, drop_pos))
    assert canvas._drag_target is not None, "drag move should resolve drop target"

    canvas.mouseReleaseEvent(_make_mouse_event(QEvent.Type.MouseButtonRelease, drop_pos))

    assert len(ctrl.lock_calls) == 1, f"expected one lock call, got {ctrl.lock_calls}"
    cl_id, p_idx, zone, x, y, z = ctrl.lock_calls[0]
    assert cl_id == 42
    assert p_idx == 3
    assert zone == "RM"
    # The drop point was (2.5, 3.5) in world coords, so it lands in
    # local cell (2, 3) at z=0 (no pallets in that column).
    assert (x, y, z) == (2, 3, 0)


def test_clear_locks_button_calls_controller(qapp):
    ctrl = _FakeController()
    canvas = IsoBayCanvas(ctrl)
    canvas.refresh()
    canvas._on_clear_locks()
    assert ctrl.clear_calls == 1


# ── 4. Camera orbit + rotation ────────────────────────────────────────


def test_camera_orbit_changes_projection(qapp):
    """Changing the camera yaw must move a pallet's projected position.

    Pure-math sanity: project at default yaw, rotate the camera, and
    confirm the same world cube now lives at a different screen point.
    """
    import math
    ctrl = _FakeController()
    canvas = IsoBayCanvas(ctrl)
    canvas.resize(800, 600)
    canvas.refresh()

    p0 = canvas.project(2.0, 1.0, 0.0)
    canvas._yaw += math.radians(45)
    p1 = canvas.project(2.0, 1.0, 0.0)
    # x and y must both shift — the rotation isn't axis-aligned for a
    # non-origin point at a fresh 45-degree yaw bump.
    assert (abs(p1.x() - p0.x()) > 1e-3
            or abs(p1.y() - p0.y()) > 1e-3), (
        f"Orbit had no effect on projection: {p0} -> {p1}"
    )


def test_unproject_round_trip_after_orbit(qapp):
    """project() followed by unproject_ground() must round-trip on the
    z=0 ground plane for arbitrary yaw and pitch."""
    import math
    ctrl = _FakeController()
    canvas = IsoBayCanvas(ctrl)
    canvas.resize(800, 600)
    canvas.refresh()

    canvas._yaw = math.radians(33)
    canvas._pitch = math.radians(48)

    wx, wy = 3.0, 5.0
    pt = canvas.project(wx, wy, 0.0)
    rx, ry = canvas.unproject_ground(pt.x(), pt.y())
    assert abs(rx - wx) < 1e-6, f"round-trip rx={rx}, expected {wx}"
    assert abs(ry - wy) < 1e-6, f"round-trip ry={ry}, expected {wy}"


class _RotFakeController(_FakeController):
    """Variant of _FakeController whose lock_pallet captures the
    orientation kwarg (so the rotation test can verify it)."""

    def __init__(self):
        super().__init__()
        # 16 SCU = (w=2, l=4) per scu_boxes.json — exercise the
        # rotation by starting with the natural footprint.
        self.rect = _FakeRect(
            cargo_line_id=42, zone_label="RM", bay="main",
            cell_x=0, cell_y=0, cell_z=0,
            cell_w=2, cell_l=4, cell_h=2,
            pallet_size=16, pallet_index=0,
        )

    def lock_pallet(self, cargo_line_id, pallet_index, zone, x, y, z,
                    orientation=0):
        self.lock_calls.append((cargo_line_id, pallet_index, zone,
                                x, y, z, orientation))


def _make_key_event(kind, key) -> QKeyEvent:
    return QKeyEvent(kind, key, Qt.KeyboardModifier.NoModifier)


def test_pallet_rotation_swaps_footprint(qapp):
    """Drag a 16-SCU pallet (w=2, l=4), press R to rotate, drop.

    After the drop the controller is called with orientation=1 and the
    held rect carries the swapped (w=4, l=2) footprint so the renderer
    paints the rotated outline.
    """
    ctrl = _RotFakeController()
    canvas = IsoBayCanvas(ctrl)
    canvas.resize(800, 600)
    canvas.refresh()
    assert canvas._shapes, "geometry should include the seeded 16-SCU pallet"

    shape = canvas._shapes[0]
    cx = sum(pt.x() for pt in shape.top) / 4
    cy = sum(pt.y() for pt in shape.top) / 4
    press_pos = QPoint(int(cx), int(cy))

    canvas.mousePressEvent(_make_mouse_event(
        QEvent.Type.MouseButtonPress, press_pos,
    ))
    assert canvas._drag_shape is not None
    assert canvas._drag_orientation == 0
    # Natural footprint at the start of the drag.
    rect = canvas._drag_shape.rect
    assert (rect.cell_w, rect.cell_l) == (2, 4)

    # Press R — orientation flips and the rect's footprint swaps.
    canvas.keyPressEvent(_make_key_event(
        QEvent.Type.KeyPress, Qt.Key.Key_R,
    ))
    assert canvas._drag_orientation == 1
    assert (rect.cell_w, rect.cell_l) == (4, 2), (
        f"R should swap (w, l) → got ({rect.cell_w}, {rect.cell_l})"
    )

    # Drop somewhere valid inside the 4x6 RM zone. (0, 0) is fine for
    # a rotated 16 SCU (4x2 footprint), since 0+4 <= 4 and 0+2 <= 6.
    drop_pt = canvas.project(0.0, 0.0, 0)
    # Aim slightly into the cell so unproject_ground floors to (0, 0).
    drop_pos = QPoint(int(drop_pt.x()) + 1, int(drop_pt.y()) + 1)

    canvas.mouseMoveEvent(_make_mouse_event(
        QEvent.Type.MouseMove, drop_pos,
    ))
    canvas.mouseReleaseEvent(_make_mouse_event(
        QEvent.Type.MouseButtonRelease, drop_pos,
    ))

    assert len(ctrl.lock_calls) == 1, ctrl.lock_calls
    cl_id, p_idx, zone, x, y, z, orientation = ctrl.lock_calls[0]
    assert (cl_id, p_idx, zone) == (42, 0, "RM")
    assert orientation == 1, (
        f"Expected orientation=1 after R press, got {orientation}"
    )


# ── 5. Face visibility + painter's depth regressions ──────────────────
#
# These guard the camera math that produced the "inside-out cubes"
# render: the visible-face picker selected BACK faces (view vector had
# flipped signs and swapped sin/cos) and the depth sort drew NEAR
# pallets first so far ones painted over them.

def _toward_camera(canvas) -> tuple[float, float, float]:
    """World-space toward-camera vector for the canvas's current
    yaw/pitch. In the yaw-rotated frame the view axis is (0, sp, cp)
    — displacement along it leaves the screen position unchanged and
    Z is positive because the camera looks down."""
    import math
    sy = math.sin(canvas._yaw)
    cy = math.cos(canvas._yaw)
    sp = math.sin(canvas._pitch)
    cp = math.cos(canvas._pitch)
    return (sy * sp, cy * sp, cp)


def test_visible_side_faces_point_at_camera(controller):
    """Every rendered side face's outward normal must have a positive
    dot product with the toward-camera vector — otherwise we're
    painting the inside of the box."""
    _seed_tiny_workday(controller)
    canvas = IsoBayCanvas(controller)
    canvas.refresh()
    # Try several camera angles including the default.
    for yaw in (0.3, 1.2, 2.5, 4.0, 5.5):
        canvas._yaw = yaw
        canvas._rebuild_geometry()
        view = _toward_camera(canvas)
        for shape in canvas._shapes:
            for normal in (shape.side_a_normal, shape.side_b_normal):
                dot = (normal[0] * view[0] + normal[1] * view[1]
                       + normal[2] * view[2])
                assert dot > -1e-9, (
                    f"yaw={yaw}: face normal {normal} faces AWAY from "
                    f"the camera (dot={dot:.3f}) — inside-out cube."
                )


def test_depth_sort_draws_far_pallets_first(controller):
    """The shape list must be ordered back-to-front: a pallet stacked
    ON TOP of another (same x,y, higher z) must come later in the
    list (drawn over its base), and a pallet closer to the camera
    must come after one farther away."""
    _seed_tiny_workday(controller)
    canvas = IsoBayCanvas(controller)
    canvas.refresh()
    # Stacking: depth key must increase with wz at a fixed (x, y).
    d_low = canvas._depth_for(3.0, 3.0, 0.0)
    d_high = canvas._depth_for(3.0, 3.0, 2.0)
    assert d_high > d_low, (
        "A pallet stacked higher must be NEARER (larger depth key) "
        "so it draws after — i.e. on top of — its base."
    )
    # Nearness along the ground: at default yaw, larger rotated-Y is
    # lower on screen = closer to the camera.
    import math
    sy = math.sin(canvas._yaw)
    cy = math.cos(canvas._yaw)
    d_far = canvas._depth_for(-10 * sy, -10 * cy, 0.0)
    d_near = canvas._depth_for(10 * sy, 10 * cy, 0.0)
    assert d_near > d_far
    # And the rendered list is ascending on depth_key.
    keys = [s.depth_key for s in canvas._shapes]
    assert keys == sorted(keys)
