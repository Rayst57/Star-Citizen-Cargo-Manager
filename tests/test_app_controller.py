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

def test_move_cargo_rejects_line_too_big_for_target_zone(controller):
    """move_cargo relocates the WHOLE cargo line into one zone. A line
    bigger than the target zone's capacity can't physically live there,
    so the move must be refused with a clear error — not silently
    accepted into an overflowing pin that the next recompute discards.
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

    # 71 SCU line — bigger than every Starlancer zone (RBA is 64).
    controller.add_contract({
        "pickup_station": "Yellow Core",
        "max_pallet_size": 8,
        "deliveries": [
            {"destination": "Baijini Point", "commodity": "Tungsten", "scu": 71},
        ],
    })
    result = run_recompute(wid, controller.conn)
    controller._last_result = result
    assign_destination_colors(wid, controller.conn)

    cl_id = controller.conn.execute(
        "SELECT cargo_line_id FROM zone_assignments WHERE workday_id = ?",
        (wid,),
    ).fetchone()["cargo_line_id"]

    # Pinning a 71 SCU line into the 64 SCU RBA must raise.
    with pytest.raises(ToolError, match="bigger than"):
        controller.move_cargo(cl_id, "RBA")

    # A line that DOES fit should still move fine.
    controller.add_contract({
        "pickup_station": "Yellow Core",
        "max_pallet_size": 8,
        "deliveries": [
            {"destination": "Baijini Point", "commodity": "Tungsten", "scu": 16},
        ],
    })
    result = run_recompute(wid, controller.conn)
    controller._last_result = result
    small_cl = controller.conn.execute(
        "SELECT cargo_line_id FROM zone_assignments WHERE workday_id = ? "
        "ORDER BY cargo_line_id DESC LIMIT 1",
        (wid,),
    ).fetchone()["cargo_line_id"]
    controller.move_cargo(small_cl, "RBA")  # 16 SCU into 64 SCU zone — fine
