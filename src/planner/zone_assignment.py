"""
Zone assignment — places cargo lines into ship zones.

Design (handbook §10, §9):
  - One destination per zone whenever capacity allows. Mixed-destination zones
    are a LAST RESORT for overflow only — the UI will flag them as a warning
    state requiring user acknowledgement.
  - Earliest-unload cargo goes into zones with the lowest unload_priority.
  - Cargo for the same destination stays together when practical.
  - Conflict cargo is kept in separate zones per contract (never merged).
  - Conflict pallets land at low-Y (ramp side) within their zone for fast
    deconfliction at unload.
  - Zone capacity is enforced via SCU totals (slot-level 3D bin packing is a
    later enhancement; physical cube_x/y/z fields in zone_assignments are
    populated with nulls for now).

Algorithm:
  1. Group cargo lines by delivery destination.
  2. Order destinations by their stop position in the route (earliest = first).
  3. Order zones by unload_priority ascending.
  4. For each destination, claim the next free zone with enough capacity.
     If the destination's total SCU exceeds one zone, overflow to additional
     zones until placed.
  5. Conflict cargo is always allocated to a fresh zone (no overflow shared
     with other destinations) and gets sibling-zone separation.
  6. Mixed-destination zones occur only when no fresh zone can fit the
     remaining cargo — the planner logs a WARN.

The function writes rows to zone_assignments and logs warnings to
validation_log.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

from .palletizer import palletize, palletize_summary
from .conflicts import ConflictGroup


_log = logging.getLogger("cargo_manager")


@dataclass
class ZoneState:
    zone_label: str
    bay_label: str
    scu_capacity: int
    unload_priority: int
    remaining_scu: int
    # delivery_station_ids whose cargo currently occupies this zone
    occupants: set[int]


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
            occupants=set(),
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
    """Assign cargo lines to zones and persist to zone_assignments."""
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

    # ── Delivery priority map ────────────────────────────────────────────
    delivery_priority: dict[int, int] = {
        sid: idx for idx, sid in enumerate(route_stop_order)
    }

    # ── Identify conflicted cargo lines + their group ────────────────────
    conflicted_ids: set[int] = {
        cl_id for grp in conflict_groups for cl_id in grp.cargo_line_ids
    }
    cl_to_group: dict[int, int] = {}
    for grp in conflict_groups:
        for cl_id in grp.cargo_line_ids:
            cl_to_group[cl_id] = grp.group_id

    # ── Load cargo lines ─────────────────────────────────────────────────
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

    # ── Group by delivery destination ────────────────────────────────────
    by_destination: dict[int, list[dict]] = {}
    for r in rows:
        by_destination.setdefault(r["delivery_station_id"], []).append({
            "cargo_line_id": r["id"],
            "scu_amount": r["scu_amount"],
            "max_pallet_size": r["max_pallet_size"],
            "is_conflicted": r["id"] in conflicted_ids,
            "group_id": cl_to_group.get(r["id"]),
        })

    # Sort destinations by route position (earliest first = highest priority)
    ordered_dests = sorted(
        by_destination.keys(),
        key=lambda did: delivery_priority.get(did, 9999),
    )

    # Track which zone each conflict group is using (so siblings differ)
    conflict_group_zone: dict[int, str] = {}

    _log.info(
        "zone_assignment: %d destinations, ordered=%s",
        len(ordered_dests), ordered_dests,
    )

    # ── Assign each destination to one (or more) zones ───────────────────
    for did in ordered_dests:
        cargo = by_destination[did]
        total_dest_scu = sum(c["scu_amount"] for c in cargo)
        is_dest_conflicted = any(c["is_conflicted"] for c in cargo)

        # Build sibling-zone exclusion set (for conflict cargo)
        excluded_zones: set[str] = set()
        if is_dest_conflicted:
            grp_ids = {c["group_id"] for c in cargo if c["group_id"] is not None}
            for gid in grp_ids:
                if gid in conflict_group_zone:
                    excluded_zones.add(conflict_group_zone[gid])

        # First-fit decreasing — biggest cargo lines claim zones first so
        # small ones top off leftover space instead of opening fresh zones
        # (Baijini's 9 lines used to leak a 20 SCU tail into F3, blocking
        # Port Tressler's 35 SCU from getting its own zone).
        cargo.sort(key=lambda c: -c["scu_amount"])

        target_zone = _claim_fresh_zone(
            zones, total_dest_scu, excluded_zones=excluded_zones
        )

        if target_zone is not None:
            _log.info(
                "  dest %d: %d SCU → fresh zone %s "
                "(conflict=%s, excluded=%s)",
                did, total_dest_scu, target_zone.zone_label,
                is_dest_conflicted, sorted(excluded_zones),
            )
            _place_cargo_in_zone(
                workday_id, target_zone, did, cargo, conn,
                conflict_group_zone,
            )
            continue
        _log.info(
            "  dest %d: %d SCU → no fresh zone fits, trying mixed",
            did, total_dest_scu,
        )

        # Fall back: try any zone with enough headroom — might mix destinations
        # but stays single-zone for THIS destination.
        target_zone = _claim_zone_with_capacity(
            zones, total_dest_scu, excluded_zones=excluded_zones,
            allow_mixed=True,
        )
        if target_zone is not None:
            _log_warn(
                workday_id,
                f"Destination {did} ({total_dest_scu} SCU) shares zone "
                f"{target_zone.zone_label} with other destinations (overflow mixed).",
                None, conn,
            )
            _place_cargo_in_zone(
                workday_id, target_zone, did, cargo, conn,
                conflict_group_zone,
            )
            continue

        # Last resort: split across multiple zones.
        _place_cargo_with_overflow(
            workday_id, zones, did, cargo, conn,
            excluded_zones, conflict_group_zone,
        )

    conn.commit()


# ── helpers ──────────────────────────────────────────────────────────────

def _claim_fresh_zone(
    zones: list[ZoneState],
    needed_scu: int,
    *,
    excluded_zones: set[str],
) -> ZoneState | None:
    """Return the first empty zone with capacity >= needed_scu, or None."""
    for z in zones:
        if z.zone_label in excluded_zones:
            continue
        if z.occupants:
            continue
        if z.scu_capacity >= needed_scu:
            return z
    return None


def _claim_zone_with_capacity(
    zones: list[ZoneState],
    needed_scu: int,
    *,
    excluded_zones: set[str],
    allow_mixed: bool,
) -> ZoneState | None:
    """Return any zone with at least *needed_scu* free, fresh-first."""
    # Prefer empty zones first
    for z in zones:
        if z.zone_label in excluded_zones:
            continue
        if not z.occupants and z.remaining_scu >= needed_scu:
            return z
    if not allow_mixed:
        return None
    # Then accept zones with existing occupants
    for z in zones:
        if z.zone_label in excluded_zones:
            continue
        if z.remaining_scu >= needed_scu:
            return z
    return None


def _place_cargo_in_zone(
    workday_id: int,
    zone: ZoneState,
    delivery_station_id: int,
    cargo: list[dict],
    conn: sqlite3.Connection,
    conflict_group_zone: dict[int, str],
) -> None:
    """Insert zone_assignment rows for *cargo* into *zone*."""
    for c in cargo:
        pallets = palletize(c["scu_amount"], c["max_pallet_size"])
        summary = palletize_summary(pallets)
        conn.execute(
            """
            INSERT INTO zone_assignments
                (workday_id, cargo_line_id, primary_zone_label,
                 pallet_breakdown, is_manual_override)
            VALUES (?, ?, ?, ?, 0)
            """,
            (workday_id, c["cargo_line_id"], zone.zone_label, summary),
        )
        zone.remaining_scu -= c["scu_amount"]
        if c["group_id"] is not None and c["group_id"] not in conflict_group_zone:
            conflict_group_zone[c["group_id"]] = zone.zone_label
    zone.occupants.add(delivery_station_id)


def _place_cargo_with_overflow(
    workday_id: int,
    zones: list[ZoneState],
    delivery_station_id: int,
    cargo: list[dict],
    conn: sqlite3.Connection,
    excluded_zones: set[str],
    conflict_group_zone: dict[int, str],
) -> None:
    """Place cargo across multiple zones when no single zone fits it all.

    Per-cargo-line greedy fill, with the sort order tuned to MAXIMISE
    consolidation:
      1. Continue filling a zone this destination already uses
         (smallest-remaining first so we top it off before opening another)
      2. A fresh zone (no occupants)
      3. A zone occupied by ANOTHER destination — last resort, this is
         the only path that introduces a mixed-destination zone

    The previous sort preferred "fresh zones with largest free space"
    every iteration, which sprayed each cargo line into a different
    fresh zone and depleted the fresh pool prematurely. That left
    later destinations no choice but to mix.
    """
    def _rank(z: ZoneState) -> tuple[int, int]:
        if delivery_station_id in z.occupants:
            # Same destination — fill this zone first; smaller remaining
            # wins so we top it off rather than choose its sibling.
            return (0, z.remaining_scu)
        if not z.occupants:
            # Fresh zone — start a new home for this destination.
            # Largest first so we don't block big future cargo.
            return (1, -z.remaining_scu)
        # Other destination's zone — only mix as last resort.
        return (2, -z.remaining_scu)

    for c in cargo:
        scu = c["scu_amount"]
        pallets = palletize(scu, c["max_pallet_size"])

        # Single-zone fit (this whole cargo line into one zone) — preferred.
        candidates = [
            z for z in zones
            if z.zone_label not in excluded_zones and z.remaining_scu >= scu
        ]
        candidates.sort(key=_rank)
        if candidates:
            target = candidates[0]
            summary = palletize_summary(pallets)
            conn.execute(
                """
                INSERT INTO zone_assignments
                    (workday_id, cargo_line_id, primary_zone_label,
                     pallet_breakdown, is_manual_override, notes)
                VALUES (?, ?, ?, ?, 0, 'OVERFLOW — single zone')
                """,
                (workday_id, c["cargo_line_id"], target.zone_label, summary),
            )
            target.remaining_scu -= scu
            target.occupants.add(delivery_station_id)
            if c["group_id"] is not None and c["group_id"] not in conflict_group_zone:
                conflict_group_zone[c["group_id"]] = target.zone_label
            continue

        # Split across zones — fill ONE zone to its max before moving on.
        # Same destination-aware ranking: continue our own zones first,
        # then fresh, then mix.
        avail = sorted(
            [z for z in zones
             if z.zone_label not in excluded_zones and z.remaining_scu > 0],
            key=_rank,
        )
        zone_pallets: dict[str, list[int]] = {}
        unplaced: list[int] = []
        remaining = list(pallets)        # largest-first ordering preserved
        zi = 0
        while remaining and zi < len(avail):
            z = avail[zi]
            took_any = False
            kept: list[int] = []
            for size in remaining:
                if z.remaining_scu >= size:
                    zone_pallets.setdefault(z.zone_label, []).append(size)
                    z.remaining_scu -= size
                    z.occupants.add(delivery_station_id)
                    took_any = True
                else:
                    kept.append(size)
            remaining = kept
            if not took_any:
                # Nothing fit in this zone; advance.
                zi += 1
            elif z.remaining_scu == 0:
                # Filled to capacity, move on.
                zi += 1
            # If we DID place but the zone still has room and there are
            # still pallets, we've already tried fitting them in the
            # outer for-loop (size > remaining_scu blocked them), so
            # advance.
            else:
                zi += 1
        unplaced.extend(remaining)

        if unplaced:
            _log_warn(
                workday_id,
                f"Cargo line {c['cargo_line_id']} could not place "
                f"{sum(unplaced)} SCU ({len(unplaced)} pallet(s)) — "
                f"ship is over capacity.",
                c["cargo_line_id"], conn,
            )

        # Persist a row per zone receiving a portion
        first_zone = None
        for zone_label, sizes in zone_pallets.items():
            sizes.sort(reverse=True)
            summary = palletize_summary(sizes)
            conn.execute(
                """
                INSERT INTO zone_assignments
                    (workday_id, cargo_line_id, primary_zone_label,
                     pallet_breakdown, is_manual_override, notes)
                VALUES (?, ?, ?, ?, 0, 'OVERFLOW — split across zones')
                """,
                (workday_id, c["cargo_line_id"], zone_label, summary),
            )
            if first_zone is None:
                first_zone = zone_label

        if (c["group_id"] is not None
                and c["group_id"] not in conflict_group_zone
                and first_zone is not None):
            conflict_group_zone[c["group_id"]] = first_zone

        if zone_pallets:
            _log_warn(
                workday_id,
                f"Cargo line {c['cargo_line_id']} ({scu} SCU) split across "
                f"{', '.join(zone_pallets.keys())}.",
                c["cargo_line_id"], conn,
            )
