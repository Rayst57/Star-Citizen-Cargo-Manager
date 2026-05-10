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
    # Round-robin routes have the origin at BOTH stop 1 (depart) and the
    # final stop. We want the FIRST occurrence (= when the destination's
    # cargo is first picked up / unloaded), so build the map only from
    # not-yet-seen station ids.
    delivery_priority: dict[int, int] = {}
    for idx, sid in enumerate(route_stop_order):
        if sid not in delivery_priority:
            delivery_priority[sid] = idx

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

    # Sort destinations by TOTAL SCU descending. Big destinations claim
    # zones first so their cargo stays contiguous; small ones top off
    # what's left. delivery_priority is the tiebreaker — earliest-route
    # ties so neighbours' cargo ends up in adjacent zones.
    #
    # Why not earliest-route first (the prior strategy)? With that order,
    # large round-robin destinations like the origin (Seraphim is dest 1
    # AND the final unload at the end of a round-robin) ended up
    # processed last, after every fresh zone was claimed by smaller
    # destinations — and had to fragment across 5+ leftover zones. Sorting
    # by size keeps Baijini's 236 SCU and Seraphim's 195 SCU each in two
    # adjacent R-bay zones instead.
    def _dest_total_scu(did: int) -> int:
        return sum(c["scu_amount"] for c in by_destination[did])

    ordered_dests = sorted(
        by_destination.keys(),
        key=lambda did: (-_dest_total_scu(did),
                         delivery_priority.get(did, 9999)),
    )

    # Resolve station names once for readable logging.
    station_names: dict[int, str] = {
        r["id"]: r["name"]
        for r in conn.execute(
            "SELECT id, name FROM stations WHERE id IN (%s)"
            % ",".join("?" * len(ordered_dests)),
            list(ordered_dests),
        ).fetchall()
    } if ordered_dests else {}

    # Track every zone each conflict group's cargo lands in. We need the
    # full set (not just the first zone) so a destination's conflict
    # cargo avoids ALL of its partner's zones, not just the partner's
    # initial one. Earlier versions stored a single zone per group, which
    # let conflict pallets end up in the same zone as their twin once the
    # partner overflowed.
    conflict_group_zones: dict[int, set[str]] = {}
    # Which station's cargo lives in each zone for each group — lets us
    # explain WHY a mix is happening (conflict-induced vs capacity-induced).
    conflict_group_stations: dict[int, dict[str, set[int]]] = {}

    _log.info(
        "zone_assignment: %d destinations (size-descending order):",
        len(ordered_dests),
    )
    for did in ordered_dests:
        name = station_names.get(did, f"station {did}")
        _log.info(
            "  dest %d %s — %d SCU (route_priority=%d)",
            did, name, _dest_total_scu(did),
            delivery_priority.get(did, 9999),
        )

    # ── Assign each destination to one (or more) zones ───────────────────
    for did in ordered_dests:
        cargo = by_destination[did]
        total_dest_scu = sum(c["scu_amount"] for c in cargo)
        is_dest_conflicted = any(c["is_conflicted"] for c in cargo)
        dest_name = station_names.get(did, str(did))

        # Build sibling-zone exclusion set: every zone a conflict partner
        # of this destination has cargo in (across every group we share).
        excluded_zones: set[str] = set()
        excluded_reason: dict[str, list[tuple[int, int]]] = {}  # zone → [(group_id, partner_did)]
        if is_dest_conflicted:
            grp_ids = {c["group_id"] for c in cargo if c["group_id"] is not None}
            for gid in grp_ids:
                for zone_label in conflict_group_zones.get(gid, set()):
                    partner_dids = sorted(
                        sid for sid in conflict_group_stations
                            .get(gid, {})
                            .get(zone_label, set())
                        if sid != did
                    )
                    if not partner_dids:
                        continue
                    excluded_zones.add(zone_label)
                    excluded_reason.setdefault(zone_label, []).extend(
                        (gid, sid) for sid in partner_dids
                    )

        # First-fit decreasing — biggest cargo lines claim zones first so
        # small ones top off leftover space instead of opening fresh zones.
        cargo.sort(key=lambda c: -c["scu_amount"])

        target_zone = _claim_fresh_zone(
            zones, total_dest_scu, excluded_zones=excluded_zones
        )

        if target_zone is not None:
            _log.info(
                "  dest %d %s: %d SCU → fresh zone %s "
                "(conflict=%s, excluded=%s)",
                did, dest_name, total_dest_scu, target_zone.zone_label,
                is_dest_conflicted, sorted(excluded_zones),
            )
            _place_cargo_in_zone(
                workday_id, target_zone, did, cargo, conn,
                conflict_group_zones, conflict_group_stations,
            )
            continue

        # Fall back: try any zone with enough headroom — might mix destinations
        # but stays single-zone for THIS destination.
        target_zone = _claim_zone_with_capacity(
            zones, total_dest_scu, excluded_zones=excluded_zones,
            allow_mixed=True,
        )
        if target_zone is not None:
            mix_note: str | None = None
            if target_zone.occupants:
                # Why we're mixing: at this destination's turn, no fresh
                # zone had enough headroom; the existing occupants block a
                # clean home but this zone still fits the whole load.
                others = sorted(target_zone.occupants)
                conflict_partners = _conflict_partners_in_zone(
                    target_zone.zone_label, did, cargo,
                    conflict_group_zones, conflict_group_stations,
                )
                reason = _mix_reason(
                    excluded_zones, target_zone, conflict_partners,
                )
                mix_note = (
                    f"MIXED — sharing {target_zone.zone_label} with "
                    f"destination(s) {others} ({reason})"
                )
                _log.info(
                    "  dest %d %s: %d SCU → MIXED into %s with %s — %s",
                    did, dest_name, total_dest_scu,
                    target_zone.zone_label, others, reason,
                )
                _log_warn(
                    workday_id,
                    f"Destination {did} ({total_dest_scu} SCU) shares zone "
                    f"{target_zone.zone_label} with destination(s) {others} "
                    f"— {reason}.",
                    None, conn,
                )
            else:
                _log.info(
                    "  dest %d %s: %d SCU → claimed %s "
                    "(still fresh, no fresh-with-headroom path)",
                    did, dest_name, total_dest_scu, target_zone.zone_label,
                )
            _place_cargo_in_zone(
                workday_id, target_zone, did, cargo, conn,
                conflict_group_zones, conflict_group_stations,
                notes=mix_note,
            )
            continue

        _log.info(
            "  dest %d %s: %d SCU → no single zone fits, falling to overflow "
            "(excluded=%s)",
            did, dest_name, total_dest_scu, sorted(excluded_zones),
        )
        # Last resort: split across multiple zones.
        _place_cargo_with_overflow(
            workday_id, zones, did, cargo, conn,
            excluded_zones, conflict_group_zones, conflict_group_stations,
            excluded_reason=excluded_reason,
        )

    conn.commit()


def _conflict_partners_in_zone(
    zone_label: str,
    delivery_station_id: int,
    cargo: list[dict],
    conflict_group_zones: dict[int, set[str]],
    conflict_group_stations: dict[int, dict[str, set[int]]],
) -> list[tuple[int, int]]:
    """Return [(group_id, partner_did), ...] for partners sharing this zone."""
    partners: list[tuple[int, int]] = []
    grp_ids = {c["group_id"] for c in cargo if c["group_id"] is not None}
    for gid in grp_ids:
        if zone_label not in conflict_group_zones.get(gid, set()):
            continue
        for sid in conflict_group_stations.get(gid, {}).get(zone_label, set()):
            if sid != delivery_station_id:
                partners.append((gid, sid))
    return partners


def _mix_reason(
    excluded_zones: set[str],
    target_zone: ZoneState,
    conflict_partners: list[tuple[int, int]],
) -> str:
    """Plain-language reason a mixed placement happened."""
    if conflict_partners:
        # Shouldn't normally happen — exclusion should have blocked this —
        # but flag it loudly if it does so we can debug.
        partner_dids = sorted({sid for _, sid in conflict_partners})
        return (
            f"WARNING — sharing with conflict partner(s) {partner_dids}; "
            f"exclusion failed"
        )
    if excluded_zones:
        return (
            "no fresh zone fits the full load AND conflict-zone exclusion "
            f"({sorted(excluded_zones)}) ruled out additional fresh options"
        )
    return "no fresh zone fits the full load"


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
    conflict_group_zones: dict[int, set[str]],
    conflict_group_stations: dict[int, dict[str, set[int]]],
    notes: str | None = None,
) -> None:
    """Insert zone_assignment rows for *cargo* into *zone*."""
    for c in cargo:
        pallets = palletize(c["scu_amount"], c["max_pallet_size"])
        summary = palletize_summary(pallets)
        conn.execute(
            """
            INSERT INTO zone_assignments
                (workday_id, cargo_line_id, primary_zone_label,
                 pallet_breakdown, is_manual_override, notes)
            VALUES (?, ?, ?, ?, 0, ?)
            """,
            (workday_id, c["cargo_line_id"], zone.zone_label, summary, notes),
        )
        zone.remaining_scu -= c["scu_amount"]
        gid = c["group_id"]
        if gid is not None:
            conflict_group_zones.setdefault(gid, set()).add(zone.zone_label)
            conflict_group_stations.setdefault(gid, {}).setdefault(
                zone.zone_label, set()
            ).add(delivery_station_id)
    zone.occupants.add(delivery_station_id)


def _place_cargo_with_overflow(
    workday_id: int,
    zones: list[ZoneState],
    delivery_station_id: int,
    cargo: list[dict],
    conn: sqlite3.Connection,
    excluded_zones: set[str],
    conflict_group_zones: dict[int, set[str]],
    conflict_group_stations: dict[int, dict[str, set[int]]],
    excluded_reason: dict[str, list[tuple[int, int]]] | None = None,
) -> None:
    """Place cargo across multiple zones when no single zone fits it all.

    Pool-and-pack: all of a destination's pallets (across every one of
    its cargo lines) go into a single FFD-sorted pool, then we greedily
    pack them by repeatedly picking the best-ranked zone with room for
    the smallest remaining pallet:

      0. A zone this destination already occupies (smallest remaining
         first → top it off before opening another)
      1. A fresh zone (largest first → leave smaller ones for tail
         pallets)
      2. A zone occupied by another destination — last resort, the
         only path that produces a mixed-destination zone

    Re-ranking after each pass means once we open a fresh zone for the
    destination it becomes a same-dest zone and stays first in line, so
    cargo lines from multiple contracts going to the same destination
    consolidate into the same zone(s) instead of each one opening its
    own fresh zone.
    """
    # 1. Pool every pallet across every cargo line for this destination.
    pool: list[tuple[int, int]] = []   # (cargo_line_id, pallet_size)
    cargo_meta: dict[int, dict] = {}
    for c in cargo:
        cargo_meta[c["cargo_line_id"]] = c
        for size in palletize(c["scu_amount"], c["max_pallet_size"]):
            pool.append((c["cargo_line_id"], size))
    # FFD: largest pallets first. Ties break on cargo_line_id so pallets
    # from the same line stay adjacent.
    pool.sort(key=lambda p: (-p[1], p[0]))

    def _rank(z: ZoneState) -> tuple[int, int]:
        if delivery_station_id in z.occupants:
            return (0, z.remaining_scu)        # same dest, top off
        if not z.occupants:
            return (1, -z.remaining_scu)       # fresh, biggest first
        return (2, -z.remaining_scu)           # mixed, last resort

    # placement[(cargo_line_id, zone_label)] = list[pallet_size]
    placement: dict[tuple[int, str], list[int]] = {}
    # zone_kind[zone_label] = 'fresh' | 'topoff' | 'mixed'
    # captured at the moment we picked the zone, so we know WHY a mix
    # happened (no fresh zone left to land in).
    zone_kind: dict[str, str] = {}
    zone_partners: dict[str, list[int]] = {}
    unplaced: list[tuple[int, int]] = []

    excluded_reason = excluded_reason or {}

    while pool:
        smallest = min(s for _, s in pool)
        avail = sorted(
            [z for z in zones
             if z.zone_label not in excluded_zones
             and z.remaining_scu >= smallest],
            key=_rank,
        )
        if not avail:
            unplaced.extend(pool)
            break
        target = avail[0]
        if target.zone_label not in zone_kind:
            r = _rank(target)[0]
            if r == 0:
                zone_kind[target.zone_label] = "topoff"
                _log.info(
                    "    overflow dest %d → top-off into %s "
                    "(remaining=%d SCU)",
                    delivery_station_id, target.zone_label, target.remaining_scu,
                )
            elif r == 1:
                zone_kind[target.zone_label] = "fresh"
                _log.info(
                    "    overflow dest %d → fresh %s "
                    "(capacity=%d SCU, pool remaining=%d)",
                    delivery_station_id, target.zone_label,
                    target.remaining_scu, sum(s for _, s in pool),
                )
            else:
                zone_kind[target.zone_label] = "mixed"
                zone_partners[target.zone_label] = sorted(target.occupants)
                # Why are we mixing? Either no fresh zone exists, or
                # conflict-zone exclusion forbade the fresh ones that did.
                fresh_left = [
                    z.zone_label for z in zones
                    if not z.occupants and z.remaining_scu >= smallest
                ]
                fresh_blocked_by_conflict = [
                    z for z in fresh_left if z in excluded_zones
                ]
                if fresh_blocked_by_conflict:
                    why = (
                        f"conflict-zone exclusion ruled out fresh zone(s) "
                        f"{fresh_blocked_by_conflict} (excluded={sorted(excluded_zones)})"
                    )
                else:
                    why = "no fresh zone has room for the smallest pallet"
                _log.info(
                    "    overflow dest %d → MIXED into %s with %s — %s",
                    delivery_station_id, target.zone_label,
                    sorted(target.occupants), why,
                )

        # Pack everything that fits into this zone before moving on,
        # honoring the FFD ordering already in `pool`.
        new_pool: list[tuple[int, int]] = []
        placed_any = False
        for cl_id, size in pool:
            if target.remaining_scu >= size:
                placement.setdefault((cl_id, target.zone_label), []).append(size)
                target.remaining_scu -= size
                target.occupants.add(delivery_station_id)
                placed_any = True
            else:
                new_pool.append((cl_id, size))
        pool = new_pool
        if not placed_any:
            # Defensive guard: smallest-fit selection should make this
            # impossible, but bail rather than loop forever.
            unplaced.extend(pool)
            break

    # 2. Persist one zone_assignments row per (cargo_line, zone) pair.
    cl_zones: dict[int, list[str]] = {}
    for cl_id, zone_label in placement.keys():
        cl_zones.setdefault(cl_id, []).append(zone_label)

    def _note_for(zone_label: str, split: bool) -> str:
        kind = zone_kind.get(zone_label, "fresh")
        if kind == "mixed":
            partners = zone_partners.get(zone_label, [])
            return (
                f"MIXED — sharing {zone_label} with destination(s) "
                f"{partners} (no fresh zone left)"
            )
        if kind == "topoff":
            return f"OVERFLOW — top-off into existing same-destination zone {zone_label}"
        return (
            "OVERFLOW — split across zones"
            if split else
            "OVERFLOW — single zone"
        )

    for (cl_id, zone_label), sizes in placement.items():
        sizes.sort(reverse=True)
        summary = palletize_summary(sizes)
        note = _note_for(zone_label, split=len(cl_zones[cl_id]) > 1)
        conn.execute(
            """
            INSERT INTO zone_assignments
                (workday_id, cargo_line_id, primary_zone_label,
                 pallet_breakdown, is_manual_override, notes)
            VALUES (?, ?, ?, ?, 0, ?)
            """,
            (workday_id, cl_id, zone_label, summary, note),
        )

    # 3. Record EVERY zone each conflict group uses (not just the
    #    first), so a future destination's exclusion covers the full
    #    footprint of its partner.
    for (cl_id, zone_label) in placement.keys():
        c = cargo_meta[cl_id]
        gid = c.get("group_id")
        if gid is None:
            continue
        conflict_group_zones.setdefault(gid, set()).add(zone_label)
        conflict_group_stations.setdefault(gid, {}).setdefault(
            zone_label, set()
        ).add(delivery_station_id)

    # 4. Last-resort fallback: if conflict-zone exclusion left cargo
    #    with no legal home, place it ANYWAY into the best mixed zone
    #    available (now ignoring excluded_zones). Without this the cargo
    #    line has no zone_assignments row and shows up in the UI as "?".
    #    Each fallback placement is loudly tagged so the pilot knows the
    #    conflict still needs manual resolution at delivery.
    fallback_placements: dict[tuple[int, str], list[int]] = {}
    if unplaced and excluded_zones:
        _log.info(
            "    overflow dest %d → %d SCU still unplaced after "
            "conflict-respecting pass; retrying with exclusion ignored",
            delivery_station_id, sum(s for _, s in unplaced),
        )
        retry_pool = list(unplaced)
        unplaced = []
        while retry_pool:
            smallest = min(s for _, s in retry_pool)
            avail = sorted(
                [z for z in zones if z.remaining_scu >= smallest],
                key=_rank,
            )
            if not avail:
                unplaced.extend(retry_pool)
                break
            target = avail[0]
            new_pool: list[tuple[int, int]] = []
            placed_any = False
            for cl_id, size in retry_pool:
                if target.remaining_scu >= size:
                    fallback_placements.setdefault(
                        (cl_id, target.zone_label), []
                    ).append(size)
                    target.remaining_scu -= size
                    target.occupants.add(delivery_station_id)
                    placed_any = True
                else:
                    new_pool.append((cl_id, size))
            retry_pool = new_pool
            if not placed_any:
                unplaced.extend(retry_pool)
                break

        for (cl_id, zone_label), sizes in fallback_placements.items():
            sizes.sort(reverse=True)
            summary = palletize_summary(sizes)
            others = sorted(
                d for d in next(z for z in zones if z.zone_label == zone_label).occupants
                if d != delivery_station_id
            )
            note = (
                f"MIXED (CONFLICT-OVERRIDE) — sharing {zone_label} with "
                f"destination(s) {others}; conflict exclusion overridden "
                f"because the ship had no legal home for this cargo. "
                f"Resolve the ambiguous pallets manually at delivery."
            )
            conn.execute(
                """
                INSERT INTO zone_assignments
                    (workday_id, cargo_line_id, primary_zone_label,
                     pallet_breakdown, is_manual_override, notes)
                VALUES (?, ?, ?, ?, 0, ?)
                """,
                (workday_id, cl_id, zone_label, summary, note),
            )
            _log.info(
                "    overflow dest %d → CONFLICT-OVERRIDE placement of "
                "cargo %d in %s with %s",
                delivery_station_id, cl_id, zone_label, others,
            )
            cl_zones.setdefault(cl_id, []).append(zone_label)

    # Warn on truly unplaced (over capacity even with overrides)
    if unplaced:
        per_line_unplaced: dict[int, int] = {}
        for cl_id, size in unplaced:
            per_line_unplaced[cl_id] = per_line_unplaced.get(cl_id, 0) + size
        for cl_id, scu in per_line_unplaced.items():
            _log_warn(
                workday_id,
                f"Cargo line {cl_id} could not place {scu} SCU "
                f"— ship is over capacity.",
                cl_id, conn,
            )

    for cl_id, zone_labels in cl_zones.items():
        if len(zone_labels) > 1:
            scu = cargo_meta[cl_id]["scu_amount"]
            _log_warn(
                workday_id,
                f"Cargo line {cl_id} ({scu} SCU) split across "
                f"{', '.join(zone_labels)}.",
                cl_id, conn,
            )

