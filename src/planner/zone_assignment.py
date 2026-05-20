"""
Zone assignment — destination-first allocation (post-CIG-fix model).

CIG's cargo-grid update gave every pallet a visible ID and a locked
destination, so the old "indistinguishable identical pallets" problem
is no longer a hard in-game conflict. The strict-exclusion / consoli-
dation logic lives in legacy_zone_assignment.py and is opt-in via the
`strict_pallet_conflict_mode` setting — flip it on if CIG ever regresses.

The active planner is simpler. Priorities:

  1. Each destination's cargo goes in its OWN zone whenever capacity
     allows.
  2. Overflow (one destination needs more than one zone) is fine.
  3. Two destinations sharing a zone is fine when needed —

     EXCEPT when that share would put two physically indistinguishable
     pallets into the same zone: same commodity + a shared pallet size
     + different contracts + different destinations. (Your example:
     20 SCU Aluminum for Baijini + 16 SCU Aluminum for Everus both
     palletize into a 16-SCU Al pallet, so an inspector can't tell
     them apart at the door.) The planner SOFT-avoids that pairing —
     it will pick another zone if one exists, but will place them
     together as a last resort and log a validation warning.

Algorithm:
  - Honor manual user overrides (pinned cargo stays put; its zone is
    pre-occupied so subsequent allocation sees the real free space).
  - Order destinations by total SCU descending (big ones claim
    contiguous zones first so small ones top off leftover space).
  - For each destination: try to claim a fresh single zone that fits
    the full load. If so, place there and continue.
  - Otherwise distribute across zones in rank order
    (same-dest top-off > fresh > shared-with-other-dest), skipping any
    candidate that would create the narrow conflict above unless no
    other option has room.

The `conflict_groups` parameter is kept for API parity with the legacy
path; this planner ignores it. (Legacy detect_conflicts is still
useful when strict mode is on; in normal mode the recompute pipeline
short-circuits it.)

TODO — transload consolidation (handbook §16.1).
This planner computes the INITIAL loadout at Stop 1 and never
rebalances. The desired next step is a per-stop transload pass:
after each stop's unloads, consolidate split destinations into
fewer zones before the stop's new cargo is loaded. Order of ops at
each stop becomes Unload → Transload → Upload. Not yet implemented.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field

from .palletizer import palletize, palletize_summary


_log = logging.getLogger("cargo_manager")


@dataclass
class _ZoneState:
    zone_label: str
    bay_label: str
    scu_capacity: int
    unload_priority: int
    remaining_scu: int
    occupants: set[int] = field(default_factory=set)
    # cargo lines currently placed in the zone — used to detect the
    # narrow same-commodity / shared-pallet-size conflict before
    # adding more cargo to the same zone.
    placed: list[dict] = field(default_factory=list)


def _load_zones(ship_id: int, conn: sqlite3.Connection) -> list[_ZoneState]:
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
        _ZoneState(
            zone_label=r["zone_label"],
            bay_label=r["bay_label"],
            scu_capacity=r["scu_capacity"],
            unload_priority=r["unload_priority"],
            remaining_scu=r["scu_capacity"],
        )
        for r in rows
    ]


def _log_warn(
    workday_id: int, message: str, cargo_line_id: int | None,
    conn: sqlite3.Connection,
) -> None:
    from datetime import datetime, timezone
    conn.execute(
        """
        INSERT INTO validation_log
            (workday_id, timestamp, severity, source, message, cargo_line_id)
        VALUES (?, ?, 'WARN', 'zone_assignment', ?, ?)
        """,
        (workday_id, datetime.now(timezone.utc).isoformat(),
         message, cargo_line_id),
    )


def _would_narrow_conflict(zone: _ZoneState, candidate: dict) -> bool:
    """Would placing *candidate* in *zone* put two indistinguishable
    pallets side by side?

    A narrow conflict needs ALL of:
      - same commodity_id
      - different contract_id
      - different delivery_station_id
      - at least one shared pallet size
    """
    cand_sizes = set(candidate["pallet_sizes"])
    if not cand_sizes:
        return False
    for resident in zone.placed:
        if resident["commodity_id"] != candidate["commodity_id"]:
            continue
        if resident["contract_id"] == candidate["contract_id"]:
            continue
        if resident["delivery_station_id"] == candidate["delivery_station_id"]:
            continue
        if cand_sizes & set(resident["pallet_sizes"]):
            return True
    return False


# ── claim helpers ────────────────────────────────────────────────────────

def _claim_fresh_zone(
    zones: list[_ZoneState], needed_scu: int,
) -> _ZoneState | None:
    """First empty zone (lowest unload_priority) with capacity >= needed."""
    for z in zones:
        if not z.occupants and z.scu_capacity >= needed_scu:
            return z
    return None


def _rank_for_dest(z: _ZoneState, delivery_station_id: int) -> tuple[int, int]:
    """Rank candidate zones for placing more of a destination's cargo:
        0 = same-dest top-off (smallest remaining first)
        1 = fresh (largest first, so small tails land in small zones)
        2 = mixed with another destination (largest first, last resort)
    """
    if delivery_station_id in z.occupants:
        return (0, z.remaining_scu)
    if not z.occupants:
        return (1, -z.remaining_scu)
    return (2, -z.remaining_scu)


def _place(
    zone: _ZoneState, candidate: dict, scu: int, pallets: list[int],
) -> None:
    zone.remaining_scu -= scu
    zone.occupants.add(candidate["delivery_station_id"])
    zone.placed.append({
        "cargo_line_id":       candidate["cargo_line_id"],
        "contract_id":         candidate["contract_id"],
        "commodity_id":        candidate["commodity_id"],
        "delivery_station_id": candidate["delivery_station_id"],
        "pallet_sizes":        list(pallets),
    })


# ── main entry ───────────────────────────────────────────────────────────

def build_zone_plan(
    workday_id: int,
    route_stop_order: list[int],
    conflict_groups,             # noqa: ARG001 — kept for API parity, unused
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
        _log_warn(workday_id,
                  "No zones found for ship — cannot assign cargo.", None, conn)
        conn.commit()
        return

    # ── Clear previous non-manual assignments ────────────────────────────
    conn.execute(
        "DELETE FROM zone_assignments "
        "WHERE workday_id = ? AND is_manual_override = 0",
        (workday_id,),
    )

    # ── Pre-occupy zones holding user-pinned cargo ───────────────────────
    manual_rows = conn.execute(
        """
        SELECT cl.id AS cargo_line_id, cl.contract_id, cl.commodity_id,
               cl.delivery_station_id, cl.scu_amount,
               ct.max_pallet_size, za.primary_zone_label
        FROM zone_assignments za
        JOIN cargo_lines cl ON cl.id = za.cargo_line_id
        JOIN contracts   ct ON ct.id = cl.contract_id
        WHERE za.workday_id = ?
          AND za.is_manual_override = 1
          AND ct.status != 'complete'
        """,
        (workday_id,),
    ).fetchall()
    manual_cl_ids: set[int] = {r["cargo_line_id"] for r in manual_rows}
    if manual_rows:
        by_label = {z.zone_label: z for z in zones}
        for r in manual_rows:
            z = by_label.get(r["primary_zone_label"])
            if z is None:
                continue
            _place(
                z,
                {
                    "cargo_line_id":       r["cargo_line_id"],
                    "contract_id":         r["contract_id"],
                    "commodity_id":        r["commodity_id"],
                    "delivery_station_id": r["delivery_station_id"],
                },
                r["scu_amount"],
                palletize(r["scu_amount"], r["max_pallet_size"]),
            )
        _log.info(
            "zone_assignment: %d cargo line(s) pinned by user override",
            len(manual_rows),
        )

    # ── Load active cargo lines (skipping manually-pinned) ───────────────
    rows = conn.execute(
        """
        SELECT cl.id, cl.contract_id, cl.scu_amount, cl.commodity_id,
               cl.delivery_station_id,
               ct.max_pallet_size, ct.pickup_station_id, ct.contract_number
        FROM cargo_lines cl
        JOIN contracts ct ON ct.id = cl.contract_id
        WHERE ct.workday_id = ?
          AND ct.status != 'complete'
        ORDER BY cl.id
        """,
        (workday_id,),
    ).fetchall()
    rows = [r for r in rows if r["id"] not in manual_cl_ids]
    if not rows:
        conn.commit()
        return

    # Bundle each cargo line with its pallet breakdown so the narrow-
    # conflict check is cheap.
    by_destination: dict[int, list[dict]] = {}
    for r in rows:
        by_destination.setdefault(r["delivery_station_id"], []).append({
            "cargo_line_id":       r["id"],
            "contract_id":         r["contract_id"],
            "commodity_id":        r["commodity_id"],
            "delivery_station_id": r["delivery_station_id"],
            "scu_amount":          r["scu_amount"],
            "max_pallet_size":     r["max_pallet_size"],
            "pickup_station_id":   r["pickup_station_id"],
            "contract_number":     r["contract_number"],
            "pallet_sizes":        palletize(r["scu_amount"], r["max_pallet_size"]),
        })

    # Sort destinations by total SCU descending — big destinations claim
    # zones first so their cargo stays contiguous, small ones top off
    # the leftover space. Route position is the tiebreaker.
    delivery_priority: dict[int, int] = {}
    for idx, sid in enumerate(route_stop_order):
        delivery_priority.setdefault(sid, idx)
    def _dest_total(did: int) -> int:
        return sum(c["scu_amount"] for c in by_destination[did])
    ordered_dests = sorted(
        by_destination,
        key=lambda did: (-_dest_total(did), delivery_priority.get(did, 9999)),
    )

    # ── Assign each destination ──────────────────────────────────────────
    persisted: dict[tuple[int, str], list[int]] = {}
    notes_for: dict[tuple[int, str], str] = {}

    for did in ordered_dests:
        cargo = sorted(by_destination[did], key=lambda c: -c["scu_amount"])
        total = sum(c["scu_amount"] for c in cargo)

        # Fast path — full load fits in one fresh zone (and no narrow
        # conflict possible, since fresh = empty).
        target = _claim_fresh_zone(zones, total)
        if target is not None:
            for c in cargo:
                _place(target, c, c["scu_amount"], c["pallet_sizes"])
                key = (c["cargo_line_id"], target.zone_label)
                persisted[key] = list(c["pallet_sizes"])
            continue

        # Otherwise distribute cargo lines across zones in rank order.
        # Same-contract pallets always travel together (their cargo
        # line is the atomic unit), so we place WHOLE cargo lines
        # first; only pallet-level split as a final fallback.
        for c in cargo:
            placed_zone = _try_place_whole(c, zones)
            if placed_zone is not None:
                key = (c["cargo_line_id"], placed_zone.zone_label)
                persisted.setdefault(key, []).extend(c["pallet_sizes"])
                continue
            # No single zone can take the whole line — pallet-level split.
            _split_pallets(c, zones, persisted, workday_id, conn)

    # ── Persist assignments + emit warnings for shared / split lines ─────
    cl_zones: dict[int, list[str]] = {}
    for (cl_id, zone_label) in persisted:
        cl_zones.setdefault(cl_id, []).append(zone_label)

    cargo_meta = {
        c["cargo_line_id"]: c
        for clist in by_destination.values() for c in clist
    }
    zone_by_label = {z.zone_label: z for z in zones}

    for (cl_id, zone_label), sizes in persisted.items():
        z = zone_by_label[zone_label]
        # Narrow-conflict residents already in the zone with THIS one?
        c_meta = cargo_meta[cl_id]
        shared_with = sorted(
            o for o in z.occupants
            if o != c_meta["delivery_station_id"]
        )
        if shared_with:
            note = (f"SHARED zone {zone_label} with destination(s) "
                    f"{shared_with}.")
        elif len(cl_zones[cl_id]) > 1:
            note = f"SPLIT — also placed in {sorted(cl_zones[cl_id])}."
        else:
            note = None
        sizes.sort(reverse=True)
        conn.execute(
            """
            INSERT INTO zone_assignments
                (workday_id, cargo_line_id, primary_zone_label,
                 pallet_breakdown, is_manual_override, notes)
            VALUES (?, ?, ?, ?, 0, ?)
            """,
            (workday_id, cl_id, zone_label,
             palletize_summary(sizes), note),
        )

    for cl_id, labels in cl_zones.items():
        if len(labels) > 1:
            scu = cargo_meta[cl_id]["scu_amount"]
            _log_warn(
                workday_id,
                f"Cargo line {cl_id} ({scu} SCU) split across "
                f"{', '.join(sorted(labels))}.",
                cl_id, conn,
            )

    conn.commit()


def _try_place_whole(
    candidate: dict, zones: list[_ZoneState],
) -> _ZoneState | None:
    """Place a whole cargo line in one zone if possible.

    Ranks candidates same-dest top-off > fresh > mixed, and
    soft-avoids zones where adding this cargo would create the narrow
    same-commodity / shared-pallet-size conflict — only picking one
    as a last resort.
    """
    scu = candidate["scu_amount"]
    did = candidate["delivery_station_id"]

    fits = [z for z in zones if z.remaining_scu >= scu]
    if not fits:
        return None

    clean = [z for z in fits if not _would_narrow_conflict(z, candidate)]
    dirty = [z for z in fits if _would_narrow_conflict(z, candidate)]

    clean.sort(key=lambda z: _rank_for_dest(z, did))
    if clean:
        target = clean[0]
        _place(target, candidate, scu, candidate["pallet_sizes"])
        return target

    # Fallback — every zone with room would create a narrow conflict.
    dirty.sort(key=lambda z: _rank_for_dest(z, did))
    if dirty:
        target = dirty[0]
        _place(target, candidate, scu, candidate["pallet_sizes"])
        return target
    return None


def _split_pallets(
    candidate: dict, zones: list[_ZoneState],
    persisted: dict[tuple[int, str], list[int]],
    workday_id: int, conn: sqlite3.Connection,
) -> None:
    """Last resort: split a cargo line at the pallet level.

    Walks pallets largest-first, drops each into the best-ranked zone
    with room (clean before narrow-conflict-dirty). Anything that
    can't fit anywhere becomes an over-capacity warning.
    """
    did = candidate["delivery_station_id"]
    remaining = sorted(candidate["pallet_sizes"], reverse=True)
    while remaining:
        size = remaining[0]
        candidates = [z for z in zones if z.remaining_scu >= size]
        if not candidates:
            _log_warn(
                workday_id,
                f"Cargo line {candidate['cargo_line_id']} could not "
                f"place {sum(remaining)} SCU — ship is over capacity.",
                candidate["cargo_line_id"], conn,
            )
            return
        # Pretend we're placing JUST this one pallet to check the
        # narrow-conflict check.
        probe = {**candidate, "pallet_sizes": [size]}
        clean = [z for z in candidates if not _would_narrow_conflict(z, probe)]
        dirty = [z for z in candidates if _would_narrow_conflict(z, probe)]
        clean.sort(key=lambda z: _rank_for_dest(z, did))
        dirty.sort(key=lambda z: _rank_for_dest(z, did))
        target = clean[0] if clean else dirty[0]
        _place(target, candidate, size, [size])
        persisted.setdefault(
            (candidate["cargo_line_id"], target.zone_label), []
        ).append(size)
        remaining = remaining[1:]
