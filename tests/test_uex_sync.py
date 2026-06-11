"""Tests for the UEX startup station sync + expanded geography.

The sync engine is exercised with an injected catalog (no network in
tests). Covers:

1. normalize_location maps UEX rows to seed-shaped dicts and drops
   unavailable / decommissioned entries.
2. sync_stations_from_uex inserts unknown stations, matches existing
   ones by name or alias (no duplicates), backfills parent_body, and
   is idempotent.
3. API-unreachable returns ok=False and leaves the DB untouched.
4. The refreshed seed gives near-total geography coverage, including
   Pyro and Nyx bodies, and cross-system legs return None.
"""

from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from src.db.init_db import initialize_database
from src.db.uex_sync import normalize_location, sync_stations_from_uex
from src.planner.geography import body_position, leg_km, station_positions


@pytest.fixture
def conn(tmp_path):
    return initialize_database(tmp_path / "test.db")


# ── 1. normalization ─────────────────────────────────────────────────


def test_normalize_location_maps_fields():
    row = {
        "name": "Checkmate Station",
        "nickname": "Checkmate",
        "star_system_name": "Pyro",
        "planet_name": "Monox",
        "moon_name": None,
        "orbit_name": "Monox",
        "is_available": 1,
        "is_decommissioned": 0,
        "is_jump_point": 0,
        "is_lagrange": 0,
    }
    loc = normalize_location(row, "space_station")
    assert loc == {
        "system": "Pyro",
        "name": "Checkmate Station",
        "parent_body": "Monox",
        "station_type": "orbital",
        "is_gateway": 0,
        "aliases": ["Checkmate"],
    }


def test_normalize_location_drops_unavailable():
    row = {"name": "Ghost", "star_system_name": "Pyro", "is_available": 0}
    assert normalize_location(row, "space_station") is None
    row = {"name": "Gone", "star_system_name": "Pyro",
           "is_available": 1, "is_decommissioned": 1}
    assert normalize_location(row, "space_station") is None


def test_normalize_gateway_and_lagrange_types():
    base = {"star_system_name": "Pyro", "is_available": 1}
    gw = normalize_location(
        {**base, "name": "Some Gateway", "is_jump_point": 1}, "space_station",
    )
    assert gw["station_type"] == "gateway" and gw["is_gateway"] == 1
    lg = normalize_location(
        {**base, "name": "Some L-Point", "is_lagrange": 1}, "space_station",
    )
    assert lg["station_type"] == "lagrange" and lg["is_gateway"] == 0


# ── 2. DB sync ───────────────────────────────────────────────────────


def _catalog(locations):
    return {"systems": ["Pyro", "Stanton"], "locations": locations}


def test_sync_inserts_unknown_station(conn):
    summary = sync_stations_from_uex(conn, catalog=_catalog([{
        "system": "Pyro",
        "name": "Brand New Depot",
        "parent_body": "Terminus",
        "station_type": "outpost",
        "is_gateway": 0,
        "aliases": ["BND"],
    }]))
    assert summary["ok"] and summary["added"] == 1
    row = conn.execute(
        "SELECT s.parent_body, sy.name AS sys FROM stations s "
        "JOIN systems sy ON sy.id = s.system_id "
        "WHERE s.name = 'Brand New Depot'"
    ).fetchone()
    assert row is not None
    assert row["sys"] == "Pyro"
    assert row["parent_body"] == "Terminus"
    # Alias landed too.
    alias = conn.execute(
        "SELECT 1 FROM station_aliases a JOIN stations s ON s.id = a.station_id "
        "WHERE s.name = 'Brand New Depot' AND a.alias = 'BND'"
    ).fetchone()
    assert alias is not None


def test_sync_matches_existing_by_name_and_alias_no_duplicates(conn):
    before = conn.execute("SELECT COUNT(*) c FROM stations").fetchone()["c"]
    summary = sync_stations_from_uex(conn, catalog=_catalog([
        # Exact name match.
        {"system": "Stanton", "name": "Seraphim Station",
         "parent_body": "Crusader", "station_type": "orbital",
         "is_gateway": 0, "aliases": []},
        # Alias match — seed knows 'Ambitious Dream' as an alias.
        {"system": "Stanton", "name": "Ambitious Dream",
         "parent_body": "CRU-L1", "station_type": "lagrange",
         "is_gateway": 0, "aliases": []},
    ]))
    assert summary["ok"] and summary["added"] == 0
    after = conn.execute("SELECT COUNT(*) c FROM stations").fetchone()["c"]
    assert after == before, "Matched stations must not be re-inserted."


def test_sync_is_idempotent(conn):
    cat = _catalog([{
        "system": "Pyro", "name": "Idempotent Outpost",
        "parent_body": "Bloom", "station_type": "outpost",
        "is_gateway": 0, "aliases": [],
    }])
    s1 = sync_stations_from_uex(conn, catalog=cat)
    s2 = sync_stations_from_uex(conn, catalog=cat)
    assert s1["added"] == 1 and s2["added"] == 0
    n = conn.execute(
        "SELECT COUNT(*) c FROM stations WHERE name = 'Idempotent Outpost'"
    ).fetchone()["c"]
    assert n == 1


def test_sync_offline_leaves_db_untouched(conn, monkeypatch):
    import src.db.uex_sync as mod
    monkeypatch.setattr(mod, "_fetch_json", lambda *a, **k: None)
    before = conn.execute("SELECT COUNT(*) c FROM stations").fetchone()["c"]
    summary = sync_stations_from_uex(conn)
    assert summary["ok"] is False
    after = conn.execute("SELECT COUNT(*) c FROM stations").fetchone()["c"]
    assert after == before


# ── 3. expanded geography ────────────────────────────────────────────


def test_pyro_and_nyx_bodies_resolve():
    assert body_position("Monox") is not None
    assert body_position("Terminus") is not None
    assert body_position("Adir") == body_position("Pyro V")  # moon co-location
    assert body_position("Delamar") is not None
    assert body_position("Stanton Gateway (Pyro system)") is not None


def test_seed_geography_coverage(conn):
    """Nearly every seeded station must resolve to a map position —
    only 'Other'-parent oddballs (Wikelo Emporium) may be unmapped."""
    positions = station_positions(conn)
    rows = conn.execute("SELECT id, name, parent_body FROM stations").fetchall()
    unmapped = [r["name"] for r in rows if r["id"] not in positions]
    allowed_unmapped = {n for n in unmapped if "Wikelo" in n}
    assert set(unmapped) == allowed_unmapped, (
        f"Unexpected unmapped stations: "
        f"{sorted(set(unmapped) - allowed_unmapped)[:10]}"
    )


def test_cross_system_leg_is_none(conn):
    positions = station_positions(conn)
    sera = conn.execute(
        "SELECT id FROM stations WHERE name = 'Seraphim Station'"
    ).fetchone()["id"]
    ruin = conn.execute(
        "SELECT id FROM stations WHERE name = 'Ruin Station'"
    ).fetchone()["id"]
    assert leg_km(positions[sera], positions[ruin]) is None
    # Same-system pairs still compute.
    checkmate = conn.execute(
        "SELECT id FROM stations WHERE name = 'Checkmate Station'"
    ).fetchone()["id"]
    assert leg_km(positions[ruin], positions[checkmate]) > 0
