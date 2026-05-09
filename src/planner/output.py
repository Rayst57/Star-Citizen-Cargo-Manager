"""
Cockpit stop-template formatter.

Generates the compact pilot-readable output for every route stop, following
the handbook §5 stop template exactly:

    Stop N: [Station] — [Action]
    ─────────────────────────────────────────
    Current Contracts at Play: …
    Unload: …
    Load: …
    Conflict Notes: …
    Current Cargo Loadout: …

Conflict Notes follow the handbook §16 / conflicts.md display rules:
  - Non-conflict unloads listed first, by zone.
  - Each conflict group handled one at a time with explicit instructions:
      · Bring up zone X only (that contract's zone).
      · Unique pallets listed first (safe to load).
      · Ambiguous pallets listed after (test these).
      · Rejected pallets → reload to sibling zone.
"""

from __future__ import annotations

import sqlite3
from collections import Counter

from .conflicts import ConflictGroup, DestConflictInfo
from .loadout import LoadoutEntry, Snapshot, format_loadout
from .palletizer import VALID_SIZES


# ── helpers ───────────────────────────────────────────────────────────────

def _divider(width: int = 45) -> str:
    return "─" * width


def _pallet_str(size_counts: dict[int, int]) -> str:
    """'6×8 + 1×4 + 1×2 + 1×1' from a {size: count} dict."""
    parts = [
        f"{size_counts[s]}×{s}"
        for s in VALID_SIZES
        if size_counts.get(s, 0) > 0
    ]
    return " + ".join(parts) if parts else "—"


def _ambig_str(sizes: list[int], counts: dict[int, int]) -> str:
    parts = [f"1×{s}" for s in sizes if counts.get(s, 0) > 0]
    return " + ".join(parts) if parts else "—"


# ── zone lookup helpers ───────────────────────────────────────────────────

def _zone_for_cargo_line(cl_id: int, workday_id: int, conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT primary_zone_label FROM zone_assignments WHERE cargo_line_id = ? AND workday_id = ?",
        (cl_id, workday_id),
    ).fetchone()
    return row["primary_zone_label"] if row else "?"


def _conflict_zone_map(grp: ConflictGroup, workday_id: int, conn: sqlite3.Connection) -> dict[int, str]:
    """Map delivery_station_id → zone_label for a conflict group."""
    result: dict[int, str] = {}
    for dest in grp.destinations:
        for cl_id in dest.cargo_line_ids:
            z = _zone_for_cargo_line(cl_id, workday_id, conn)
            if z != "?":
                result[dest.delivery_station_id] = z
                break
    return result


# ── main formatter ────────────────────────────────────────────────────────

def format_stop(
    stop_number: int,
    station_name: str,
    action: str,
    *,
    workday_id: int,
    conn: sqlite3.Connection,
    conflict_groups: list[ConflictGroup],
    snapshot_before: list[LoadoutEntry],  # cargo onboard ARRIVING at this stop
    snapshot_after: list[LoadoutEntry],   # cargo onboard LEAVING this stop
    loads: list[dict],    # [{"cargo_line_id", "scu_amount", "commodity_name", ...}]
    unloads: list[dict],
) -> str:
    """Return the full cockpit text block for one route stop."""

    lines: list[str] = []

    # ── Header ───────────────────────────────────────────────────────────
    lines.append(f"Stop {stop_number}: {station_name} — {action}")
    lines.append(_divider())

    # ── Contracts at play ─────────────────────────────────────────────────
    # All contracts that have any cargo in transit at this stop (before unload)
    active_contracts: dict[int, str] = {}
    for entry in snapshot_before:
        key = entry.contract_number
        if key not in active_contracts:
            active_contracts[key] = entry.delivery_station_name
    for entry in snapshot_after:
        key = entry.contract_number
        if key not in active_contracts:
            active_contracts[key] = entry.delivery_station_name

    lines.append("Current Contracts at Play:")
    if active_contracts:
        for cn in sorted(active_contracts):
            lines.append(f"  - Contract {cn} → {active_contracts[cn]}")
    else:
        lines.append("  - None")

    # ── Unload ────────────────────────────────────────────────────────────
    lines.append("")
    lines.append("Unload:")
    if unloads:
        # Build conflict_cl_ids set for this stop
        conflict_cl_ids: set[int] = set()
        for grp in conflict_groups:
            if any(d.delivery_station_id == _station_id_from_unloads(unloads, conn)
                   for d in grp.destinations):
                conflict_cl_ids.update(grp.cargo_line_ids)

        for u in unloads:
            z = _zone_for_cargo_line(u["cargo_line_id"], workday_id, conn)
            tag = "  ⚠ CONFLICT" if u["cargo_line_id"] in conflict_cl_ids else ""
            lines.append(
                f"  {z} → {u['scu_amount']} SCU {u['commodity_name']} "
                f"→ {u['delivery_name']}  [Contract {u['contract_number']}]{tag}"
            )
    else:
        lines.append("  None")

    # ── Load ──────────────────────────────────────────────────────────────
    lines.append("")
    lines.append("Load:")
    if loads:
        for ld in loads:
            z = _zone_for_cargo_line(ld["cargo_line_id"], workday_id, conn)
            # check if this cargo line is in any conflict group
            in_conflict = any(
                ld["cargo_line_id"] in grp.cargo_line_ids for grp in conflict_groups
            )
            tag = "  ⚠ CONFLICT" if in_conflict else ""
            lines.append(
                f"  {z} → {ld['scu_amount']} SCU {ld['commodity_name']} "
                f"→ {ld['delivery_name']}  [Contract {ld['contract_number']}]{tag}"
            )
    else:
        lines.append("  None")

    # ── Conflict Notes ────────────────────────────────────────────────────
    lines.append("")
    lines.append("Conflict Notes:")

    # Collect conflict groups relevant to this stop
    # A group is relevant if this stop is a pickup OR delivery for any of its cargo lines
    stop_cl_ids = {u["cargo_line_id"] for u in unloads} | {ld["cargo_line_id"] for ld in loads}
    relevant_groups = [
        grp for grp in conflict_groups
        if any(cl_id in stop_cl_ids for cl_id in grp.cargo_line_ids)
    ]

    if not relevant_groups:
        lines.append("  None")
    else:
        for grp in relevant_groups:
            zone_map = _conflict_zone_map(grp, workday_id, conn)
            lines.append(
                f"  ⚠ GROUP {grp.group_id} — {grp.pickup_station_name} × {grp.commodity_name}"
            )

            # Determine if this is a pickup stop or delivery stop for this group
            is_pickup = any(
                ld["cargo_line_id"] in grp.cargo_line_ids for ld in loads
            )
            is_delivery = any(
                u["cargo_line_id"] in grp.cargo_line_ids for u in unloads
            )

            if is_pickup:
                # At pickup: show zone assignments and warn about ambiguous sizes
                for dest in grp.destinations:
                    z = zone_map.get(dest.delivery_station_id, "?")
                    unique_str = _pallet_str({s: dest.pallet_counts[s] for s in dest.unique_sizes if s in dest.pallet_counts})
                    ambig_str = _pallet_str({s: dest.pallet_counts[s] for s in dest.ambiguous_sizes if s in dest.pallet_counts})
                    lines.append(f"    {dest.delivery_station_name} → zone {z}")
                    if unique_str != "—":
                        lines.append(f"      Unique (safe): {unique_str} SCU {dest.commodity_name}")
                    if ambig_str != "—":
                        lines.append(f"      Ambiguous ⚠:   {ambig_str} SCU {dest.commodity_name}")
                lines.append(f"    Load each destination into its assigned zone separately.")
                lines.append(f"    Keep zone assignments clean — do not mix zones at pickup.")

            if is_delivery:
                # At delivery: give step-by-step resolution instructions
                # Find which destination(s) are being delivered here
                delivery_here = [
                    d for d in grp.destinations
                    if any(u["cargo_line_id"] in d.cargo_line_ids for u in unloads)
                ]
                sibling_dests = [d for d in grp.destinations if d not in delivery_here]

                for dest in delivery_here:
                    z_here = zone_map.get(dest.delivery_station_id, "?")
                    ambig_str = _pallet_str(
                        {s: dest.pallet_counts[s] for s in dest.ambiguous_sizes if s in dest.pallet_counts}
                    )
                    unique_str = _pallet_str(
                        {s: dest.pallet_counts[s] for s in dest.unique_sizes if s in dest.pallet_counts}
                    )
                    lines.append(
                        f"    Delivering Contract cargo → {dest.delivery_station_name}"
                    )
                    lines.append(f"    → Bring up zone {z_here} only.")
                    if unique_str != "—":
                        lines.append(f"       Unique (deliver confidently): {unique_str} SCU")
                    lines.append(f"       Ambiguous (test these last): {ambig_str} SCU")
                    if sibling_dests:
                        for sib in sibling_dests:
                            z_sib = zone_map.get(sib.delivery_station_id, "?")
                            lines.append(
                                f"       If station REJECTS a pallet → it belongs to "
                                f"{sib.delivery_station_name}. Reload to zone {z_sib}."
                            )

            if not is_pickup and not is_delivery:
                lines.append(f"    [In transit — conflict ongoing]")

    # ── Current Cargo Loadout (after this stop) ───────────────────────────
    lines.append("")
    lines.append("Current Cargo Loadout:")
    lines.append(format_loadout(snapshot_after) if snapshot_after else "  Empty")

    lines.append("")
    return "\n".join(lines)


def format_full_route(
    workday_id: int,
    conn: sqlite3.Connection,
    conflict_groups: list[ConflictGroup],
    snapshots: Snapshot,
    route_stops,        # list[RouteStop] from route.py
) -> str:
    """Format all stops in the route as one text block."""
    blocks: list[str] = []

    for stop in route_stops:
        # Build loads/unloads dicts with enriched data
        loads_data = _enrich_cargo_refs(stop.loads, conn)
        unloads_data = _enrich_cargo_refs(stop.unloads, conn)

        snapshot_before = snapshots.get(stop.stop_number - 1, [])
        snapshot_after = snapshots.get(stop.stop_number, [])

        block = format_stop(
            stop.stop_number,
            stop.station_name,
            stop.action,
            workday_id=workday_id,
            conn=conn,
            conflict_groups=conflict_groups,
            snapshot_before=snapshot_before,
            snapshot_after=snapshot_after,
            loads=loads_data,
            unloads=unloads_data,
        )
        blocks.append(block)

    return ("\n" + "═" * 45 + "\n").join(blocks)


def _enrich_cargo_refs(refs, conn: sqlite3.Connection) -> list[dict]:
    """Convert CargoLineRef objects → enriched dicts with delivery_name."""
    result = []
    for r in refs:
        row = conn.execute(
            """
            SELECT s.name AS delivery_name
            FROM cargo_lines cl
            JOIN stations s ON s.id = cl.delivery_station_id
            WHERE cl.id = ?
            """,
            (r.cargo_line_id,),
        ).fetchone()
        result.append({
            "cargo_line_id": r.cargo_line_id,
            "contract_number": r.contract_number,
            "scu_amount": r.scu_amount,
            "commodity_name": r.commodity_name,
            "delivery_name": row["delivery_name"] if row else "?",
        })
    return result


def _station_id_from_unloads(unloads: list[dict], conn: sqlite3.Connection) -> int | None:
    """Get the delivery station_id that matches this stop's unloads."""
    if not unloads:
        return None
    row = conn.execute(
        "SELECT delivery_station_id FROM cargo_lines WHERE id = ?",
        (unloads[0]["cargo_line_id"],),
    ).fetchone()
    return row["delivery_station_id"] if row else None
