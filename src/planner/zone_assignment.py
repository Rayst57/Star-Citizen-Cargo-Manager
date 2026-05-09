"""
Zone assignment — places cargo lines into ship zones.

Design (handbook §10, §9):
  - Earliest-unload cargo goes into zones with the lowest unload_priority.
  - Cargo for the same destination stays together when practical.
  - Conflict cargo is kept in separate zones per contract (never merged).
  - Zone capacity is enforced via SCU totals (slot-level 3D bin packing is a
    later enhancement; physical cube_x/y/z fields in zone_assignments are
    populated with nulls for now).

Algorithm:
  1. Order delivery destinations by their stop position in the route
     (earlier stop = lower unload_priority zone preferred).
  2. Order zones by unload_priority ascending (zone unloaded first = assigned
     first-delivery cargo).
  3. For each destination's cargo lines, fill the current zone until capacity
     is exhausted, then overflow to the next zone.
  4. Conflict cargo lines receive the same zone-segregation treatment but are
     allocated to different zones than the sibling conflict group.

The function writes rows to zone_assignments and logs any over-capacity
warnings to validation_log.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from .palletizer import palletize, palletize_summary
from .conflicts import ConflictGroup


@dataclass
class ZoneState:
    zone_label: str
    bay_label: str
    scu_capacity: int
    unload_priority: int
    remaining_scu: int


def _load_zones(ship_id: int, conn: sqlite3.Connection) -> list[ZoneState]:
    rows = conn.execute(
        """
        SELECT zone_label, bay_label, scu_capacity, unload_priority
        FROM ship_zones
        WHERE ship_id = ?
        ORDER BY unload_priority ASC
        """,
        (ship_id,),
    ).fetchall()
    return [
        ZoneState(
            zone_label=r["zone_label"],
            bay_label=r["bay_label"],
            scu_capacity=r["scu_capacity"],
            unload_priority=r["unload_priority"],
            remaining_scu=r["scu_capacity"],
        )
        for r in rows
    ]


def _log_warn(workday_id: int, message: str, cargo_line_id: int | None, conn: sqlite3.Connection) -> None:
    from datetime import datetime, timezone

    conn.execute(
        """
        INSERT INTO validation_log
            (workday_id, timestamp, severity, source, message, cargo_line_id)
        VALUES (?, ?, 'WARN', 'zone_assignment', ?, ?)
        """,
        (
            workday_id,
            datetime.now(timezone.utc).isoformat(),
            message,
            cargo_line_id,
        ),
    )


def build_zone_plan(
    workday_id: int,
    route_stop_order: list[int],
    conflict_groups: list[ConflictGroup],
    conn: sqlite3.Connection,
) -> None:
    """Assign cargo lines to zones and persist to zone_assignments.

    Args:
        workday_id:       Active workday ID.
        route_stop_order: Station IDs in route order (earliest first).
                          Used to rank deliveries by urgency.
        conflict_groups:  Detected conflict groups (from conflicts.detect_conflicts).
        conn:             SQLite connection.
    """
    workday = conn.execute(
        "SELECT ship_id FROM workdays WHERE id = ?", (workday_id,)
    ).fetchone()
    if not workday:
        raise ValueError(f"Workday {workday_id} not found")

    zones = _load_zones(workday["ship_id"], conn)
    if not zones:
        _log_warn(workday_id, "No zones found for ship — cannot assign cargo.", None, conn)
        conn.commit()
        return

    # ── Clear previous non-manual assignments ────────────────────────────
    conn.execute(
        """
        DELETE FROM zone_assignments
        WHERE workday_id = ? AND is_manual_override = 0
        """,
        (workday_id,),
    )

    # ── Build delivery priority map ───────────────────────────────────────
    # delivery_station_id → position in route (lower = earlier = higher priority)
    delivery_priority: dict[int, int] = {
        sid: idx for idx, sid in enumerate(route_stop_order)
    }

    # ── Identify conflicted cargo line IDs ───────────────────────────────
    conflicted_ids: set[int] = {
        cl_id for grp in conflict_groups for cl_id in grp.cargo_line_ids
    }

    # ── Load cargo lines grouped by delivery station ──────────────────────
    rows = conn.execute(
        """
        SELECT cl.id, cl.contract_id, cl.scu_amount, cl.commodity_id,
               cl.delivery_station_id,
               ct.max_pallet_size
        FROM cargo_lines cl
        JOIN contracts ct ON ct.id = cl.contract_id
        WHERE ct.workday_id = ?
          AND ct.status != 'complete'
        ORDER BY cl.id
        """,
        (workday_id,),
    ).fetchall()

    if not rows:
        conn.commit()
        return

    # Sort cargo lines: earlier delivery first, non-conflict before conflict
    def _sort_key(r) -> tuple:
        prio = delivery_priority.get(r["delivery_station_id"], 9999)
        is_conflict = 1 if r["id"] in conflicted_ids else 0
        return (is_conflict, prio, r["id"])

    sorted_rows = sorted(rows, key=_sort_key)

    # ── Assign: walk zones in unload_priority order ───────────────────────
    # For conflict pallets, track which zone each conflict GROUP has started
    # using so we can attempt to keep each destination's conflict in a
    # different zone.
    conflict_group_zone: dict[int, str] = {}  # conflict_group_id → zone_label

    def _conflict_group_for(cl_id: int) -> int | None:
        for grp in conflict_groups:
            if cl_id in grp.cargo_line_ids:
                return grp.group_id
        return None

    def _sibling_zone(cl_id: int, grp_id: int) -> str | None:
        """Zone already claimed by another cargo_line in the same conflict group."""
        return conflict_group_zone.get(grp_id)

    # Pointer into zones list — we advance it as zones fill up
    zone_idx = 0

    for r in sorted_rows:
        cl_id = r["id"]
        scu = r["scu_amount"]
        pallets = palletize(scu, r["max_pallet_size"])
        summary = palletize_summary(pallets)
        grp_id = _conflict_group_for(cl_id)

        # For conflict lines: prefer a zone OTHER than the sibling's zone
        preferred_zone_idx = zone_idx
        if grp_id is not None:
            sibling_zone_label = _sibling_zone(cl_id, grp_id)
            if sibling_zone_label:
                # Find the next available zone that is NOT the sibling zone
                for alt_idx in range(len(zones)):
                    if (
                        zones[alt_idx].zone_label != sibling_zone_label
                        and zones[alt_idx].remaining_scu >= scu
                    ):
                        preferred_zone_idx = alt_idx
                        break

        # Find a zone with enough capacity starting from preferred_zone_idx
        assigned = False
        for search_idx in range(preferred_zone_idx, len(zones)):
            z = zones[search_idx]
            if z.remaining_scu >= scu:
                # Assign here
                conn.execute(
                    """
                    INSERT INTO zone_assignments
                        (workday_id, cargo_line_id, primary_zone_label,
                         pallet_breakdown, is_manual_override)
                    VALUES (?, ?, ?, ?, 0)
                    """,
                    (workday_id, cl_id, z.zone_label, summary),
                )
                z.remaining_scu -= scu

                if grp_id is not None and grp_id not in conflict_group_zone:
                    conflict_group_zone[grp_id] = z.zone_label

                assigned = True
                break

        if not assigned:
            # Try any zone with partial capacity (split handling)
            for z in zones:
                if z.remaining_scu > 0:
                    _log_warn(
                        workday_id,
                        f"Cargo line {cl_id} ({scu} SCU) overflows zone {z.zone_label} "
                        f"(remaining {z.remaining_scu} SCU). Partial fit — splitting not yet supported.",
                        cl_id,
                        conn,
                    )
                    # Assign anyway and mark the overflow in notes
                    conn.execute(
                        """
                        INSERT INTO zone_assignments
                            (workday_id, cargo_line_id, primary_zone_label,
                             pallet_breakdown, is_manual_override, notes)
                        VALUES (?, ?, ?, ?, 0, 'OVERFLOW — recompute or reassign')
                        """,
                        (workday_id, cl_id, z.zone_label, summary),
                    )
                    z.remaining_scu = max(0, z.remaining_scu - scu)
                    assigned = True
                    break

            if not assigned:
                _log_warn(
                    workday_id,
                    f"Cargo line {cl_id} ({scu} SCU) could not be assigned to any zone "
                    f"— ship is over capacity.",
                    cl_id,
                    conn,
                )

    conn.commit()
