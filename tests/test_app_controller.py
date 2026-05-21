"""End-to-end tests for the AppController and the UI bootstrap.

These run headless (Qt offscreen) and don't depend on a display server.
"""

from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sqlite3
import tempfile
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from src.app_controller import AppController, ToolError
from src.db.init_db import initialize_database
from src.palletizer_color import assign_destination_colors
from src.planner.recompute import recompute as run_recompute


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def controller(qapp, tmp_path):
    db_path = tmp_path / "test.db"
    conn = initialize_database(db_path)
    return AppController(conn, db_path)


# ── workday lifecycle ────────────────────────────────────────────────────

def test_start_and_end_workday(controller):
    seraphim = controller.conn.execute(
        "SELECT id FROM stations WHERE name = 'Seraphim Station'"
    ).fetchone()["id"]
    wid = controller.start_workday(seraphim, None, False)
    assert wid > 0
    assert controller.workday_id == wid
    controller.end_workday()
    assert controller.workday_id is None


def test_resume_open_workday(controller):
    seraphim = controller.conn.execute(
        "SELECT id FROM stations WHERE name = 'Seraphim Station'"
    ).fetchone()["id"]
    wid = controller.start_workday(seraphim, None, False)
    # Drop the controller's reference but keep the open workday in DB
    controller.workday_id = None
    open_wd = controller.find_open_workday()
    assert open_wd["id"] == wid
    controller.resume_workday(wid)
    assert controller.workday_id == wid


# ── contracts ────────────────────────────────────────────────────────────

def test_add_remove_contract(controller):
    seraphim = controller.conn.execute(
        "SELECT id FROM stations WHERE name = 'Seraphim Station'"
    ).fetchone()["id"]
    controller.start_workday(seraphim, None, False)

    cid = controller.add_contract({
        "pickup_station": "Yellow Core",
        "max_pallet_size": 8,
        "deliveries": [
            {"destination": "Everus Harbor", "commodity": "Tungsten", "scu": 55},
        ],
    })
    assert cid > 0

    contracts = controller.list_contracts()
    assert len(contracts) == 1
    assert contracts[0]["contract_number"] == 1
    assert contracts[0]["total_scu"] == 55

    controller.remove_contract(1)
    assert controller.list_contracts() == []


def test_undo_restores_contracts(controller):
    seraphim = controller.conn.execute(
        "SELECT id FROM stations WHERE name = 'Seraphim Station'"
    ).fetchone()["id"]
    controller.start_workday(seraphim, None, False)

    controller.add_contract({
        "pickup_station": "Yellow Core",
        "max_pallet_size": 8,
        "deliveries": [
            {"destination": "Everus Harbor", "commodity": "Tungsten", "scu": 55},
        ],
    })
    assert len(controller.list_contracts()) == 1

    controller.remove_contract(1)
    assert len(controller.list_contracts()) == 0

    assert controller.undo() is True
    assert len(controller.list_contracts()) == 1


def test_invalid_max_pallet_raises(controller):
    seraphim = controller.conn.execute(
        "SELECT id FROM stations WHERE name = 'Seraphim Station'"
    ).fetchone()["id"]
    controller.start_workday(seraphim, None, False)
    with pytest.raises(ToolError):
        controller.add_contract({
            "pickup_station": "Yellow Core",
            "max_pallet_size": 7,   # not a valid pallet size
            "deliveries": [
                {"destination": "Everus Harbor", "commodity": "Tungsten", "scu": 10},
            ],
        })


def test_unknown_station_raises(controller):
    seraphim = controller.conn.execute(
        "SELECT id FROM stations WHERE name = 'Seraphim Station'"
    ).fetchone()["id"]
    controller.start_workday(seraphim, None, False)
    with pytest.raises(ToolError):
        controller.add_contract({
            "pickup_station": "Atlantis Bay",   # not a real station
            "deliveries": [
                {"destination": "Everus Harbor", "commodity": "Tungsten", "scu": 10},
            ],
        })


# ── pallet rect rendering ──────────────────────────────────────────────

def test_pallet_rects_after_recompute(controller):
    seraphim = controller.conn.execute(
        "SELECT id FROM stations WHERE name = 'Seraphim Station'"
    ).fetchone()["id"]
    wid = controller.start_workday(seraphim, None, False)

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
    assert len(rects) == 14   # 9 + 5 pallets
    # Each pallet stays inside its zone bounds
    for r in rects:
        if r.bay == "forward":
            assert 0 <= r.cell_x <= 6 - r.cell_w
            assert 0 <= r.cell_y <= 9 - r.cell_l
        else:
            assert 0 <= r.cell_x <= 8 - r.cell_w
            assert 0 <= r.cell_y <= 15 - r.cell_l


# ── context snapshot ───────────────────────────────────────────────────

def test_context_snapshot_with_no_workday(controller):
    snap = controller.build_context_snapshot()
    assert snap == "WORKDAY: none"


def test_context_snapshot_after_recompute(controller):
    seraphim = controller.conn.execute(
        "SELECT id FROM stations WHERE name = 'Seraphim Station'"
    ).fetchone()["id"]
    wid = controller.start_workday(seraphim, None, False)
    controller.add_contract({
        "pickup_station": "Yellow Core",
        "max_pallet_size": 8,
        "deliveries": [
            {"destination": "Everus Harbor", "commodity": "Tungsten", "scu": 55},
        ],
    })
    result = run_recompute(wid, controller.conn)
    controller._last_result = result

    snap = controller.build_context_snapshot()
    assert "Seraphim Station" in snap
    assert "Tungsten" in snap
    assert "Everus Harbor" in snap


# ── tool dispatch ──────────────────────────────────────────────────────

def test_dispatch_add_contract_via_tool(controller):
    seraphim = controller.conn.execute(
        "SELECT id FROM stations WHERE name = 'Seraphim Station'"
    ).fetchone()["id"]
    controller.start_workday(seraphim, None, False)
    msg = controller.dispatch_tool("add_contract", {
        "pickup_station": "Yellow Core",
        "max_pallet_size": 8,
        "deliveries": [
            {"destination": "Everus Harbor", "commodity": "Tungsten", "scu": 55},
        ],
    })
    assert "Contract 1" in msg


def test_dispatch_unknown_tool_raises(controller):
    with pytest.raises(ToolError):
        controller.dispatch_tool("bogus_tool", {})


# ── BayCanvas physical packing ────────────────────────────────────────

def test_full_starlancer_rb_renders_pallets_even_when_overflowing(controller):
    """Regression: a Starlancer RB carrying 71 + 17 = 88 SCU
    (10x8 SCU + 1x4 + 1x2 + 2x1) can't be packed perfectly into the
    4x11x2 cube grid (10 large pallets fill y=0..9 leaving only a 4x1
    strip too shallow for a 2x2 small pallet). Before the partial-fit
    fix, the BayCanvas returned zero PalletRects for that zone and the
    Zone Detail dialog rendered empty.

    The fix tries more orderings and accepts a partial layout, so the
    user always sees what could be packed.
    """
    starlancer = controller.conn.execute(
        "SELECT id FROM ships WHERE name LIKE 'Starlancer%'"
    ).fetchone()
    if not starlancer:
        pytest.skip("Starlancer not seeded in this environment")

    seraphim = controller.conn.execute(
        "SELECT id FROM stations WHERE name = 'Seraphim Station'"
    ).fetchone()["id"]
    wid = controller.start_workday(seraphim, None, False)
    controller.conn.execute(
        "UPDATE workdays SET ship_id = ? WHERE id = ?",
        (starlancer["id"], wid),
    )
    controller.conn.commit()

    # 71 SCU Baijini + 17 SCU Seraphim into the same zone via the
    # manual-move path so they share RB at the same stop.
    controller.add_contract({
        "pickup_station": "Yellow Core",
        "max_pallet_size": 8,
        "deliveries": [
            {"destination": "Baijini Point", "commodity": "Tungsten", "scu": 71},
        ],
    })
    controller.add_contract({
        "pickup_station": "Yellow Core",
        "max_pallet_size": 8,
        "deliveries": [
            {"destination": "Seraphim Station", "commodity": "Tungsten", "scu": 17},
        ],
    })
    result = run_recompute(wid, controller.conn)
    controller._last_result = result
    assign_destination_colors(wid, controller.conn)

    # Force both lines into RB so we hit the 88/88 overcrowd case.
    rows = controller.conn.execute(
        "SELECT cargo_line_id FROM zone_assignments WHERE workday_id = ?",
        (wid,),
    ).fetchall()
    for r in rows:
        controller.move_cargo(r["cargo_line_id"], "RB")

    # Snapshot any stop that has cargo onboard.
    onboard_stops = [
        s.stop_number for s in result.route_stops
        if result.snapshots.get(s.stop_number)
    ]
    assert onboard_stops, "Expected at least one stop with cargo onboard"

    for sn in onboard_stops:
        rects = controller.get_pallet_rects(stop_number=sn)
        rb_rects = [r for r in rects if r.zone_label == "RB"]
        # Pre-fix the count here was 0 because the packer silently
        # bailed. With the partial-fit fix the user should always see
        # at least the large pallets.
        assert rb_rects, (
            f"Stop {sn}: no RB pallets rendered — the packer must "
            f"return a partial layout rather than nothing."
        )
