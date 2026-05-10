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
        # Find cargo lines for this contract to attach to the pickup stop
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

    return stops
