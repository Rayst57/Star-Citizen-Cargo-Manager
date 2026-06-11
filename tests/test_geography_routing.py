"""Tests for distance-aware route ordering (planner.geography).

The route builder orders stops by greedy nearest-neighbor on the
Stanton map instead of the static sort_order column. Key behaviors:

1. Body positions resolve for planets, moons, Lagrange points; unknown
   bodies return None.
2. A nearby delivery (CRU-L1 next to Seraphim) is visited before a
   far one (Port Tressler at microTech) — the user's motivating case.
3. A delivery never precedes its own pickup, even when the delivery
   station is closer to the origin than the pickup is.
4. Every stop after the first carries a distance_from_prev_km leg
   (when both endpoints have known positions).
"""

from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import math

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from src.app_controller import AppController
from src.db.init_db import initialize_database
from src.planner.geography import body_position, distance_km
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


# ── 1. Body position resolution ──────────────────────────────────────


def test_body_positions_resolve():
    crusader = body_position("Crusader")
    assert crusader is not None

    # Moons co-locate with their planet.
    assert body_position("Daymar") == crusader

    # Lagrange point L1 sits between the star and the planet,
    # closer to the planet than to the star.
    cru_l1 = body_position("CRU-L1")
    assert cru_l1 is not None
    d_l1_planet = distance_km(cru_l1, crusader)
    d_l1_star = math.hypot(*cru_l1)
    assert d_l1_planet < d_l1_star

    # L3 is on the opposite side of the star — farther from the
    # planet than the planet is from the star.
    arc = body_position("ArcCorp")
    arc_l3 = body_position("ARC-L3")
    assert distance_km(arc_l3, arc) > math.hypot(*arc)

    # Unknown bodies are unmapped, not crashes.
    assert body_position("Other") is None
    assert body_position(None) is None


def test_seraphim_to_cru_l1_much_closer_than_port_tressler():
    seraphim = body_position("Crusader")       # Seraphim orbits Crusader
    cru_l1 = body_position("CRU-L1")
    tressler = body_position("microTech")      # Port Tressler orbits microTech
    near = distance_km(seraphim, cru_l1)
    far = distance_km(seraphim, tressler)
    assert near * 5 < far, (
        f"CRU-L1 ({near:.0f} km) should be several times closer to "
        f"Seraphim than microTech ({far:.0f} km)."
    )


# ── 2. Near deliveries first ─────────────────────────────────────────


def test_route_visits_near_delivery_before_far_one(controller):
    """Origin Seraphim, cargo aboard for Ambitious Dream (CRU-L1, a
    short hop) and Port Tressler (microTech, across the system). The
    route must hit Ambitious Dream first."""
    wid = controller.start_workday(_seraphim(controller), None, False)
    controller.add_contract({
        "pickup_station": "Seraphim Station",
        "max_pallet_size": 8,
        "deliveries": [
            {"destination": "Port Tressler", "commodity": "Tungsten",
             "scu": 32},
            {"destination": "Ambitious Dream", "commodity": "Tungsten",
             "scu": 32},
        ],
    })
    result = run_recompute(wid, controller.conn)
    names = [s.station_name for s in result.route_stops]
    i_near = next(i for i, n in enumerate(names) if "Ambitious Dream" in n)
    i_far = next(i for i, n in enumerate(names) if "Port Tressler" in n)
    assert i_near < i_far, f"Route order wrong: {names}"


# ── 3. Deliveries wait for their pickups ─────────────────────────────


def test_delivery_never_precedes_its_pickup(controller):
    """Cargo for Ambitious Dream sits at Yellow Core (ArcCorp). Even
    though Ambitious Dream is right next to the Seraphim origin, the
    route must go to Yellow Core FIRST — flying to the delivery empty
    is the exact waste this feature kills."""
    wid = controller.start_workday(_seraphim(controller), None, False)
    controller.add_contract({
        "pickup_station": "Yellow Core",
        "max_pallet_size": 8,
        "deliveries": [
            {"destination": "Ambitious Dream", "commodity": "Tungsten",
             "scu": 32},
        ],
    })
    result = run_recompute(wid, controller.conn)
    names = [s.station_name for s in result.route_stops]
    i_pickup = next(i for i, n in enumerate(names) if "Yellow Core" in n)
    i_dropoff = next(i for i, n in enumerate(names) if "Ambitious Dream" in n)
    assert i_pickup < i_dropoff, f"Route order wrong: {names}"
    # And there must be only ONE Ambitious Dream visit — no empty
    # early stop plus double-dip return.
    assert sum("Ambitious Dream" in n for n in names) == 1, names


# ── 4. Per-leg distances ─────────────────────────────────────────────


def test_route_stops_carry_leg_distances(controller):
    wid = controller.start_workday(_seraphim(controller), None, False)
    controller.add_contract({
        "pickup_station": "Seraphim Station",
        "max_pallet_size": 8,
        "deliveries": [
            {"destination": "Everus Harbor", "commodity": "Tungsten",
             "scu": 16},
        ],
    })
    result = run_recompute(wid, controller.conn)
    stops = result.route_stops
    assert stops[0].distance_from_prev_km == 0.0
    for st in stops[1:]:
        assert st.distance_from_prev_km is not None, (
            f"Stop {st.station_name} missing leg distance."
        )
        assert st.distance_from_prev_km > 0
