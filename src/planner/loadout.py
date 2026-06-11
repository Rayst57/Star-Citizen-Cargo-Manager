"""
Cargo loadout snapshots — per-stop view of what's onboard.

Answers handbook question #3 at every stop:
  "What cargo is still onboard after this stop?"

The snapshot is built purely from in-memory data (route stops + cargo lines
+ zone assignments) so it can be regenerated cheaply without extra DB writes.

Returns a dict keyed by stop_number → list of LoadoutEntry objects, each
describing what's sitting in a zone at that point in the route.

A single cargo line may be split across multiple zones (overflow case), so
we emit ONE LoadoutEntry per (cargo_line, zone_assignment row). The
scu_amount field is the portion in THAT zone, computed from the row's
pallet_breakdown.
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
    scu_amount: int                 # portion of cargo line in THIS zone
    delivery_station_name: str
    pallet_breakdown: str
    is_conflicted: bool = False
    conflict_group_id: int | None = None
    # Pickup station for the originating contract — used by the 3D
    # view's hover tooltip to show the full Pickup → Destination route.
    pickup_station_name: str = ""


Snapshot = dict[int, list[LoadoutEntry]]  # stop_number → entries


def _sum_breakdown(text: str | None) -> int:
    """Sum the SCU represented by '6×8 + 1×4 + 1×2' → 54."""
    if not text:
        return 0
    total = 0
    for part in text.replace(" ", "").split("+"):
        if "×" in part or "x" in part:
            sep = "×" if "×" in part else "x"
            count_str, size_str = part.split(sep, 1)
            try:
                total += int(count_str) * int(size_str)
            except ValueError:
                continue
    return total


def build_loadout_snapshots(
    workday_id: int,
    route_stop_order: list[tuple[int, int]],  # [(stop_number, station_id), ...]
    conn: sqlite3.Connection,
) -> Snapshot:
    """Build cargo loadout snapshot for every stop."""
    # ── Cargo lines + their zone assignments (possibly multiple per line)
    line_rows = conn.execute(
        """
        SELECT cl.id            AS cl_id,
               cl.scu_amount,
               cl.delivery_station_id,
               ds.name          AS delivery_name,
               ct.pickup_station_id,
               ps.name          AS pickup_name,
               ct.contract_number,
               cm.name          AS commodity_name
        FROM cargo_lines cl
        JOIN contracts   ct ON ct.id = cl.contract_id
        JOIN stations    ds ON ds.id = cl.delivery_station_id
        JOIN stations    ps ON ps.id = ct.pickup_station_id
        JOIN commodities cm ON cm.id = cl.commodity_id
        WHERE ct.workday_id = ?
          AND ct.status != 'complete'
        """,
        (workday_id,),
    ).fetchall()

    cl_meta: dict[int, dict] = {r["cl_id"]: dict(r) for r in line_rows}

    za_rows = conn.execute(
        """
        SELECT cargo_line_id, primary_zone_label, pallet_breakdown
        FROM zone_assignments
        WHERE workday_id = ?
        """,
        (workday_id,),
    ).fetchall()

    # cargo_line_id → list of {zone_label, breakdown, scu_in_zone}
    cl_zones: dict[int, list[dict]] = {}
    for r in za_rows:
        cl_zones.setdefault(r["cargo_line_id"], []).append({
            "zone_label": r["primary_zone_label"],
            "breakdown": r["pallet_breakdown"],
            "scu_in_zone": _sum_breakdown(r["pallet_breakdown"]),
        })

    # Map pickup_station_id → [cargo_line_ids]; delivery → [cl_ids]
    pickup_to_cls: dict[int, list[int]] = {}
    delivery_to_cls: dict[int, list[int]] = {}
    for cl_id, data in cl_meta.items():
        pickup_to_cls.setdefault(data["pickup_station_id"], []).append(cl_id)
        delivery_to_cls.setdefault(data["delivery_station_id"], []).append(cl_id)

    # Conflict info
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

    # ── Simulate route: track onboard set ─────────────────────────────
    onboard: set[int] = set()
    snapshots: Snapshot = {}

    for stop_number, station_id in route_stop_order:
        # Unload first
        for cl_id in list(onboard):
            if cl_meta[cl_id]["delivery_station_id"] == station_id:
                onboard.discard(cl_id)
        # Then load
        for cl_id in pickup_to_cls.get(station_id, []):
            onboard.add(cl_id)

        # Build per-zone entries for everything currently onboard
        entries: list[LoadoutEntry] = []
        for cl_id in sorted(onboard):
            data = cl_meta[cl_id]
            grp_id = cl_conflict.get(cl_id)
            zones = cl_zones.get(cl_id) or [{
                "zone_label": "?",
                "breakdown": "",
                "scu_in_zone": data["scu_amount"],
            }]
            for z in zones:
                entries.append(LoadoutEntry(
                    zone_label=z["zone_label"],
                    cargo_line_id=cl_id,
                    contract_number=data["contract_number"],
                    commodity_name=data["commodity_name"],
                    scu_amount=z["scu_in_zone"] or data["scu_amount"],
                    delivery_station_name=data["delivery_name"],
                    pallet_breakdown=z["breakdown"] or "",
                    is_conflicted=grp_id is not None,
                    conflict_group_id=grp_id,
                    pickup_station_name=data.get("pickup_name", "") or "",
                ))

        entries.sort(key=lambda e: (e.zone_label, e.delivery_station_name))
        snapshots[stop_number] = entries

    return snapshots


def format_loadout(snapshot_entries: list[LoadoutEntry]) -> str:
    """Compact cockpit-readable loadout string for a single stop."""
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
