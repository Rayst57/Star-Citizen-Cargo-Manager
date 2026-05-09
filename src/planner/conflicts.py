"""
Hidden pallet identity conflict detection.

See docs/conflicts.md for the full algorithm and display rules.

A conflict exists when two or more delivery destinations share identical
pallet sizes AND the same commodity at the same pickup station.

Commodity granularity clarification: the station elevator UI shows the
commodity name on mouseover.  This means 2 SCU Tungsten and 2 SCU Aluminum
from different contracts are NOT ambiguous — the pilot can distinguish them
by commodity.  Conflicts are therefore grouped by:
    pickup_station_id × commodity_id

Detection algorithm (handbook §17-19 / conflicts.md):
  1. Palletize every cargo line.
  2. Group by pickup_station_id × commodity_id.
  3. Within each group, collect the pallet-size sets per delivery destination.
  4. Sizes that appear in ≥ 2 destination sets are "ambiguous".
  5. Sizes that appear in exactly 1 destination set are "unique" to that dest.
  6. If any ambiguous sizes exist → create a ConflictGroup.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

from .palletizer import palletize


@dataclass
class ConflictGroup:
    group_id: int
    pickup_station_id: int
    pickup_station_name: str
    cargo_line_ids: list[int]
    delivery_station_ids: list[int]
    ambiguous_sizes: list[int]  # sizes shared across ≥ 2 destinations (descending)
    unique_sizes_per_dest: dict[int, list[int]]  # delivery_station_id → unique sizes
    status: str = "active"

    def ambiguous_sizes_json(self) -> str:
        return json.dumps(sorted(self.ambiguous_sizes, reverse=True))

    def unique_sizes_json(self, delivery_station_id: int) -> str:
        return json.dumps(
            sorted(self.unique_sizes_per_dest.get(delivery_station_id, []), reverse=True)
        )


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
               ct.max_pallet_size
        FROM cargo_lines cl
        JOIN contracts ct ON ct.id = cl.contract_id
        JOIN stations  ds ON ds.id = cl.delivery_station_id
        JOIN stations  ps ON ps.id = ct.pickup_station_id
        WHERE ct.workday_id = ?
          AND ct.status != 'complete'
        ORDER BY ct.pickup_station_id, cl.commodity_id, cl.delivery_station_id
        """,
        (workday_id,),
    ).fetchall()

    if not rows:
        return []

    # ── Step 1: palletize + group by (pickup × commodity) ───────────────
    # The elevator shows commodity on mouseover, so only same-commodity
    # pallets at the same pickup can be ambiguous.
    by_pickup_commodity: dict[tuple[int, int], list[dict]] = {}
    for r in rows:
        key = (r["pickup_station_id"], r["commodity_id"])
        entry = {
            "cargo_line_id": r["cargo_line_id"],
            "delivery_station_id": r["delivery_station_id"],
            "delivery_name": r["delivery_name"],
            "pickup_station_id": r["pickup_station_id"],
            "pickup_name": r["pickup_name"],
            "commodity_id": r["commodity_id"],
            "pallets": palletize(r["scu_amount"], r["max_pallet_size"]),
        }
        by_pickup_commodity.setdefault(key, []).append(entry)

    # ── Steps 2-6: compare per-destination pallet-size sets ─────────────
    groups: list[ConflictGroup] = []
    group_id = 1

    for (pickup_id, _commodity_id), lines in by_pickup_commodity.items():
        dest_size_sets: dict[int, set[int]] = {}
        dest_line_ids: dict[int, list[int]] = {}
        pickup_name = lines[0]["pickup_name"]

        for entry in lines:
            did = entry["delivery_station_id"]
            dest_size_sets.setdefault(did, set()).update(entry["pallets"])
            dest_line_ids.setdefault(did, []).append(entry["cargo_line_id"])

        if len(dest_size_sets) < 2:
            continue  # single destination at this (pickup, commodity) → no conflict

        size_dest_count: dict[int, int] = {}
        for sizes in dest_size_sets.values():
            for s in sizes:
                size_dest_count[s] = size_dest_count.get(s, 0) + 1

        ambiguous = sorted(
            [s for s, cnt in size_dest_count.items() if cnt >= 2], reverse=True
        )
        if not ambiguous:
            continue

        unique_per_dest: dict[int, list[int]] = {
            did: sorted(
                [s for s in sizes if size_dest_count.get(s, 0) == 1], reverse=True
            )
            for did, sizes in dest_size_sets.items()
        }

        all_line_ids = [lid for ids in dest_line_ids.values() for lid in ids]

        groups.append(
            ConflictGroup(
                group_id=group_id,
                pickup_station_id=pickup_id,
                pickup_station_name=pickup_name,
                cargo_line_ids=all_line_ids,
                delivery_station_ids=list(dest_size_sets.keys()),
                ambiguous_sizes=ambiguous,
                unique_sizes_per_dest=unique_per_dest,
            )
        )
        group_id += 1

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
        for cl_id in grp.cargo_line_ids:
            # Determine which delivery station this cargo_line belongs to
            row = conn.execute(
                "SELECT delivery_station_id FROM cargo_lines WHERE id = ?", (cl_id,)
            ).fetchone()
            if not row:
                continue
            did = row["delivery_station_id"]
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
                    grp.ambiguous_sizes_json(),
                    grp.unique_sizes_json(did),
                ),
            )
    conn.commit()
