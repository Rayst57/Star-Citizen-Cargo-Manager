"""Tests for the force-push + holding-table workflow.

These cover the controller methods that move pallets in/out of the
pallet_holding table, the auto-place machinery that tries to find
homes for holding pallets, the HoldingTableWidget's destination
grouping, and the ForcePushDialog's default-acceptance behaviour.
"""

from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QLabel

from src.app_controller import AppController
from src.db.init_db import initialize_database
from src.palletizer_color import assign_destination_colors
from src.planner.recompute import recompute as run_recompute
from src.ui.dialogs.force_push import ForcePushDialog
from src.ui.widgets.holding_table import HoldingTableWidget


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


def _cargo_line_id_for(controller, contract_number: int) -> int:
    row = controller.conn.execute(
        """
        SELECT cl.id FROM cargo_lines cl
        JOIN contracts ct ON ct.id = cl.contract_id
        WHERE ct.workday_id = ? AND ct.contract_number = ?
        ORDER BY cl.line_number LIMIT 1
        """,
        (controller.workday_id, contract_number),
    ).fetchone()
    return row["id"]


# ── Controller tests ────────────────────────────────────────────────


def test_push_pallets_creates_holding_rows(controller):
    """A pallet that was locked, then pushed to holding, should have
    its pallet_locks row removed and a matching pallet_holding row
    added."""
    wid = controller.start_workday(_seraphim(controller), None, False)
    controller.add_contract({
        "pickup_station": "Yellow Core",
        "max_pallet_size": 8,
        "deliveries": [
            {"destination": "Long Forest", "commodity": "Tungsten",
             "scu": 8},
        ],
    })
    run_recompute(wid, controller.conn)
    cl_id = _cargo_line_id_for(controller, 1)

    # Pick the first ship zone for the lock target.
    zone_row = controller.conn.execute(
        "SELECT z.zone_label FROM ship_zones z "
        "JOIN workdays w ON w.ship_id = z.ship_id "
        "WHERE w.id = ? LIMIT 1",
        (wid,),
    ).fetchone()
    target_zone = zone_row["zone_label"]

    controller.lock_pallet(cl_id, 0, target_zone, 0, 0, 0)
    pre_locks = controller.list_pallet_locks()
    assert any(
        r["cargo_line_id"] == cl_id and r["pallet_index"] == 0
        for r in pre_locks
    )
    assert controller.list_holding_pallets() == []

    controller.push_pallets_to_holding([(cl_id, 0)])
    assert controller.is_pallet_in_holding(cl_id, 0)
    holding = controller.list_holding_pallets()
    assert len(holding) == 1
    assert holding[0]["cargo_line_id"] == cl_id
    assert holding[0]["pallet_index"] == 0

    # Lock row must be gone.
    post_locks = controller.list_pallet_locks()
    assert not any(
        r["cargo_line_id"] == cl_id and r["pallet_index"] == 0
        for r in post_locks
    )


def test_auto_place_holding_finds_free_spot(controller):
    """Two pallets dropped into holding on a mostly-empty workday
    should both find a home and end up locked."""
    wid = controller.start_workday(_seraphim(controller), None, False)
    controller.add_contract({
        "pickup_station": "Yellow Core",
        "max_pallet_size": 8,
        "deliveries": [
            {"destination": "Long Forest", "commodity": "Tungsten",
             "scu": 16},
        ],
    })
    run_recompute(wid, controller.conn)
    cl_id = _cargo_line_id_for(controller, 1)

    controller.push_pallets_to_holding([(cl_id, 0), (cl_id, 1)])
    assert len(controller.list_holding_pallets()) == 2

    failures = controller.auto_place_holding_pallets()
    assert failures == {}, f"Unexpected failures: {failures}"
    assert controller.list_holding_pallets() == []
    locks = controller.list_pallet_locks()
    assert len([
        r for r in locks
        if r["cargo_line_id"] == cl_id and r["pallet_index"] in (0, 1)
    ]) == 2


def test_auto_place_holding_reports_failures_when_full(controller):
    """When every zone is already full, auto_place_holding_pallets()
    should return a failure entry for every holding pallet and leave
    the holding table untouched."""
    wid = controller.start_workday(_seraphim(controller), None, False)
    # Fill the ship completely: C2 = 696 SCU.
    total_scu = controller.total_scu_capacity()
    controller.add_contract({
        "pickup_station": "Yellow Core",
        "max_pallet_size": 8,
        "deliveries": [
            {"destination": "Long Forest", "commodity": "Tungsten",
             "scu": total_scu},
        ],
    })
    run_recompute(wid, controller.conn)
    fill_cl = _cargo_line_id_for(controller, 1)

    # Pin every fill pallet to a deterministic spot so the auto-placer
    # sees an entirely reserved ship. The packer treats locked pallets
    # as reserved cubes; with the whole ship taken there's nowhere to
    # land a new pallet.
    from src.planner.palletizer import palletize
    fill_pallets = palletize(total_scu, 8)
    # Stamp them into pallet_locks row-by-row via lock_pallet on
    # whatever zone has room. We can short-circuit by using the
    # auto-placer itself: push the fill cargo line into holding, then
    # auto-place. After that holding is empty and ship is full.
    controller.push_pallets_to_holding(
        [(fill_cl, i) for i in range(len(fill_pallets))]
    )
    failures = controller.auto_place_holding_pallets()
    assert failures == {}, f"Initial fill failed: {failures}"
    assert controller.list_holding_pallets() == []

    # Now stage 50 small pallets in holding via a brand-new contract.
    # These don't fit because the ship is locked solid.
    controller.add_contract({
        "pickup_station": "Yellow Core",
        "max_pallet_size": 1,
        "deliveries": [
            {"destination": "Everus Harbor", "commodity": "Tungsten",
             "scu": 50},
        ],
    })
    extra_cl = _cargo_line_id_for(controller, 2)
    controller.push_pallets_to_holding(
        [(extra_cl, i) for i in range(50)]
    )
    pre_holding = controller.list_holding_pallets()
    assert len(pre_holding) == 50

    failures = controller.auto_place_holding_pallets()
    assert len(failures) == 50
    for i in range(50):
        assert (extra_cl, i) in failures
    # Holding stays at 50 — none of the extras were placeable.
    assert len(controller.list_holding_pallets()) == 50


# ── Widget test ─────────────────────────────────────────────────────


def test_holding_widget_renders_grouped_by_destination(controller):
    """Three holding pallets bound for two destinations should produce
    two destination group headers in the rendered widget."""
    wid = controller.start_workday(_seraphim(controller), None, False)
    controller.add_contract({
        "pickup_station": "Yellow Core",
        "max_pallet_size": 8,
        "deliveries": [
            {"destination": "Long Forest", "commodity": "Tungsten",
             "scu": 16},
        ],
    })
    controller.add_contract({
        "pickup_station": "Yellow Core",
        "max_pallet_size": 8,
        "deliveries": [
            {"destination": "Everus Harbor", "commodity": "Tungsten",
             "scu": 8},
        ],
    })
    run_recompute(wid, controller.conn)
    cl_long = _cargo_line_id_for(controller, 1)
    cl_everus = _cargo_line_id_for(controller, 2)

    controller.push_pallets_to_holding([
        (cl_long, 0),
        (cl_long, 1),
        (cl_everus, 0),
    ])

    widget = HoldingTableWidget(controller)
    # Process events so the constructor's initial refresh and any
    # deleteLater() bookkeeping finishes before we count children.
    QApplication.processEvents()
    widget.refresh()
    QApplication.processEvents()

    # Group header labels carry a 'groupHeader' Qt property.
    headers = [
        lbl for lbl in widget.findChildren(QLabel)
        if lbl.property("groupHeader")
    ]
    assert len(headers) == 2, (
        f"Expected 2 destination headers, got {len(headers)}: "
        f"{[h.text() for h in headers]}"
    )
    seen_destinations = {h.property("destinationName") for h in headers}
    # Station names canonicalize to their full DB names on insert.
    assert "Everus Harbor" in seen_destinations
    assert any("Long Forest" in s for s in seen_destinations)


# ── Dialog test ─────────────────────────────────────────────────────


def test_force_push_dialog_default_displaces_all(controller):
    """Accepting the dialog without unchecking any row should put both
    displaced pallets into confirmed_displacements."""
    incoming = {
        "cargo_line_id": 1,
        "pallet_index": 0,
        "size": 8,
        "destination": "Long Forest",
    }
    displaced = [
        {
            "cargo_line_id": 2,
            "pallet_index": 0,
            "size": 8,
            "destination": "Everus Harbor",
            "commodity": "Tungsten",
            "contract_number": 2,
        },
        {
            "cargo_line_id": 3,
            "pallet_index": 1,
            "size": 4,
            "destination": "Baijini Point",
            "commodity": "Iron",
            "contract_number": 3,
        },
    ]
    dlg = ForcePushDialog(
        controller,
        incoming_pallet=incoming,
        displaced_pallets=displaced,
    )
    # Simulate the user clicking OK without touching the checkboxes.
    dlg._on_accept()
    assert dlg.result() != 0  # accepted
    assert dlg.confirmed_displacements == [(2, 0), (3, 1)]
