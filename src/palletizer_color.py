"""
Auto-assign destination colors when they first appear in a workday.

Stations get a color from a curated palette and persist it to
stations.color_hex so the same destination keeps the same color across
workdays.

Per ui_interactions.md §"Color assignment".
"""

from __future__ import annotations

import sqlite3


# Curated palette — anchored on theme/colors.json brand colors then
# extended with high-contrast adjacent hues so 8+ destinations stay
# distinguishable.
PALETTE: list[str] = [
    "#ffbe20",  # accent yellow (anchor)
    "#00498f",  # primary blue (anchor)
    "#deb447",  # muted gold
    "#7bbf3f",  # green
    "#e74c3c",  # red
    "#9b59b6",  # purple
    "#1abc9c",  # teal
    "#f39c12",  # orange
    "#3498db",  # sky blue
    "#e91e63",  # pink
    "#16a085",  # dark teal
    "#d35400",  # burnt orange
]


def assign_destination_colors(workday_id: int, conn: sqlite3.Connection) -> None:
    """Ensure every delivery destination in *workday_id* has a color."""
    used_rows = conn.execute(
        "SELECT color_hex FROM stations WHERE color_hex IS NOT NULL"
    ).fetchall()
    used = {r["color_hex"].lower() for r in used_rows}

    needs_color = conn.execute(
        """
        SELECT DISTINCT s.id, s.name
        FROM stations s
        JOIN cargo_lines cl ON cl.delivery_station_id = s.id
        JOIN contracts ct ON ct.id = cl.contract_id
        WHERE ct.workday_id = ?
          AND (s.color_hex IS NULL OR s.color_hex = '')
        ORDER BY s.id
        """,
        (workday_id,),
    ).fetchall()

    for st in needs_color:
        # Pick the first unused color, or wrap around if all are used
        chosen = None
        for c in PALETTE:
            if c.lower() not in used:
                chosen = c
                break
        if chosen is None:
            chosen = PALETTE[st["id"] % len(PALETTE)]
        conn.execute(
            "UPDATE stations SET color_hex = ? WHERE id = ?", (chosen, st["id"])
        )
        used.add(chosen.lower())

    conn.commit()
