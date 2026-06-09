"""Tests for the advisory engine + AdvisoryPanel widget.

Runs headless under QT_QPA_PLATFORM=offscreen like the other UI tests.
"""

from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtGui import QPixmap
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from src.app_controller import AppController
from src.db.init_db import initialize_database
from src.palletizer_color import assign_destination_colors
from src.planner.advisory import Advisory, advisories_for_result
from src.planner.recompute import recompute as run_recompute
from src.ui.widgets.advisory_panel import AdvisoryPanel


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


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
    return controller.conn.execute(
        "SELECT id FROM ships WHERE name = 'C2 Hercules'"
    ).fetchone()["id"]


def _recompute_into_controller(controller, workday_id: int) -> None:
    """Run the planner and stash the result on the controller the way
    the recompute thread normally would."""
    result = run_recompute(workday_id, controller.conn)
    controller._last_result = result
    controller._advisories_cache = None
    assign_destination_colors(workday_id, controller.conn)


# ── high mixing ────────────────────────────────────────────────────────


def test_high_mixing_advisory(controller):
    """A workday with three+ distinct destinations in the same zone
    should produce a 'high_mixing' advisory.

    We build a clean workday, then poke ``zone_assignments`` directly
    to put three cargo lines into the same zone. The advisory engine
    runs off the in-memory snapshots, so we rebuild those after the
    poke.
    """
    wid = controller.start_workday(
        _seraphim(controller), None, False, ship_id=_c2(controller),
    )
    for dest in ("Everus Harbor", "Baijini Point", "Port Tressler"):
        controller.add_contract({
            "pickup_station": "Yellow Core",
            "max_pallet_size": 8,
            "deliveries": [
                {"destination": dest, "commodity": "Tungsten", "scu": 8},
            ],
        })
    _recompute_into_controller(controller, wid)

    # Force all three cargo lines into zone F1 so the snapshot shows
    # three distinct destinations sharing a single zone.
    controller.conn.execute(
        "UPDATE zone_assignments SET primary_zone_label = 'F1' "
        "WHERE workday_id = ?",
        (wid,),
    )
    controller.conn.commit()

    # Patch the in-memory snapshots to match (compute_advisories reads
    # snapshots, not the DB).
    for entries in controller._last_result.snapshots.values():
        for e in entries:
            e.zone_label = "F1"
    controller._advisories_cache = None

    advisories = controller.compute_advisories()
    found = []
    for stop_num, advs in advisories.items():
        for a in advs:
            if a.code == "high_mixing":
                found.append((stop_num, a))
    assert found, (
        f"Expected at least one high_mixing advisory; got: "
        f"{[(s, [a.code for a in v]) for s, v in advisories.items()]}"
    )
    _, adv = found[0]
    assert adv.severity == "warn"
    assert "F1" in adv.affected_zones
    assert len(adv.affected_cargo_lines) >= 3


# ── outboard early-unload ──────────────────────────────────────────────


def test_outboard_early_unload_advisory(controller):
    """Cargo destined for the NEXT stop sitting in a top-quartile
    unload_priority (outboard) zone should be flagged."""
    wid = controller.start_workday(
        _seraphim(controller), None, False, ship_id=_c2(controller),
    )
    # Fill the ship enough that some cargo for an early destination
    # ends up in a high-priority zone. The C2 has zones with priorities
    # 1..7 (R4=7 is the highest / most outboard). We use several
    # contracts so the packer spreads across many zones.
    for dest, scu in [
        ("Everus Harbor", 64),
        ("Baijini Point", 64),
        ("Port Tressler", 64),
        ("CRU-L1 Ambitious Dream Station", 64),
        ("CRU-L5 Beautiful Glen Station", 64),
        ("CRU-L4 Shallow Fields Station", 64),
        ("ARC-L1 Wide Forest Station", 64),
        ("ARC-L2 Lively Pathway Station", 64),
    ]:
        controller.add_contract({
            "pickup_station": "Yellow Core",
            "max_pallet_size": 8,
            "deliveries": [
                {"destination": dest, "commodity": "Tungsten", "scu": scu},
            ],
        })
    _recompute_into_controller(controller, wid)

    advisories = controller.compute_advisories()
    matched = [
        a for advs in advisories.values() for a in advs
        if a.code == "outboard_early_unload"
    ]
    assert matched, (
        "Expected at least one outboard_early_unload advisory; got: "
        f"{[(s, [a.code for a in v]) for s, v in advisories.items()]}"
    )
    for adv in matched:
        assert adv.severity == "warn"
        assert adv.affected_zones


# ── bulk in use ───────────────────────────────────────────────────────


def test_bulk_in_use_advisory(controller):
    """Cargo sitting in the highest-unload_priority (bulk) zone should
    surface an informational 'bulk_in_use' advisory.

    On the C2 the bulk-floor zone is R4 (unload_priority=7). We
    overfill the lower-priority zones with a single very large
    contract that has to spill into R4.
    """
    wid = controller.start_workday(
        _seraphim(controller), None, False, ship_id=_c2(controller),
    )
    # Total C2 capacity is 696 SCU. Filling it with one big run of
    # cargo forces the planner to use every zone, including R4 (the
    # bulk zone). Use a single contract → single destination so we
    # don't trigger high_mixing noise.
    controller.add_contract({
        "pickup_station": "Yellow Core",
        "max_pallet_size": 32,
        "deliveries": [
            {"destination": "Baijini Point", "commodity": "Tungsten",
             "scu": 696},
        ],
    })
    _recompute_into_controller(controller, wid)

    advisories = controller.compute_advisories()
    matched = [
        a for advs in advisories.values() for a in advs
        if a.code == "bulk_in_use"
    ]
    assert matched, (
        "Expected at least one bulk_in_use advisory; got: "
        f"{[(s, [a.code for a in v]) for s, v in advisories.items()]}"
    )
    for adv in matched:
        assert adv.severity == "info"
        assert adv.affected_zones  # at least one bulk zone listed


# ── clean workday ─────────────────────────────────────────────────────


def test_no_advisories_on_clean_workday(controller):
    """A small one-contract workday should produce no warn/error
    advisories. The 'info' tier (bottom_tier_late / bulk_in_use) is
    fine; we only assert there are no warnings or errors."""
    wid = controller.start_workday(
        _seraphim(controller), None, False, ship_id=_c2(controller),
    )
    controller.add_contract({
        "pickup_station": "Yellow Core",
        "max_pallet_size": 8,
        "deliveries": [
            {"destination": "Everus Harbor", "commodity": "Tungsten",
             "scu": 16},
        ],
    })
    _recompute_into_controller(controller, wid)

    advisories = controller.compute_advisories()
    # No warn or error severity advisories for any stop.
    bad = [
        (stop, a) for stop, advs in advisories.items() for a in advs
        if a.severity in ("warn", "error")
    ]
    assert not bad, (
        f"Expected no warn/error advisories on a clean workday; got: "
        f"{[(s, a.code) for s, a in bad]}"
    )


# ── widget rendering ───────────────────────────────────────────────────


def test_advisory_panel_renders(qapp, controller):
    """The AdvisoryPanel should instantiate and grab() without raising,
    both for an empty stop and for a stop with synthetic advisories."""
    panel = AdvisoryPanel(controller)
    panel.resize(360, 480)

    # Empty state.
    panel.set_stop(None)
    pm = panel.grab()
    assert not pm.isNull()

    # Inject a synthetic advisories dict for stop 2 and confirm render.
    panel.set_advisories({
        2: [
            Advisory(
                stop_number=2,
                severity="warn",
                code="high_mixing",
                summary="Zone F2 holds 3 distinct destinations",
                detail="Zone F2 is mixing cargo for: A, B, C.\n\n"
                       "Consider consolidating.",
                affected_zones=["F2"],
                affected_cargo_lines=[101, 102, 103],
            ),
            Advisory(
                stop_number=2,
                severity="error",
                code="overflow",
                summary="Zone F3 overflows: 80 SCU > 72 SCU capacity",
                detail="Pallets will not fit.",
                affected_zones=["F3"],
                affected_cargo_lines=[104],
            ),
            Advisory(
                stop_number=2,
                severity="info",
                code="bulk_in_use",
                summary="Bulk-floor zone(s) in use: R4",
                detail="Informational only.",
                affected_zones=["R4"],
                affected_cargo_lines=[],
            ),
        ],
    })
    panel.set_stop(2)
    pm2 = panel.grab()
    assert not pm2.isNull()
