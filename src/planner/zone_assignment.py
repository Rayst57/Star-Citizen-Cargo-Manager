"""
Zone assignment — event-driven stop-by-stop simulator.

Walks the route in temporal order so the planner can SEE that early-
unloaded cargo vacates zones for later loads to reuse. Replaces the old
static one-shot placer that thought all 479 SCU had to be onboard
simultaneously.

At every stop:
  1. UNLOAD: remove cargo whose delivery is this stop, freeing zones.
  2. TRANSLOAD: consolidate same-destination cargo into the fewest
     zones possible (always-on; pilot decides at the bay whether to
     physically move the pallets — the planner just shows the move).
  3. LOAD: place new cargo using
       same-dest topoff  >  fresh smallest-priority-first  >  mixed
     so RB (highest unload_priority) is genuinely a last resort, and
     small bays drain before big bays.

Outputs:
  - One zone_assignments row per cargo line, primary_zone_label = the
    INITIAL load zone (where the pilot grabs the pallet at pickup).
    The post-transload zone may differ; that's carried in the snapshots.
  - A per-stop list of TransloadMove records.
  - A per-stop LoadoutEntry snapshot.

Manual overrides:
  Cargo lines pinned by the user are loaded INTO their pinned zone at
  pickup. The simulator still considers transloading them later — the
  override only fixes initial placement.

Narrow-conflict avoidance (handbook §16):
  Two pallets are "indistinguishable" when they share commodity +
  pallet size + come from different contracts going to different
  destinations. The simulator soft-avoids putting such pallets in the
  same zone at load AND at transload, only mixing as a last resort.

The strict-conflict-exclusion algorithm lives in
legacy_zone_assignment.py and is opt-in via the
`strict_pallet_conflict_mode` setting.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field

from .loadout import LoadoutEntry, Snapshot
from .palletizer import palletize, palletize_summary
from .physical_packer import can_fit, max_fitting_subset
from .route import RouteStop


_log = logging.getLogger("cargo_manager")


# ── Public output types ──────────────────────────────────────────────────

@dataclass
class TransloadMove:
    """A consolidation move recommended at a given stop.

    The planner emits one of these whenever same-destination cargo can
    be merged from a source zone into a target zone that already holds
    (or will hold) more of that destination. The pilot performs it
    between unload and the next load — or skips it if the move isn't
    worth the effort.
    """
    cargo_line_id: int
    contract_number: int
    commodity_name: str
    scu_amount: int
    from_zone: str
    to_zone: str
    delivery_station_name: str
    pallet_breakdown: str = ""


@dataclass
class SimulationResult:
    snapshots: Snapshot                    # stop_number → list[LoadoutEntry]
    transload_moves: dict[int, list[TransloadMove]]  # stop_number → moves


# ── Internal state ───────────────────────────────────────────────────────

@dataclass
class _PlacedCargo:
    """A cargo line (or portion of one) sitting in a zone."""
    cargo_line_id: int
    contract_id: int
    contract_number: int
    commodity_id: int
    commodity_name: str
    delivery_station_id: int
    delivery_station_name: str
    scu: int
    pallet_sizes: list[int]
    pickup_station_name: str = ""


@dataclass
class _ZoneState:
    zone_label: str
    bay_label: str
    scu_capacity: int
    unload_priority: int
    width_units: int
    length_units: int
    height_units: int
    front_zone_label: str | None = None
    back_zone_label: str | None = None
    placed: list[_PlacedCargo] = field(default_factory=list)

    @property
    def used_scu(self) -> int:
        return sum(c.scu for c in self.placed)

    @property
    def remaining_scu(self) -> int:
        return self.scu_capacity - self.used_scu

    @property
    def occupants(self) -> set[int]:
        return {c.delivery_station_id for c in self.placed}

    @property
    def is_empty(self) -> bool:
        return not self.placed

    def existing_pallet_sizes(self) -> list[int]:
        return [s for c in self.placed for s in c.pallet_sizes]

    def can_physically_fit(self, additional_sizes: list[int]) -> bool:
        """Would *additional_sizes* still pack given what's already
        placed? Uses the same packer the renderer uses, so the planner
        never commits a layout the canvas can't draw."""
        if not additional_sizes:
            return True
        return can_fit(
            self.width_units, self.length_units, self.height_units,
            self.existing_pallet_sizes() + additional_sizes,
        )


# ── DB helpers (mostly inherited verbatim) ───────────────────────────────

def _load_zones(ship_id: int, conn: sqlite3.Connection) -> list[_ZoneState]:
    rows = conn.execute(
        """
        SELECT zone_label, bay_label, scu_capacity, unload_priority,
               width_units, length_units, height_units,
               front_zone_label, back_zone_label
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
            width_units=r["width_units"],
            length_units=r["length_units"],
            height_units=r["height_units"],
            front_zone_label=r["front_zone_label"],
            back_zone_label=r["back_zone_label"],
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


def _zones_summary(zones: list[_ZoneState]) -> str:
    return ", ".join(
        f"{z.zone_label}(cap={z.scu_capacity},prio={z.unload_priority})"
        for z in zones
    )


# ── Narrow-conflict check ────────────────────────────────────────────────

def _would_narrow_conflict(zone: _ZoneState, c: _PlacedCargo) -> bool:
    """Would adding *c* to *zone* park two indistinguishable pallets
    together? (Same commodity + shared pallet size + different
    contract + different destination.)
    """
    cand_sizes = set(c.pallet_sizes)
    if not cand_sizes:
        return False
    for r in zone.placed:
        if r.commodity_id != c.commodity_id:
            continue
        if r.contract_id == c.contract_id:
            continue
        if r.delivery_station_id == c.delivery_station_id:
            continue
        if cand_sizes & set(r.pallet_sizes):
            return True
    return False


# ── Placement primitives ─────────────────────────────────────────────────

def _place_whole(zone: _ZoneState, c: _PlacedCargo) -> None:
    zone.placed.append(c)


def _split_into_zone(
    c: _PlacedCargo, zone: _ZoneState,
) -> tuple[_PlacedCargo, _PlacedCargo]:
    """Split *c* into a piece that physically fits *zone* (given its
    current contents) and a remainder.

    Uses the same packer the renderer uses, so the placed_piece is
    guaranteed to draw without overflow. The placed_piece may be empty
    if not even the smallest pallet fits.
    """
    fitting, leftover = max_fitting_subset(
        zone.width_units, zone.length_units, zone.height_units,
        zone.existing_pallet_sizes(),
        c.pallet_sizes,
    )
    placed_piece = _PlacedCargo(
        cargo_line_id=c.cargo_line_id,
        contract_id=c.contract_id,
        contract_number=c.contract_number,
        commodity_id=c.commodity_id,
        commodity_name=c.commodity_name,
        delivery_station_id=c.delivery_station_id,
        delivery_station_name=c.delivery_station_name,
        scu=sum(fitting),
        pallet_sizes=fitting,
        pickup_station_name=c.pickup_station_name,
    )
    leftover_piece = _PlacedCargo(
        cargo_line_id=c.cargo_line_id,
        contract_id=c.contract_id,
        contract_number=c.contract_number,
        commodity_id=c.commodity_id,
        commodity_name=c.commodity_name,
        delivery_station_id=c.delivery_station_id,
        delivery_station_name=c.delivery_station_name,
        scu=sum(leftover),
        pallet_sizes=leftover,
        pickup_station_name=c.pickup_station_name,
    )
    return placed_piece, leftover_piece


# ── Zone ranking ─────────────────────────────────────────────────────────

def _pick_topoff_zone(
    zones: list[_ZoneState], c: _PlacedCargo,
) -> _ZoneState | None:
    """Smallest-remaining same-destination zone where *c* both fits by
    SCU AND physically packs given current contents."""
    dest = c.delivery_station_id
    fits = [z for z in zones
            if dest in z.occupants
            and z.remaining_scu >= c.scu
            and z.can_physically_fit(c.pallet_sizes)]
    if not fits:
        return None
    fits.sort(key=lambda z: z.remaining_scu)
    return fits[0]


def _pick_fresh_zone(
    zones: list[_ZoneState], c: _PlacedCargo,
) -> _ZoneState | None:
    """Empty zone, lowest unload_priority first, that fits *c* whole
    both by SCU and physically.

    Sorting by `unload_priority` keeps RB last: on the Starlancer the
    priorities are R1=1, R2=2, F1=3, F2=4, RB=5, so we drain
    R1/R2/F1/F2 before RB regardless of cargo size.
    """
    fits = [z for z in zones
            if z.is_empty
            and z.scu_capacity >= c.scu
            and z.can_physically_fit(c.pallet_sizes)]
    if not fits:
        return None
    fits.sort(key=lambda z: z.unload_priority)
    return fits[0]


def _pick_mixed_zone(
    zones: list[_ZoneState], c: _PlacedCargo,
) -> _ZoneState | None:
    """Last-resort: zone already holding ANOTHER destination, with
    room. Prefer no narrow conflict, then lowest unload_priority."""
    dest = c.delivery_station_id
    fits = [z for z in zones
            if (not z.is_empty)
            and dest not in z.occupants
            and z.remaining_scu >= c.scu
            and z.can_physically_fit(c.pallet_sizes)]
    if not fits:
        return None
    fits.sort(key=lambda z: (
        1 if _would_narrow_conflict(z, c) else 0,
        z.unload_priority,
    ))
    return fits[0]


def _pick_zone_for_whole_cargo(
    zones: list[_ZoneState], c: _PlacedCargo,
) -> tuple[_ZoneState | None, str]:
    """Best single zone for *c*. Returns (zone, reason). Every
    candidate is checked for physical fit so the planner never
    commits a layout that wouldn't actually pack."""
    z = _pick_topoff_zone(zones, c)
    if z is not None:
        return z, "topoff"
    z = _pick_fresh_zone(zones, c)
    if z is not None:
        return z, "fresh"
    z = _pick_mixed_zone(zones, c)
    if z is not None:
        return z, "mixed"
    return None, "none"


# ── Stop-by-stop simulation ──────────────────────────────────────────────

def _unload_at_stop(
    zones: list[_ZoneState], station_id: int,
) -> list[_PlacedCargo]:
    """Remove every cargo whose delivery_station == *station_id*.
    Returns the removed pieces (for logging).
    """
    removed: list[_PlacedCargo] = []
    for z in zones:
        keep = []
        for c in z.placed:
            if c.delivery_station_id == station_id:
                removed.append(c)
            else:
                keep.append(c)
        z.placed = keep
    return removed


def _consolidate(
    zones: list[_ZoneState],
    pinned_ids: set[int] | None = None,
) -> list[TransloadMove]:
    """Merge same-destination cargo into the fewest zones possible.

    For each destination split across multiple zones, the planner
    considers two kinds of target:

      a) an existing same-dest zone with room (default — pure topoff);
      b) a FRESH empty zone, when moving every piece of the destination
         into it would actually reduce the zone count. Step (b) is what
         makes consolidation useful right after a big unload — the
         freed bay becomes a single roomy home for cargo that was
         split across several smaller zones at load time.

    Conservative: cargo lines move as whole pieces (no re-splitting).
    Won't relocate other destinations' cargo to make room. Every
    candidate move is checked against the physical packer so the
    consolidated layout is always renderable.
    """
    moves: list[TransloadMove] = []
    pinned_ids = pinned_ids or set()
    all_dests = {c.delivery_station_id
                 for z in zones for c in z.placed}

    for dest_id in all_dests:
        while True:
            dest_zones = [z for z in zones if dest_id in z.occupants]
            if len(dest_zones) <= 1:
                break

            def _dest_scu(z: _ZoneState) -> int:
                return sum(c.scu for c in z.placed
                           if c.delivery_station_id == dest_id)

            dest_total = sum(_dest_scu(z) for z in dest_zones)
            dest_zones.sort(key=lambda z: -_dest_scu(z))

            # ── (b) Fresh-bay collapse ──
            # If there's a fresh empty zone big enough to hold the
            # destination's ENTIRE cargo, moving everything there
            # collapses N zones into 1. Always a win.
            fresh_fits = [z for z in zones
                          if z.is_empty and z.scu_capacity >= dest_total]
            if fresh_fits:
                # Smallest fresh that fits (don't waste an R-bay if an
                # F-bay would do).
                fresh_fits.sort(key=lambda z: (z.scu_capacity, z.unload_priority))
                target = fresh_fits[0]
                moved_any = False
                for source in dest_zones:
                    pieces = [c for c in source.placed
                              if c.delivery_station_id == dest_id]
                    pieces.sort(key=lambda c: -c.scu)
                    for piece in pieces:
                        if piece.cargo_line_id in pinned_ids:
                            continue
                        if target.remaining_scu < piece.scu:
                            continue
                        if _would_narrow_conflict(target, piece):
                            continue
                        if not target.can_physically_fit(piece.pallet_sizes):
                            continue
                        source.placed.remove(piece)
                        target.placed.append(piece)
                        moves.append(TransloadMove(
                            cargo_line_id=piece.cargo_line_id,
                            contract_number=piece.contract_number,
                            commodity_name=piece.commodity_name,
                            scu_amount=piece.scu,
                            from_zone=source.zone_label,
                            to_zone=target.zone_label,
                            delivery_station_name=piece.delivery_station_name,
                            pallet_breakdown=palletize_summary(piece.pallet_sizes),
                        ))
                        moved_any = True
                if moved_any:
                    continue  # re-evaluate the same destination
                # else fall through to topoff

            # ── (a) Existing same-dest topoff ──
            # Pick the LOWEST-priority same-dest zone as the target so
            # we always consolidate toward the ramp (drain-direction).
            # The opposite — biggest-cluster target — fought
            # _drain_to_lower_priority in earlier builds, producing
            # ping-pong moves where _consolidate pulled cargo into F1
            # and _drain immediately pushed it back to R2.
            dest_zones.sort(key=lambda z: z.unload_priority)
            target = dest_zones[0]
            moved_this_pass = False
            for source in dest_zones[1:]:
                # Source priority must be >= target priority. Moving
                # toward HIGHER priority would just feed the
                # ping-pong; _drain_to_lower_priority handles that
                # direction already.
                if source.unload_priority < target.unload_priority:
                    continue
                pieces = [c for c in source.placed
                          if c.delivery_station_id == dest_id]
                pieces.sort(key=lambda c: -c.scu)
                for piece in pieces:
                    if piece.cargo_line_id in pinned_ids:
                        continue
                    if target.remaining_scu < piece.scu:
                        continue
                    if _would_narrow_conflict(target, piece):
                        continue
                    if not target.can_physically_fit(piece.pallet_sizes):
                        continue
                    source.placed.remove(piece)
                    target.placed.append(piece)
                    moves.append(TransloadMove(
                        cargo_line_id=piece.cargo_line_id,
                        contract_number=piece.contract_number,
                        commodity_name=piece.commodity_name,
                        scu_amount=piece.scu,
                        from_zone=source.zone_label,
                        to_zone=target.zone_label,
                        delivery_station_name=piece.delivery_station_name,
                        pallet_breakdown=palletize_summary(piece.pallet_sizes),
                    ))
                    moved_this_pass = True
            if not moved_this_pass:
                break

    return moves


def _drain_to_lower_priority(
    zones: list[_ZoneState],
    pinned_ids: set[int] | None = None,
) -> list[TransloadMove]:
    """Pull cargo OUT of higher-priority zones into lower-priority
    same-destination zones whenever there's room.

    The user-facing intent: keep the bulk floor (RBA/RBF, which carry
    the highest unload_priority numbers) empty whenever possible.
    Anything that lands there because no other zone could take it at
    load time should migrate back into a primary bay as soon as a
    same-destination topoff target opens up — typically right after an
    unload frees room. The same rule applies across all priority
    tiers, so a piece in F2 that could topoff into F1 (lower priority,
    drains first) also migrates.

    Topoff only — never moves cargo into an EMPTY lower-priority zone,
    which would preempt that zone for later short-hop placements.
    Same-destination only (no narrow-conflict creation), and every
    candidate move is physical-fit checked so the renderer agrees.
    """
    moves: list[TransloadMove] = []
    pinned_ids = pinned_ids or set()
    # Process from the highest priority (bulk-most) downward so a
    # piece that hops RBF→RBA→F2 in one pass actually completes the
    # cascade.
    for source in sorted(zones, key=lambda z: -z.unload_priority):
        for piece in list(source.placed):
            # Respect manual moves: a user-pinned cargo line stays
            # where the user put it, even if a lower-priority same-
            # dest target has room. Without this guard, recompute
            # silently undoes "Move cargo to F1" by draining F1 → R2.
            if piece.cargo_line_id in pinned_ids:
                continue
            targets = [z for z in zones
                       if z is not source
                       and z.unload_priority < source.unload_priority
                       and piece.delivery_station_id in z.occupants
                       and z.remaining_scu >= piece.scu
                       and not _would_narrow_conflict(z, piece)
                       and z.can_physically_fit(piece.pallet_sizes)]
            if not targets:
                continue
            # Prefer the smallest-capacity fit so we don't burn a big
            # bay (R1/R2) for a small drain when an F-bay would do.
            targets.sort(key=lambda z: (z.scu_capacity, z.unload_priority))
            target = targets[0]
            source.placed.remove(piece)
            target.placed.append(piece)
            moves.append(TransloadMove(
                cargo_line_id=piece.cargo_line_id,
                contract_number=piece.contract_number,
                commodity_name=piece.commodity_name,
                scu_amount=piece.scu,
                from_zone=source.zone_label,
                to_zone=target.zone_label,
                delivery_station_name=piece.delivery_station_name,
                pallet_breakdown=palletize_summary(piece.pallet_sizes),
            ))
    return moves


def _clone_placed(c: _PlacedCargo, sizes: list[int]) -> _PlacedCargo:
    """Clone *c* keeping every field except pallet_sizes/scu, which are
    taken from *sizes*. Preserves the _max_pallet_size tag."""
    piece = _PlacedCargo(
        cargo_line_id=c.cargo_line_id,
        contract_id=c.contract_id,
        contract_number=c.contract_number,
        commodity_id=c.commodity_id,
        commodity_name=c.commodity_name,
        delivery_station_id=c.delivery_station_id,
        delivery_station_name=c.delivery_station_name,
        scu=sum(sizes),
        pallet_sizes=list(sizes),
        pickup_station_name=c.pickup_station_name,
    )
    mps = getattr(c, "_max_pallet_size", None)
    if mps is not None:
        piece._max_pallet_size = mps  # type: ignore[attr-defined]
    return piece


def _load_at_stop(
    zones: list[_ZoneState],
    new_cargo: list[_PlacedCargo],
    pinned_pieces: dict[int, list[tuple[str, list[int] | None]]],
    workday_id: int,
    conn: sqlite3.Connection,
    delivery_priority: dict[int, int],
) -> dict[int, str]:
    """Place every line in *new_cargo* into a zone.

    Pinned cargo (manual override) is placed first into its pinned
    zone. Partially-pinned lines are carved into one piece per pinned
    zone plus a residual piece, so the rest of this function treats
    each piece as an independent (smaller) cargo line. Remaining cargo
    is grouped by destination: when several lines at this stop share a
    destination, the planner reserves a zone big enough for the GROUP
    rather than picking smallest-fit line-by-line, which would scatter
    same-dest cargo. Destination groups are processed earliest-unloaded
    first.

    Returns cargo_line_id → initial_zone_label for placed lines.
    """
    initial_zones: dict[int, str] = {}
    by_label = {z.zone_label: z for z in zones}

    # ── Carve partially-pinned lines into (pinned piece, residual)
    # _PlacedCargo objects so the rest of the function's logic is
    # unchanged. piece_pin maps a carved piece's id() to its zone.
    expanded: list[_PlacedCargo] = []
    piece_pin: dict[int, str] = {}
    for c in new_cargo:
        pieces = pinned_pieces.get(c.cargo_line_id)
        if not pieces:
            expanded.append(c)
            continue
        residual = list(c.pallet_sizes)
        for zone_label, sizes in pieces:
            if sizes is None:        # legacy whole-line pin
                carved = list(residual)
                residual = []
            else:
                carved = []
                for s in sizes:
                    if s in residual:
                        residual.remove(s)
                        carved.append(s)
            if carved:
                piece = _clone_placed(c, carved)
                piece_pin[id(piece)] = zone_label
                expanded.append(piece)
        if residual:
            expanded.append(_clone_placed(c, residual))
    new_cargo = expanded

    # ── Apply pins first so subsequent grouping sees their footprint ──
    pinned, auto = [], []
    for c in new_cargo:
        if piece_pin.get(id(c)) in by_label:
            pinned.append(c)
        else:
            auto.append(c)
    for c in pinned:
        z = by_label[piece_pin[id(c)]]
        if z.remaining_scu >= c.scu:
            _place_whole(z, c)
            initial_zones[c.cargo_line_id] = z.zone_label
            _log.info(
                "    cl#%d (%d SCU → %s): PINNED into %s",
                c.cargo_line_id, c.scu, c.delivery_station_name, z.zone_label,
            )
        else:
            _log.warning(
                "    cl#%d (%d SCU → %s): pinned zone %s has no room "
                "(%d SCU free) — falling back to auto-placement",
                c.cargo_line_id, c.scu, c.delivery_station_name,
                z.zone_label, z.remaining_scu,
            )
            auto.append(c)

    # ── Group remaining lines by destination ──
    by_dest: dict[int, list[_PlacedCargo]] = {}
    for c in auto:
        by_dest.setdefault(c.delivery_station_id, []).append(c)

    # Earliest-unloaded destinations first (tie-break: bigger group first).
    dest_order = sorted(
        by_dest,
        key=lambda did: (
            delivery_priority.get(did, 9999),
            -sum(c.scu for c in by_dest[did]),
        ),
    )

    for did in dest_order:
        group = sorted(by_dest[did], key=lambda c: -c.scu)
        group_total = sum(c.scu for c in group)

        # If the group total fits whole in one zone, take that zone
        # FIRST (reserving it for the destination's run) — this avoids
        # the small-fit-per-line trap that scatters same-dest cargo.
        # Every candidate is also checked for PHYSICAL fit so the
        # planner never reserves a zone that can't actually hold the
        # group's pallets.
        anchor: _ZoneState | None = None
        group_pallets = [s for c in group for s in c.pallet_sizes]
        topoff = [z for z in zones
                  if did in z.occupants
                  and z.remaining_scu >= group_total
                  and z.can_physically_fit(group_pallets)]
        if topoff:
            topoff.sort(key=lambda z: z.remaining_scu)
            anchor = topoff[0]
        else:
            fresh_fits = [z for z in zones
                          if z.is_empty
                          and z.scu_capacity >= group_total
                          and z.can_physically_fit(group_pallets)]
            if fresh_fits:
                fresh_fits.sort(key=lambda z: z.unload_priority)
                anchor = fresh_fits[0]

        if anchor is not None:
            for c in group:
                _place_whole(anchor, c)
                initial_zones[c.cargo_line_id] = anchor.zone_label
                _log.info(
                    "    cl#%d (%d SCU → %s): group-anchor in %s "
                    "[%d SCU dest total, prio=%d]",
                    c.cargo_line_id, c.scu, c.delivery_station_name,
                    anchor.zone_label, group_total, anchor.unload_priority,
                )
            continue

        # Group doesn't fit in any single zone — pack into the FEWEST
        # zones possible. Walk biggest fresh first, cram as many of
        # the destination's still-unplaced lines as fit, repeat. This
        # keeps the same-dest cargo contiguous so the planner doesn't
        # spread three 50 SCU lines across three bays when one R-bay
        # could hold two of them.
        remaining_lines = list(group)
        while remaining_lines:
            c = remaining_lines[0]
            # If more lines of this group still need a home, prefer
            # the BIGGEST fresh zone for the anchor so subsequent lines
            # can stack on top of c rather than scatter. When c is the
            # only line left, fall back to the normal smallest-fit
            # ranker (don't waste an R-bay on a leftover).
            z: _ZoneState | None = None
            reason = ""
            if len(remaining_lines) > 1:
                topoff = _pick_topoff_zone(zones, c)
                if topoff is not None:
                    z, reason = topoff, "topoff"
                else:
                    fresh_fits = [zz for zz in zones
                                  if zz.is_empty
                                  and zz.scu_capacity >= c.scu
                                  and zz.can_physically_fit(c.pallet_sizes)]
                    if fresh_fits:
                        fresh_fits.sort(
                            key=lambda zz: (-zz.scu_capacity, zz.unload_priority),
                        )
                        z, reason = fresh_fits[0], "fresh-largest"
            if z is None:
                z, reason = _pick_zone_for_whole_cargo(zones, c)
            if z is None:
                # Single line doesn't even fit in its best zone whole —
                # fall back to pallet-split for THIS line and continue.
                placed_total, first_zone = _place_split(c, zones)
                if placed_total > 0:
                    initial_zones[c.cargo_line_id] = first_zone
                    if placed_total < c.scu:
                        lost = c.scu - placed_total
                        _log_warn(
                            workday_id,
                            f"Cargo line {c.cargo_line_id} ({c.scu} SCU → "
                            f"{c.delivery_station_name}): only "
                            f"{placed_total} SCU placed; {lost} SCU "
                            f"abandoned (over capacity).",
                            c.cargo_line_id, conn,
                        )
                else:
                    _log_warn(
                        workday_id,
                        f"Cargo line {c.cargo_line_id} ({c.scu} SCU → "
                        f"{c.delivery_station_name}): NO zone room — "
                        f"entire line abandoned.",
                        c.cargo_line_id, conn,
                    )
                remaining_lines.pop(0)
                continue

            _place_whole(z, c)
            initial_zones[c.cargo_line_id] = z.zone_label
            _log.info(
                "    cl#%d (%d SCU → %s): pack-anchor in %s "
                "[%s, prio=%d, %d SCU free after]",
                c.cargo_line_id, c.scu, c.delivery_station_name,
                z.zone_label, reason, z.unload_priority, z.remaining_scu,
            )
            remaining_lines.pop(0)

            # Top off the same zone with whatever else from the group fits.
            i = 0
            while i < len(remaining_lines):
                nxt = remaining_lines[i]
                if (z.remaining_scu >= nxt.scu
                        and not _would_narrow_conflict(z, nxt)
                        and z.can_physically_fit(nxt.pallet_sizes)):
                    _place_whole(z, nxt)
                    initial_zones[nxt.cargo_line_id] = z.zone_label
                    _log.info(
                        "    cl#%d (%d SCU → %s): topoff into %s "
                        "[%d SCU free after]",
                        nxt.cargo_line_id, nxt.scu, nxt.delivery_station_name,
                        z.zone_label, z.remaining_scu,
                    )
                    remaining_lines.pop(i)
                else:
                    i += 1

    return initial_zones


def _place_split(
    c: _PlacedCargo, zones: list[_ZoneState],
) -> tuple[int, str]:
    """Split *c* across zones using physical pack-fit.

    For each iteration, walks the ranked candidate list (topoff first,
    then fresh by unload_priority, then mixed with narrow-conflict
    deprioritized) and picks the FIRST zone that can physically hold
    at least one of *c*'s remaining pallets. Places that subset, then
    repeats with the leftover.

    Returns (placed_total_scu, first_zone_label_used).
    """
    first_zone_label = ""
    placed_total = 0
    remaining = c
    while remaining.scu > 0:
        dest = remaining.delivery_station_id

        topoff = [z for z in zones
                  if dest in z.occupants and z.remaining_scu > 0]
        topoff.sort(key=lambda z: -z.remaining_scu)
        # Fresh: prefer the zone that can fit the MOST of the
        # remaining cargo. Without this, splitting a big line walked
        # zones in priority order (R1→R2→F1) and used 3 zones for
        # cargo that would have fit in 2 (R1+F1) had the algorithm
        # picked the bigger zone first. Tiebreak by unload_priority
        # ASC so same-fit-size ties still drain-first.
        remaining_total = remaining.scu
        # Zones whose column partner ALREADY holds this destination get a
        # bump — overflow within a column is preferred (no bulkhead between
        # F1<->R1, F2<->R2, etc. on the Hermes).
        partner_zones: set[str] = set()
        for z in zones:
            if dest in z.occupants:
                for p in (z.front_zone_label, z.back_zone_label):
                    if p:
                        partner_zones.add(p)
        fresh = [z for z in zones if z.is_empty]
        fresh.sort(key=lambda z: (
            0 if z.zone_label in partner_zones else 1,
            -min(z.scu_capacity, remaining_total),
            z.unload_priority,
        ))
        # Mixed: same fit-most preference as fresh. Earlier this
        # sorted by unload_priority alone, which fragmented a big
        # split line across many low-priority zones that each had
        # only a sliver of free space — e.g. a 39 SCU line crammed
        # 1+6+16+16 into R1..R4 instead of dropping 34 into an F-bay
        # that had a single large opening. Avoid narrow conflicts
        # first, then pick the zone that swallows the most, then
        # fall back to unload_priority.
        mixed = [z for z in zones
                 if not z.is_empty and dest not in z.occupants
                 and z.remaining_scu > 0]
        mixed.sort(key=lambda z: (
            1 if _would_narrow_conflict(z, remaining) else 0,
            0 if z.zone_label in partner_zones else 1,
            -min(z.remaining_scu, remaining_total),
            z.unload_priority,
        ))

        candidates = topoff + fresh + mixed
        if not candidates:
            break

        # Scan candidates for the first one that physically holds
        # something. The SCU-cap check alone isn't enough — a zone
        # with 16 free SCU but only a 4x1 cube strip can't take an
        # 8 SCU pallet (2x2 footprint).
        placed_piece: _PlacedCargo | None = None
        chosen: _ZoneState | None = None
        next_remaining: _PlacedCargo = remaining
        for z in candidates:
            trial_placed, trial_remaining = _split_into_zone(remaining, z)
            if trial_placed.scu > 0:
                placed_piece = trial_placed
                next_remaining = trial_remaining
                chosen = z
                break

        if placed_piece is None or chosen is None:
            # No candidate physically fits any pallet. Bail.
            break

        _place_whole(chosen, placed_piece)
        placed_total += placed_piece.scu
        remaining = next_remaining
        if not first_zone_label:
            first_zone_label = chosen.zone_label
        _log.info(
            "    cl#%d (split %d SCU into %s; %d SCU remaining)",
            c.cargo_line_id, placed_piece.scu, chosen.zone_label,
            remaining.scu,
        )

    return placed_total, first_zone_label


# ── Snapshot rendering ───────────────────────────────────────────────────

def _build_snapshot(
    zones: list[_ZoneState],
    cl_conflict: dict[int, int],
) -> list[LoadoutEntry]:
    """One LoadoutEntry per (zone × cargo line) currently onboard."""
    entries: list[LoadoutEntry] = []
    for z in zones:
        # Group pieces of the same cargo line that landed in this zone
        # (after splits or transload merges) into a single entry.
        by_cl: dict[int, list[_PlacedCargo]] = {}
        for c in z.placed:
            by_cl.setdefault(c.cargo_line_id, []).append(c)
        for cl_id, pieces in by_cl.items():
            sizes: list[int] = []
            scu = 0
            sample = pieces[0]
            for p in pieces:
                sizes.extend(p.pallet_sizes)
                scu += p.scu
            sizes.sort(reverse=True)
            entries.append(LoadoutEntry(
                zone_label=z.zone_label,
                cargo_line_id=cl_id,
                contract_number=sample.contract_number,
                commodity_name=sample.commodity_name,
                scu_amount=scu,
                delivery_station_name=sample.delivery_station_name,
                pallet_breakdown=palletize_summary(sizes),
                is_conflicted=cl_id in cl_conflict,
                conflict_group_id=cl_conflict.get(cl_id),
                pickup_station_name=getattr(
                    sample, "pickup_station_name", "",
                ),
            ))
    entries.sort(key=lambda e: (e.zone_label, e.delivery_station_name))
    return entries


# ── Persisted-row builder ────────────────────────────────────────────────

def _persist_assignments(
    workday_id: int,
    initial_zone: dict[int, str],
    cargo_meta: dict[int, _PlacedCargo],
    line_total_scu: dict[int, int],
    placed_total_scu: dict[int, int],
    manual_pinned_cl_ids: set[int],
    conn: sqlite3.Connection,
) -> None:
    """Write one zone_assignments row per cargo line we placed.

    Skips pinned cargo lines — they already have an
    is_manual_override=1 row that survived the DELETE above, so
    inserting an auto row would create a duplicate.

    primary_zone_label is the INITIAL load zone. Snapshots in
    RecomputeResult carry the post-transload state.
    """
    for cl_id, label in initial_zone.items():
        if cl_id in manual_pinned_cl_ids:
            continue
        c = cargo_meta[cl_id]
        placed = placed_total_scu.get(cl_id, 0)
        total = line_total_scu.get(cl_id, c.scu)
        note_bits = []
        if placed < total:
            note_bits.append(f"ABANDONED {total - placed} SCU "
                             f"(over capacity).")
        note = " ".join(note_bits) if note_bits else None
        # Use the line's full palletization for the row breakdown so the
        # downstream BayCanvas placement reflects every pallet.
        full_pallets = list(palletize(total, max_pallet_size_for(c)))
        conn.execute(
            """
            INSERT INTO zone_assignments
                (workday_id, cargo_line_id, primary_zone_label,
                 pallet_breakdown, is_manual_override, notes)
            VALUES (?, ?, ?, ?, 0, ?)
            """,
            (workday_id, cl_id, label,
             palletize_summary(full_pallets), note),
        )


def max_pallet_size_for(c: _PlacedCargo) -> int:
    # Recover the line's max pallet size from its palletization. The
    # actual db value is stashed in cargo_meta by build_zone_plan.
    return c._max_pallet_size  # type: ignore[attr-defined]


# ── Main entry ───────────────────────────────────────────────────────────

def build_zone_plan(
    workday_id: int,
    route_stops: list[RouteStop],
    conflict_groups,             # noqa: ARG001 — kept for API parity, unused
    conn: sqlite3.Connection,
) -> SimulationResult:
    """Walk the route and produce snapshots + transload moves.

    Side effect: rewrites non-manual `zone_assignments` rows for this
    workday with each cargo line's INITIAL load zone.
    """
    workday = conn.execute(
        "SELECT ship_id FROM workdays WHERE id = ?", (workday_id,)
    ).fetchone()
    if not workday:
        raise ValueError(f"Workday {workday_id} not found")

    zones = _load_zones(workday["ship_id"], conn)
    ship_capacity = sum(z.scu_capacity for z in zones)
    _log.info(
        "zone_assignment: workday=%s ship_id=%s zones=%d total_capacity=%d SCU",
        workday_id, workday["ship_id"], len(zones), ship_capacity,
    )
    _log.info("zone_assignment: zones — %s", _zones_summary(zones))

    if not zones:
        _log_warn(workday_id,
                  "No zones found for ship — cannot assign cargo.", None, conn)
        conn.commit()
        return SimulationResult(snapshots={}, transload_moves={})

    # Clear previous non-manual assignments — we'll rewrite below.
    conn.execute(
        "DELETE FROM zone_assignments "
        "WHERE workday_id = ? AND is_manual_override = 0",
        (workday_id,),
    )

    # Manual pins. A pinned cargo line may be pinned WHOLE (legacy —
    # pin_zones is NULL) or carved into per-zone PARTIAL pins (pin_zones
    # holds a JSON list of pieces). pinned_pieces maps a cargo line to
    # a list of (zone_label, sizes) pieces; sizes is None for a legacy
    # whole-line pin (meaning "all pallets").
    import json as _json
    manual_rows = conn.execute(
        """
        SELECT cl.id AS cargo_line_id, za.primary_zone_label, za.pin_zones
        FROM zone_assignments za
        JOIN cargo_lines cl ON cl.id = za.cargo_line_id
        JOIN contracts   ct ON ct.id = cl.contract_id
        WHERE za.workday_id = ?
          AND za.is_manual_override = 1
          AND ct.status != 'complete'
        """,
        (workday_id,),
    ).fetchall()
    pinned_pieces: dict[int, list[tuple[str, list[int] | None]]] = {}
    # Cargo line ids that have a surviving is_manual_override=1
    # zone_assignments row — these must NOT get an auto row written by
    # _persist_assignments (would create a duplicate). Pallet-lock-only
    # cargo lines DON'T have such a row, so they should still get one.
    manual_pinned_cl_ids: set[int] = set()
    for r in manual_rows:
        cl_id = r["cargo_line_id"]
        manual_pinned_cl_ids.add(cl_id)
        bucket = pinned_pieces.setdefault(cl_id, [])
        if r["pin_zones"]:
            try:
                parsed = _json.loads(r["pin_zones"])
            except (ValueError, TypeError):
                parsed = []
            for piece in parsed:
                bucket.append((piece["zone"], list(piece["sizes"])))
        else:
            # Legacy whole-line pin — no per-zone size data.
            bucket.append((r["primary_zone_label"], None))

    # Per-pallet locks. A lock fixes a single pallet of a cargo line to
    # a specific cube in a specific zone. We carry the (zone, x, y, z)
    # quad here for the renderer; for the planner's placement decision
    # only the zone matters. Convert each lock into an additional
    # pinned_pieces entry so the carve-and-pin logic in _load_at_stop
    # handles it just like a partial pin. A line with ANY pallet
    # locked is also tagged in locked_cl_ids so _consolidate /
    # _drain_to_lower_priority leave it alone (conservative: don't
    # second-guess user-placed cargo).
    lock_rows = conn.execute(
        """
        SELECT pl.cargo_line_id, pl.pallet_index,
               pl.zone_label, pl.cube_x, pl.cube_y, pl.cube_z
        FROM pallet_locks pl
        JOIN cargo_lines cl ON cl.id = pl.cargo_line_id
        JOIN contracts   ct ON ct.id = cl.contract_id
        WHERE pl.workday_id = ?
          AND ct.status != 'complete'
        ORDER BY pl.cargo_line_id, pl.pallet_index
        """,
        (workday_id,),
    ).fetchall()
    # Keep a structure keyed by cargo_line_id for callers/tests.
    pallet_locks: dict[int, list[tuple[int, str, int, int, int]]] = {}
    locked_cl_ids: set[int] = set()
    for r in lock_rows:
        pallet_locks.setdefault(r["cargo_line_id"], []).append(
            (r["pallet_index"], r["zone_label"],
             r["cube_x"], r["cube_y"], r["cube_z"])
        )
        locked_cl_ids.add(r["cargo_line_id"])

    # Build cargo meta for every cargo line in the workday.
    cargo_rows = conn.execute(
        """
        SELECT cl.id, cl.contract_id, cl.scu_amount, cl.commodity_id,
               cl.delivery_station_id,
               cm.name AS commodity_name,
               ds.name AS delivery_name,
               ps.name AS pickup_name,
               ct.contract_number, ct.max_pallet_size
        FROM cargo_lines cl
        JOIN contracts   ct ON ct.id = cl.contract_id
        JOIN stations    ds ON ds.id = cl.delivery_station_id
        JOIN stations    ps ON ps.id = ct.pickup_station_id
        JOIN commodities cm ON cm.id = cl.commodity_id
        WHERE ct.workday_id = ?
          AND ct.status != 'complete'
        ORDER BY cl.id
        """,
        (workday_id,),
    ).fetchall()

    cargo_meta: dict[int, _PlacedCargo] = {}
    line_total_scu: dict[int, int] = {}
    for r in cargo_rows:
        pallets = list(palletize(r["scu_amount"], r["max_pallet_size"]))
        c = _PlacedCargo(
            cargo_line_id=r["id"],
            contract_id=r["contract_id"],
            contract_number=r["contract_number"],
            commodity_id=r["commodity_id"],
            commodity_name=r["commodity_name"],
            delivery_station_id=r["delivery_station_id"],
            delivery_station_name=r["delivery_name"],
            scu=r["scu_amount"],
            pallet_sizes=pallets,
            pickup_station_name=r["pickup_name"] or "",
        )
        # Tag with max_pallet_size so we can re-palletize on persist.
        c._max_pallet_size = r["max_pallet_size"]  # type: ignore[attr-defined]
        cargo_meta[r["id"]] = c
        line_total_scu[r["id"]] = r["scu_amount"]

    if not cargo_meta:
        _log.info("zone_assignment: no cargo lines to place")
        conn.commit()
        return SimulationResult(snapshots={}, transload_moves={})

    # Merge pallet_locks into pinned_pieces. Each lock turns into a
    # single-pallet pin piece with sizes=[size_at_index]. Use the
    # cargo line's deterministic palletize() output to translate
    # pallet_index → size. Locks with out-of-range indices are
    # silently dropped (defensive; the controller already validates).
    for cl_id, locks in pallet_locks.items():
        c = cargo_meta.get(cl_id)
        if c is None:
            continue
        bucket = pinned_pieces.setdefault(cl_id, [])
        for (pidx, zone_label, _x, _y, _z) in locks:
            if 0 <= pidx < len(c.pallet_sizes):
                bucket.append((zone_label, [c.pallet_sizes[pidx]]))

    total_cargo = sum(line_total_scu.values())
    _log.info(
        "zone_assignment: %d cargo line(s), %d SCU total vs %d SCU capacity (%s)",
        len(cargo_meta), total_cargo, ship_capacity,
        "FITS" if total_cargo <= ship_capacity else "may exceed peak",
    )

    # Delivery priority: each destination's earliest stop index.
    delivery_priority: dict[int, int] = {}
    for idx, rs in enumerate(route_stops):
        for ref in rs.unloads:
            did = cargo_meta[ref.cargo_line_id].delivery_station_id
            delivery_priority.setdefault(did, idx)

    # Pickup index: stop_index → list of cargo_line_ids being loaded there.
    pickup_at: dict[int, list[int]] = {}
    for idx, rs in enumerate(route_stops):
        for ref in rs.loads:
            pickup_at.setdefault(idx, []).append(ref.cargo_line_id)

    initial_zone: dict[int, str] = {}
    placed_total_scu: dict[int, int] = {}
    snapshots: dict[int, list[LoadoutEntry]] = {}
    transload_moves: dict[int, list[TransloadMove]] = {}

    # Conflict info for snapshot tagging (strict mode only; empty otherwise).
    cl_conflict: dict[int, int] = {
        r["cargo_line_id"]: r["conflict_group_id"]
        for r in conn.execute(
            "SELECT cargo_line_id, conflict_group_id FROM pallet_conflicts "
            "WHERE workday_id = ?", (workday_id,),
        ).fetchall()
    }

    # ── Walk the route ───────────────────────────────────────────────────
    for idx, rs in enumerate(route_stops):
        _log.info(
            "  stop %d: %s (%s) loads=%d unloads=%d",
            rs.stop_number, rs.station_name, rs.action,
            len(rs.loads), len(rs.unloads),
        )

        # 1. UNLOAD
        unloaded = _unload_at_stop(zones, rs.station_id)
        if unloaded:
            _log.info("    unloaded %d cargo line(s) totalling %d SCU",
                      len(unloaded), sum(u.scu for u in unloaded))

        # 2. LOAD
        new_cargo_ids = pickup_at.get(idx, [])
        if new_cargo_ids:
            # Make a fresh _PlacedCargo per line so the original
            # cargo_meta entries aren't mutated as we walk stops.
            new_cargo = []
            for cl_id in new_cargo_ids:
                src = cargo_meta[cl_id]
                cp = _PlacedCargo(
                    cargo_line_id=src.cargo_line_id,
                    contract_id=src.contract_id,
                    contract_number=src.contract_number,
                    commodity_id=src.commodity_id,
                    commodity_name=src.commodity_name,
                    delivery_station_id=src.delivery_station_id,
                    delivery_station_name=src.delivery_station_name,
                    scu=src.scu,
                    pallet_sizes=list(src.pallet_sizes),
                    pickup_station_name=src.pickup_station_name,
                )
                cp._max_pallet_size = src._max_pallet_size  # type: ignore[attr-defined]
                new_cargo.append(cp)
            placed_by_id = _load_at_stop(
                zones, new_cargo, pinned_pieces, workday_id, conn,
                delivery_priority,
            )
            for cl_id, label in placed_by_id.items():
                initial_zone.setdefault(cl_id, label)
            # Track how many SCU we actually placed per line for warnings.
            for c in new_cargo:
                cl_id = c.cargo_line_id
                onboard = sum(
                    p.scu for z in zones for p in z.placed
                    if p.cargo_line_id == cl_id
                )
                placed_total_scu[cl_id] = onboard

        # 3. TRANSLOAD — consolidate AFTER loading, so any same-dest
        # split created by the load can be merged before takeoff. This
        # is also the natural physical order: pilot unloads, loads, then
        # tidies up the bay before flying out. Skipped on the final
        # stop (everything's unloaded by then).
        moves: list[TransloadMove] = []
        if idx < len(route_stops) - 1:
            pinned_ids = set(pinned_pieces.keys())
            moves = _consolidate(zones, pinned_ids=pinned_ids)
            # Drain bulk floor (highest-priority zones) into lower-
            # priority same-dest zones whenever there's now room.
            # Runs AFTER _consolidate so any merges into the largest
            # same-dest holder happen first, then this pulls anything
            # still stranded in RBA/RBF down to the F/R columns.
            drain = _drain_to_lower_priority(zones, pinned_ids=pinned_ids)
            moves = moves + drain
            if moves:
                _log.info(
                    "    transload: %d move(s) to consolidate same-dest cargo",
                    len(moves),
                )
                for m in moves:
                    _log.info(
                        "      cl#%d (%d SCU %s → %s): %s → %s",
                        m.cargo_line_id, m.scu_amount, m.commodity_name,
                        m.delivery_station_name, m.from_zone, m.to_zone,
                    )
        transload_moves[rs.stop_number] = moves

        # 4. SNAPSHOT (post-unload, post-load, post-transload)
        snapshots[rs.stop_number] = _build_snapshot(zones, cl_conflict)

    # ── Persist initial-zone rows ────────────────────────────────────────
    _persist_assignments(
        workday_id, initial_zone, cargo_meta,
        line_total_scu, placed_total_scu,
        manual_pinned_cl_ids, conn,
    )

    _log.info("zone_assignment: %d cargo line(s) placed, %d transload moves",
              len(initial_zone),
              sum(len(v) for v in transload_moves.values()))

    conn.commit()
    return SimulationResult(snapshots=snapshots, transload_moves=transload_moves)
