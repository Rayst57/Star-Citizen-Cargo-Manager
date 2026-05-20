"""
Recompute orchestrator — ties together all planner steps.

Called when the user clicks [Recompute] or says "Hey Giant, recompute".

Steps:
  1. build_simple_route        → ordered RouteStop list
  2. detect_conflicts          → ConflictGroup list
  3. persist_conflicts         → write to pallet_conflicts table
  4. build_zone_plan           → write to zone_assignments table
  5. build_loadout_snapshots   → in-memory Snapshot dict
  6. persist_route_stops       → write to route_stops table
  7. Clear plan_dirty flag on workday

Returns a RecomputeResult dataclass with all computed data for the UI to
render without additional DB round-trips.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .route import RouteStop, build_simple_route
from .conflicts import ConflictGroup, detect_conflicts, persist_conflicts
from .loadout import Snapshot, build_loadout_snapshots
from ..settings import AppSettings


@dataclass
class RecomputeResult:
    route_stops: list[RouteStop]
    conflict_groups: list[ConflictGroup]
    snapshots: Snapshot
    validation_warnings: list[str] = field(default_factory=list)


def recompute(workday_id: int, conn: sqlite3.Connection) -> RecomputeResult:
    """Run a full recompute for *workday_id*.

    Clears and rewrites route_stops, zone_assignments (non-manual), and
    pallet_conflicts for this workday.  Sets workdays.plan_dirty = 0 and
    workdays.last_computed_at to now.
    """
    workday = conn.execute(
        """
        SELECT id, ship_id, origin_station_id,
               final_destination_station_id, round_robin
        FROM workdays WHERE id = ?
        """,
        (workday_id,),
    ).fetchone()
    if not workday:
        raise ValueError(f"Workday {workday_id} not found")

    # ── 1. Build route ────────────────────────────────────────────────────
    route_stops = build_simple_route(
        workday_id,
        conn,
        final_destination_id=workday["final_destination_station_id"],
        round_robin=bool(workday["round_robin"]),
    )

    if not route_stops:
        # Zero-contract workday: clear any stale plan data and return empty.
        _clear_plan_data(workday_id, conn)
        _mark_clean(workday_id, conn)
        return RecomputeResult(route_stops=[], conflict_groups=[], snapshots={})

    # ── 2 & 3. Detect + persist conflicts ────────────────────────────────
    # Only relevant in legacy strict-conflict mode. In the default
    # post-CIG-fix mode the planner doesn't use them, so we skip the
    # work and clear any stale rows from a prior strict-mode recompute.
    strict_mode = AppSettings(conn).get("strict_pallet_conflict_mode")
    if strict_mode:
        conflict_groups = detect_conflicts(workday_id, conn)
        persist_conflicts(workday_id, conflict_groups, conn)
    else:
        conflict_groups = []
        conn.execute(
            "DELETE FROM pallet_conflicts WHERE workday_id = ?", (workday_id,)
        )

    # ── 4. Zone assignment ────────────────────────────────────────────────
    # Ordered list of station IDs for delivery-priority ranking.
    station_order = [s.station_id for s in route_stops]
    if strict_mode:
        from .legacy_zone_assignment import build_zone_plan
    else:
        from .zone_assignment import build_zone_plan
    build_zone_plan(workday_id, station_order, conflict_groups, conn)

    # ── 5. Loadout snapshots ──────────────────────────────────────────────
    stop_pairs = [(s.stop_number, s.station_id) for s in route_stops]
    snapshots = build_loadout_snapshots(workday_id, stop_pairs, conn)

    # ── 6. Persist route stops ────────────────────────────────────────────
    conn.execute(
        "DELETE FROM route_stops WHERE workday_id = ?", (workday_id,)
    )
    for s in route_stops:
        conn.execute(
            """
            INSERT INTO route_stops
                (workday_id, stop_number, station_id, action, notes)
            VALUES (?, ?, ?, ?, ?)
            """,
            (workday_id, s.stop_number, s.station_id, s.action, s.notes or None),
        )

    # ── 7. Mark clean ─────────────────────────────────────────────────────
    _mark_clean(workday_id, conn)

    # ── Collect validation warnings for caller ────────────────────────────
    warn_rows = conn.execute(
        """
        SELECT message FROM validation_log
        WHERE workday_id = ? AND severity IN ('WARN', 'ERROR')
        ORDER BY id DESC
        LIMIT 50
        """,
        (workday_id,),
    ).fetchall()

    return RecomputeResult(
        route_stops=route_stops,
        conflict_groups=conflict_groups,
        snapshots=snapshots,
        validation_warnings=[r["message"] for r in warn_rows],
    )


def _clear_plan_data(workday_id: int, conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM route_stops WHERE workday_id = ?", (workday_id,))
    conn.execute(
        "DELETE FROM zone_assignments WHERE workday_id = ? AND is_manual_override = 0",
        (workday_id,),
    )
    conn.execute("DELETE FROM pallet_conflicts WHERE workday_id = ?", (workday_id,))
    conn.commit()


def _mark_clean(workday_id: int, conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        UPDATE workdays
        SET plan_dirty = 0, last_computed_at = ?
        WHERE id = ?
        """,
        (datetime.now(timezone.utc).isoformat(), workday_id),
    )
    conn.commit()
