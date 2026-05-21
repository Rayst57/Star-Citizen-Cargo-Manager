"""Tests for the temporal stop-by-stop zone planner.

These cases verify the simulator behaviour the static placer couldn't
produce — bay reuse after unload, RB-as-last-resort, transload
consolidation, and the over-capacity peak distinction.
"""

from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from src.app_controller import AppController
from src.db.init_db import initialize_database
from src.palletizer_color import assign_destination_colors
from src.planner.recompute import recompute as run_recompute


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


def _zones_used_at(result, stop_number: int) -> set[str]:
    return {e.zone_label for e in result.snapshots.get(stop_number, [])}


def test_bay_freed_by_unload_is_reused_on_next_load(controller):
    """Cargo that unloads early should free its zone for subsequent loads.

    Pick up 64 SCU for Ambitious Dream (delivered at the 2nd stop),
    then 64 SCU for Long Forest (delivered at the last stop). The Long
    Forest load happens AFTER Ambitious Dream has unloaded, so its
    cargo should be allowed to reuse the zone the AD cargo just left.
    """
    wid = controller.start_workday(_seraphim(controller), None, False)
    controller.add_contract({
        "pickup_station": "Seraphim Station",
        "max_pallet_size": 8,
        "deliveries": [
            {"destination": "Ambitious Dream", "commodity": "Tungsten", "scu": 64},
        ],
    })
    controller.add_contract({
        # Pickup at a station that's visited AFTER Ambitious Dream so
        # the AD unload has happened before the LF load.
        "pickup_station": "Yellow Core",
        "max_pallet_size": 8,
        "deliveries": [
            {"destination": "Long Forest", "commodity": "Tungsten", "scu": 64},
        ],
    })
    result = run_recompute(wid, controller.conn)
    controller._last_result = result
    assign_destination_colors(wid, controller.conn)

    # Only one zone should be in use at any single stop.
    for stop in result.route_stops:
        zones_now = _zones_used_at(result, stop.stop_number)
        assert len(zones_now) <= 1, (
            f"Stop {stop.stop_number} ({stop.station_name}): "
            f"expected ≤1 active zone, got {zones_now}. The simulator "
            f"isn't reusing the bay vacated by Ambitious Dream's unload."
        )


def test_loads_drain_lowest_priority_bay_first(controller):
    """A single 32 SCU load should land in the zone with the LOWEST
    unload_priority that fits — not the largest-capacity zone.

    On the C2 the lowest-prio zones are F1/F2/F3 (cap=72, prio 1–3);
    R-bays come later (prio 4+). 32 SCU should land in F1.
    """
    wid = controller.start_workday(_seraphim(controller), None, False)
    controller.add_contract({
        "pickup_station": "Yellow Core",
        "max_pallet_size": 8,
        "deliveries": [
            {"destination": "Long Forest", "commodity": "Tungsten", "scu": 32},
        ],
    })
    result = run_recompute(wid, controller.conn)
    controller._last_result = result

    # The load happens at Yellow Core (stop 2). Snapshot AFTER that
    # stop should show the cargo in F1.
    yc_stop = next(s for s in result.route_stops
                   if s.station_name == "Yellow Core")
    snap = result.snapshots.get(yc_stop.stop_number, [])
    assert snap, "Expected cargo on board after Yellow Core load"
    used = {e.zone_label for e in snap}
    assert used == {"F1"}, (
        f"32 SCU should drain F1 (prio=1) before any other bay. "
        f"Got zones {used}."
    )


def test_same_destination_consolidates_via_transload(controller):
    """Two same-destination loads picked up at different stops should
    end up in fewer zones after the simulator's transload phase.

    Setup: Contract A picks up 16 SCU → Long Forest at Yellow Core.
    Contract B picks up 32 SCU → Long Forest at Wide Forest (later).
    The two loads land in separate zones initially. After the Wide
    Forest stop, consolidation should be able to merge them.
    """
    wid = controller.start_workday(_seraphim(controller), None, False)
    controller.add_contract({
        "pickup_station": "Yellow Core",
        "max_pallet_size": 8,
        "deliveries": [
            {"destination": "Long Forest", "commodity": "Tungsten", "scu": 16},
        ],
    })
    controller.add_contract({
        "pickup_station": "Wide Forest",
        "max_pallet_size": 8,
        "deliveries": [
            {"destination": "Long Forest", "commodity": "Tungsten", "scu": 32},
        ],
    })
    result = run_recompute(wid, controller.conn)
    controller._last_result = result

    # Pre-final stop should have at most ONE zone holding Long Forest
    # cargo (the two loads should have been merged).
    long_forest_stops = [
        s for s in result.route_stops if s.station_name == "Long Forest"
    ]
    assert long_forest_stops, "Long Forest not in the route"
    delivery_stop = long_forest_stops[-1]
    # Snapshot for the stop BEFORE Long Forest's delivery
    pre_delivery = delivery_stop.stop_number - 1
    snap = result.snapshots.get(pre_delivery, [])
    lf_zones = {e.zone_label for e in snap
                if e.delivery_station_name == "Long Forest"}
    # Either a transload move was emitted, or the planner placed both
    # lines in the same zone to begin with. Either way: one zone.
    assert len(lf_zones) <= 1, (
        f"Long Forest cargo still split across {lf_zones} just before "
        f"delivery — transload consolidation didn't fire. "
        f"Moves={result.transload_moves}"
    )


def test_transload_moves_recorded_when_consolidation_helps(controller):
    """When same-dest cargo is split across zones because a topoff zone
    was full at load time, AND a later unload frees room, the simulator
    should EMIT a transload move merging the two pieces.

    Scenario: 40 SCU Long Forest at Stop 1 anchors F1 (cap 72). Then a
    big AD load (96 SCU) occupies R1. Stop 2 picks up 60 more SCU →
    Long Forest — F1 has only 32 free, so the 60 goes into R2 (fresh
    largest). After AD unloads at its stop, freeing R1, the simulator
    should consolidate the two Long Forest pieces (40 + 60 = 100) into
    a single R-bay.
    """
    wid = controller.start_workday(_seraphim(controller), None, False)
    # Long Forest 40 SCU — picked up at origin.
    controller.add_contract({
        "pickup_station": "Seraphim Station",
        "max_pallet_size": 8,
        "deliveries": [
            {"destination": "Long Forest", "commodity": "Tungsten", "scu": 40},
        ],
    })
    # Ambitious Dream 96 SCU — picked up at origin, unloads early.
    controller.add_contract({
        "pickup_station": "Seraphim Station",
        "max_pallet_size": 8,
        "deliveries": [
            {"destination": "Ambitious Dream", "commodity": "Tungsten", "scu": 96},
        ],
    })
    # Long Forest 60 SCU — picked up at a later stop so F1's 32 free
    # can't hold it; it'll land in a fresh R-bay.
    controller.add_contract({
        "pickup_station": "Yellow Core",
        "max_pallet_size": 8,
        "deliveries": [
            {"destination": "Long Forest", "commodity": "Tungsten", "scu": 60},
        ],
    })
    result = run_recompute(wid, controller.conn)
    controller._last_result = result

    total_moves = sum(len(v) for v in result.transload_moves.values())
    assert total_moves > 0, (
        "Expected at least one transload move once the early-unloading "
        "destination frees its zone. "
        f"Snapshots={result.snapshots}, moves={result.transload_moves}"
    )
    # Find any transload move involving Long Forest cargo.
    lf_moves = [m for stop_moves in result.transload_moves.values()
                for m in stop_moves
                if m.delivery_station_name == "Long Forest"]
    assert lf_moves, (
        "Expected a transload move for Long Forest cargo specifically. "
        f"All moves: {result.transload_moves}"
    )


def test_planner_respects_physical_pack_constraints(controller):
    """Regression: in the user's log the planner placed cl#1 (31 SCU
    = 3 large 8-SCU pads + smalls) AND cl#3 (16 SCU = 2 large 8-SCU
    pads) into Starlancer R1 because 31 + 16 = 47 <= 48 SCU. But R1
    is 2x8x3 cubes — only 4 large 2x2x2 pads physically fit, and
    cl#1 alone already needs 3 of those slots. cl#3's two large pads
    won't both fit; one overflows.

    With the physical-aware planner, the second Everus line should
    land somewhere ELSE (likely F1 which is 2x16x2 = clean 8-pad
    capacity), and the BayCanvas should render zero overflow.
    """
    starlancer = controller.conn.execute(
        "SELECT id FROM ships WHERE name LIKE 'Starlancer%'"
    ).fetchone()
    if not starlancer:
        pytest.skip("Starlancer not seeded in this environment")

    wid = controller.start_workday(_seraphim(controller), None, False)
    controller.conn.execute(
        "UPDATE workdays SET ship_id = ? WHERE id = ?",
        (starlancer["id"], wid),
    )
    controller.conn.commit()

    # Three Everus-bound contracts that, if naively SCU-summed, would
    # all fit in R1 (31 + 16 = 47 <= 48) but physically don't.
    for scu in (31, 16):
        controller.add_contract({
            "pickup_station": "Wide Forest",
            "max_pallet_size": 8,
            "deliveries": [
                {"destination": "Everus Harbor", "commodity": "Tungsten", "scu": scu},
            ],
        })
    result = run_recompute(wid, controller.conn)
    controller._last_result = result
    assign_destination_colors(wid, controller.conn)

    # Render every stop with cargo onboard; the packer must report
    # zero overflow in every zone. (The packer logs WARN when it
    # overflows; we check rect-vs-SCU sums.)
    for s in result.route_stops:
        if not result.snapshots.get(s.stop_number):
            continue
        rects = controller.get_pallet_rects(stop_number=s.stop_number)
        # Sum rendered SCU by zone, compare to snapshot SCU.
        rendered_by_zone: dict[str, int] = {}
        for r in rects:
            rendered_by_zone[r.zone_label] = (
                rendered_by_zone.get(r.zone_label, 0) + r.pallet_size
            )
        snapshot_by_zone: dict[str, int] = {}
        for e in result.snapshots[s.stop_number]:
            snapshot_by_zone[e.zone_label] = (
                snapshot_by_zone.get(e.zone_label, 0) + e.scu_amount
            )
        for zone, snap_scu in snapshot_by_zone.items():
            rendered = rendered_by_zone.get(zone, 0)
            assert rendered == snap_scu, (
                f"Stop {s.stop_number} zone {zone}: snapshot says "
                f"{snap_scu} SCU but renderer only fit {rendered} — "
                f"the planner committed a layout the packer can't draw."
            )
