"""
Route builder — converts a workday's contracts into an ordered list of stops.

Design (handbook §2.1):
  Priority: unload sanity > station count > reshuffling > SCU waste > travel time.

Algorithm (when distances are null, use station sort_order as a proxy for
geographic position — later replaced by actual Dijkstra on station_distances +
jump_gates):

1. Collect every unique station that appears as pickup or delivery.
2. Sort by sort_order (ascending = nearest to origin first).
3. Emit one RouteStop per station:
   - "Depart"      — origin (always first)
   - "Load"        — pickup-only stop
   - "Unload"      — delivery-only stop
   - "Arrive"      — station has both load and unload work
   - "Final unload"— last delivery stop (overrides "Unload")
4. If round_robin is on, append a return-to-origin stop at the end
   (unless origin already appears as the final stop).
5. No-op stops are excluded (handbook §13).
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Optional


_log = logging.getLogger("cargo_manager")


@dataclass
class CargoLineRef:
    """Lightweight ref used inside route stops (no heavy DB row objects)."""

    cargo_line_id: int
    contract_id: int
    contract_number: int
    commodity_id: int
    commodity_name: str
    scu_amount: int
    max_pallet_size: int


@dataclass
class RouteStop:
    stop_number: int
    station_id: int
    station_name: str
    action: str  # approved handbook labels
    loads: list[CargoLineRef] = field(default_factory=list)
    unloads: list[CargoLineRef] = field(default_factory=list)
    notes: str = ""


def build_simple_route(
    workday_id: int,
    conn: sqlite3.Connection,
    *,
    final_destination_id: Optional[int] = None,
    round_robin: bool = False,
) -> list[RouteStop]:
    """Build the route for *workday_id*.

    Returns an empty list if there are no active contracts (handbook §6).
    """
    # ── Load contracts + cargo lines ─────────────────────────────────────
    contracts = conn.execute(
        """
        SELECT c.id, c.contract_number, c.pickup_station_id, c.max_pallet_size,
               ps.name  AS pickup_name, ps.sort_order AS pickup_sort
        FROM contracts c
        JOIN stations ps ON ps.id = c.pickup_station_id
        WHERE c.workday_id = ?
          AND c.status != 'complete'
        ORDER BY c.contract_number
        """,
        (workday_id,),
    ).fetchall()

    _log.info(
        "route: building for workday=%d, %d contracts, "
        "final_destination_id=%s, round_robin=%s",
        workday_id, len(contracts), final_destination_id, round_robin,
    )
    if not contracts:
        _log.info("route: no contracts, returning empty route")
        return []

    cargo_lines = conn.execute(
        """
        SELECT cl.id, cl.contract_id, cl.scu_amount, cl.commodity_id,
               cm.name       AS commodity_name,
               cl.delivery_station_id,
               ds.name       AS delivery_name,
               ds.sort_order AS delivery_sort,
               ct.contract_number,
               ct.max_pallet_size
        FROM cargo_lines cl
        JOIN contracts ct ON ct.id = cl.contract_id
        JOIN stations  ds ON ds.id = cl.delivery_station_id
        JOIN commodities cm ON cm.id = cl.commodity_id
        WHERE ct.workday_id = ?
          AND ct.status != 'complete'
        ORDER BY cl.id
        """,
        (workday_id,),
    ).fetchall()

    workday = conn.execute(
        "SELECT origin_station_id FROM workdays WHERE id = ?",
        (workday_id,),
    ).fetchone()
    origin_id = workday["origin_station_id"]

    origin_station = conn.execute(
        "SELECT id, name, sort_order FROM stations WHERE id = ?",
        (origin_id,),
    ).fetchone()

    # ── Build station set ─────────────────────────────────────────────────
    # Map station_id -> (name, sort_order, loads=[], unloads=[])
    station_map: dict[int, dict] = {}

    def _ensure(sid: int, name: str, sort: int | None) -> None:
        if sid not in station_map:
            station_map[sid] = {
                "name": name,
                "sort_order": sort if sort is not None else 9999,
                "loads": [],
                "unloads": [],
            }

    # Origin always present (but may have zero load/unload — handled later)
    _ensure(origin_id, origin_station["name"], origin_station["sort_order"])

    # Pickups
    for c in contracts:
        _ensure(c["pickup_station_id"], c["pickup_name"], c["pickup_sort"])

    # Deliveries
    for cl in cargo_lines:
        _ensure(
            cl["delivery_station_id"], cl["delivery_name"], cl["delivery_sort"]
        )

    # Final destination (if explicitly set and different from all above)
    if final_destination_id:
        fd = conn.execute(
            "SELECT id, name, sort_order FROM stations WHERE id = ?",
            (final_destination_id,),
        ).fetchone()
        if fd:
            _ensure(fd["id"], fd["name"], fd["sort_order"])

    # ── Populate load/unload lists ────────────────────────────────────────
    # Build a contract_id → contract lookup for max_pallet_size
    contract_lookup = {c["id"]: c for c in contracts}

    for cl in cargo_lines:
        ref = CargoLineRef(
            cargo_line_id=cl["id"],
            contract_id=cl["contract_id"],
            contract_number=cl["contract_number"],
            commodity_id=cl["commodity_id"],
            commodity_name=cl["commodity_name"],
            scu_amount=cl["scu_amount"],
            max_pallet_size=cl["max_pallet_size"],
        )
        # Load happens at the pickup station
        station_map[cl["delivery_station_id"]]["unloads"].append(ref)

    for c in contracts:
        # Find cargo lines for this contract to attach to the pickup stop.
        #
        # TODO (handbook §16.2) — late-binding pickup: when the pickup
        # station gets visited more than once BEFORE the delivery, this
        # eagerly attaches the load to the earliest visit. Safe under
        # strict_pallet_conflict_mode (the conflict spec assumes
        # everything's on board for deconfliction), but in the default
        # post-CIG-fix mode it wastes zone space mid-route. Future: walk
        # visits per cargo line and bind the load to the latest visit
        # still before the delivery.
        for cl in cargo_lines:
            if cl["contract_id"] == c["id"]:
                ref = CargoLineRef(
                    cargo_line_id=cl["id"],
                    contract_id=cl["contract_id"],
                    contract_number=cl["contract_number"],
                    commodity_id=cl["commodity_id"],
                    commodity_name=cl["commodity_name"],
                    scu_amount=cl["scu_amount"],
                    max_pallet_size=cl["max_pallet_size"],
                )
                station_map[c["pickup_station_id"]]["loads"].append(ref)

    # ── Sort stations ─────────────────────────────────────────────────────
    # Origin always first, then ascending sort_order, final_destination last.
    def _sort_key(sid: int) -> tuple:
        if sid == origin_id:
            return (0, 0)
        if final_destination_id and sid == final_destination_id:
            return (2, 0)
        return (1, station_map[sid]["sort_order"])

    ordered_ids = sorted(station_map.keys(), key=_sort_key)

    # Remove no-op stops (handbook §13): stations with no load, no unload,
    # and not origin / final destination.
    def _has_work(sid: int) -> bool:
        if sid == origin_id:
            return True
        if final_destination_id and sid == final_destination_id:
            return True
        info = station_map[sid]
        return bool(info["loads"] or info["unloads"])

    ordered_ids = [sid for sid in ordered_ids if _has_work(sid)]

    _log.info(
        "route: ordered station ids=%s (origin=%d, final=%s)",
        ordered_ids, origin_id, final_destination_id,
    )
    for sid in ordered_ids:
        info = station_map[sid]
        _log.info(
            "  station %d (%s) sort=%d loads=%d unloads=%d",
            sid, info["name"], info["sort_order"],
            len(info["loads"]), len(info["unloads"]),
        )

    # If the origin is also a delivery destination, those unloads must
    # happen at the END of the route, not at the Initial Departure
    # (the ship leaves origin empty, so there's nothing to unload then).
    # Save them off; they'll go onto a synthesized return-to-origin stop.
    pending_origin_unloads: list[CargoLineRef] = []
    if origin_id in station_map and station_map[origin_id]["unloads"]:
        pending_origin_unloads = station_map[origin_id]["unloads"]
        station_map[origin_id]["unloads"] = []
    # If origin now has no work AND origin isn't the final_destination,
    # drop it from the middle ordering (it will be re-added as
    # departure stop 1 below).
    # (Origin is at idx=0 in ordered_ids per _sort_key above.)

    # ── Assign actions ────────────────────────────────────────────────────
    stops: list[RouteStop] = []
    for idx, sid in enumerate(ordered_ids):
        info = station_map[sid]
        is_first = idx == 0
        is_last = idx == len(ordered_ids) - 1
        has_load = bool(info["loads"])
        has_unload = bool(info["unloads"])

        if is_first:
            action = "Depart"
        elif is_last and not round_robin and not pending_origin_unloads:
            action = "Final unload" if has_unload else "Arrive"
        elif has_load and has_unload:
            action = "Arrive"
        elif has_load:
            action = "Load"
        elif has_unload:
            action = "Unload"
        else:
            action = "Arrive"  # final_destination with no cargo work

        stops.append(
            RouteStop(
                stop_number=idx + 1,
                station_id=sid,
                station_name=info["name"],
                action=action,
                loads=list(info["loads"]),
                unloads=list(info["unloads"]),
            )
        )

    # ── Double-dip: detect cargo whose pickup happens AFTER its only
    # delivery visit in the route, and add a return-visit stop so it
    # actually gets delivered. Without this, cargo for an early-sort
    # destination (e.g. Baijini, sort=80) picked up at a later-sort
    # station (e.g. Long Forest, sort=120) just rides the rest of the
    # route undelivered.
    pickup_idx_by_cl: dict[int, int] = {}
    delivery_idx_by_cl: dict[int, int] = {}
    for idx, st in enumerate(stops):
        for ref in st.loads:
            pickup_idx_by_cl[ref.cargo_line_id] = idx
        for ref in st.unloads:
            delivery_idx_by_cl[ref.cargo_line_id] = idx

    # cargo lines that need a second delivery visit, grouped by
    # delivery_station_id → list[(latest_pickup_idx, CargoLineRef)]
    stranded_by_dest: dict[int, list[tuple[int, CargoLineRef]]] = {}
    for cl in cargo_lines:
        cl_id = cl["id"]
        d_idx = delivery_idx_by_cl.get(cl_id)
        p_idx = pickup_idx_by_cl.get(cl_id)
        if d_idx is None or p_idx is None:
            continue
        if p_idx > d_idx:
            ref = CargoLineRef(
                cargo_line_id=cl_id,
                contract_id=cl["contract_id"],
                contract_number=cl["contract_number"],
                commodity_id=cl["commodity_id"],
                commodity_name=cl["commodity_name"],
                scu_amount=cl["scu_amount"],
                max_pallet_size=cl["max_pallet_size"],
            )
            stranded_by_dest.setdefault(
                cl["delivery_station_id"], []
            ).append((p_idx, ref))
            # Remove this cargo from the original (early) delivery stop
            # — it can't possibly be there since pickup is later.
            stops[d_idx].unloads = [
                u for u in stops[d_idx].unloads if u.cargo_line_id != cl_id
            ]

    # Append return-visit stops in order of "latest pickup idx" so each
    # double-dip happens AFTER its required pickups. Multiple cargo
    # lines for the same destination collapse into one return visit
    # gated by their latest pickup.
    return_visit_plan: list[tuple[int, int, list[CargoLineRef]]] = []  # (after_idx, dest_sid, refs)
    for dest_sid, refs_with_pickup in stranded_by_dest.items():
        latest_pickup = max(p for p, _ in refs_with_pickup)
        refs = [r for _, r in refs_with_pickup]
        return_visit_plan.append((latest_pickup, dest_sid, refs))
        _log.info(
            "  double-dip: destination %d (%s) needs return visit "
            "after stop %d for %d stranded cargo line(s)",
            dest_sid, station_map[dest_sid]["name"],
            latest_pickup + 1, len(refs),
        )
    # Sort by "after which stop" so we keep the natural progression.
    return_visit_plan.sort(key=lambda t: t[0])
    for _, dest_sid, refs in return_visit_plan:
        info = station_map[dest_sid]
        stops.append(RouteStop(
            stop_number=0,    # renumbered after all appends
            station_id=dest_sid,
            station_name=info["name"],
            action="Unload",
            loads=[],
            unloads=refs,
            notes="Return visit (double-dip) for cargo picked up later in the route",
        ))

    # ── Return-to-origin Final unload ────────────────────────────────────
    # Triggered when:
    #   (a) round_robin is on and origin isn't already the last stop, OR
    #   (b) origin has cargo to unload (origin == one of the deliveries).
    needs_return = (
        (round_robin and stops and stops[-1].station_id != origin_id)
        or pending_origin_unloads
    )
    if needs_return:
        stops.append(
            RouteStop(
                stop_number=len(stops) + 1,
                station_id=origin_id,
                station_name=origin_station["name"],
                action="Final unload" if pending_origin_unloads else "Arrive",
                loads=[],
                unloads=pending_origin_unloads,
                notes=(
                    "Return to origin — final unload"
                    if pending_origin_unloads
                    else "Round-robin return to origin"
                ),
            )
        )

    # Renumber after any return-visit / final-unload appends so
    # stop_number always equals position-in-list.
    for i, st in enumerate(stops):
        st.stop_number = i + 1

    # If the original "early" delivery stop is now empty (its sole
    # cargo got moved to a return visit), drop it from the route.
    stops = [
        st for st in stops
        if st.loads or st.unloads or st.station_id == origin_id
    ]
    for i, st in enumerate(stops):
        st.stop_number = i + 1

    # Splice in any user-injected manual stops. Done last so manual
    # stops can reference any scheduled station (including
    # return-visits and final-unloads).
    _insert_manual_stops(workday_id, conn, stops)

    # Auto-insert relief unload stops whenever the simulated onboard
    # cargo crosses the configured capacity threshold (default 75%).
    # Runs AFTER manual stops so user-explicit wishes are honoured
    # first, then auto-relief is applied to the merged route.
    stops = _inject_capacity_relief_stops(stops, workday_id, conn)

    return stops


def _insert_manual_stops(
    workday_id: int,
    conn: sqlite3.Connection,
    stops: list[RouteStop],
) -> None:
    """Splice user-defined manual stops into *stops* in place.

    Each manual_stops row says "insert a stop at <station_id> AFTER
    <after_station_id>" (or at the start when after_station_id is
    NULL). The new RouteStop has no contract-driven loads/unloads —
    the temporal zone planner unloads by station id so any onboard
    cargo bound for the manual station drops naturally.

    When *after_station_id* matches multiple existing stops we pick the
    LAST occurrence so the manual stop lands after the most recent
    visit (matches operator intent: "after the next time we hit X").
    """
    rows = conn.execute(
        """
        SELECT ms.id, ms.station_id, ms.after_station_id, ms.notes,
               s.name AS station_name
        FROM manual_stops ms
        JOIN stations s ON s.id = ms.station_id
        WHERE ms.workday_id = ?
        ORDER BY ms.sort_order, ms.id
        """,
        (workday_id,),
    ).fetchall()
    if not rows:
        return

    for row in rows:
        station_id = row["station_id"]
        after_id = row["after_station_id"]
        # Find insertion index. NULL after_station_id → insert at start
        # (index 0). Otherwise find the LAST stop whose station matches.
        if after_id is None:
            insert_at = 0
        else:
            insert_at = None
            for idx in range(len(stops) - 1, -1, -1):
                if stops[idx].station_id == after_id:
                    insert_at = idx + 1
                    break
            if insert_at is None:
                # Anchor station is no longer in the route; skip rather
                # than silently dropping at an arbitrary position.
                _log.warning(
                    "manual stop %d: after_station_id=%s no longer in "
                    "route, skipping insertion of station %d (%s)",
                    row["id"], after_id, station_id, row["station_name"],
                )
                continue

        manual = RouteStop(
            stop_number=0,  # renumbered below
            station_id=station_id,
            station_name=row["station_name"],
            action="Manual Stop",
            loads=[],
            unloads=[],
            notes=row["notes"] or "Manual stop added by operator",
        )
        stops.insert(insert_at, manual)
        _log.info(
            "manual stop %d: inserted '%s' at position %d (after station=%s)",
            row["id"], row["station_name"], insert_at + 1, after_id,
        )

    # Renumber so stop_number == position in the list.
    for i, st in enumerate(stops):
        st.stop_number = i + 1


_AUTO_RELIEF_ACTION = "Auto Unload (75%+ relief)"


def _inject_capacity_relief_stops(
    stops: list[RouteStop],
    workday_id: int,
    conn: sqlite3.Connection,
    threshold: float | None = None,
) -> list[RouteStop]:
    """Splice "relief" unload stops whenever onboard cargo crosses *threshold*.

    Walks the route stop-by-stop tracking what's onboard. When the
    onboard total after a stop hits ``threshold * ship.total_scu`` and
    the NEXT planned stop wouldn't already bring it back below, we
    insert a new RouteStop right after the offender that unloads
    everything currently bound for the destination with the most cargo
    onboard.

    The function is idempotent — existing relief stops (action
    ``"Auto Unload (75%+ relief)"``) are filtered out before the
    simulation begins so re-running produces the same output.
    """
    if not stops:
        return stops

    # Idempotency: strip any prior auto-relief stops so we recompute
    # from a clean baseline. Manual stops and contract-driven stops
    # are preserved.
    stops = [s for s in stops if s.action != _AUTO_RELIEF_ACTION]

    ship = conn.execute(
        "SELECT s.total_scu FROM ships s "
        "JOIN workdays w ON w.ship_id = s.id WHERE w.id = ?",
        (workday_id,),
    ).fetchone()
    if not ship or not ship["total_scu"]:
        return stops
    total_scu = int(ship["total_scu"])

    # Resolve threshold — caller-supplied wins, else settings, else 0.75.
    if threshold is None:
        try:
            from ..settings import AppSettings
            threshold = float(AppSettings(conn).get("auto_relief_threshold"))
        except Exception:
            threshold = 0.75
    capacity_limit = threshold * total_scu

    # Helper: simulate the running onboard state through *stops*,
    # tracking per-destination CargoLineRef lists. Returns a list of
    # (onboard_after, per_dest_map_after_stop, per_dest_name_after_stop).
    def _simulate(seq: list[RouteStop]) -> list[tuple[int, dict[int, list[CargoLineRef]], dict[int, str]]]:
        onboard_by_dest: dict[int, list[CargoLineRef]] = {}
        name_by_dest: dict[int, str] = {}
        trace: list[tuple[int, dict[int, list[CargoLineRef]], dict[int, str]]] = []
        for st in seq:
            for ref in st.loads:
                # Each ref's "destination" is the station where it
                # eventually unloads. We don't store that on the ref
                # itself, so we infer it from the route: it's the
                # station_id of the stop where this cargo_line_id is
                # listed under unloads. Pre-build a lookup once outside.
                onboard_by_dest.setdefault(_dest_of_cargo[ref.cargo_line_id], []).append(ref)
                name_by_dest[_dest_of_cargo[ref.cargo_line_id]] = _dest_name_of_cargo[ref.cargo_line_id]
            # Remove anything that this stop is unloading.
            for ref in st.unloads:
                dest = _dest_of_cargo.get(ref.cargo_line_id)
                if dest is None or dest not in onboard_by_dest:
                    continue
                onboard_by_dest[dest] = [
                    r for r in onboard_by_dest[dest]
                    if r.cargo_line_id != ref.cargo_line_id
                ]
                if not onboard_by_dest[dest]:
                    del onboard_by_dest[dest]
            onboard_scu = sum(
                r.scu_amount for refs in onboard_by_dest.values() for r in refs
            )
            # Snapshot copies so the trace doesn't see future mutations.
            trace.append((
                onboard_scu,
                {d: list(refs) for d, refs in onboard_by_dest.items()},
                dict(name_by_dest),
            ))
        return trace

    def _build_dest_lookups(seq: list[RouteStop]) -> tuple[dict[int, int], dict[int, str]]:
        """Pre-compute cargo_line_id → (delivery_station_id, name)."""
        dest_of: dict[int, int] = {}
        name_of: dict[int, str] = {}
        for st in seq:
            for ref in st.unloads:
                # First time we see this cargo line being unloaded,
                # remember WHERE it's being delivered.
                if ref.cargo_line_id not in dest_of:
                    dest_of[ref.cargo_line_id] = st.station_id
                    name_of[ref.cargo_line_id] = st.station_name
        return dest_of, name_of

    # Iterate: find the FIRST over-threshold stop, inject relief there,
    # re-simulate, repeat. A `seen_anchors` set prevents pathological
    # infinite loops (shouldn't happen since each relief unloads ≥1
    # SCU, but belt-and-braces).
    max_iterations = len(stops) * 2 + 4
    for _ in range(max_iterations):
        _dest_of_cargo, _dest_name_of_cargo = _build_dest_lookups(stops)
        trace = _simulate(stops)

        injected_at: int | None = None
        for idx, (onboard_scu, onboard_map, name_map) in enumerate(trace):
            if onboard_scu < capacity_limit:
                continue
            # Skip if the NEXT planned stop already drops us below
            # the threshold on its own.
            if idx + 1 < len(trace):
                next_onboard_scu = trace[idx + 1][0]
                if next_onboard_scu < capacity_limit:
                    continue
            # Pick the destination with the most onboard SCU.
            best_dest = max(
                onboard_map.keys(),
                key=lambda d: sum(r.scu_amount for r in onboard_map[d]),
            )
            refs = list(onboard_map[best_dest])
            relief = RouteStop(
                stop_number=0,  # renumbered below
                station_id=best_dest,
                station_name=name_map[best_dest],
                action=_AUTO_RELIEF_ACTION,
                loads=[],
                unloads=refs,
                notes=(
                    f"Auto relief: onboard {onboard_scu}/{total_scu} SCU "
                    f"({onboard_scu / total_scu:.0%}) crossed "
                    f"{threshold:.0%} threshold; unloading "
                    f"{sum(r.scu_amount for r in refs)} SCU bound for "
                    f"{name_map[best_dest]}."
                ),
            )
            # When the same cargo line is delivered later in the
            # planned route, remove it from that planned unload so we
            # don't double-unload (the SCU has already left the ship).
            relief_ids = {r.cargo_line_id for r in refs}
            for st in stops[idx + 1:]:
                if not st.unloads:
                    continue
                st.unloads = [
                    u for u in st.unloads if u.cargo_line_id not in relief_ids
                ]
            stops.insert(idx + 1, relief)
            injected_at = idx + 1
            _log.info(
                "auto-relief: onboard=%d/%d (%.0f%%) at stop %d (%s); "
                "injected unload at %s for %d SCU.",
                onboard_scu, total_scu, 100 * onboard_scu / total_scu,
                idx + 1, stops[idx].station_name,
                name_map[best_dest],
                sum(r.scu_amount for r in refs),
            )
            break

        if injected_at is None:
            break

    # Renumber so stop_number is contiguous 1..N.
    for i, st in enumerate(stops):
        st.stop_number = i + 1
    return stops
