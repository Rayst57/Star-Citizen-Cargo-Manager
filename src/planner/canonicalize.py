"""
Alias resolution for station and commodity names.

Matches case-insensitively against canonical names AND the aliases tables.
Returns (id, canonical_name) or raises LookupError when nothing matches.
"""

from __future__ import annotations

import sqlite3
import unicodedata


def _normalize(text: str) -> str:
    """Lowercase + collapse whitespace + strip accents."""
    nfkd = unicodedata.normalize("NFKD", text)
    ascii_text = nfkd.encode("ascii", "ignore").decode()
    return " ".join(ascii_text.lower().split())


def canonical_station(
    raw_name: str, conn: sqlite3.Connection
) -> tuple[int, str]:
    """Resolve *raw_name* to (station_id, canonical_name).

    Tries exact match first, then normalised alias match.

    Raises:
        LookupError: when no station matches.
    """
    needle = _normalize(raw_name)

    # 1. Try canonical name (normalised).
    row = conn.execute(
        "SELECT id, name FROM stations WHERE lower(name) = ?",
        (raw_name.lower(),),
    ).fetchone()
    if row:
        return row["id"], row["name"]

    # 2. Try alias table.
    row = conn.execute(
        """
        SELECT s.id, s.name
        FROM stations s
        JOIN station_aliases a ON a.station_id = s.id
        WHERE lower(a.alias) = ?
        LIMIT 1
        """,
        (raw_name.lower(),),
    ).fetchone()
    if row:
        return row["id"], row["name"]

    # 3. Try normalised fuzzy match on both name and alias.
    rows = conn.execute(
        "SELECT id, name FROM stations WHERE is_active = 1"
    ).fetchall()
    for r in rows:
        if _normalize(r["name"]) == needle:
            return r["id"], r["name"]

    alias_rows = conn.execute(
        """
        SELECT s.id, s.name, a.alias
        FROM stations s
        JOIN station_aliases a ON a.station_id = s.id
        WHERE s.is_active = 1
        """
    ).fetchall()
    for r in alias_rows:
        if _normalize(r["alias"]) == needle:
            return r["id"], r["name"]

    raise LookupError(f"Unknown station: '{raw_name}'")


def canonical_commodity(
    raw_name: str, conn: sqlite3.Connection
) -> tuple[int, str]:
    """Resolve *raw_name* to (commodity_id, canonical_name).

    Raises:
        LookupError: when no commodity matches.
    """
    needle = _normalize(raw_name)

    row = conn.execute(
        "SELECT id, name FROM commodities WHERE lower(name) = ?",
        (raw_name.lower(),),
    ).fetchone()
    if row:
        return row["id"], row["name"]

    row = conn.execute(
        """
        SELECT c.id, c.name
        FROM commodities c
        JOIN commodity_aliases a ON a.commodity_id = c.id
        WHERE lower(a.alias) = ?
        LIMIT 1
        """,
        (raw_name.lower(),),
    ).fetchone()
    if row:
        return row["id"], row["name"]

    rows = conn.execute(
        "SELECT id, name FROM commodities WHERE is_active = 1"
    ).fetchall()
    for r in rows:
        if _normalize(r["name"]) == needle:
            return r["id"], r["name"]

    alias_rows = conn.execute(
        """
        SELECT c.id, c.name, a.alias
        FROM commodities c
        JOIN commodity_aliases a ON a.commodity_id = c.id
        WHERE c.is_active = 1
        """
    ).fetchall()
    for r in alias_rows:
        if _normalize(r["alias"]) == needle:
            return r["id"], r["name"]

    raise LookupError(f"Unknown commodity: '{raw_name}'")
