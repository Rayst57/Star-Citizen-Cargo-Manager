"""Tests for multi-candidate pickup contracts.

A multi-pickup contract is one whose cargo MAY be at any of several
candidate stations — the pilot finds out on arrival. The planner
conservatively assumes the cargo is on board from the first candidate
visit so SCU stays reserved; additional candidates show up in the
route as informational "Candidate Pickup" stops the pilot should
physically visit during gameplay.
"""

from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from src.app_controller import AppController
from src.db.init_db import initialize_database
from src.planner.recompute import recompute as run_recompute
from src.planner.route import CANDIDATE_PICKUP_ACTION


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


def _station_id(controller, name: str) -> int:
    return controller.conn.execute(
        "SELECT id FROM stations WHERE name = ?", (name,)
    ).fetchone()["id"]


def test_multi_pickup_inserts_candidate_rows(controller):
    """add_contract with pickup_candidates writes one row per unique
    station to contract_pickup_candidates, primary first."""
    controller.start_workday(_seraphim(controller), None, False)

    cid = controller.add_contract({
        "pickup_station": "Yellow Core",
        "pickup_candidates": ["Long Forest", "Faint Glen"],
        "max_pallet_size": 8,
        "deliveries": [
            {"destination": "Everus Harbor", "commodity": "Tungsten", "scu": 32},
        ],
    })

    rows = controller.list_pickup_candidates(cid)
    assert len(rows) == 3, (
        f"expected 3 candidate rows (primary + 2 alts), got {len(rows)}"
    )

    expected_order = [
        (0, "Yellow Core"),
        (1, "Long Forest"),
        (2, "Faint Glen"),
    ]
    actual = [(r["sequence_order"], r["station_name"]) for r in rows]
    # Station names may have been renamed (e.g. "Yellow Core" →
    # "ARC-L5 Yellow Core Station"); match by suffix instead.
    for (exp_seq, exp_name), (act_seq, act_name) in zip(expected_order, actual):
        assert act_seq == exp_seq, f"sequence_order mismatch: {actual}"
        assert exp_name in act_name, (
            f"expected station containing {exp_name!r} at seq {exp_seq}, "
            f"got {act_name!r}"
        )

    # And the contract's existing pickup_station_id still points to
    # the primary candidate.
    c = controller.conn.execute(
        "SELECT pickup_station_id FROM contracts WHERE id = ?", (cid,)
    ).fetchone()
    assert c["pickup_station_id"] == rows[0]["station_id"]


def test_multi_pickup_route_visits_all_candidates(controller):
    """Each candidate station appears as a stop in the route in
    sequence_order. The primary has the cargo's loads; subsequent
    candidates have empty loads and the Candidate Pickup action."""
    wid = controller.start_workday(_seraphim(controller), None, False)

    controller.add_contract({
        "pickup_station": "Yellow Core",
        "pickup_candidates": ["Long Forest", "Faint Glen"],
        "max_pallet_size": 8,
        "deliveries": [
            {"destination": "Everus Harbor", "commodity": "Tungsten", "scu": 32},
        ],
    })
    result = run_recompute(wid, controller.conn)

    # The route should mention each candidate station at least once.
    visited = [(s.stop_number, s.station_id, s.action, list(s.loads))
               for s in result.route_stops]

    yc = _station_id(controller, "ARC-L5 Yellow Core Station")
    lf = _station_id(controller, "MIC-L2 Long Forest Station")
    fg = _station_id(controller, "ARC-L4 Faint Glen Station")

    # Find positions.
    yc_idx = next(
        (i for i, (_, sid, act, loads) in enumerate(visited)
         if sid == yc and loads),
        None,
    )
    assert yc_idx is not None, "primary pickup stop missing from route"

    # The two candidate stops should be the very next stops, in order.
    lf_idx = next(
        (i for i, (_, sid, act, _l) in enumerate(visited)
         if sid == lf and act == CANDIDATE_PICKUP_ACTION),
        None,
    )
    fg_idx = next(
        (i for i, (_, sid, act, _l) in enumerate(visited)
         if sid == fg and act == CANDIDATE_PICKUP_ACTION),
        None,
    )
    assert lf_idx is not None, "Long Forest candidate stop missing"
    assert fg_idx is not None, "Faint Glen candidate stop missing"

    # Ordering: primary first, then sequence_order 1, then 2.
    assert lf_idx == yc_idx + 1, (
        f"Long Forest (seq 1) should follow primary; got order "
        f"yc={yc_idx} lf={lf_idx}"
    )
    assert fg_idx == yc_idx + 2, (
        f"Faint Glen (seq 2) should follow Long Forest; got order "
        f"yc={yc_idx} lf={lf_idx} fg={fg_idx}"
    )

    # Loads: primary stop has the cargo, candidates have empty loads.
    primary_stop = result.route_stops[yc_idx]
    lf_stop = result.route_stops[lf_idx]
    fg_stop = result.route_stops[fg_idx]
    assert primary_stop.loads, "primary stop should carry the cargo loads"
    assert lf_stop.loads == [], "candidate stop should have empty loads"
    assert fg_stop.loads == [], "candidate stop should have empty loads"
    assert lf_stop.action == CANDIDATE_PICKUP_ACTION
    assert fg_stop.action == CANDIDATE_PICKUP_ACTION


def test_single_pickup_unchanged(controller):
    """Contract added WITHOUT pickup_candidates behaves exactly as
    before: one pickup stop, cargo loads there, no candidate stops,
    no rows in contract_pickup_candidates."""
    wid = controller.start_workday(_seraphim(controller), None, False)

    cid = controller.add_contract({
        "pickup_station": "Yellow Core",
        "max_pallet_size": 8,
        "deliveries": [
            {"destination": "Everus Harbor", "commodity": "Tungsten", "scu": 32},
        ],
    })

    rows = controller.list_pickup_candidates(cid)
    assert rows == [], (
        "single-pickup contract should leave contract_pickup_candidates empty"
    )

    result = run_recompute(wid, controller.conn)
    actions = [s.action for s in result.route_stops]
    assert CANDIDATE_PICKUP_ACTION not in actions, (
        "no candidate-pickup info stops should appear for single-pickup "
        "contracts"
    )

    # Exactly one stop loads this contract's cargo.
    n_load_stops = sum(
        1 for s in result.route_stops
        if any(ref.contract_id == cid for ref in s.loads)
    )
    assert n_load_stops == 1, (
        f"single-pickup contract should have exactly 1 load stop, "
        f"got {n_load_stops}"
    )
