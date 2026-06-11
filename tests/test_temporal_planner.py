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


def _c2(controller) -> int:
    """C2 ship id — for tests whose assertions depend on C2-specific
    zone layout (cap-per-zone, prio order)."""
    return controller.conn.execute(
        "SELECT id FROM ships WHERE name = 'C2 Hercules'"
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
    wid = controller.start_workday(
        _seraphim(controller), None, False, ship_id=_c2(controller)
    )
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
                   if "Yellow Core" in s.station_name)
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
        s for s in result.route_stops if "Long Forest" in s.station_name
    ]
    assert long_forest_stops, "Long Forest not in the route"
    delivery_stop = long_forest_stops[-1]
    # Snapshot for the stop BEFORE Long Forest's delivery
    pre_delivery = delivery_stop.stop_number - 1
    snap = result.snapshots.get(pre_delivery, [])
    lf_zones = {e.zone_label for e in snap
                if "Long Forest" in e.delivery_station_name}
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
    wid = controller.start_workday(
        _seraphim(controller), None, False, ship_id=_c2(controller)
    )
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
                if "Long Forest" in m.delivery_station_name]
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


def test_bulk_drains_into_lower_priority_same_dest_target():
    """Regression for the user's Departure 7 observation: when bulk
    (RBA) has cargo bound for a destination that ALSO has cargo in a
    lower-priority zone with room (F2), the drain pass should
    relocate the bulk piece into that lower-priority zone.

    Hand-built zone state so the test doesn't depend on the loader's
    placement choices.
    """
    from src.planner.zone_assignment import (
        _ZoneState, _PlacedCargo, _drain_to_lower_priority,
    )

    f1 = _ZoneState("F1", "forward", 64, 3, 2, 16, 2)
    f1.placed = [_PlacedCargo(
        cargo_line_id=1, contract_id=1, contract_number=1,
        commodity_id=1, commodity_name="Tungsten",
        delivery_station_id=1, delivery_station_name="Seraphim Station",
        scu=64, pallet_sizes=[8] * 8,
    )]
    f2 = _ZoneState("F2", "forward", 64, 4, 2, 16, 2)
    f2.placed = [_PlacedCargo(
        cargo_line_id=2, contract_id=2, contract_number=2,
        commodity_id=1, commodity_name="Tungsten",
        delivery_station_id=1, delivery_station_name="Seraphim Station",
        scu=16, pallet_sizes=[8, 8],
    )]
    rba = _ZoneState("RBA", "rear", 64, 5, 4, 8, 2)
    rba.placed = [_PlacedCargo(
        cargo_line_id=3, contract_id=3, contract_number=3,
        commodity_id=1, commodity_name="Tungsten",
        delivery_station_id=1, delivery_station_name="Seraphim Station",
        scu=16, pallet_sizes=[8, 8],
    )]

    moves = _drain_to_lower_priority([f1, f2, rba])

    assert len(moves) == 1, f"Expected 1 drain, got {len(moves)}"
    assert moves[0].from_zone == "RBA"
    assert moves[0].to_zone == "F2"
    assert moves[0].scu_amount == 16
    assert rba.used_scu == 0
    assert f2.used_scu == 32


def test_drain_skips_when_only_target_is_empty():
    """Drain is conservative: never moves into an EMPTY lower-priority
    zone, which would preempt that zone for later short-hop
    placements. Topoff (same-dest) is required."""
    from src.planner.zone_assignment import (
        _ZoneState, _PlacedCargo, _drain_to_lower_priority,
    )

    rba = _ZoneState("RBA", "rear", 64, 5, 4, 8, 2)
    rba.placed = [_PlacedCargo(
        cargo_line_id=1, contract_id=1, contract_number=1,
        commodity_id=1, commodity_name="Tungsten",
        delivery_station_id=1, delivery_station_name="Seraphim Station",
        scu=16, pallet_sizes=[8, 8],
    )]
    r1 = _ZoneState("R1", "rear", 48, 1, 2, 8, 3)

    moves = _drain_to_lower_priority([r1, rba])

    assert moves == []
    assert rba.used_scu == 16


def test_manual_stop_insertion(controller):
    """A manual stop should appear in the route at the requested
    position AND retain the scheduled visit to the same station.

    Build a contract delivering 16 SCU to Baijini Point (which sorts
    late in the route). Add a manual stop forcing a visit to Baijini
    after the first stop. After recompute the route must show Baijini
    TWICE — once at the manual insertion point and once at its normal
    scheduled position — with sequential stop_numbers. Onboard
    Baijini cargo should drop at the manual stop because the
    simulator's unload step is purely station-id-based.
    """
    wid = controller.start_workday(_seraphim(controller), None, False)
    # Pick up at Yellow Core (which sorts before Baijini) so that by
    # the time we hit Baijini there's actually cargo to unload.
    controller.add_contract({
        "pickup_station": "Yellow Core",
        "max_pallet_size": 8,
        "deliveries": [
            {"destination": "Baijini Point", "commodity": "Tungsten",
             "scu": 16},
        ],
    })

    # Compute once to learn the scheduled stops, then add the manual
    # stop anchored on a scheduled station that's NOT Baijini.
    result = run_recompute(wid, controller.conn)
    controller._last_result = result

    baijini_id = controller.conn.execute(
        "SELECT id FROM stations WHERE name = 'Baijini Point'"
    ).fetchone()["id"]

    # Pick the first scheduled stop other than Baijini as the anchor.
    anchor_stop = next(
        s for s in result.route_stops
        if s.station_id != baijini_id
    )
    anchor_station_id = anchor_stop.station_id
    anchor_position = anchor_stop.stop_number

    controller.add_manual_stop(baijini_id, anchor_station_id)
    result = run_recompute(wid, controller.conn)
    controller._last_result = result

    # ── Assertion 1: stop_numbers are contiguous 1..N ───────────────
    expected = list(range(1, len(result.route_stops) + 1))
    actual = [s.stop_number for s in result.route_stops]
    assert actual == expected, (
        f"stop_number must be a contiguous 1..N sequence after manual "
        f"insertion; got {actual}"
    )

    # ── Assertion 2: Baijini appears AT LEAST twice ─────────────────
    baijini_stops = [
        s for s in result.route_stops if s.station_id == baijini_id
    ]
    assert len(baijini_stops) >= 2, (
        f"Expected Baijini to appear at least twice (manual + "
        f"scheduled), got {len(baijini_stops)} occurrence(s). "
        f"Route: {[(s.stop_number, s.station_name) for s in result.route_stops]}"
    )

    # ── Assertion 3: the manual Baijini comes RIGHT AFTER the anchor.
    # anchor_position is 1-based; the slot immediately after is
    # anchor_position (the new stop's stop_number) → list index
    # anchor_position (0-based).
    manual_baijini = result.route_stops[anchor_position]
    assert manual_baijini.station_id == baijini_id, (
        f"Expected Baijini at position {anchor_position + 1}, got "
        f"{manual_baijini.station_name} (id={manual_baijini.station_id})"
    )
    assert manual_baijini.action == "Manual Stop", (
        f"Manual stop should be tagged 'Manual Stop'; got "
        f"{manual_baijini.action!r}"
    )

    # ── Assertion 4: at the manual stop, onboard Baijini cargo has
    # already been unloaded (the simulator's unload pass walks every
    # stop's station_id, not just contract-driven ones).
    snap_after_manual = result.snapshots.get(manual_baijini.stop_number, [])
    leftover_baijini = [
        e for e in snap_after_manual
        if "Baijini" in e.delivery_station_name
    ]
    assert not leftover_baijini, (
        f"Baijini cargo should be unloaded at the manual Baijini stop "
        f"(stop {manual_baijini.stop_number}); still saw "
        f"{[(e.zone_label, e.scu_amount) for e in leftover_baijini]}"
    )


def test_partial_pin_moves_only_one_zones_pallets(controller):
    """move_cargo_pallets pins only the portion of a split cargo line
    that sits in one zone, leaving the rest auto-placed.

    A 96 SCU line on the Starlancer is bigger than any single zone, so
    it splits across 2+ zones. Pinning one zone's piece to a different
    zone should: (a) empty the source zone of this line, (b) put some
    of the line in the target zone, and (c) keep the line's total SCU
    on board unchanged.
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

    controller.add_contract({
        "pickup_station": "Wide Forest",
        "max_pallet_size": 8,
        "deliveries": [
            {"destination": "Baijini Point", "commodity": "Tungsten",
             "scu": 96},
        ],
    })
    result = run_recompute(wid, controller.conn)
    controller._last_result = result
    assign_destination_colors(wid, controller.conn)

    cl_id = controller.conn.execute(
        "SELECT cl.id FROM cargo_lines cl "
        "JOIN contracts ct ON ct.id = cl.contract_id "
        "WHERE ct.workday_id = ?",
        (wid,),
    ).fetchone()["id"]

    # Find a stop where this line occupies >= 2 zones.
    source = target = None
    chosen_stop = None
    for stop in result.route_stops:
        zones = sorted({
            e.zone_label
            for e in result.snapshots.get(stop.stop_number, [])
            if e.cargo_line_id == cl_id
        })
        if len(zones) >= 2:
            chosen_stop = stop.stop_number
            source = zones[0]
            # Target: a different zone the line is NOT already in.
            all_zones = [
                r["zone_label"] for r in controller.conn.execute(
                    "SELECT zone_label FROM ship_zones WHERE ship_id = ?",
                    (starlancer["id"],),
                ).fetchall()
            ]
            target = next(z for z in all_zones if z not in zones)
            break
    assert source is not None, (
        "Expected the 96 SCU line to split across >= 2 zones at some "
        f"stop. snapshots={result.snapshots}"
    )

    total_before = sum(
        e.scu_amount
        for e in result.snapshots[chosen_stop]
        if e.cargo_line_id == cl_id
    )

    controller.move_cargo_pallets(cl_id, source, target)

    result2 = run_recompute(wid, controller.conn)
    controller._last_result = result2

    # After recompute the source zone must no longer hold this line at
    # the chosen stop, and the target zone must hold some of it.
    src_after = [
        e for e in result2.snapshots.get(chosen_stop, [])
        if e.cargo_line_id == cl_id and e.zone_label == source
    ]
    tgt_after = [
        e for e in result2.snapshots.get(chosen_stop, [])
        if e.cargo_line_id == cl_id and e.zone_label == target
    ]
    assert not src_after, (
        f"Line should have vacated {source} after the partial pin; "
        f"still found {src_after}"
    )
    assert tgt_after, (
        f"Target zone {target} should hold the pinned piece of the "
        f"line after recompute. snapshots={result2.snapshots[chosen_stop]}"
    )

    total_after = sum(
        e.scu_amount
        for e in result2.snapshots[chosen_stop]
        if e.cargo_line_id == cl_id
    )
    assert total_after == total_before, (
        f"Partial move lost cargo: {total_before} SCU before, "
        f"{total_after} SCU after."
    )


def test_hermes_column_overflow_prefers_partner(controller):
    """On the Hermes, F and R halves of the same column form one
    continuous open bay (no bulkhead). When a cargo line splits across
    two zones, the planner should prefer the column-partner of the
    first zone for the overflow piece — keeping both halves in the
    SAME physical column — rather than spilling into an unrelated
    column's zone.
    """
    hermes = controller.conn.execute(
        "SELECT id FROM ships WHERE name LIKE 'RSI Hermes%'"
    ).fetchone()
    if not hermes:
        pytest.skip("Hermes not seeded in this environment")

    wid = controller.start_workday(_seraphim(controller), None, False)
    controller.conn.execute(
        "UPDATE workdays SET ship_id = ? WHERE id = ?",
        (hermes["id"], wid),
    )
    controller.conn.commit()

    # 50 SCU > one zone (36 SCU) so it must split. Single destination,
    # picked up at the origin.
    controller.add_contract({
        "pickup_station": "Seraphim Station",
        "max_pallet_size": 8,
        "deliveries": [
            {"destination": "Long Forest", "commodity": "Tungsten",
             "scu": 50},
        ],
    })
    result = run_recompute(wid, controller.conn)
    controller._last_result = result
    assign_destination_colors(wid, controller.conn)

    cl_id = controller.conn.execute(
        "SELECT cl.id FROM cargo_lines cl "
        "JOIN contracts ct ON ct.id = cl.contract_id "
        "WHERE ct.workday_id = ?",
        (wid,),
    ).fetchone()["id"]

    # Find a stop where this line is split across >= 2 zones.
    zones_at_stop: list[str] = []
    for stop in result.route_stops:
        zones = sorted({
            e.zone_label
            for e in result.snapshots.get(stop.stop_number, [])
            if e.cargo_line_id == cl_id
        })
        if len(zones) >= 2:
            zones_at_stop = zones
            break
    assert zones_at_stop, (
        "Expected the 50 SCU line to split across >= 2 zones. "
        f"snapshots={result.snapshots}"
    )

    # Look up column-partner adjacency for all zones the line occupies.
    partners: dict[str, set[str]] = {}
    for label in zones_at_stop:
        row = controller.conn.execute(
            "SELECT front_zone_label, back_zone_label FROM ship_zones "
            "WHERE ship_id = ? AND zone_label = ?",
            (hermes["id"], label),
        ).fetchone()
        partners[label] = {p for p in (
            row["front_zone_label"], row["back_zone_label"]
        ) if p}

    # At least one pair of occupied zones must be column partners.
    occupied = set(zones_at_stop)
    paired = any(
        bool(partners[label] & occupied) for label in zones_at_stop
    )
    assert paired, (
        "Expected the split line's two halves to sit in the same "
        "column (e.g. R1+F1), but they spread across unrelated "
        f"columns: {zones_at_stop}. Partner map: {partners}"
    )


def _use_hermes(controller, wid: int) -> int:
    """Pin the workday's ship to the Hermes (288 SCU). Skip if not seeded."""
    hermes = controller.conn.execute(
        "SELECT id, total_scu FROM ships WHERE name LIKE '%Hermes%'"
    ).fetchone()
    if not hermes:
        pytest.skip("Hermes not seeded in this environment")
    controller.conn.execute(
        "UPDATE workdays SET ship_id = ? WHERE id = ?",
        (hermes["id"], wid),
    )
    controller.conn.commit()
    return hermes["total_scu"]


def test_auto_relief_inserts_unload_at_75pct(controller):
    """Loading >75% of capacity before any unload should trigger an
    automatic relief unload stop for the heaviest onboard destination.

    Hermes is 288 SCU - 75% = 216. Four 80-SCU pickups (320 SCU total,
    all bound for Long Forest) cross 216 by the third pickup; the
    planner should splice in an "Auto Unload (75%+ relief)" stop at
    Long Forest before the route continues.
    """
    wid = controller.start_workday(_seraphim(controller), None, False)
    total = _use_hermes(controller, wid)
    for pickup in (
        "Yellow Core",      # ARC-L5 Yellow Core Station
        "Wide Forest",      # ARC-L1 Wide Forest Station
        "Lively Pathway",   # ARC-L2 Lively Pathway Station
        "Port Tressler",
    ):
        controller.add_contract({
            "pickup_station": pickup,
            "max_pallet_size": 8,
            "deliveries": [
                {"destination": "Long Forest", "commodity": "Tungsten",
                 "scu": 80},
            ],
        })

    result = run_recompute(wid, controller.conn)

    relief = [
        s for s in result.route_stops
        if s.action == "Auto Unload (75%+ relief)"
    ]
    assert relief, (
        "Expected at least one auto-relief stop on a 320-SCU/288-cap "
        f"route. Got actions: {[s.action for s in result.route_stops]}"
    )

    # Replay onboard SCU through the route and confirm that the first
    # relief stop is preceded by a >75% peak and that the relief stop
    # itself brings onboard back below 75%.
    threshold_scu = 0.75 * total
    onboard: dict[int, int] = {}  # cargo_line_id -> scu

    relief_stop = relief[0]
    relief_idx = result.route_stops.index(relief_stop)

    # Simulate up to (but not including) the relief stop.
    peak_before = 0
    for s in result.route_stops[:relief_idx]:
        for ref in s.loads:
            onboard[ref.cargo_line_id] = ref.scu_amount
        for ref in s.unloads:
            onboard.pop(ref.cargo_line_id, None)
        peak_before = max(peak_before, sum(onboard.values()))

    assert peak_before > threshold_scu, (
        f"Pre-relief peak {peak_before} SCU should exceed 75% of "
        f"{total} ({threshold_scu})."
    )

    # Apply the relief stop's unloads and confirm we're back under 75%.
    for ref in relief_stop.unloads:
        onboard.pop(ref.cargo_line_id, None)
    onboard_after = sum(onboard.values())
    assert onboard_after < threshold_scu, (
        f"After relief stop, onboard={onboard_after} SCU should be "
        f"below 75% of {total} ({threshold_scu})."
    )


def test_auto_relief_idempotent(controller):
    """Recomputing the same workday twice must not stack relief stops.

    The helper strips prior auto-relief stops before re-simulating, so
    the second pass should produce a route the same length as the
    first.
    """
    wid = controller.start_workday(_seraphim(controller), None, False)
    _use_hermes(controller, wid)
    for pickup in (
        "Yellow Core", "Wide Forest", "Lively Pathway", "Port Tressler",
    ):
        controller.add_contract({
            "pickup_station": pickup,
            "max_pallet_size": 8,
            "deliveries": [
                {"destination": "Long Forest", "commodity": "Tungsten",
                 "scu": 80},
            ],
        })

    first = run_recompute(wid, controller.conn)
    first_len = len(first.route_stops)
    first_relief = sum(
        1 for s in first.route_stops
        if s.action == "Auto Unload (75%+ relief)"
    )

    second = run_recompute(wid, controller.conn)
    second_len = len(second.route_stops)
    second_relief = sum(
        1 for s in second.route_stops
        if s.action == "Auto Unload (75%+ relief)"
    )

    assert first_len == second_len, (
        f"Route grew on recompute: {first_len} -> {second_len}. "
        f"Relief stops should be idempotent."
    )
    assert first_relief == second_relief and first_relief > 0, (
        f"Relief-stop count changed on recompute: {first_relief} -> "
        f"{second_relief}."
    )


# ── Per-pallet locks ────────────────────────────────────────────────────

def _cargo_line_id(controller, wid: int) -> int:
    return controller.conn.execute(
        "SELECT cl.id FROM cargo_lines cl "
        "JOIN contracts ct ON ct.id = cl.contract_id "
        "WHERE ct.workday_id = ? LIMIT 1",
        (wid,),
    ).fetchone()["id"]


def test_locked_pallet_stays_at_pinned_position(controller):
    """Locking pallet 0 of a cargo line to a specific (zone, x, y, z)
    should make the corresponding PalletRect render at exactly that
    position after recompute.
    """
    from src.planner.recompute import recompute as run_recompute

    wid = controller.start_workday(_seraphim(controller), None, False)
    controller.add_contract({
        "pickup_station": "Seraphim Station",
        "max_pallet_size": 8,
        "deliveries": [
            {"destination": "Long Forest", "commodity": "Tungsten",
             "scu": 32},
        ],
    })
    result = run_recompute(wid, controller.conn)
    controller._last_result = result
    assign_destination_colors(wid, controller.conn)

    cl_id = _cargo_line_id(controller, wid)

    # Pick the first stop with this line onboard.
    chosen_stop = None
    for s in result.route_stops:
        if any(e.cargo_line_id == cl_id
               for e in result.snapshots.get(s.stop_number, [])):
            chosen_stop = s.stop_number
            break
    assert chosen_stop is not None

    # Verify the line gets at least one rendered pallet pre-lock so the
    # rest of the test has something to compare against. Capture the
    # zone it lands in.
    pre_rects = controller.get_pallet_rects(stop_number=chosen_stop)
    pre_pallets = [r for r in pre_rects if r.cargo_line_id == cl_id]
    assert pre_pallets, "Cargo line should have rendered pallets pre-lock"
    target_zone = pre_pallets[0].zone_label

    # Lock pallet 0 to the (0,0,0) corner of the chosen zone (which is
    # always a valid origin for an 8-SCU 2x2x2 pad on the C2 zones).
    controller.lock_pallet(cl_id, 0, target_zone, 0, 0, 0)
    result2 = run_recompute(wid, controller.conn)
    controller._last_result = result2

    post_rects = controller.get_pallet_rects(stop_number=chosen_stop)
    locked_rects = [
        r for r in post_rects
        if r.cargo_line_id == cl_id and r.pallet_index == 0
    ]
    assert locked_rects, (
        "Expected exactly one rendered pallet for pallet_index=0 "
        f"after the lock. Got rects: {post_rects}"
    )
    locked = locked_rects[0]
    # The renderer adds zone.cube_offset_x/cube_offset_y to the cube
    # coordinates, so we have to add the offset to the expected
    # position for the comparison.
    zone_row = controller.conn.execute(
        "SELECT z.cube_offset_x, z.cube_offset_y FROM ship_zones z "
        "JOIN workdays w ON w.ship_id = z.ship_id "
        "WHERE w.id = ? AND z.zone_label = ?",
        (wid, target_zone),
    ).fetchone()
    expected_x = zone_row["cube_offset_x"] + 0
    expected_y = zone_row["cube_offset_y"] + 0
    assert locked.zone_label == target_zone
    assert (locked.cell_x, locked.cell_y, locked.cell_z) == (
        expected_x, expected_y, 0
    ), (
        f"Locked pallet rendered at ({locked.cell_x},{locked.cell_y},"
        f"{locked.cell_z}); expected ({expected_x},{expected_y},0)."
    )


def test_unlocked_pallets_pack_around_locked(controller):
    """When a pallet is locked at the (0,0,0) corner of a zone, the
    other auto-placed pallets in that zone must not overlap the
    locked cube.
    """
    from src.planner.recompute import recompute as run_recompute

    wid = controller.start_workday(_seraphim(controller), None, False)
    # 64 SCU = eight 8-SCU pads — enough to populate a 72-SCU zone.
    controller.add_contract({
        "pickup_station": "Seraphim Station",
        "max_pallet_size": 8,
        "deliveries": [
            {"destination": "Long Forest", "commodity": "Tungsten",
             "scu": 64},
        ],
    })
    result = run_recompute(wid, controller.conn)
    controller._last_result = result
    assign_destination_colors(wid, controller.conn)

    cl_id = _cargo_line_id(controller, wid)
    chosen_stop = None
    for s in result.route_stops:
        if any(e.cargo_line_id == cl_id
               for e in result.snapshots.get(s.stop_number, [])):
            chosen_stop = s.stop_number
            break
    assert chosen_stop is not None

    pre_rects = controller.get_pallet_rects(stop_number=chosen_stop)
    pre_pallets = [r for r in pre_rects if r.cargo_line_id == cl_id]
    assert pre_pallets, "Cargo line should have rendered pallets pre-lock"
    target_zone = pre_pallets[0].zone_label

    # Lock pallet 0 at the corner.
    controller.lock_pallet(cl_id, 0, target_zone, 0, 0, 0)
    result2 = run_recompute(wid, controller.conn)
    controller._last_result = result2

    post_rects = controller.get_pallet_rects(stop_number=chosen_stop)
    zone_row = controller.conn.execute(
        "SELECT z.cube_offset_x, z.cube_offset_y FROM ship_zones z "
        "JOIN workdays w ON w.ship_id = z.ship_id "
        "WHERE w.id = ? AND z.zone_label = ?",
        (wid, target_zone),
    ).fetchone()
    locked_cube = (
        target_zone,
        zone_row["cube_offset_x"],
        zone_row["cube_offset_y"],
        0,
    )

    locked = [
        r for r in post_rects
        if r.cargo_line_id == cl_id and r.pallet_index == 0
    ]
    assert locked, "Locked pallet should render"
    lr = locked[0]
    assert (lr.zone_label, lr.cell_x, lr.cell_y, lr.cell_z) == locked_cube

    # No OTHER rect in the same zone should cover the locked cube.
    for r in post_rects:
        if r.cargo_line_id == cl_id and r.pallet_index == 0:
            continue
        if r.zone_label != target_zone:
            continue
        for dx in range(r.cell_w):
            for dy in range(r.cell_l):
                for dz in range(r.cell_h):
                    assert (r.zone_label,
                            r.cell_x + dx, r.cell_y + dy,
                            r.cell_z + dz) != locked_cube, (
                        f"Auto-placed pallet (cl#{r.cargo_line_id}, "
                        f"idx={r.pallet_index}, size={r.pallet_size}) "
                        f"overlaps locked cube {locked_cube}"
                    )


def test_locked_rotated_pallet_persists(controller):
    """Locking a rotatable pallet with orientation=1 should swap (w, l)
    on the rendered PalletRect after recompute.

    A 2-SCU pallet has a natural (w=2, l=1) footprint and is rotatable.
    With orientation=1 the renderer must stamp it as (w=1, l=2) so the
    pinned cube layout matches what the user set. Using a 2-SCU pallet
    keeps both orientations physically valid inside the C2's 2-wide
    zones.
    """
    from src.planner.recompute import recompute as run_recompute

    wid = controller.start_workday(_seraphim(controller), None, False)
    controller.add_contract({
        "pickup_station": "Seraphim Station",
        "max_pallet_size": 2,
        "deliveries": [
            {"destination": "Long Forest", "commodity": "Tungsten",
             "scu": 2},
        ],
    })
    result = run_recompute(wid, controller.conn)
    controller._last_result = result
    assign_destination_colors(wid, controller.conn)

    cl_id = _cargo_line_id(controller, wid)

    chosen_stop = None
    for s in result.route_stops:
        if any(e.cargo_line_id == cl_id
               for e in result.snapshots.get(s.stop_number, [])):
            chosen_stop = s.stop_number
            break
    assert chosen_stop is not None

    pre_rects = controller.get_pallet_rects(stop_number=chosen_stop)
    pre_pallets = [r for r in pre_rects if r.cargo_line_id == cl_id]
    assert pre_pallets, "Cargo line should have rendered pallets pre-lock"
    target_zone = pre_pallets[0].zone_label

    # Lock the pallet rotated 90° so it occupies (w=1, l=2) instead of
    # the natural (w=2, l=1). Origin (0, 0) is a valid placement for
    # both orientations within the zone.
    controller.lock_pallet(cl_id, 0, target_zone, 0, 0, 0, orientation=1)

    result2 = run_recompute(wid, controller.conn)
    controller._last_result = result2

    post_rects = controller.get_pallet_rects(stop_number=chosen_stop)
    locked = [
        r for r in post_rects
        if r.cargo_line_id == cl_id and r.pallet_index == 0
    ]
    assert locked, (
        "Expected exactly one rendered pallet for pallet_index=0 "
        f"after the rotated lock. Got rects: {post_rects}"
    )
    rotated = locked[0]
    assert (rotated.cell_w, rotated.cell_l) == (1, 2), (
        f"Rotated lock should swap (w, l) to (1, 2); got "
        f"({rotated.cell_w}, {rotated.cell_l})"
    )


def test_lock_validation_rejects_out_of_bounds(controller):
    """lock_pallet with cube_x outside the zone's width should raise."""
    from src.app_controller import ToolError
    from src.planner.recompute import recompute as run_recompute

    wid = controller.start_workday(_seraphim(controller), None, False)
    controller.add_contract({
        "pickup_station": "Seraphim Station",
        "max_pallet_size": 8,
        "deliveries": [
            {"destination": "Long Forest", "commodity": "Tungsten",
             "scu": 8},
        ],
    })
    run_recompute(wid, controller.conn)

    cl_id = _cargo_line_id(controller, wid)

    # Pick any C2 zone and look up its width; cube_x = width is OOB.
    zone_row = controller.conn.execute(
        "SELECT z.zone_label, z.width_units FROM ship_zones z "
        "JOIN workdays w ON w.ship_id = z.ship_id "
        "WHERE w.id = ? LIMIT 1",
        (wid,),
    ).fetchone()
    bad_x = zone_row["width_units"]  # one past the last valid index

    with pytest.raises(ToolError):
        controller.lock_pallet(
            cl_id, 0, zone_row["zone_label"], bad_x, 0, 0,
        )


def test_cross_zone_lock_renders_immediately_without_recompute(controller):
    """Regression: dragging a pallet to a DIFFERENT zone in the 3D
    view stores the lock under the new zone, but the renderer used to
    look locks up only within the snapshot's (stale) zone — so the
    pallet silently rendered back in its old spot until the next
    recompute. The lock must relocate the pallet's rendering at once.
    """
    wid = controller.start_workday(_seraphim(controller), None, False)
    controller.add_contract({
        "pickup_station": "Yellow Core",
        "max_pallet_size": 8,
        "deliveries": [
            {"destination": "Everus Harbor", "commodity": "Tungsten",
             "scu": 16},
        ],
    })
    result = run_recompute(wid, controller.conn)
    controller._last_result = result
    assign_destination_colors(wid, controller.conn)

    stop = max(result.snapshots.keys(),
               key=lambda sn: sum(e.scu_amount for e in result.snapshots[sn]))
    rects = controller.get_pallet_rects(stop_number=stop)
    assert rects, "Expected pallets on board"
    src = rects[0]
    src_zone = src.zone_label

    # Pick a different zone on the ship as the target.
    other = controller.conn.execute(
        """
        SELECT z.zone_label FROM ship_zones z
        JOIN workdays w ON w.ship_id = z.ship_id
        WHERE w.id = ? AND z.zone_label != ?
        LIMIT 1
        """,
        (wid, src_zone),
    ).fetchone()["zone_label"]

    controller.lock_pallet(src.cargo_line_id, src.pallet_index,
                           other, 0, 0, 0)

    # NO recompute — rendering must already honor the lock.
    rects2 = controller.get_pallet_rects(stop_number=stop)
    moved = [r for r in rects2
             if r.cargo_line_id == src.cargo_line_id
             and r.pallet_index == src.pallet_index]
    assert len(moved) == 1
    assert moved[0].zone_label == other, (
        f"Locked pallet should render in {other}, got "
        f"{moved[0].zone_label} — stale-snapshot zone won."
    )

    # Re-lock to a third position (back in the source zone): the
    # pallet must follow — moving more than once is fine.
    controller.lock_pallet(src.cargo_line_id, src.pallet_index,
                           src_zone, 0, 0, 0)
    rects3 = controller.get_pallet_rects(stop_number=stop)
    moved3 = [r for r in rects3
              if r.cargo_line_id == src.cargo_line_id
              and r.pallet_index == src.pallet_index]
    assert len(moved3) == 1
    assert moved3[0].zone_label == src_zone
