"""Smoke tests for DB initialization and seed loading."""

import sqlite3
import tempfile
from pathlib import Path

import pytest

from src.db.init_db import initialize_database


@pytest.fixture
def tmp_db(tmp_path):
    db = tmp_path / "test.db"
    conn = initialize_database(db)
    yield conn
    conn.close()


def test_schema_tables_created(tmp_db):
    tables = {
        r[0]
        for r in tmp_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    expected = {
        "systems", "stations", "station_aliases", "station_distances",
        "jump_gates", "commodities", "commodity_aliases",
        "ships", "ship_zones",
        "workdays", "contracts", "cargo_lines",
        "route_stops", "zone_assignments", "pallet_conflicts",
        "validation_log", "app_settings",
    }
    assert expected.issubset(tables)


def test_stanton_system_seeded(tmp_db):
    row = tmp_db.execute(
        "SELECT id FROM systems WHERE name = 'Stanton'"
    ).fetchone()
    assert row is not None


def test_stations_seeded(tmp_db):
    count = tmp_db.execute("SELECT count(*) FROM stations").fetchone()[0]
    assert count >= 13  # 12 Stanton + 1 Pyro gateway


def test_c2_ship_seeded(tmp_db):
    row = tmp_db.execute(
        "SELECT total_scu FROM ships WHERE name = 'C2 Hercules'"
    ).fetchone()
    assert row is not None
    assert row["total_scu"] == 696


def test_c2_zones_seeded(tmp_db):
    zones = tmp_db.execute(
        """
        SELECT zone_label FROM ship_zones
        JOIN ships ON ships.id = ship_zones.ship_id
        WHERE ships.name = 'C2 Hercules'
        ORDER BY load_order
        """
    ).fetchall()
    labels = [z["zone_label"] for z in zones]
    assert labels == ["F1", "F2", "F3", "R1", "R2", "R3", "R4"]


def test_commodities_seeded(tmp_db):
    count = tmp_db.execute("SELECT count(*) FROM commodities").fetchone()[0]
    assert count >= 10


def test_station_alias_resolution(tmp_db):
    row = tmp_db.execute(
        """
        SELECT s.name FROM stations s
        JOIN station_aliases a ON a.station_id = s.id
        WHERE lower(a.alias) = 'baijini'
        LIMIT 1
        """
    ).fetchone()
    assert row is not None
    assert row["name"] == "Baijini Point"


def test_jump_gate_seeded(tmp_db):
    count = tmp_db.execute("SELECT count(*) FROM jump_gates").fetchone()[0]
    assert count >= 2  # bidirectional Stanton ↔ Pyro


def test_ironclad_seeded(tmp_db):
    row = tmp_db.execute(
        "SELECT total_scu FROM ships WHERE name = 'Drake Ironclad'"
    ).fetchone()
    assert row is not None
    assert row["total_scu"] == 2160


def test_ironclad_zones_count_and_dimensions(tmp_db):
    zones = tmp_db.execute(
        """
        SELECT zone_label, bay_label, width_units, length_units,
               height_units, scu_capacity
        FROM ship_zones
        JOIN ships ON ships.id = ship_zones.ship_id
        WHERE ships.name = 'Drake Ironclad'
        """
    ).fetchall()
    assert len(zones) == 12

    aft = [z for z in zones if z["bay_label"] == "aft"]
    fwd = [z for z in zones if z["bay_label"] == "forward"]
    assert len(aft) == 6
    assert len(fwd) == 6

    aft_labels = sorted(z["zone_label"] for z in aft)
    fwd_labels = sorted(z["zone_label"] for z in fwd)
    assert aft_labels == ["R1", "R2", "R3", "R4", "R5", "R6"]
    assert fwd_labels == ["F1", "F2", "F3", "F4", "F5", "F6"]

    for z in aft:
        assert z["width_units"] == 2
        assert z["length_units"] == 20
        assert z["height_units"] == 6
        assert z["scu_capacity"] == 240

    for z in fwd:
        assert z["width_units"] == 2
        assert z["length_units"] == 10
        assert z["height_units"] == 6
        assert z["scu_capacity"] == 120

    total = sum(z["scu_capacity"] for z in zones)
    assert total == 2160


def test_ironclad_adjacency(tmp_db):
    rows = tmp_db.execute(
        """
        SELECT zone_label, left_zone_label, right_zone_label,
               front_zone_label, back_zone_label
        FROM ship_zones
        JOIN ships ON ships.id = ship_zones.ship_id
        WHERE ships.name = 'Drake Ironclad'
        """
    ).fetchall()
    by_label = {r["zone_label"]: r for r in rows}

    # Port aft triplet: R1<->R2<->R3
    assert by_label["R1"]["left_zone_label"] is None
    assert by_label["R1"]["right_zone_label"] == "R2"
    assert by_label["R2"]["left_zone_label"] == "R1"
    assert by_label["R2"]["right_zone_label"] == "R3"
    assert by_label["R3"]["left_zone_label"] == "R2"

    # Starboard aft triplet: R4<->R5<->R6
    assert by_label["R4"]["right_zone_label"] == "R5"
    assert by_label["R5"]["left_zone_label"] == "R4"
    assert by_label["R5"]["right_zone_label"] == "R6"
    assert by_label["R6"]["left_zone_label"] == "R5"
    assert by_label["R6"]["right_zone_label"] is None

    # Aft spine gap: R3.right and R4.left are null
    assert by_label["R3"]["right_zone_label"] is None
    assert by_label["R4"]["left_zone_label"] is None

    # Port forward triplet: F1<->F2<->F3
    assert by_label["F1"]["left_zone_label"] is None
    assert by_label["F1"]["right_zone_label"] == "F2"
    assert by_label["F2"]["left_zone_label"] == "F1"
    assert by_label["F2"]["right_zone_label"] == "F3"
    assert by_label["F3"]["left_zone_label"] == "F2"

    # Starboard forward triplet: F4<->F5<->F6
    assert by_label["F4"]["right_zone_label"] == "F5"
    assert by_label["F5"]["left_zone_label"] == "F4"
    assert by_label["F5"]["right_zone_label"] == "F6"
    assert by_label["F6"]["left_zone_label"] == "F5"
    assert by_label["F6"]["right_zone_label"] is None

    # Forward spine gap: F3.right and F4.left are null
    assert by_label["F3"]["right_zone_label"] is None
    assert by_label["F4"]["left_zone_label"] is None

    # Aft and forward bays do not share a continuous floor
    for label in ("R1", "R2", "R3", "R4", "R5", "R6",
                  "F1", "F2", "F3", "F4", "F5", "F6"):
        assert by_label[label]["front_zone_label"] is None
        assert by_label[label]["back_zone_label"] is None
