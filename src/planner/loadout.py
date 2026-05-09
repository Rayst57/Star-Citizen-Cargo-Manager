"""
Cargo loadout snapshots — per-stop view of what's onboard.

Answers handbook question #3 at every stop:
  "What cargo is still onboard after this stop?"

The snapshot is built purely from in-memory data (route stops + cargo lines
+ zone assignments) so it can be regenerated cheaply without extra DB writes.

Returns a dict keyed by stop_number → list of LoadoutEntry objects, each
describing what's sitting in a zone at that point in the route.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass
class LoadoutEntry:
    zone_label: str
    cargo_line_id: int
    contract_number: int
    commodity_name: str
    scu_amount: int
    delivery_station_name: str
    pallet_breakdown: str
    is_conflicted: bool = False
    conflict_group_id: int | None = None


Snapshot = dict[int, list[LoadoutEntry]]  # stop_number → entries


def build_loadout_snapshots(
    workday_id: int,
    route_stop_order: list[tuple[int, int]],  # [(stop_number, station_id), ...]
    conn: sqlite3.Connection,
) -> Snapshot:
    """Build cargo loadout snapshot for every stop.

    Args:
        workday_id:        Active workday.
        route_stop_order:  Ordered list of (stop_number, station_id) tuples.
        conn:              SQLite connection.

    Returns:
        Snapshot dict: stop_number → sorted list of LoadoutEntry objects.
    """
    # ── Load cargo lines with zone assignments ────────────────────────────
    rows = conn.execute(
        """
        SELECT cl.id            AS cl_id,
               cl.scu_amount,
               cl.delivery_station_id,
               ds.name          AS delivery_name,
               ct.pickup_station_id,
               ct.contract_number,
               cm.name          AS commodity_name,
               za.primary_zone_label,
               za.pallet_breakdown
        FROM cargo_lines cl
        JOIN contracts   ct ON ct.id = cl.contract_id
        JOIN stations    ds ON ds.id = cl.delivery_station_id
        JOIN commodities cm ON cm.id = cl.commodity_id
        LEFT JOIN zone_assignments za
               ON za.cargo_line_id = cl.id
              AND za.workday_id = ?
        WHERE ct.workday_id = ?
          AND ct.status != 'complete'
        """,
        (workday_id, workday_id),
    ).fetchall()

    # Map cargo_line_id → row data
    cl_data: dict[int, dict] = {}
    for r in rows:
        cl_data[r["cl_id"]] = dict(r)

    # Map pickup_station_id → [cargo_line_ids]
    pickup_to_cls: dict[int, list[int]] = {}
    for cl_id, data in cl_data.items():
        pid = data["pickup_station_id"]
        pickup_to_cls.setdefault(pid, []).append(cl_id)

    # Map delivery_station_id → [cargo_line_ids]
    delivery_to_cls: dict[int, list[int]] = {}
    for cl_id, data in cl_data.items():
        did = data["delivery_station_id"]
        delivery_to_cls.setdefault(did, []).append(cl_id)

    # ── Load conflict info ────────────────────────────────────────────────
    conflict_rows = conn.execute(
        """
        SELECT cargo_line_id, conflict_group_id
        FROM pallet_conflicts
        WHERE workday_id = ?
        """,
        (workday_id,),
    ).fetchall()
    cl_conflict: dict[int, int] = {
        r["cargo_line_id"]: r["conflict_group_id"] for r in conflict_rows
    }

    # ── Simulate route: track onboard set ────────────────────────────────
    onboard: set[int] = set()  # cargo_line_ids currently onboard
    snapshots: Snapshot = {}

    for stop_number, station_id in route_stop_order:
        # Unload: remove cargo lines delivered at this station
        for cl_id in list(onboard):
            if cl_data[cl_id]["delivery_station_id"] == station_id:
                onboard.discard(cl_id)

        # Load: add cargo lines picked up at this station
        for cl_id in pickup_to_cls.get(station_id, []):
            onboard.add(cl_id)

        # Build snapshot for this stop
        entries: list[LoadoutEntry] = []
        for cl_id in sorted(onboard):
            data = cl_data[cl_id]
            grp_id = cl_conflict.get(cl_id)
            entries.append(
                LoadoutEntry(
                    zone_label=data["primary_zone_label"] or "?",
                    cargo_line_id=cl_id,
                    contract_number=data["contract_number"],
                    commodity_name=data["commodity_name"],
                    scu_amount=data["scu_amount"],
                    delivery_station_name=data["delivery_name"],
                    pallet_breakdown=data["pallet_breakdown"] or "",
                    is_conflicted=grp_id is not None,
                    conflict_group_id=grp_id,
                )
            )

        # Sort by zone_label then delivery station for cockpit readability
        entries.sort(key=lambda e: (e.zone_label, e.delivery_station_name))
        snapshots[stop_number] = entries

    return snapshots


def format_loadout(snapshot_entries: list[LoadoutEntry]) -> str:
    """Compact cockpit-readable loadout string for a single stop.

    Example output:
        F1: 55 SCU Tungsten → Everus Harbor
        R1: 27 SCU Aluminum → Baijini Point  [CONFLICT]
    """
    if not snapshot_entries:
        return "  Empty"

    lines = []
    for e in snapshot_entries:
        conflict_tag = "  [CONFLICT]" if e.is_conflicted else ""
        lines.append(
            f"  {e.zone_label}: {e.scu_amount} SCU {e.commodity_name} "
            f"→ {e.delivery_station_name}{conflict_tag}"
        )
    return "\n".join(lines)
