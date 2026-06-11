"""Regression tests for the 3D editor bugs around holding, displacement,
and post-drop auto-shuffling.

Covers:

1. ``get_pallet_rects`` does not redraw a pallet that's in
   ``pallet_holding`` (so closing + reopening the 3D dialog can't
   "auto-arrange" it back onto the ship).
2. ``IsoBayCanvas._find_displaced_pallets`` ignores pallets from a
   different bay — without the bay filter, dropping in F1 collided
   with a same-cell pallet in R1 because bay-local cube coordinates
   repeat across bays.
3. ``AppController.freeze_visible_layout`` locks every currently
   rendered, non-spanning, non-excluded pallet at its current cell, so
   the next ``get_pallet_rects`` returns the same layout (no erratic
   re-arrange).
4. ``IsoBayCanvas.start_pickup`` arms a pickup ghost for a holding
   pallet and a click commits the drop, calling ``lock_pallet`` and
   ``remove_from_holding`` together.
"""

from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication

from src.app_controller import AppController
from src.db.init_db import initialize_database
from src.palletizer_color import assign_destination_colors
from src.planner.recompute import recompute as run_recompute
from src.ui.widgets.iso_bay_canvas import IsoBayCanvas


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def controller(qapp, tmp_path):
    db_path = tmp_path / "test.db"
    conn = initialize_database(db_path)
    return AppController(conn, db_path)


def _ironclad(controller) -> int:
    return controller.conn.execute(
        "SELECT id FROM ships WHERE name = 'Drake Ironclad'"
    ).fetchone()["id"]


def _seraphim(controller) -> int:
    return controller.conn.execute(
        "SELECT id FROM stations WHERE name = 'Seraphim Station'"
    ).fetchone()["id"]


def _seed_two_bay_workday(controller) -> int:
    """A workday on the Drake Ironclad (Aft + Forward bays) with three
    contracts so several pallets end up on the ship."""
    wid = controller.start_workday(
        _seraphim(controller), None, False, ship_id=_ironclad(controller),
    )
    for dest in ("Everus Harbor", "Baijini Point", "Port Tressler"):
        controller.add_contract({
            "pickup_station": "Yellow Core",
            "max_pallet_size": 8,
            "deliveries": [
                {"destination": dest, "commodity": "Tungsten", "scu": 32},
            ],
        })
    result = run_recompute(wid, controller.conn)
    controller._last_result = result
    assign_destination_colors(wid, controller.conn)
    return wid


# ── 1. Holding pallets don't redraw ──────────────────────────────────


def test_holding_pallet_does_not_render(controller):
    wid = _seed_two_bay_workday(controller)
    snaps = controller._last_result.snapshots
    stop = max(snaps.keys(),
               key=lambda sn: sum(e.scu_amount for e in snaps[sn]))
    rects = controller.get_pallet_rects(stop_number=stop)
    assert rects, "Fixture must produce some pallets to start with."

    target = rects[0]
    cl_id = target.cargo_line_id
    p_idx = target.pallet_index

    controller.push_pallets_to_holding([(cl_id, p_idx)])

    rects_after = controller.get_pallet_rects(stop_number=stop)
    assert not any(
        r.cargo_line_id == cl_id and r.pallet_index == p_idx
        for r in rects_after
    ), (
        "A pallet in pallet_holding must not appear in get_pallet_rects "
        "— otherwise reopening the 3D dialog 're-arranges' it onto the "
        "ship."
    )


# ── 2. Cross-bay displacement false-positive ─────────────────────────


def test_displacement_ignores_pallets_in_other_bays(controller):
    """A drop in the Forward Bay must not "displace" pallets in the Aft
    Bay just because they share the bay-local cube coordinate (0, 0, 0).
    """
    _seed_two_bay_workday(controller)
    snaps = controller._last_result.snapshots
    busiest = max(snaps.keys(),
                  key=lambda sn: sum(e.scu_amount for e in snaps[sn]))
    canvas = IsoBayCanvas(controller)
    canvas.refresh()
    canvas._stop_number = busiest
    canvas._pallets = list(controller.get_pallet_rects(stop_number=busiest))
    canvas._rebuild_geometry()
    assert canvas._pallets, "Need at least one rendered pallet."

    # Synthesize a same-cube/different-bay scenario: an existing pallet
    # sits in Aft Bay at bay-local (0, 0, 0), and the user drops a
    # different pallet at Forward Bay F1 (0, 0, 0). Pre-fix the
    # displacement walker would flag the aft pallet because it didn't
    # filter by bay.
    import copy
    src = canvas._pallets[0]
    aft_existing = copy.copy(src)
    aft_existing.bay = "Aft Bay"
    aft_existing.zone_label = "R1"
    aft_existing.cell_x = aft_existing.cell_y = aft_existing.cell_z = 0
    aft_existing.pallet_index = src.pallet_index + 1000  # distinct id

    dragged = copy.copy(src)
    dragged.bay = "Forward Bay"
    dragged.zone_label = "F1"
    dragged.cell_x = dragged.cell_y = dragged.cell_z = 0

    canvas._pallets = [aft_existing, dragged]
    canvas._rebuild_geometry()
    dragged_shape = next(
        (s for s in canvas._shapes if s.rect is dragged), None,
    )
    assert dragged_shape is not None

    displaced = canvas._find_displaced_pallets(
        "F1", 0, 0, 0, dragged_shape, orientation=0,
        dragged_cl=dragged.cargo_line_id,
        dragged_idx=dragged.pallet_index,
    )
    assert aft_existing not in displaced, (
        "Cross-bay pallet at the same bay-local cube must NOT be "
        "flagged as displaced — bay filter is missing."
    )


# ── 3. freeze_visible_layout pins the rest of the bay ────────────────


def test_freeze_visible_layout_locks_unlocked_pallets(controller):
    wid = _seed_two_bay_workday(controller)
    snaps = controller._last_result.snapshots
    stop = max(snaps.keys(),
               key=lambda sn: sum(e.scu_amount for e in snaps[sn]))
    rects_before = controller.get_pallet_rects(stop_number=stop)
    assert rects_before

    # Lock nothing yet — every rendered pallet is auto-packed.
    n_locks = controller.conn.execute(
        "SELECT COUNT(*) AS c FROM pallet_locks WHERE workday_id = ?",
        (wid,),
    ).fetchone()["c"]
    assert n_locks == 0

    # Exclude the first pallet (the one the user is "dropping").
    excluded = (rects_before[0].cargo_line_id, rects_before[0].pallet_index)
    n_frozen = controller.freeze_visible_layout(
        stop_number=stop, exclude=[excluded],
    )
    assert n_frozen > 0

    n_locks = controller.conn.execute(
        "SELECT COUNT(*) AS c FROM pallet_locks WHERE workday_id = ?",
        (wid,),
    ).fetchone()["c"]
    assert n_locks == n_frozen, "Frozen pallets must persist as locks."

    # The frozen layout must round-trip: cells stay put on the next
    # call to get_pallet_rects, including for the EXCLUDED pallet
    # whose unlocked position the auto-packer should still honour
    # (since all the other slots are now reserved).
    rects_after = controller.get_pallet_rects(stop_number=stop)
    before_by_key = {
        (r.cargo_line_id, r.pallet_index): (r.zone_label, r.cell_x,
                                            r.cell_y, r.cell_z)
        for r in rects_before
    }
    after_by_key = {
        (r.cargo_line_id, r.pallet_index): (r.zone_label, r.cell_x,
                                            r.cell_y, r.cell_z)
        for r in rects_after
    }
    for key, pos in before_by_key.items():
        if key == excluded:
            continue
        assert after_by_key.get(key) == pos, (
            f"Frozen pallet {key} moved between renders: "
            f"before={pos} after={after_by_key.get(key)}"
        )


# ── 4. start_pickup arms a pickup and click commits + clears holding ─


def test_start_pickup_commits_lock_and_removes_from_holding(controller):
    wid = _seed_two_bay_workday(controller)
    snaps = controller._last_result.snapshots
    busiest = max(snaps.keys(),
                  key=lambda sn: sum(e.scu_amount for e in snaps[sn]))
    canvas = IsoBayCanvas(controller)
    canvas.resize(800, 600)
    canvas.refresh()
    # Force the canvas to the busiest stop so on-board pallets exist.
    canvas._stop_number = busiest
    canvas._pallets = list(controller.get_pallet_rects(stop_number=busiest))
    canvas._rebuild_geometry()
    assert canvas._pallets

    target = canvas._pallets[0]
    cl_id, p_idx = target.cargo_line_id, target.pallet_index
    controller.push_pallets_to_holding([(cl_id, p_idx)])
    canvas.refresh()

    holding_before = {
        (int(r["cargo_line_id"]), int(r["pallet_index"]))
        for r in controller.list_holding_pallets()
    }
    assert (cl_id, p_idx) in holding_before

    canvas.start_pickup(cl_id, p_idx)
    assert canvas._pickup_active is True
    assert canvas._drag_shape is not None

    # Pick a drop target by finding a zone with room and asking the
    # canvas to translate (zone-local x, y) -> screen pixels via its
    # projection. The simplest path: pick the first zone's origin.
    zone_label = next(iter(canvas._zone_meta))
    zm = canvas._zone_meta[zone_label]
    sp = canvas.project(zm["world_x"] + 0.5, zm["world_y"] + 0.5, 0)
    canvas._drag_cursor = QPoint(int(sp.x()), int(sp.y()))
    canvas._drag_target = canvas._drop_target(canvas._drag_cursor)
    assert canvas._drag_target is not None, (
        "Test scaffold: drop target reverse-projection failed."
    )

    canvas._commit_drop()

    assert canvas._pickup_active is False
    holding_after = {
        (int(r["cargo_line_id"]), int(r["pallet_index"]))
        for r in controller.list_holding_pallets()
    }
    assert (cl_id, p_idx) not in holding_after, (
        "Pickup commit must remove the pallet from holding."
    )
    locked = controller.conn.execute(
        "SELECT zone_label, cube_x, cube_y, cube_z "
        "FROM pallet_locks WHERE workday_id = ? "
        "  AND cargo_line_id = ? AND pallet_index = ?",
        (wid, cl_id, p_idx),
    ).fetchone()
    assert locked is not None, (
        "Pickup commit must persist a pallet_locks row."
    )
