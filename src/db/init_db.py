"""
Database initialization and connection management.

On first launch the app calls initialize_database(), which:
  1. Creates all tables from data/schema.sql.
  2. Loads seed data (stations, commodities, C2 ship) via seed.py.

Subsequent launches detect the existing schema and skip seeding.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent.parent
SCHEMA_PATH = _REPO_ROOT / "data" / "schema.sql"
DEFAULT_DB_PATH = _REPO_ROOT / "cargo_manager.db"


def get_connection(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open (or create) the SQLite DB and return a connection.

    row_factory is set to sqlite3.Row so columns are accessible by name.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _is_initialized(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='systems'"
    ).fetchone()
    return row[0] > 0


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == column for r in rows)


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Bring an already-initialized DB up to the current schema.

    Each step is idempotent — re-running the migration on an up-to-date
    DB is a no-op. New steps go at the bottom.
    """
    # Forward-compatible ramp metadata: distinguishes ramp-at-Y=0 (C2
    # default) from nose-ramp / sealed bays for ships beyond the C2.
    # Existing C2 zones keep their current Y=0 ramp behavior.
    if not _has_column(conn, "ship_zones", "ramp_side"):
        conn.execute(
            "ALTER TABLE ship_zones "
            "ADD COLUMN ramp_side TEXT NOT NULL DEFAULT 'low_y'"
        )
        conn.commit()

    # Rear-view top-down rendering metadata. 'high' = current behavior
    # (no flip; high local-Y at top of screen). For C2 the F-bay needs
    # 'low' so the nose ramp appears at the top of every diagram, but
    # we default existing rows to 'high' to avoid changing rendering
    # for any bay where we can't infer the correct value automatically.
    if not _has_column(conn, "ship_zones", "ship_forward_y"):
        conn.execute(
            "ALTER TABLE ship_zones "
            "ADD COLUMN ship_forward_y TEXT NOT NULL DEFAULT 'high'"
        )
        # Backfill known C2 forward bay zones — anything labelled
        # 'forward' on the C2 has its nose ramp at low-Y.
        conn.execute(
            "UPDATE ship_zones SET ship_forward_y = 'low' "
            "WHERE bay_label = 'forward'"
        )
        conn.commit()

    # Front/back adjacency. Optional companions to left/right that let
    # a seed declare two zones as physically continuous (no bulkhead).
    # Default NULL = structural separation (matches C2/Starlancer).
    if not _has_column(conn, "ship_zones", "front_zone_label"):
        conn.execute(
            "ALTER TABLE ship_zones ADD COLUMN front_zone_label TEXT"
        )
        conn.execute(
            "ALTER TABLE ship_zones ADD COLUMN back_zone_label TEXT"
        )
        conn.commit()

    # Station renames. The core trade stations were re-canonicalised
    # to full Lagrange names (e.g. "Ambitious Dream" → "CRU-L1
    # Ambitious Dream Station"), and two were relocated to different
    # L-points. Rename in place so existing DBs update their rows
    # instead of the seed re-sync inserting a second copy under the
    # new name. FK references (contracts, workdays, cargo_lines) are
    # by station id, so an in-place rename keeps all of them intact.
    # Idempotent: once renamed the old name is gone, so re-runs skip.
    _STATION_RENAMES = [
        ("Ambitious Dream",  "CRU-L1 Ambitious Dream Station"),
        ("Beautiful Glen",   "CRU-L5 Beautiful Glen Station"),
        ("Shallow Fields",   "CRU-L4 Shallow Fields Station"),
        ("Shallow Frontier", "MIC-L1 Shallow Frontier Station"),
        ("Yellow Core",      "ARC-L5 Yellow Core Station"),
        ("Wide Forest",      "ARC-L1 Wide Forest Station"),
        ("Lively Pathway",   "ARC-L2 Lively Pathway Station"),
        ("Long Forest",      "MIC-L2 Long Forest Station"),
        ("Faint Glen",       "ARC-L4 Faint Glen Station"),
        ("Modern Express",   "ARC-L3 Modern Express Station"),
        ("Pyro Jump Point",  "Pyro Gateway (Stanton)"),
    ]
    for old_name, new_name in _STATION_RENAMES:
        # Only rename when the old row exists and the new name is free
        # — avoids a UNIQUE(system_id, name) clash on a DB that
        # somehow already has both.
        old_row = conn.execute(
            "SELECT id, system_id FROM stations WHERE name = ?",
            (old_name,),
        ).fetchone()
        if old_row is None:
            continue
        clash = conn.execute(
            "SELECT 1 FROM stations WHERE system_id = ? AND name = ?",
            (old_row["system_id"], new_name),
        ).fetchone()
        if clash:
            continue
        conn.execute(
            "UPDATE stations SET name = ? WHERE id = ?",
            (new_name, old_row["id"]),
        )
    conn.commit()

    # Re-sync reference data. load_all_seeds() only runs on first init,
    # so without this an existing DB never sees stations or ships added
    # (or layouts corrected) after it was created. Both loaders are
    # idempotent — load_stations() upserts on (system_id, name),
    # sync_ships() is a declarative upsert — so this is safe every launch.
    # Partial pins: a zone_assignments row can carry a JSON description
    # of which pallets of a cargo line are pinned to which zones, so a
    # cargo line bigger than any single zone can still have part of it
    # pinned. NULL = no partial-pin data (whole-line / auto-placed).
    if not _has_column(conn, "zone_assignments", "pin_zones"):
        conn.execute("ALTER TABLE zone_assignments ADD COLUMN pin_zones TEXT")
        conn.commit()

    # User-injected route stops. Operator-forced extras between
    # contract stops (e.g. "fly to Baijini and unload"); the route
    # builder splices them in after the contract-driven stops are
    # assembled. Idempotent: CREATE-IF-NOT-EXISTS is a no-op once
    # the table has been added.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS manual_stops (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            workday_id      INTEGER NOT NULL REFERENCES workdays(id) ON DELETE CASCADE,
            station_id      INTEGER NOT NULL REFERENCES stations(id),
            after_station_id INTEGER REFERENCES stations(id),
            sort_order      INTEGER NOT NULL DEFAULT 0,
            notes           TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_manual_stops_workday "
        "ON manual_stops(workday_id)"
    )
    conn.commit()

    # Per-pallet locks: pin a single pallet of a cargo line to a
    # specific cube in a zone. Identity is (cargo_line_id,
    # pallet_index) where pallet_index is the 0-based position in the
    # deterministic palletize(...) output. Idempotent CREATE IF NOT
    # EXISTS so re-launches are a no-op.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pallet_locks (
            workday_id      INTEGER NOT NULL REFERENCES workdays(id) ON DELETE CASCADE,
            cargo_line_id   INTEGER NOT NULL REFERENCES cargo_lines(id) ON DELETE CASCADE,
            pallet_index    INTEGER NOT NULL,
            zone_label      TEXT    NOT NULL,
            cube_x          INTEGER NOT NULL,
            cube_y          INTEGER NOT NULL,
            cube_z          INTEGER NOT NULL,
            PRIMARY KEY (workday_id, cargo_line_id, pallet_index)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pallet_locks_workday "
        "ON pallet_locks(workday_id)"
    )
    conn.commit()

    # Multi-pickup candidate stations for a contract. When present,
    # the contract's cargo may be at any of these stations; the pilot
    # finds out on arrival. The planner conservatively assumes the
    # cargo is on board from the FIRST candidate visit so SCU is
    # reserved. Idempotent CREATE IF NOT EXISTS so re-launches are
    # a no-op.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS contract_pickup_candidates (
            contract_id    INTEGER NOT NULL REFERENCES contracts(id) ON DELETE CASCADE,
            station_id     INTEGER NOT NULL REFERENCES stations(id),
            sequence_order INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (contract_id, station_id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_contract_pickup_candidates_contract "
        "ON contract_pickup_candidates(contract_id)"
    )
    conn.commit()

    from .seed import load_stations, sync_ships

    load_stations(conn)
    sync_ships(conn)


def initialize_database(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Initialize the database on first launch; open existing DB otherwise.

    Returns an open connection ready for use.
    """
    conn = get_connection(db_path)

    if not _is_initialized(conn):
        schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
        conn.executescript(schema_sql)
        conn.commit()

        from .seed import load_all_seeds

        load_all_seeds(conn)
    else:
        _apply_migrations(conn)

    return conn
