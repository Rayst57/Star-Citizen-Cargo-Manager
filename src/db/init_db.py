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

    return conn
