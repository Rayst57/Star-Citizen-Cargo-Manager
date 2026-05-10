"""
Hidden pallet identity conflict detection.

See docs/conflicts.md for the full algorithm and display rules.

CONFLICT RULE (from user clarification):
  A pallet size is AMBIGUOUS when ≥ 2 delivery destinations have the SAME
  COUNT of that size from the same pickup × commodity.

  Example — Yellow Core, Tungsten, max 8:
    Everus : 55 SCU → [8,8,8,8,8,8, 4, 2, 1]   (6×8, 1×4, 1×2, 1×1)
    Baijini: 27 SCU → [8,8,8,       2, 1]        (3×8, 1×2, 1×1)

    Size 8:  Everus=6, Baijini=3  → DIFFERENT counts → NOT ambiguous
               (you can count 6 vs 3 on the elevator and identify each stack)
    Size 4:  Everus=1, Baijini=0  → unique to Everus → NOT ambiguous
    Size 2:  Everus=1, Baijini=1  → SAME count → AMBIGUOUS ⚠
    Size 1:  Everus=1, Baijini=1  → SAME count → AMBIGUOUS ⚠

Commodity granularity:
  The elevator mouseover shows commodity name.  2 SCU Tungsten and 2 SCU
  Aluminum from different contracts are NOT ambiguous.  Conflicts are
  grouped by:  pickup_station_id × commodity_id
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections import Counter
from dataclasses import dataclass, field

from .palletizer import palletize, palletize_summary, VALID_SIZES


_log = logging.getLogger("cargo_manager")


@dataclass
class DestConflictInfo:
    """Per-destination data within a conflict group."""
    delivery_station_id: int
    delivery_station_name: str
    cargo_line_ids: list[int]
    commodity_name: str
    # Pallet counts for this destination (size → count)
    pallet_counts: dict[int, int]
    # Sizes that appear in exactly 1 destination (safe to load confidently)
    unique_sizes: list[int]      # descending
    # Sizes where this dest's count == another dest's count (elevator ambiguity)
    ambiguous_sizes: list[int]   # descending


@dataclass
class ConflictGroup:
    group_id: int
    pickup_station_id: int
    pickup_station_name: str
    commodity_id: int
    commodity_name: str
    # Ordered by delivery_station_id for stable output
    destinations: list[DestConflictInfo]
    # All ambiguous sizes across the group (union)
    ambiguous_sizes: list[int]   # descending
    status: str = "active"

    # ── convenience helpers ─────────────────────────────────────────────

    @property
    def cargo_line_ids(self) -> list[int]:
        return [cl_id for d in self.destinations for cl_id in d.cargo_line_ids]

    @property
    def delivery_station_ids(self) -> list[int]:
        return [d.delivery_station_id for d in self.destinations]

    def dest_info(self, delivery_station_id: int) -> DestConflictInfo | None:
        for d in self.destinations:
            if d.delivery_station_id == delivery_station_id:
                return d
        return None

    def ambiguous_sizes_json(self) -> str:
        return json.dumps(sorted(self.ambiguous_sizes, reverse=True))

    def unique_sizes_json(self, delivery_station_id: int) -> str:
        info = self.dest_info(delivery_station_id)
        return json.dumps(sorted(info.unique_sizes, reverse=True) if info else [])

    def sibling_zone_label(
        self, delivery_station_id: int, zone_map: dict[int, str]
    ) -> str | None:
        """Return the zone label of the OTHER destination's cargo (for reload instructions)."""
        for d in self.destinations:
            if d.delivery_station_id != delivery_station_id:
                return zone_map.get(d.delivery_station_id)
        return None


def detect_conflicts(workday_id: int, conn: sqlite3.Connection) -> list[ConflictGroup]:
    """Detect hidden pallet identity conflicts for all active contracts.

    Returns a (possibly empty) list of ConflictGroup objects.
    """
    rows = conn.execute(
        """
        SELECT cl.id            AS cargo_line_id,
               cl.contract_id,
               cl.scu_amount,
               cl.delivery_station_id,
               cl.commodity_id,
               ds.name          AS delivery_name,
               ct.pickup_station_id,
               ps.name          AS pickup_name,
               cm.name          AS commodity_name,
               ct.max_pallet_size
        FROM cargo_lines cl
        JOIN contracts   ct ON ct.id  = cl.contract_id
        JOIN stations    ds ON ds.id  = cl.delivery_station_id
        JOIN stations    ps ON ps.id  = ct.pickup_station_id
        JOIN commodities cm ON cm.id  = cl.commodity_id
        WHERE ct.workday_id = ?
          AND ct.status != 'complete'
        ORDER BY ct.pickup_station_id, cl.commodity_id, cl.delivery_station_id
        """,
        (workday_id,),
    ).fetchall()

    _log.info(
        "conflicts: scanning %d cargo lines across %d (pickup × commodity) groups",
        len(rows),
        len({(r["pickup_station_id"], r["commodity_id"]) for r in rows}),
    )
    if not rows:
        return []

    # ── Group by (pickup × commodity) ────────────────────────────────────
    by_key: dict[tuple[int, int], list[dict]] = {}
    for r in rows:
        key = (r["pickup_station_id"], r["commodity_id"])
        entry = {
            "cargo_line_id": r["cargo_line_id"],
            "delivery_station_id": r["delivery_station_id"],
            "delivery_name": r["delivery_name"],
            "pickup_station_id": r["pickup_station_id"],
            "pickup_name": r["pickup_name"],
            "commodity_id": r["commodity_id"],
            "commodity_name": r["commodity_name"],
            "pallets": palletize(r["scu_amount"], r["max_pallet_size"]),
        }
        by_key.setdefault(key, []).append(entry)

    groups: list[ConflictGroup] = []
    group_id = 1

    for (pickup_id, commodity_id), lines in by_key.items():
        pickup_name = lines[0]["pickup_name"]
        commodity_name = lines[0]["commodity_name"]

        # ── Aggregate pallet COUNTS per destination ───────────────────────
        # dict: delivery_station_id → Counter(size → count)
        dest_counts: dict[int, Counter] = {}
        dest_line_ids: dict[int, list[int]] = {}
        dest_names: dict[int, str] = {}

        for entry in lines:
            did = entry["delivery_station_id"]
            dest_counts.setdefault(did, Counter()).update(entry["pallets"])
            dest_line_ids.setdefault(did, []).append(entry["cargo_line_id"])
            dest_names[did] = entry["delivery_name"]

        if len(dest_counts) < 2:
            continue  # only one destination → no conflict possible

        # ── Find ambiguous sizes ──────────────────────────────────────────
        # A size is ambiguous when ≥ 2 destinations have the SAME COUNT of it.
        # If counts differ (e.g. 6 vs 3), you can distinguish by quantity.
        all_sizes: set[int] = set()
        for ctr in dest_counts.values():
            all_sizes.update(ctr.keys())

        ambiguous_sizes: list[int] = []
        for size in all_sizes:
            counts_for_size = [ctr[size] for ctr in dest_counts.values() if ctr[size] > 0]
            if len(counts_for_size) >= 2:
                unique_counts = set(counts_for_size)
                if len(unique_counts) == 1:
                    # All destinations that have this size have the SAME count → ambiguous
                    ambiguous_sizes.append(size)
                # If counts differ (e.g. 6 vs 3) → distinguishable → NOT ambiguous

        if not ambiguous_sizes:
            continue

        ambiguous_sizes.sort(reverse=True)
        ambiguous_set = set(ambiguous_sizes)

        # ── Build per-destination info ────────────────────────────────────
        dest_infos: list[DestConflictInfo] = []
        for did, ctr in sorted(dest_counts.items()):
            # Unique: size present in THIS dest but in no other dest
            unique = sorted(
                [s for s in ctr if all(
                    other_ctr[s] == 0
                    for odid, other_ctr in dest_counts.items()
                    if odid != did
                )],
                reverse=True,
            )
            # Ambiguous for this dest: intersect with global ambiguous set
            ambig_here = sorted(
                [s for s in ctr if s in ambiguous_set],
                reverse=True,
            )
            dest_infos.append(
                DestConflictInfo(
                    delivery_station_id=did,
                    delivery_station_name=dest_names[did],
                    cargo_line_ids=dest_line_ids[did],
                    commodity_name=commodity_name,
                    pallet_counts=dict(ctr),
                    unique_sizes=unique,
                    ambiguous_sizes=ambig_here,
                )
            )

        groups.append(
            ConflictGroup(
                group_id=group_id,
                pickup_station_id=pickup_id,
                pickup_station_name=pickup_name,
                commodity_id=commodity_id,
                commodity_name=commodity_name,
                destinations=dest_infos,
                ambiguous_sizes=ambiguous_sizes,
            )
        )
        _log.info(
            "  conflict group %d: %s × %s — %d dests, ambiguous_sizes=%s",
            group_id, pickup_name, commodity_name,
            len(dest_infos), ambiguous_sizes,
        )
        group_id += 1

    _log.info("conflicts: detected %d group(s) total", len(groups))
    return groups


def persist_conflicts(
    workday_id: int, groups: list[ConflictGroup], conn: sqlite3.Connection
) -> None:
    """Write detected conflict groups to pallet_conflicts table.

    Clears any previous conflict rows for this workday first.
    """
    conn.execute(
        "DELETE FROM pallet_conflicts WHERE workday_id = ?", (workday_id,)
    )

    for grp in groups:
        for dest_info in grp.destinations:
            for cl_id in dest_info.cargo_line_ids:
                conn.execute(
                    """
                    INSERT INTO pallet_conflicts
                        (workday_id, conflict_group_id, cargo_line_id,
                         pickup_station_id, ambiguous_sizes, unique_sizes,
                         status, handling_zone_label)
                    VALUES (?, ?, ?, ?, ?, ?, 'active', NULL)
                    """,
                    (
                        workday_id,
                        grp.group_id,
                        cl_id,
                        grp.pickup_station_id,
                        json.dumps(sorted(dest_info.ambiguous_sizes, reverse=True)),
                        json.dumps(sorted(dest_info.unique_sizes, reverse=True)),
                    ),
                )
    conn.commit()
