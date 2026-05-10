"""Tests for the zone-strip aggregation and the one-destination-per-zone
zone assignment refactor."""

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


# ── Zone segregation ─────────────────────────────────────────────────────

def test_two_destinations_get_two_zones(controller):
    wid = controller.start_workday(_seraphim(controller), None, False)
    controller.add_contract({
        "pickup_station": "Yellow Core",
        "max_pallet_size": 8,
        "deliveries": [
            {"destination": "Everus Harbor", "commodity": "Tungsten", "scu": 55},
        ],
    })
    controller.add_contract({
        "pickup_station": "Yellow Core",
        "max_pallet_size": 8,
        "deliveries": [
            {"destination": "Baijini Point", "commodity": "Tungsten", "scu": 27},
        ],
    })
    result = run_recompute(wid, controller.conn)
    controller._last_result = result
    assign_destination_colors(wid, controller.conn)

    strips = controller.get_zone_strips()
    occupied = [s for s in strips if not s.is_empty]

    # Each destination gets its own zone — no mixing
    assert all(not s.is_mixed for s in occupied), \
        f"Mixed zones found: {[s.zone_label for s in occupied if s.is_mixed]}"
    # Two zones occupied (one per destination)
    assert len(occupied) == 2


def test_conflict_zones_use_separate_zones(controller):
    wid = controller.start_workday(_seraphim(controller), None, False)
    # Yellow Core × Tungsten conflict
    controller.add_contract({
        "pickup_station": "Yellow Core",
        "max_pallet_size": 8,
        "deliveries": [
            {"destination": "Everus Harbor", "commodity": "Tungsten", "scu": 55},
        ],
    })
    controller.add_contract({
        "pickup_station": "Yellow Core",
        "max_pallet_size": 8,
        "deliveries": [
            {"destination": "Baijini Point", "commodity": "Tungsten", "scu": 27},
        ],
    })
    result = run_recompute(wid, controller.conn)
    controller._last_result = result
    assign_destination_colors(wid, controller.conn)

    strips = controller.get_zone_strips()
    everus = next(s for s in strips
                  if any(d.station_name == "Everus Harbor" for d in s.destinations))
    baijini = next(s for s in strips
                   if any(d.station_name == "Baijini Point" for d in s.destinations))
    assert everus.zone_label != baijini.zone_label
    assert everus.is_conflicted
    assert baijini.is_conflicted


def test_zone_strip_aggregates_scu(controller):
    wid = controller.start_workday(_seraphim(controller), None, False)
    controller.add_contract({
        "pickup_station": "Shallow Fields",
        "max_pallet_size": 16,
        "deliveries": [
            {"destination": "Port Tressler", "commodity": "Aluminum", "scu": 96},
        ],
    })
    result = run_recompute(wid, controller.conn)
    controller._last_result = result
    assign_destination_colors(wid, controller.conn)

    strips = controller.get_zone_strips()
    occupied = [s for s in strips if not s.is_empty]
    assert len(occupied) == 1
    s = occupied[0]
    assert s.used_scu == 96
    assert len(s.destinations) == 1
    assert s.destinations[0].station_name == "Port Tressler"
    assert not s.is_conflicted


# ── Conflict pallets at ramp side ───────────────────────────────────────

def test_conflict_small_pallets_placed_at_ramp(controller):
    """Ambiguous (small) pallets should land at low Y (ramp side)."""
    wid = controller.start_workday(_seraphim(controller), None, False)
    controller.add_contract({
        "pickup_station": "Yellow Core",
        "max_pallet_size": 8,
        "deliveries": [
            {"destination": "Everus Harbor", "commodity": "Tungsten", "scu": 55},
        ],
    })
    controller.add_contract({
        "pickup_station": "Yellow Core",
        "max_pallet_size": 8,
        "deliveries": [
            {"destination": "Baijini Point", "commodity": "Tungsten", "scu": 27},
        ],
    })
    result = run_recompute(wid, controller.conn)
    controller._last_result = result
    assign_destination_colors(wid, controller.conn)

    rects = controller.get_pallet_rects(stop_number=2)

    # Find Everus's small (1, 2, 4) pallets and large (8) pallets
    everus_rects = [r for r in rects
                    if r.delivery_station_name == "Everus Harbor"]
    smalls = [r for r in everus_rects if r.pallet_size <= 4]
    larges = [r for r in everus_rects if r.pallet_size == 8]
    assert smalls and larges

    # Smallest pallet (1) should be at the lowest Y in the zone
    smallest_y = min(r.cell_y for r in smalls)
    largest_avg_y = sum(r.cell_y for r in larges) / len(larges)
    assert smallest_y < largest_avg_y, \
        "Small (ambiguous) pallets should land at lower Y than the 8s"


# ── Wide pallet auto-rotation ───────────────────────────────────────────

def test_wide_pallets_auto_rotate(controller):
    """16 SCU pallets (4×2 footprint) must rotate to fit a 2-wide zone."""
    wid = controller.start_workday(_seraphim(controller), None, False)
    controller.add_contract({
        "pickup_station": "Shallow Fields",
        "max_pallet_size": 16,
        "deliveries": [
            {"destination": "Port Tressler", "commodity": "Aluminum", "scu": 96},
        ],
    })
    result = run_recompute(wid, controller.conn)
    controller._last_result = result
    assign_destination_colors(wid, controller.conn)

    rects = controller.get_pallet_rects(stop_number=2)
    # All 16-SCU pallets should have width <= 2 (rotated)
    sixteens = [r for r in rects if r.pallet_size == 16]
    assert sixteens
    for r in sixteens:
        assert r.cell_w <= 2, f"16 SCU pallet not rotated: w={r.cell_w}"
        assert r.cell_l == 4, f"16 SCU pallet wrong length: l={r.cell_l}"


# ── Empty zones ─────────────────────────────────────────────────────────

def test_empty_zones_appear_in_strips(controller):
    wid = controller.start_workday(_seraphim(controller), None, False)
    controller.add_contract({
        "pickup_station": "Yellow Core",
        "max_pallet_size": 8,
        "deliveries": [
            {"destination": "Everus Harbor", "commodity": "Tungsten", "scu": 16},
        ],
    })
    result = run_recompute(wid, controller.conn)
    controller._last_result = result
    assign_destination_colors(wid, controller.conn)

    strips = controller.get_zone_strips()
    # Always 7 zones for the C2 (F1, F2, F3, R1, R2, R3, R4)
    assert len(strips) == 7
    empty_count = sum(1 for s in strips if s.is_empty)
    assert empty_count == 6


# ── Multi-contract destination consolidation ────────────────────────────

def test_multiple_contracts_same_destination_consolidate(controller):
    """A destination receiving cargo from many contracts should pack
    into the smallest number of zones, not spread one-zone-per-contract.

    Three contracts → Seraphim with 64 + 56 + 32 = 152 SCU. Total fits in
    two R-bay zones (120 + 32). No mixing required.
    """
    wid = controller.start_workday(_seraphim(controller), None, False)
    for scu in (64, 56, 32):
        controller.add_contract({
            "pickup_station": "Yellow Core",
            "max_pallet_size": 8,
            "deliveries": [
                {"destination": "Long Forest", "commodity": "Tungsten", "scu": scu},
            ],
        })
    result = run_recompute(wid, controller.conn)
    controller._last_result = result
    assign_destination_colors(wid, controller.conn)

    strips = controller.get_zone_strips()
    occupied = [s for s in strips if not s.is_empty]
    # All cargo is for Long Forest — no other dest, so no zone should mix.
    assert all(not s.is_mixed for s in occupied), \
        f"Unexpected mixed zones: {[s.zone_label for s in occupied if s.is_mixed]}"
    # 152 SCU fits in 2 R-bay zones (120 + 32) — must not be ≥3.
    assert len(occupied) <= 2, \
        f"Cargo for one dest spread across {len(occupied)} zones: " \
        f"{[s.zone_label for s in occupied]}"


def test_conflict_exclusion_covers_all_partner_zones(controller):
    """When a conflict partner overflows across multiple zones, the
    sibling destination must avoid ALL of them, not just the first.

    Setup: Yellow Core × Tungsten is the conflict source. Baijini takes
    a 240 SCU load that overflows R3+R4 (both fresh, both 120). Then
    Seraphim — Baijini's conflict partner — places. None of Seraphim's
    cargo should land in R3 OR R4.
    """
    wid = controller.start_workday(_seraphim(controller), None, True)

    # Two ambiguous Tungsten contracts (same source, same commodity, two
    # destinations) → conflict group.
    controller.add_contract({
        "pickup_station": "Yellow Core",
        "max_pallet_size": 8,
        "deliveries": [
            {"destination": "Baijini Point", "commodity": "Tungsten", "scu": 240},
        ],
    })
    controller.add_contract({
        "pickup_station": "Yellow Core",
        "max_pallet_size": 8,
        "deliveries": [
            # Round-robin → Seraphim is also a destination. 100 SCU of
            # Tungsten from the same source makes the two ambiguous.
            {"destination": "Long Forest", "commodity": "Tungsten", "scu": 100},
        ],
    })
    # Add a Seraphim-bound contract (Yellow Core × Tungsten so it
    # joins the conflict group).
    controller.add_contract({
        "pickup_station": "Yellow Core",
        "max_pallet_size": 8,
        "deliveries": [
            {"destination": "Long Forest", "commodity": "Tungsten", "scu": 32},
        ],
    })

    result = run_recompute(wid, controller.conn)
    controller._last_result = result
    assign_destination_colors(wid, controller.conn)

    # Find which zones Baijini occupies; Long Forest (the conflict
    # partner here) must not share any of them.
    rows = controller.conn.execute(
        """
        SELECT za.primary_zone_label, s.name AS dest
        FROM zone_assignments za
        JOIN cargo_lines cl ON cl.id = za.cargo_line_id
        JOIN stations s ON s.id = cl.delivery_station_id
        WHERE za.workday_id = ?
        """,
        (wid,),
    ).fetchall()
    baijini_zones = {r["primary_zone_label"] for r in rows if r["dest"] == "Baijini Point"}
    long_forest_zones = {r["primary_zone_label"] for r in rows if r["dest"] == "Long Forest"}

    overlap = baijini_zones & long_forest_zones
    assert not overlap, (
        f"Conflict partners share zone(s) {overlap}: "
        f"Baijini={baijini_zones}, Long Forest={long_forest_zones}"
    )


def test_overflow_prefers_fresh_over_mixing(controller):
    """When a destination overflows its first zone, the spillover should
    land in a FRESH zone, not mix into a zone occupied by another dest.
    """
    wid = controller.start_workday(_seraphim(controller), None, False)
    # First contract: Everus Harbor takes a full R-bay zone.
    controller.add_contract({
        "pickup_station": "Yellow Core",
        "max_pallet_size": 8,
        "deliveries": [
            {"destination": "Everus Harbor", "commodity": "Tungsten", "scu": 96},
        ],
    })
    # Second contract: Baijini Point — bigger than any single zone, so it
    # must overflow. The overflow should pick a fresh zone, NOT share the
    # 24-SCU leftover of Everus's zone.
    controller.add_contract({
        "pickup_station": "Yellow Core",
        "max_pallet_size": 8,
        "deliveries": [
            {"destination": "Baijini Point", "commodity": "Tungsten", "scu": 160},
        ],
    })
    result = run_recompute(wid, controller.conn)
    controller._last_result = result
    assign_destination_colors(wid, controller.conn)

    strips = controller.get_zone_strips()
    # Capacity is plenty (696 total, 256 in use). No zone should be mixed.
    mixed = [s.zone_label for s in strips if s.is_mixed]
    assert not mixed, f"Overflow mixed when fresh zones were available: {mixed}"
