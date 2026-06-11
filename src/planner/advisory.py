"""
Advisory engine — analyses each stop's loadout and surfaces problems
the user might want to address by manually moving pallets in the 3D
view (or with the zone-detail dialog).

The engine is *advisory only* — every check returns informational text
plus structured pointers (affected zone labels, affected cargo lines)
so the UI can surface the issue clearly without prescribing a fix.

Checks implemented (severity tag in parens):

  high_mixing            (warn)  zone holds >= 3 distinct destinations
  outboard_early_unload  (warn)  early-unload cargo in a top-quartile
                                 unload_priority (outboard) zone
  overflow               (error) snapshot SCU exceeds what packs into a
                                 zone, indicating overflowed pallets
  narrow_conflict        (warn)  conflict-group cargo line is on board
  bulk_in_use            (info)  cargo sitting in the bulk-floor zone
                                 (highest unload_priority on the ship)
  bottom_tier_late       (info)  a late-unload pallet trapped at z=0
                                 under an earlier-unload pallet

Public entry point:

    advisories_for_result(result, conn) -> dict[stop_number, list[Advisory]]
"""

from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .recompute import RecomputeResult


@dataclass
class Advisory:
    stop_number: int
    severity: str           # "info" | "warn" | "error"
    code: str               # short tag like "high_mixing"
    summary: str            # one-line headline
    detail: str             # multi-line explanation with actionable suggestion
    affected_zones: list[str] = field(default_factory=list)
    affected_cargo_lines: list[int] = field(default_factory=list)


# ── public entry point ─────────────────────────────────────────────────────


def advisories_for_result(
    result: RecomputeResult,
    conn: sqlite3.Connection,
) -> dict[int, list[Advisory]]:
    """Return a dict of stop_number -> list of advisories for that stop.

    Runs a battery of checks against the simulation result and returns
    actionable advisories the user might want to address by moving
    pallets manually in the 3D view.
    """
    by_stop: dict[int, list[Advisory]] = defaultdict(list)

    if not result or not result.route_stops or not result.snapshots:
        return {}

    # ── Pull supporting data once ────────────────────────────────────────

    # Find the ship for this workday. Every route stop belongs to one
    # workday, so any cargo line will do for the lookup.
    workday_id = _workday_id_for(result, conn)
    if workday_id is None:
        return {}

    zone_rows = conn.execute(
        """
        SELECT z.zone_label, z.unload_priority, z.scu_capacity
        FROM ship_zones z
        JOIN workdays w ON w.ship_id = z.ship_id
        WHERE w.id = ?
        """,
        (workday_id,),
    ).fetchall()
    zone_priority: dict[str, int] = {
        r["zone_label"]: (r["unload_priority"] if r["unload_priority"] is not None else 0)
        for r in zone_rows
    }
    zone_capacity: dict[str, int] = {
        r["zone_label"]: (r["scu_capacity"] or 0) for r in zone_rows
    }

    # Top quartile threshold across all zone priorities. Empty / all-zero
    # priorities collapse to threshold=inf so the check no-ops cleanly.
    priorities = [p for p in zone_priority.values() if p is not None]
    if priorities:
        sorted_p = sorted(priorities)
        # 75th percentile (nearest-rank method) — picks the value at
        # position ceil(0.75 * N) within the sorted list.
        idx = max(0, int(round(0.75 * (len(sorted_p) - 1))))
        top_quartile_threshold = sorted_p[idx]
        # Bulk zone = the single zone with the maximum unload_priority
        # (doctrine: "highest unload_priority = last resort").
        max_priority = sorted_p[-1]
        bulk_zones = {
            zl for zl, p in zone_priority.items() if p == max_priority
        }
    else:
        top_quartile_threshold = float("inf")
        bulk_zones = set()

    # Map cargo_line_id -> delivery_station_id (used to figure out which
    # stop a cargo line unloads at).
    cl_rows = conn.execute(
        """
        SELECT cl.id AS cl_id, cl.delivery_station_id
        FROM cargo_lines cl
        JOIN contracts ct ON ct.id = cl.contract_id
        WHERE ct.workday_id = ?
        """,
        (workday_id,),
    ).fetchall()
    cl_to_dest_station: dict[int, int] = {
        r["cl_id"]: r["delivery_station_id"] for r in cl_rows
    }

    # cargo_line_id -> unload stop number, built from route_stops
    cl_to_unload_stop: dict[int, int] = {}
    for stop in result.route_stops:
        # Every cargo line gets unloaded at the stop matching its
        # delivery_station_id. We walk the route and record the LAST
        # matching stop (handles double-dip / return-visit cases).
        pass
    for stop in result.route_stops:
        for cl_id, dest_sid in cl_to_dest_station.items():
            if dest_sid == stop.station_id:
                cl_to_unload_stop[cl_id] = stop.stop_number

    # Conflict-group cargo lines
    conflict_cl_ids: set[int] = set()
    for grp in result.conflict_groups:
        conflict_cl_ids.update(grp.cargo_line_ids)

    # Sort stops in route order so per-stop checks see the right context.
    stops_by_number = {s.stop_number: s for s in result.route_stops}

    # ── Per-stop checks ────────────────────────────────────────────────

    for stop in result.route_stops:
        stop_num = stop.stop_number
        entries = result.snapshots.get(stop_num, [])
        if not entries:
            continue

        # Aggregate per zone for this stop's snapshot.
        by_zone: dict[str, list] = defaultdict(list)
        for e in entries:
            by_zone[e.zone_label].append(e)

        # ── 1. High mixing (>= 3 distinct destinations in a zone) ──────
        for zlabel, zone_entries in by_zone.items():
            destinations = {e.delivery_station_name for e in zone_entries}
            if len(destinations) >= 3:
                dest_list = sorted(destinations)
                cl_ids = sorted({e.cargo_line_id for e in zone_entries})
                by_stop[stop_num].append(Advisory(
                    stop_number=stop_num,
                    severity="warn",
                    code="high_mixing",
                    summary=(
                        f"Zone {zlabel} holds {len(destinations)} distinct "
                        f"destinations"
                    ),
                    detail=(
                        f"Zone {zlabel} is mixing cargo for: "
                        f"{', '.join(dest_list)}.\n\n"
                        f"Consider consolidating same-destination cargo into "
                        f"a single zone if room permits."
                    ),
                    affected_zones=[zlabel],
                    affected_cargo_lines=cl_ids,
                ))

        # ── 2. Outboard early-unload ───────────────────────────────────
        # If cargo unloads at stop N, then at stop N-1 it should NOT be
        # sitting in a top-quartile (high unload_priority) zone.
        if stop_num + 1 in stops_by_number:
            next_stop = stops_by_number[stop_num + 1]
            # cargo lines that unload at next_stop
            unload_targets = {
                cl_id for cl_id, ust in cl_to_unload_stop.items()
                if ust == next_stop.stop_number
            }
            for e in entries:
                if e.cargo_line_id not in unload_targets:
                    continue
                prio = zone_priority.get(e.zone_label)
                if prio is None or prio < top_quartile_threshold:
                    continue
                by_stop[stop_num].append(Advisory(
                    stop_number=stop_num,
                    severity="warn",
                    code="outboard_early_unload",
                    summary=(
                        f"Early-unload cargo in outboard zone {e.zone_label}"
                    ),
                    detail=(
                        f"Cargo line cl#{e.cargo_line_id} "
                        f"({e.scu_amount} SCU {e.commodity_name} → "
                        f"{e.delivery_station_name}) unloads at the next "
                        f"stop but is currently sitting in zone "
                        f"{e.zone_label} (unload_priority={prio}, top "
                        f"quartile across this ship).\n\n"
                        f"Move this cargo to an inboard zone (lower "
                        f"unload_priority) to drain first."
                    ),
                    affected_zones=[e.zone_label],
                    affected_cargo_lines=[e.cargo_line_id],
                ))

        # ── 3. Overflow / overcap ──────────────────────────────────────
        # If snapshot SCU sum per zone exceeds the zone's scu_capacity,
        # cargo physically overflowed — the packer couldn't fit everything.
        # Aggregate the overflow across every overflowing zone at this
        # stop into ONE "loadmaster attention required" advisory so the
        # operator sees a single actionable headline naming the stop,
        # zones, and total overflow SCU.
        overflow_zones: list[tuple[str, int, int]] = []  # (zone, sum, cap)
        overflow_cls: set[int] = set()
        for zlabel, zone_entries in by_zone.items():
            scu_sum = sum(e.scu_amount for e in zone_entries)
            cap = zone_capacity.get(zlabel, 0)
            if cap and scu_sum > cap:
                overflow_zones.append((zlabel, scu_sum, cap))
                for e in zone_entries:
                    overflow_cls.add(e.cargo_line_id)
        if overflow_zones:
            total_overflow = sum(
                s - c for (_z, s, c) in overflow_zones
            )
            zone_labels = sorted(z for (z, _s, _c) in overflow_zones)
            zones_str = ", ".join(zone_labels)
            has_locks = _workday_has_locks(workday_id, conn)
            extra = ""
            if has_locks:
                extra = (
                    "\n\nLocks didn't free enough space — additional "
                    "manual intervention required."
                )
            by_stop[stop_num].append(Advisory(
                stop_number=stop_num,
                severity="error",
                code="overflow",
                summary=(
                    f"Loadmaster attention required — {total_overflow} "
                    f"SCU couldn't be auto-packed at stop {stop_num}"
                ),
                detail=(
                    f"The auto-packer can't fit all cargo into zones "
                    f"{zones_str} at this stop "
                    f"({total_overflow} SCU overflow).\n\n"
                    f"Open the 3D View, place pallets manually (press R "
                    f"to rotate a held pallet, drag across adjacent "
                    f"zones for lateral spanning), then recompute. "
                    f"Locked pallets are preserved across the recompute."
                    f"{extra}"
                ),
                affected_zones=zone_labels,
                affected_cargo_lines=sorted(overflow_cls),
            ))

        # ── 4. Narrow conflict on board ────────────────────────────────
        onboard_conflict_cls = sorted({
            e.cargo_line_id for e in entries
            if e.cargo_line_id in conflict_cl_ids
        })
        if onboard_conflict_cls:
            affected_zones = sorted({
                e.zone_label for e in entries
                if e.cargo_line_id in conflict_cl_ids
            })
            by_stop[stop_num].append(Advisory(
                stop_number=stop_num,
                severity="warn",
                code="narrow_conflict",
                summary=(
                    f"{len(onboard_conflict_cls)} conflict cargo line"
                    f"{'s' if len(onboard_conflict_cls) != 1 else ''} on "
                    f"board"
                ),
                detail=(
                    f"This stop has cargo lines that share an ambiguous "
                    f"pallet size with another destination — the elevator "
                    f"display can't tell them apart.\n\n"
                    f"Track these pallets carefully at the elevator — "
                    f"they're indistinguishable from each other."
                ),
                affected_zones=affected_zones,
                affected_cargo_lines=onboard_conflict_cls,
            ))

        # ── 5. Bulk-floor in use ──────────────────────────────────────
        bulk_in_use = [zl for zl in by_zone.keys() if zl in bulk_zones]
        if bulk_in_use:
            cl_ids = sorted({
                e.cargo_line_id for e in entries
                if e.zone_label in bulk_zones
            })
            by_stop[stop_num].append(Advisory(
                stop_number=stop_num,
                severity="info",
                code="bulk_in_use",
                summary=(
                    f"Bulk-floor zone(s) in use: "
                    f"{', '.join(sorted(bulk_in_use))}"
                ),
                detail=(
                    f"By doctrine, the highest unload_priority zone "
                    f"(bulk-floor) is the last-resort placement. This "
                    f"stop has cargo sitting there.\n\n"
                    f"Informational only — nothing to act on unless the "
                    f"plan can avoid it."
                ),
                affected_zones=sorted(bulk_in_use),
                affected_cargo_lines=cl_ids,
            ))

        # ── 6. Bottom-tier-late ───────────────────────────────────────
        # An informational check: when a late-unload cargo line sits at
        # the bottom of a zone (z=0 inferred via per-zone sort) under an
        # early-unload pallet. We don't have explicit z-coordinates in
        # the snapshot, so we approximate using load order: the FIRST
        # entry recorded for a zone in the snapshot is treated as the
        # bottom-of-stack candidate. If that bottom entry unloads LATER
        # than any other entry in the same zone, flag it.
        for zlabel, zone_entries in by_zone.items():
            if len(zone_entries) < 2:
                continue
            unload_orders = []
            for e in zone_entries:
                u = cl_to_unload_stop.get(e.cargo_line_id)
                if u is None:
                    continue
                unload_orders.append((u, e))
            if len(unload_orders) < 2:
                continue
            # The "bottom" entry is the FIRST in the snapshot's
            # stable-sorted order (by_zone preserves insertion order;
            # build_loadout_snapshots sorts by (zone, dest)).
            bottom_entry = zone_entries[0]
            bottom_unload = cl_to_unload_stop.get(bottom_entry.cargo_line_id)
            if bottom_unload is None:
                continue
            # If some other entry unloads BEFORE the bottom, the bottom
            # is "trapped" — informational.
            others_earlier = [
                e for (u, e) in unload_orders
                if e is not bottom_entry and u < bottom_unload
            ]
            if not others_earlier:
                continue
            by_stop[stop_num].append(Advisory(
                stop_number=stop_num,
                severity="info",
                code="bottom_tier_late",
                summary=(
                    f"Late-unload pallet at bottom of zone {zlabel}"
                ),
                detail=(
                    f"Cargo line cl#{bottom_entry.cargo_line_id} "
                    f"({bottom_entry.delivery_station_name}) is sitting at "
                    f"the bottom of zone {zlabel}, but pallets bound for "
                    f"earlier stops are stacked on top. The early-unload "
                    f"pallets come off first — that's fine — but this "
                    f"late-unload pallet is exposed for the rest of the "
                    f"trip.\n\nInformational only."
                ),
                affected_zones=[zlabel],
                affected_cargo_lines=[bottom_entry.cargo_line_id],
            ))

    # Return a plain dict (defaultdict leaks behaviour into callers).
    return dict(by_stop)


# ── helpers ────────────────────────────────────────────────────────────────


def _workday_has_locks(workday_id: int, conn: sqlite3.Connection) -> bool:
    """True iff at least one pallet_locks row exists for *workday_id*.

    Used by the overflow advisory to surface a stronger "additional
    intervention required" note when the loadmaster has already locked
    pallets but overflow persists.
    """
    row = conn.execute(
        "SELECT 1 FROM pallet_locks WHERE workday_id = ? LIMIT 1",
        (workday_id,),
    ).fetchone()
    return row is not None


def _workday_id_for(
    result: RecomputeResult, conn: sqlite3.Connection,
) -> int | None:
    """Find the workday_id corresponding to the cargo lines in result."""
    for stop in result.route_stops:
        for ref in stop.loads or stop.unloads:
            row = conn.execute(
                """
                SELECT ct.workday_id
                FROM cargo_lines cl
                JOIN contracts ct ON ct.id = cl.contract_id
                WHERE cl.id = ?
                """,
                (ref.cargo_line_id,),
            ).fetchone()
            if row:
                return row["workday_id"]
    return None
