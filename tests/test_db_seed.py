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
