"""
Tests for conflict detection.

All cases are built from the canonical example in docs/conflicts.md.

Key rule:
  A pallet size is AMBIGUOUS only when ≥ 2 destinations have the SAME COUNT
  of that size at the same pickup × commodity.

  Size 8: Everus=6, Baijini=3  → different counts → NOT ambiguous
  Size 4: Everus=1, Baijini=0  → unique to Everus   → NOT ambiguous
  Size 2: Everus=1, Baijini=1  → same count          → AMBIGUOUS ⚠
  Size 1: Everus=1, Baijini=1  → same count          → AMBIGUOUS ⚠
"""

import pytest
from pathlib import Path

from src.db.init_db import initialize_database
from src.planner.conflicts import detect_conflicts
from src.planner.palletizer import palletize


# ── DB fixture ────────────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_path):
    conn = initialize_database(tmp_path / "test.db")
    yield conn
    conn.close()


def _station_id(name: str, conn) -> int:
    row = conn.execute(
        "SELECT id FROM stations WHERE name = ? OR id IN "
        "(SELECT station_id FROM station_aliases WHERE alias = ?)",
        (name, name),
    ).fetchone()
    assert row, f"Station not found: {name}"
    return row["id"]


def _commodity_id(name: str, conn) -> int:
    row = conn.execute("SELECT id FROM commodities WHERE name = ?", (name,)).fetchone()
    assert row, f"Commodity not found: {name}"
    return row["id"]


def _ship_id(name: str, conn) -> int:
    row = conn.execute("SELECT id FROM ships WHERE name = ?", (name,)).fetchone()
    assert row, f"Ship not found: {name}"
    return row["id"]


def _make_workday(conn, origin: str = "Seraphim Station") -> int:
    origin_id = _station_id(origin, conn)
    ship_id = _ship_id("C2 Hercules", conn)
    conn.execute(
        """
        INSERT INTO workdays (started_at, ship_id, origin_station_id, plan_dirty)
        VALUES (datetime('now'), ?, ?, 1)
        """,
        (ship_id, origin_id),
    )
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def _add_contract(conn, workday_id: int, number: int, pickup: str, max_pallet: int) -> int:
    pickup_id = _station_id(pickup, conn)
    conn.execute(
        """
        INSERT INTO contracts
          (workday_id, contract_number, pickup_station_id, max_pallet_size,
           status, created_at, updated_at)
        VALUES (?, ?, ?, ?, 'pending', datetime('now'), datetime('now'))
        """,
        (workday_id, number, pickup_id, max_pallet),
    )
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def _add_cargo_line(conn, contract_id: int, line: int, delivery: str, commodity: str, scu: int) -> int:
    delivery_id = _station_id(delivery, conn)
    commodity_id_val = _commodity_id(commodity, conn)
    conn.execute(
        """
        INSERT INTO cargo_lines
          (contract_id, line_number, delivery_station_id, commodity_id, scu_amount)
        VALUES (?, ?, ?, ?, ?)
        """,
        (contract_id, line, delivery_id, commodity_id_val, scu),
    )
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


# ── Canonical handbook example ────────────────────────────────────────────

class TestCanonicalExample:
    """Yellow Core pickup, Tungsten, Everus 55 SCU + Baijini 27 SCU, max 8."""

    @pytest.fixture
    def workday(self, db):
        wid = _make_workday(db)
        c1 = _add_contract(db, wid, 1, "Yellow Core", 8)
        c2 = _add_contract(db, wid, 2, "Yellow Core", 8)
        _add_cargo_line(db, c1, 1, "Everus Harbor", "Tungsten", 55)
        _add_cargo_line(db, c2, 1, "Baijini Point", "Tungsten", 27)
        db.commit()
        return wid, db

    def test_one_conflict_group_detected(self, workday):
        wid, conn = workday
        groups = detect_conflicts(wid, conn)
        assert len(groups) == 1

    def test_conflict_group_has_two_destinations(self, workday):
        wid, conn = workday
        groups = detect_conflicts(wid, conn)
        assert len(groups[0].destinations) == 2

    def test_ambiguous_sizes_are_2_and_1(self, workday):
        wid, conn = workday
        groups = detect_conflicts(wid, conn)
        # Same count (1 each) for sizes 2 and 1 → ambiguous
        assert set(groups[0].ambiguous_sizes) == {2, 1}

    def test_size_8_not_ambiguous(self, workday):
        """6×8 (Everus) vs 3×8 (Baijini) — different counts → not ambiguous."""
        wid, conn = workday
        groups = detect_conflicts(wid, conn)
        assert 8 not in groups[0].ambiguous_sizes

    def test_size_4_unique_to_everus(self, workday):
        """4 SCU pallet only in Everus breakdown → unique, not ambiguous."""
        wid, conn = workday
        groups = detect_conflicts(wid, conn)
        assert 4 not in groups[0].ambiguous_sizes
        everus_dest = next(
            d for d in groups[0].destinations
            if "Everus" in d.delivery_station_name
        )
        assert 4 in everus_dest.unique_sizes

    def test_everus_pallet_counts(self, workday):
        """Verify Everus breakdown: 6×8 + 1×4 + 1×2 + 1×1."""
        wid, conn = workday
        groups = detect_conflicts(wid, conn)
        everus = next(d for d in groups[0].destinations if "Everus" in d.delivery_station_name)
        assert everus.pallet_counts == {8: 6, 4: 1, 2: 1, 1: 1}

    def test_baijini_pallet_counts(self, workday):
        """Verify Baijini breakdown: 3×8 + 1×2 + 1×1."""
        wid, conn = workday
        groups = detect_conflicts(wid, conn)
        baijini = next(d for d in groups[0].destinations if "Baijini" in d.delivery_station_name)
        assert baijini.pallet_counts == {8: 3, 2: 1, 1: 1}

    def test_baijini_has_no_unique_sizes(self, workday):
        """Baijini has no sizes absent from Everus."""
        wid, conn = workday
        groups = detect_conflicts(wid, conn)
        baijini = next(d for d in groups[0].destinations if "Baijini" in d.delivery_station_name)
        assert baijini.unique_sizes == []

    def test_commodity_name_on_group(self, workday):
        wid, conn = workday
        groups = detect_conflicts(wid, conn)
        assert groups[0].commodity_name == "Tungsten"

    def test_pickup_name_on_group(self, workday):
        wid, conn = workday
        groups = detect_conflicts(wid, conn)
        assert "Yellow Core" in groups[0].pickup_station_name


# ── No conflict: different commodities ───────────────────────────────────

class TestDifferentCommodities:
    """Same size, same count, but different commodities → NOT a conflict."""

    def test_no_conflict(self, db):
        wid = _make_workday(db)
        c1 = _add_contract(db, wid, 1, "Yellow Core", 8)
        c2 = _add_contract(db, wid, 2, "Yellow Core", 8)
        # Both 1×2 SCU but different commodities
        _add_cargo_line(db, c1, 1, "Everus Harbor", "Tungsten", 2)
        _add_cargo_line(db, c2, 1, "Baijini Point", "Aluminum", 2)
        db.commit()
        groups = detect_conflicts(wid, db)
        assert len(groups) == 0


# ── No conflict: different counts are distinguishable ────────────────────

class TestDistinguishableByCount:
    """6×8 vs 3×8 — pilot can count to tell them apart → NOT ambiguous."""

    def test_different_counts_not_ambiguous(self, db):
        wid = _make_workday(db)
        c1 = _add_contract(db, wid, 1, "Yellow Core", 8)
        c2 = _add_contract(db, wid, 2, "Yellow Core", 8)
        # 48 SCU → 6×8, 24 SCU → 3×8 — both max 8, no remainder
        _add_cargo_line(db, c1, 1, "Everus Harbor", "Tungsten", 48)
        _add_cargo_line(db, c2, 1, "Baijini Point", "Tungsten", 24)
        db.commit()
        groups = detect_conflicts(wid, db)
        # 6×8 vs 3×8 → different counts → no ambiguous size → no conflict
        assert len(groups) == 0


# ── Three-way conflict ────────────────────────────────────────────────────

class TestThreeWayConflict:
    """Three destinations each with 1×2 and 1×1 SCU at the same pickup."""

    @pytest.fixture
    def workday(self, db):
        wid = _make_workday(db)
        for i, dest in enumerate(["Everus Harbor", "Baijini Point", "Port Tressler"], 1):
            c = _add_contract(db, wid, i, "Yellow Core", 2)
            _add_cargo_line(db, c, 1, dest, "Tungsten", 3)  # 3 SCU → 1×2 + 1×1
        db.commit()
        return wid, db

    def test_one_conflict_group(self, workday):
        wid, conn = workday
        groups = detect_conflicts(wid, conn)
        assert len(groups) == 1

    def test_three_destinations(self, workday):
        wid, conn = workday
        groups = detect_conflicts(wid, conn)
        assert len(groups[0].destinations) == 3

    def test_both_sizes_ambiguous(self, workday):
        wid, conn = workday
        groups = detect_conflicts(wid, conn)
        assert set(groups[0].ambiguous_sizes) == {2, 1}

    def test_no_unique_sizes_for_any_dest(self, workday):
        """All three destinations have identical breakdowns → no unique sizes."""
        wid, conn = workday
        groups = detect_conflicts(wid, conn)
        for dest in groups[0].destinations:
            assert dest.unique_sizes == []


# ── No conflict: single destination per pickup ────────────────────────────

def test_single_destination_no_conflict(db):
    wid = _make_workday(db)
    c1 = _add_contract(db, wid, 1, "Yellow Core", 8)
    _add_cargo_line(db, c1, 1, "Everus Harbor", "Tungsten", 55)
    db.commit()
    groups = detect_conflicts(wid, db)
    assert len(groups) == 0


# ── No conflict: zero contracts ───────────────────────────────────────────

def test_no_contracts_no_conflict(db):
    wid = _make_workday(db)
    db.commit()
    groups = detect_conflicts(wid, db)
    assert len(groups) == 0


# ── Multiple pickups produce separate groups ──────────────────────────────

def test_two_pickups_two_groups(db):
    """Yellow Core conflict + Shallow Fields conflict → 2 separate groups."""
    wid = _make_workday(db)
    # Yellow Core pickup
    c1 = _add_contract(db, wid, 1, "Yellow Core", 8)
    c2 = _add_contract(db, wid, 2, "Yellow Core", 8)
    _add_cargo_line(db, c1, 1, "Everus Harbor", "Tungsten", 3)   # 1×2 + 1×1
    _add_cargo_line(db, c2, 1, "Baijini Point", "Tungsten", 3)   # 1×2 + 1×1
    # Shallow Fields pickup
    c3 = _add_contract(db, wid, 3, "Shallow Fields", 8)
    c4 = _add_contract(db, wid, 4, "Shallow Fields", 8)
    _add_cargo_line(db, c3, 1, "Everus Harbor", "Tungsten", 3)
    _add_cargo_line(db, c4, 1, "Port Tressler", "Tungsten", 3)
    db.commit()

    groups = detect_conflicts(wid, db)
    assert len(groups) == 2
    pickup_names = {g.pickup_station_name for g in groups}
    assert "Yellow Core" in pickup_names
    assert "Shallow Fields" in pickup_names
