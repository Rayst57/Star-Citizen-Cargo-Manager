"""
AppController — single business-logic bridge between the UI, the planner,
the database, and the voice subsystem.

UI widgets hold a reference to one of these but never import from
src.planner directly.  All planner functions are called from here and
the controller emits Qt signals to refresh the UI.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal

from .planner.advisory import Advisory, advisories_for_result
from .planner.canonicalize import canonical_commodity, canonical_station
from .planner.conflicts import ConflictGroup
from .planner.physical_packer import best_pack, box_for, pack_with_locks
from .planner.recompute import RecomputeResult, recompute as run_recompute
from .planner.route import RouteStop
from .palletizer_color import assign_destination_colors
from .settings import AppSettings, get_api_key
from .undo import UndoStack


# Debug log — written next to the .exe / repo root so the user can share it
def _log_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent / "cargo_manager.log"
    return Path(__file__).resolve().parents[1] / "cargo_manager.log"


_log = logging.getLogger("cargo_manager")
if not _log.handlers:
    try:
        _handler = logging.FileHandler(_log_path(), mode="w", encoding="utf-8")
        _handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        )
        _log.addHandler(_handler)
        _log.setLevel(logging.INFO)
        _log.info("cargo_manager log started")
    except Exception:
        pass


# ── Zone strip / pallet rect data for BayCanvas ──────────────────────────

@dataclass
class ZoneStripDestination:
    station_id: int
    station_name: str
    color: str
    scu_amount: int


@dataclass
class ZoneStrip:
    zone_label: str
    bay: str
    cube_offset_x: int
    cube_offset_y: int
    width_units: int
    length_units: int
    height_units: int
    scu_capacity: int
    used_scu: int
    destinations: list[ZoneStripDestination]
    is_conflicted: bool
    # 'high' (no flip) or 'low' (flip on screen — F-bay style). Drives
    # the rear-view top-down rendering convention so ship-forward is
    # always at the top of the screen.
    ship_forward_y: str = "high"
    # Colors of destinations that conflict with this zone's cargo (used
    # to draw stripes in the conflicting destination's colors instead
    # of plain red).
    conflict_partner_colors: list[str] = None

    def __post_init__(self):
        if self.conflict_partner_colors is None:
            self.conflict_partner_colors = []

    @property
    def is_empty(self) -> bool:
        return self.used_scu == 0

    @property
    def is_mixed(self) -> bool:
        return len(self.destinations) > 1

    @property
    def primary_color(self) -> str:
        if not self.destinations:
            return "#3a4894"
        return self.destinations[0].color


@dataclass
class BayZoneGeom:
    """Static geometry of a single zone — no route/cargo state."""
    zone_label: str
    cube_offset_x: int
    cube_offset_y: int
    width_units: int
    length_units: int
    height_units: int = 4


@dataclass
class BayLayout:
    """A cargo bay's static structure, used by BayCanvas to draw the
    bay outline + zone grid regardless of whether a route exists."""
    bay_label: str
    width_cells: int
    length_cells: int
    scu_capacity: int
    # 'high' = render un-flipped (ramp end derived below); 'low' = flip.
    ship_forward_y: str
    # True when the ramp ends up at the TOP of the screen diagram.
    ramp_at_top: bool
    ramp_label: str
    zones: list[BayZoneGeom]


@dataclass
class PalletRect:
    """A single pallet drawn on the BayCanvas viewport."""
    cargo_line_id: int
    zone_label: str
    bay: str
    cell_x: int
    cell_y: int
    cell_z: int                 # vertical position in 1.25 m cubes (0 = floor)
    cell_w: int
    cell_l: int
    cell_h: int
    pallet_size: int
    color: str
    is_conflicted: bool         # True only if THIS pallet's size is ambiguous
    label: str
    delivery_station_name: str
    commodity_name: str
    contract_number: int
    # Pickup station name — needed for hover tooltips that show the
    # full Pickup → Destination route. Filled from the cargo line's
    # contract.pickup_station_id by get_pallet_rects.
    pickup_station_name: str = ""
    # 0-based position in the cargo line's deterministic palletize()
    # output. Combined with cargo_line_id this is the pallet's stable
    # identity — used by the UI to address individual pallets (e.g.
    # to call controller.lock_pallet(...) from a 3D bird's-eye view).
    pallet_index: int = 0
    # 'high' (no flip) or 'low' (flip on screen — F-bay style). The
    # renderer combines this with the bay's hardcoded length to mirror
    # pallet Y positions when ship-forward is at low local-Y.
    ship_forward_y: str = "high"
    # Colors of the OTHER destinations sharing this pallet's ambiguous
    # size — used to render stripes in those destinations' colors.
    conflict_partner_colors: list[str] = None

    def __post_init__(self):
        if self.conflict_partner_colors is None:
            self.conflict_partner_colors = []


# ── Recompute thread ──────────────────────────────────────────────────────

class RecomputeThread(QThread):
    finished_with_result = Signal(object)   # RecomputeResult
    failed = Signal(str)

    def __init__(self, db_path: Path, workday_id: int):
        super().__init__()
        self.db_path = db_path
        self.workday_id = workday_id

    def run(self) -> None:
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            try:
                _log.info("RecomputeThread.run() begin (workday=%s)", self.workday_id)
                result = run_recompute(self.workday_id, conn)
                _log.info("RecomputeThread.run() success — emitting result")
                self.finished_with_result.emit(result)
            finally:
                conn.close()
        except Exception as e:
            _log.error("RecomputeThread.run() failed:\n%s", traceback.format_exc())
            self.failed.emit(f"{type(e).__name__}: {e}")


# ── Tool errors ──────────────────────────────────────────────────────────

class ToolError(Exception):
    """Raised by dispatch_tool when arguments are invalid."""


# ── AppController ─────────────────────────────────────────────────────────

class AppController(QObject):
    # ── signals ──────────────────────────────────────────────────────────
    contracts_changed   = Signal()
    route_changed       = Signal()
    plan_dirty_changed  = Signal(bool)
    recompute_started   = Signal()
    recompute_done      = Signal(object)        # RecomputeResult
    recompute_failed    = Signal(str)
    stop_progress       = Signal(int, int)      # current_stop, total_stops
    scu_usage           = Signal(int, int)      # used_scu, total_scu
    mic_state_changed   = Signal(str)
    api_health_changed  = Signal(bool)
    voice_response      = Signal(str)           # transient toast

    def __init__(self, conn: sqlite3.Connection, db_path: Path):
        super().__init__()
        self.conn = conn
        self.db_path = db_path
        self.settings = AppSettings(conn)
        self.undo_stack = UndoStack()

        self.workday_id: int | None = None
        self._last_result: RecomputeResult | None = None
        self._advisories_cache: dict[int, list[Advisory]] | None = None
        self._recomputing: bool = False
        self._recompute_thread: RecomputeThread | None = None
        self._current_stop_index = 0   # 0 = before first stop completed

        self.api_key: str | None = get_api_key()
        _log.info("AppController initialised. db_path=%s", db_path)

    # ── workday lifecycle ────────────────────────────────────────────────

    def find_open_workday(self) -> dict | None:
        row = self.conn.execute(
            """
            SELECT w.id, w.started_at, w.origin_station_id,
                   s.name AS origin_name,
                   sh.name AS ship_name,
                   (SELECT count(*) FROM contracts c WHERE c.workday_id = w.id) AS n_contracts
            FROM workdays w
            JOIN stations s ON s.id = w.origin_station_id
            JOIN ships    sh ON sh.id = w.ship_id
            WHERE w.ended_at IS NULL
            ORDER BY w.id DESC LIMIT 1
            """
        ).fetchone()
        return dict(row) if row else None

    def resume_workday(self, workday_id: int) -> None:
        row = self.conn.execute(
            "SELECT id, plan_dirty FROM workdays "
            "WHERE id = ? AND ended_at IS NULL",
            (workday_id,),
        ).fetchone()
        if not row:
            raise ValueError(f"Workday {workday_id} is not open")
        self.workday_id = workday_id
        self._current_stop_index = 0
        self.contracts_changed.emit()
        self.route_changed.emit()
        self._emit_progress()
        # Resume needs to fire plan_dirty_changed so the Recompute
        # banner appears. Without this signal the banner stays
        # hidden — the user sees the contracts but has no button to
        # trigger a (re)compute.
        self.plan_dirty_changed.emit(bool(row["plan_dirty"]))

    def list_ships(self) -> list[sqlite3.Row]:
        """All selectable ships, ordered for the workday picker."""
        return self.conn.execute(
            """
            SELECT id, name, manufacturer, total_scu
            FROM ships
            WHERE is_active = 1
            ORDER BY total_scu DESC, name
            """
        ).fetchall()

    def start_workday(
        self,
        origin_station_id: int,
        final_destination_id: int | None,
        round_robin: bool,
        ship_id: int | None = None,
    ) -> int:
        """Create a fresh workday after closing any open one.

        *ship_id* picks the ship; when omitted the largest-capacity
        active ship is used as a sensible default.
        """
        # Close existing open workday
        self.conn.execute(
            "UPDATE workdays SET ended_at = ? WHERE ended_at IS NULL",
            (datetime.now(timezone.utc).isoformat(),),
        )
        if ship_id is None:
            ships = self.list_ships()
            if not ships:
                raise RuntimeError("No ships in DB — seed failed")
            ship_id = ships[0]["id"]

        cur = self.conn.execute(
            """
            INSERT INTO workdays
              (started_at, ship_id, origin_station_id,
               final_destination_station_id, round_robin, plan_dirty)
            VALUES (?, ?, ?, ?, ?, 0)
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                ship_id,
                origin_station_id,
                final_destination_id if not round_robin else origin_station_id,
                1 if round_robin else 0,
            ),
        )
        self.workday_id = cur.lastrowid
        self.conn.commit()
        self.undo_stack.clear()
        self._current_stop_index = 0
        self.contracts_changed.emit()
        self.route_changed.emit()
        self._emit_progress()
        return self.workday_id

    def end_workday(self) -> None:
        if not self.workday_id:
            return
        self.conn.execute(
            "UPDATE workdays SET ended_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), self.workday_id),
        )
        self.conn.commit()
        self.workday_id = None
        self._last_result = None
        self._advisories_cache = None
        self.contracts_changed.emit()
        self.route_changed.emit()

    # ── workday metadata setters ─────────────────────────────────────────

    def set_origin(self, station_id: int) -> None:
        self._require_workday()
        self.conn.execute(
            "UPDATE workdays SET origin_station_id = ?, plan_dirty = 1 WHERE id = ?",
            (station_id, self.workday_id),
        )
        self.conn.commit()
        self.plan_dirty_changed.emit(True)

    def set_final_destination(self, station_id: int | None) -> None:
        self._require_workday()
        self.conn.execute(
            "UPDATE workdays SET final_destination_station_id = ?, plan_dirty = 1 WHERE id = ?",
            (station_id, self.workday_id),
        )
        self.conn.commit()
        self.plan_dirty_changed.emit(True)

    def set_round_robin(self, enabled: bool) -> None:
        self._require_workday()
        self.conn.execute(
            "UPDATE workdays SET round_robin = ?, plan_dirty = 1 WHERE id = ?",
            (1 if enabled else 0, self.workday_id),
        )
        self.conn.commit()
        self.plan_dirty_changed.emit(True)

    # ── contracts ────────────────────────────────────────────────────────

    def list_contracts(self) -> list[dict]:
        if not self.workday_id:
            return []
        rows = self.conn.execute(
            """
            SELECT c.id, c.contract_number, c.max_pallet_size,
                   c.pickup_station_id, ps.name AS pickup_name,
                   c.status
            FROM contracts c
            JOIN stations ps ON ps.id = c.pickup_station_id
            WHERE c.workday_id = ?
            ORDER BY c.contract_number
            """,
            (self.workday_id,),
        ).fetchall()
        out: list[dict] = []
        for r in rows:
            lines = self.conn.execute(
                """
                SELECT cl.id, cl.line_number, cl.scu_amount,
                       cl.delivery_station_id, ds.name AS delivery_name,
                       cl.commodity_id, cm.name AS commodity_name
                FROM cargo_lines cl
                JOIN stations    ds ON ds.id = cl.delivery_station_id
                JOIN commodities cm ON cm.id = cl.commodity_id
                WHERE cl.contract_id = ?
                ORDER BY cl.line_number
                """,
                (r["id"],),
            ).fetchall()
            out.append({
                **dict(r),
                "deliveries": [dict(l) for l in lines],
                "total_scu": sum(l["scu_amount"] for l in lines),
            })
        return out

    def export_contracts(self) -> dict:
        """Return the current workday's contracts as a portable dict.

        Stations and commodities are referenced by NAME so the dump
        survives a move to another DB / install.
        """
        contracts = [
            {
                "pickup_station":  c["pickup_name"],
                "max_pallet_size": c["max_pallet_size"],
                "deliveries": [
                    {
                        "destination": d["delivery_name"],
                        "commodity":   d["commodity_name"],
                        "scu":         d["scu_amount"],
                    }
                    for d in c["deliveries"]
                ],
            }
            for c in self.list_contracts()
        ]
        return {
            "version":     1,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "contracts":   contracts,
        }

    def import_contracts(self, payload: dict) -> tuple[int, list[str]]:
        """Add every contract from *payload* to the current workday.

        Returns (added_count, error_messages). Skips and reports any
        contract whose station or commodity name doesn't resolve in
        the local DB instead of aborting the whole import.
        """
        if not isinstance(payload, dict) or "contracts" not in payload:
            raise ToolError(
                "Bad import file: expected an object with a 'contracts' list."
            )
        added = 0
        errors: list[str] = []
        for i, c in enumerate(payload["contracts"], start=1):
            try:
                self.add_contract(c)
                added += 1
            except (ToolError, KeyError, TypeError, ValueError) as e:
                errors.append(f"Contract {i}: {e}")
        return added, errors

    def add_contract(self, data: dict) -> int:
        """Add a contract.

        Args:
            data: {"pickup_station": str | int,
                   "pickup_candidates": list[str|int] (optional),
                   "max_pallet_size": int (default 8),
                   "deliveries": [{"destination": str|int,
                                   "commodity": str|int,
                                   "scu": int}, ...]}

        When ``pickup_candidates`` is non-empty the contract is a
        multi-pickup contract: the primary pickup_station is the first
        candidate (sequence_order=0) and every entry in
        pickup_candidates is appended after it as an additional
        candidate. The cargo MAY be at any of these stations; the
        planner conservatively reserves SCU from the first candidate
        visit onward.

        Returns:
            The new contract_id.
        """
        self._require_workday()
        self.undo_stack.capture(self.workday_id, self.conn)

        pickup_id = self._resolve_station(data["pickup_station"])
        max_size = int(data.get("max_pallet_size", 8))
        if max_size not in (1, 2, 4, 8, 16, 24, 32):
            raise ToolError(f"Invalid max_pallet_size: {max_size}")

        deliveries = data.get("deliveries") or []
        if not deliveries:
            raise ToolError("Contract must have at least one delivery")

        # Resolve any extra pickup candidates up front so a bad name
        # aborts before we insert the contract row.
        raw_candidates = data.get("pickup_candidates") or []
        candidate_ids: list[int] = []
        if raw_candidates:
            seen = {pickup_id}
            candidate_ids.append(pickup_id)
            for ref in raw_candidates:
                cid_station = self._resolve_station(ref)
                if cid_station in seen:
                    continue
                seen.add(cid_station)
                candidate_ids.append(cid_station)

        # Next contract_number for this workday
        row = self.conn.execute(
            "SELECT COALESCE(MAX(contract_number), 0) + 1 AS n FROM contracts WHERE workday_id = ?",
            (self.workday_id,),
        ).fetchone()
        contract_number = row["n"]
        now = datetime.now(timezone.utc).isoformat()

        cur = self.conn.execute(
            """
            INSERT INTO contracts
              (workday_id, contract_number, pickup_station_id,
               max_pallet_size, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'pending', ?, ?)
            """,
            (self.workday_id, contract_number, pickup_id, max_size, now, now),
        )
        contract_id = cur.lastrowid

        for idx, d in enumerate(deliveries, start=1):
            dest_id = self._resolve_station(d["destination"])
            comm_id = self._resolve_commodity(d["commodity"])
            scu = int(d["scu"])
            if scu <= 0:
                raise ToolError(f"SCU must be > 0 (got {scu})")
            self.conn.execute(
                """
                INSERT INTO cargo_lines
                  (contract_id, line_number, delivery_station_id,
                   commodity_id, scu_amount)
                VALUES (?, ?, ?, ?, ?)
                """,
                (contract_id, idx, dest_id, comm_id, scu),
            )

        # Persist any multi-pickup candidates (only when the caller
        # supplied them — legacy single-pickup contracts skip this).
        for seq, station_id in enumerate(candidate_ids):
            self.conn.execute(
                """
                INSERT INTO contract_pickup_candidates
                  (contract_id, station_id, sequence_order)
                VALUES (?, ?, ?)
                """,
                (contract_id, station_id, seq),
            )

        self._set_dirty()
        self.contracts_changed.emit()
        return contract_id

    def list_pickup_candidates(self, contract_id: int) -> list[sqlite3.Row]:
        """All pickup candidates for *contract_id* joined to station name,
        ordered by sequence_order (primary first)."""
        return self.conn.execute(
            """
            SELECT cpc.contract_id, cpc.station_id, cpc.sequence_order,
                   s.name AS station_name
            FROM contract_pickup_candidates cpc
            JOIN stations s ON s.id = cpc.station_id
            WHERE cpc.contract_id = ?
            ORDER BY cpc.sequence_order
            """,
            (contract_id,),
        ).fetchall()

    def edit_contract(self, contract_number: int, data: dict) -> None:
        self._require_workday()
        self.undo_stack.capture(self.workday_id, self.conn)
        c = self.conn.execute(
            "SELECT id FROM contracts WHERE workday_id = ? AND contract_number = ?",
            (self.workday_id, contract_number),
        ).fetchone()
        if not c:
            raise ToolError(f"Contract {contract_number} not found")
        cid = c["id"]

        if "max_pallet_size" in data:
            ms = int(data["max_pallet_size"])
            if ms not in (1, 2, 4, 8, 16, 24, 32):
                raise ToolError(f"Invalid max_pallet_size: {ms}")
            self.conn.execute(
                "UPDATE contracts SET max_pallet_size = ?, updated_at = ? WHERE id = ?",
                (ms, datetime.now(timezone.utc).isoformat(), cid),
            )

        if "pickup_station" in data:
            pid = self._resolve_station(data["pickup_station"])
            self.conn.execute(
                "UPDATE contracts SET pickup_station_id = ? WHERE id = ?",
                (pid, cid),
            )

        if "deliveries" in data:
            self.conn.execute("DELETE FROM cargo_lines WHERE contract_id = ?", (cid,))
            for idx, d in enumerate(data["deliveries"], start=1):
                dest_id = self._resolve_station(d["destination"])
                comm_id = self._resolve_commodity(d["commodity"])
                scu = int(d["scu"])
                if scu <= 0:
                    raise ToolError(f"SCU must be > 0 (got {scu})")
                self.conn.execute(
                    """
                    INSERT INTO cargo_lines
                      (contract_id, line_number, delivery_station_id,
                       commodity_id, scu_amount)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (cid, idx, dest_id, comm_id, scu),
                )

        self._set_dirty()
        self.contracts_changed.emit()

    def remove_contract(self, contract_number: int) -> None:
        self._require_workday()
        self.undo_stack.capture(self.workday_id, self.conn)
        c = self.conn.execute(
            "SELECT id FROM contracts WHERE workday_id = ? AND contract_number = ?",
            (self.workday_id, contract_number),
        ).fetchone()
        if not c:
            raise ToolError(f"Contract {contract_number} not found")
        self.conn.execute("DELETE FROM contracts WHERE id = ?", (c["id"],))
        # Renumber remaining contracts to keep them contiguous
        rows = self.conn.execute(
            "SELECT id FROM contracts WHERE workday_id = ? ORDER BY contract_number",
            (self.workday_id,),
        ).fetchall()
        for new_n, r in enumerate(rows, start=1):
            self.conn.execute(
                "UPDATE contracts SET contract_number = ? WHERE id = ?",
                (new_n, r["id"]),
            )
        self._set_dirty()
        self.contracts_changed.emit()

    def clear_contracts(self) -> None:
        self._require_workday()
        self.undo_stack.capture(self.workday_id, self.conn)
        self.conn.execute(
            "DELETE FROM contracts WHERE workday_id = ?", (self.workday_id,)
        )
        self._set_dirty()
        self.contracts_changed.emit()

    def undo(self) -> bool:
        if not self.undo_stack.can_undo():
            return False
        self.undo_stack.restore(self.conn)
        self.contracts_changed.emit()
        self.plan_dirty_changed.emit(True)
        return True

    # ── recompute ────────────────────────────────────────────────────────

    def recompute(self) -> None:
        """Spawn a RecomputeThread to run the planner off the UI thread."""
        _log.info("recompute() called; flag=%s workday=%s",
                  self._recomputing, self.workday_id)
        self._require_workday()
        if self._recomputing:
            _log.warning("recompute() ignored — another recompute is already running")
            return
        self._recomputing = True
        self.recompute_started.emit()
        # Commit pending state before spawning a thread that opens its own conn
        self.conn.commit()
        thread = RecomputeThread(self.db_path, self.workday_id)
        thread.finished_with_result.connect(self._on_recompute_done)
        thread.failed.connect(self._on_recompute_failed)
        thread.finished.connect(self._clear_recomputing_flag)
        thread.finished.connect(thread.deleteLater)
        # CRITICAL: keep a Python reference to the QThread or Python's
        # garbage collector will destroy it before run() completes,
        # crashing Qt with __fastfail (0xc0000409 on Windows).
        self._recompute_thread = thread
        thread.start()
        _log.info("RecomputeThread started")

    def _clear_recomputing_flag(self) -> None:
        self._recomputing = False
        self._recompute_thread = None     # safe to drop the reference now
        _log.info("recompute flag cleared")

    def _on_recompute_done(self, result: RecomputeResult) -> None:
        # Each step is wrapped so a downstream slot misbehaving cannot
        # cascade into a "Recompute failed" popup. Anything that goes
        # wrong is captured in cargo_manager.log instead.
        _log.info(
            "recompute done — %d stops, %d conflict groups, %d snapshots",
            len(result.route_stops),
            len(result.conflict_groups),
            len(result.snapshots),
        )
        # Per-stop summary
        for s in result.route_stops:
            _log.info(
                "  stop %d: %s (%s) loads=%d unloads=%d",
                s.stop_number, s.station_name, s.action,
                len(s.loads), len(s.unloads),
            )
        for grp in result.conflict_groups:
            dests = ", ".join(d.delivery_station_name for d in grp.destinations)
            _log.info(
                "  conflict group %d: %s × %s [dests=%s, ambiguous_sizes=%s]",
                grp.group_id, grp.pickup_station_name, grp.commodity_name,
                dests, grp.ambiguous_sizes,
            )

        try:
            self._last_result = result
            # Invalidate the advisory cache — next compute_advisories()
            # call will rebuild from the fresh snapshot.
            self._advisories_cache = None
            if self.workday_id:
                assign_destination_colors(self.workday_id, self.conn)
        except Exception:
            _log.error("assign_destination_colors raised:\n%s",
                       traceback.format_exc())
        for emit_fn, label in (
            (lambda: self.plan_dirty_changed.emit(False), "plan_dirty_changed"),
            (self.contracts_changed.emit, "contracts_changed"),
            (self.route_changed.emit, "route_changed"),
            (self._emit_progress, "stop_progress"),
            (lambda: self.recompute_done.emit(result), "recompute_done"),
        ):
            try:
                emit_fn()
            except Exception:
                _log.error(
                    "Slot raised on signal %s:\n%s",
                    label, traceback.format_exc(),
                )

    def _on_recompute_failed(self, message: str) -> None:
        _log.error("recompute failed: %s", message)
        self.recompute_failed.emit(message)

    def get_last_result(self) -> RecomputeResult | None:
        return self._last_result

    # ── advisories ───────────────────────────────────────────────────────

    def compute_advisories(self) -> dict[int, list[Advisory]]:
        """Return a dict of stop_number -> list of Advisory objects.

        Results are cached against the current recompute result; the
        cache is invalidated when a new recompute completes (see
        _on_recompute_done) so subsequent calls always reflect the
        latest plan.
        """
        if not self._last_result:
            return {}
        if self._advisories_cache is None:
            self._advisories_cache = advisories_for_result(
                self._last_result, self.conn,
            )
        return self._advisories_cache

    # ── BayCanvas data ───────────────────────────────────────────────────

    def get_pallet_rects(self, stop_number: int | None = None) -> list[PalletRect]:
        """Return the list of PalletRect for the BayCanvas.

        If *stop_number* is None, returns the snapshot for the current stop.
        Otherwise returns the snapshot at the requested stop.
        """
        if not self._last_result or not self.workday_id:
            return []
        snap_key = stop_number if stop_number is not None else self._current_stop_index
        snap_key = snap_key or 1
        entries = self._last_result.snapshots.get(snap_key, [])

        # Load zone metadata + ship_zones for placement math
        zone_meta = {
            r["zone_label"]: dict(r)
            for r in self.conn.execute(
                """
                SELECT z.zone_label, z.bay_label, z.width_units, z.length_units,
                       z.cube_offset_x, z.cube_offset_y, z.scu_capacity,
                       z.ship_forward_y
                FROM ship_zones z
                JOIN workdays w ON w.ship_id = z.ship_id
                WHERE w.id = ?
                """,
                (self.workday_id,),
            ).fetchall()
        }

        # Map delivery_station_name → color via DB
        color_rows = self.conn.execute(
            "SELECT name, color_hex FROM stations WHERE color_hex IS NOT NULL"
        ).fetchall()
        color_map = {r["name"]: r["color_hex"] for r in color_rows}

        # Per-cargo-line conflict info: which sizes are ambiguous AND the
        # partner destinations' colors for striping.
        cl_conflict_info: dict[int, tuple[set[int], list[str]]] = {}
        for grp in (self._last_result.conflict_groups
                    if self._last_result else []):
            amb_sizes = set(grp.ambiguous_sizes)
            dest_colors = {
                d.delivery_station_id: color_map.get(d.delivery_station_name, "#888888")
                for d in grp.destinations
            }
            for d in grp.destinations:
                partners = [
                    c for sid, c in dest_colors.items()
                    if sid != d.delivery_station_id
                ]
                for cl_id in d.cargo_line_ids:
                    cl_conflict_info[cl_id] = (amb_sizes, partners)

        # Load per-workday pallet locks. Locks are keyed by
        # (cargo_line_id, pallet_index); each zone uses only the locks
        # whose zone_label matches.
        lock_rows = self.conn.execute(
            """
            SELECT cargo_line_id, pallet_index, zone_label,
                   cube_x, cube_y, cube_z, orientation
            FROM pallet_locks
            WHERE workday_id = ?
            """,
            (self.workday_id,),
        ).fetchall()
        # Value is (cube_x, cube_y, cube_z, orientation).
        locks_by_zone: dict[str, dict[tuple[int, int], tuple[int, int, int, int]]] = {}
        for r in lock_rows:
            locks_by_zone.setdefault(r["zone_label"], {})[
                (r["cargo_line_id"], r["pallet_index"])
            ] = (r["cube_x"], r["cube_y"], r["cube_z"], r["orientation"])

        rects: list[PalletRect] = []

        # Group entries by zone so we place each zone's contents in a single
        # large→small pass.
        entries_by_zone: dict[str, list] = {}
        for e in entries:
            entries_by_zone.setdefault(e.zone_label, []).append(e)

        for zone_label, zone_entries in entries_by_zone.items():
            zone = zone_meta.get(zone_label)
            if not zone:
                continue
            zw = zone["width_units"]
            zl = zone["length_units"]
            zh = zone["scu_capacity"] // (zw * zl) if zw * zl else 4

            zone_locks = locks_by_zone.get(zone_label, {})

            # Build the (entry, size, pallet_index) keyed list for the
            # packer. pallet_index is the 0-based position of *this*
            # pallet within the cargo line's full deterministic
            # breakdown. We track per-cl_id "consumed" indices so two
            # entries for the same line (which can happen after splits
            # are merged into a single snapshot row) don't reuse the
            # same index.
            placements: list[tuple] = []  # (key, size) where key=(entry, pallet_index)
            locked_entries: list[tuple] = []  # (key, size, w, l, h, x, y, z)
            seen_index_for_cl: dict[int, int] = {}
            for entry in zone_entries:
                sizes = _parse_breakdown(entry.pallet_breakdown) or [entry.scu_amount]
                start_idx = seen_index_for_cl.get(entry.cargo_line_id, 0)
                for offset, size in enumerate(sizes):
                    pallet_idx = start_idx + offset
                    key = (entry, pallet_idx)
                    lock = zone_locks.get((entry.cargo_line_id, pallet_idx))
                    if lock is not None:
                        # Lookup the footprint for the lock.
                        box = box_for(size)
                        w, l, h = box["width"], box["length"], box["height"]
                        lock_orientation = lock[3] if len(lock) > 3 else 0
                        if lock_orientation == 1 and box.get("rotatable"):
                            # User requested a horizontal rotation.
                            w, l = l, w
                        elif w > zw and box.get("rotatable") and l <= zw:
                            # Auto-rotate fallback for orientation=0
                            # when the natural footprint won't fit.
                            w, l = l, w
                        locked_entries.append(
                            (key, size, w, l, h, lock[0], lock[1], lock[2])
                        )
                    else:
                        placements.append((key, size))
                seen_index_for_cl[entry.cargo_line_id] = start_idx + len(sizes)

            if locked_entries:
                # Stamp the locked pallets, then pack the rest around them.
                locked_layout = [
                    (w, l, h, (x, y, z))
                    for (_k, _s, w, l, h, x, y, z) in locked_entries
                ]
                locked_keys_aligned = [k for (k, *_rest) in locked_entries]
                combined, overflow = pack_with_locks(
                    zw, zl, zh, locked_layout, placements,
                    locked_keys=locked_keys_aligned,
                )
                # pack_with_locks emits locked records with size=None;
                # restore the size from locked_entries by index.
                placement_result: list[tuple] = []
                n_locked = len(locked_entries)
                for i, rec in enumerate(combined):
                    if i < n_locked:
                        # (key, None, w, l, h, x, y, z) -> use original size
                        key, _none, w, l, h, x, y, z = rec
                        size = locked_entries[i][1]
                        placement_result.append((key, size, w, l, h, x, y, z))
                    else:
                        placement_result.append(rec)
            else:
                placement_result, overflow = best_pack(zw, zl, zh, placements)

            if overflow:
                lost_scu = sum(size for _, size in overflow)
                _log.warning(
                    "zone packer: %s couldn't fit %d pallet(s) (%d SCU) "
                    "into %dx%dx%d — rendering best partial fit. "
                    "Overflow: %s",
                    zone_label, len(overflow), lost_scu, zw, zl, zh,
                    [f"{size} SCU (cl#{key[0].cargo_line_id})"
                     for key, size in overflow],
                )

            for (key, size, w, l, h, cell_x, cell_y, cell_z) in placement_result or []:
                entry, pallet_idx = key
                color = color_map.get(entry.delivery_station_name, "#888888")
                amb_sizes, partner_colors = cl_conflict_info.get(
                    entry.cargo_line_id, (set(), [])
                )
                is_pallet_conflict = size in amb_sizes

                rects.append(PalletRect(
                    cargo_line_id=entry.cargo_line_id,
                    zone_label=zone_label,
                    bay=zone["bay_label"],
                    cell_x=zone["cube_offset_x"] + cell_x,
                    cell_y=zone["cube_offset_y"] + cell_y,
                    cell_z=cell_z,
                    cell_w=w,
                    cell_l=l,
                    cell_h=h,
                    pallet_size=size,
                    color=color,
                    is_conflicted=is_pallet_conflict,
                    label=f"{size}",
                    delivery_station_name=entry.delivery_station_name,
                    commodity_name=entry.commodity_name,
                    contract_number=entry.contract_number,
                    pickup_station_name=getattr(
                        entry, "pickup_station_name", "",
                    ),
                    pallet_index=pallet_idx,
                    ship_forward_y=zone.get("ship_forward_y", "high"),
                    conflict_partner_colors=partner_colors if is_pallet_conflict else [],
                ))

        # ── Cubic occupancy fault check ───────────────────────────────
        # Walk all PalletRects we just produced and assert no two
        # occupy the same (zone, x, y, z) cube. If they do, log a
        # WARN to validation_log so it's visible in cargo_manager.log
        # — this catches placement bugs before they reach the user.
        occupied: dict[tuple[str, int, int, int], int] = {}
        for r in rects:
            for dx in range(r.cell_w):
                for dy in range(r.cell_l):
                    for dz in range(r.cell_h):
                        key = (r.zone_label,
                               r.cell_x + dx, r.cell_y + dy,
                               r.cell_z + dz)
                        if key in occupied:
                            _log.warning(
                                "Cubic overlap: zone=%s cube=(%d,%d,%d) "
                                "occupied by cargo_line=%d AND %d",
                                r.zone_label,
                                r.cell_x + dx, r.cell_y + dy, r.cell_z + dz,
                                occupied[key], r.cargo_line_id,
                            )
                        occupied[key] = r.cargo_line_id

        return rects

    def get_bay_layout(self) -> list[BayLayout]:
        """Static bay/zone structure for the active ship — what the
        BayCanvas needs to draw the empty bays before any route exists.

        Bays are returned in load order (port-side first column wins
        ties) so the canvas lays them out left-to-right consistently.
        """
        if not self.workday_id:
            return []
        rows = self.conn.execute(
            """
            SELECT z.zone_label, z.bay_label, z.cube_offset_x, z.cube_offset_y,
                   z.width_units, z.length_units, z.height_units,
                   z.scu_capacity, z.ship_forward_y, z.ramp_side, z.load_order
            FROM ship_zones z
            JOIN workdays w ON w.ship_id = z.ship_id
            WHERE w.id = ?
            ORDER BY z.load_order, z.cube_offset_x
            """,
            (self.workday_id,),
        ).fetchall()

        bays: dict[str, dict] = {}
        order: list[str] = []
        for r in rows:
            b = r["bay_label"]
            d = bays.get(b)
            if d is None:
                d = bays[b] = {
                    "zones": [], "w": 0, "l": 0, "scu": 0,
                    "ship_forward_y": r["ship_forward_y"],
                    "ramp_side": r["ramp_side"],
                    "order": r["load_order"] if r["load_order"] is not None else 1 << 30,
                }
                order.append(b)
            d["zones"].append(BayZoneGeom(
                zone_label=r["zone_label"],
                cube_offset_x=r["cube_offset_x"],
                cube_offset_y=r["cube_offset_y"],
                width_units=r["width_units"],
                length_units=r["length_units"],
                height_units=r["height_units"],
            ))
            d["w"] = max(d["w"], r["cube_offset_x"] + r["width_units"])
            d["l"] = max(d["l"], r["cube_offset_y"] + r["length_units"])
            d["scu"] += r["scu_capacity"]

        order.sort(key=lambda b: bays[b]["order"])
        layout: list[BayLayout] = []
        for b in order:
            d = bays[b]
            # The ramp lands at the top of the diagram when the ramp
            # edge and the ship-forward end are the same Y end (the
            # renderer always puts ship-forward at the top of screen).
            ramp_at_top = (d["ramp_side"] == "low_y") == (d["ship_forward_y"] == "low")
            layout.append(BayLayout(
                bay_label=b,
                width_cells=d["w"],
                length_cells=d["l"],
                scu_capacity=d["scu"],
                ship_forward_y=d["ship_forward_y"],
                ramp_at_top=ramp_at_top,
                ramp_label="nose ramp" if ramp_at_top else "rear ramp",
                zones=d["zones"],
            ))
        return layout

    def get_zone_strips(self, stop_number: int | None = None) -> list[ZoneStrip]:
        """Per-zone summary used by the main BayCanvas zone-strip view.

        Aggregates the cargo at *stop_number* (defaults to the busiest stop
        when the user hasn't started the route) into one strip per zone.
        """
        if not self._last_result or not self.workday_id:
            return []

        if stop_number is None:
            stop_number = self._busiest_stop_number()

        entries = self._last_result.snapshots.get(stop_number, [])

        # Load all zones for the active ship
        zone_rows = self.conn.execute(
            """
            SELECT z.zone_label, z.bay_label, z.cube_offset_x, z.cube_offset_y,
                   z.width_units, z.length_units, z.height_units, z.scu_capacity,
                   z.ship_forward_y
            FROM ship_zones z
            JOIN workdays w ON w.ship_id = z.ship_id
            WHERE w.id = ?
            ORDER BY z.unload_priority
            """,
            (self.workday_id,),
        ).fetchall()

        # Color lookup
        color_map = {
            r["name"]: r["color_hex"]
            for r in self.conn.execute(
                "SELECT name, color_hex FROM stations WHERE color_hex IS NOT NULL"
            ).fetchall()
        }

        # Aggregate entries per (zone, destination)
        agg: dict[str, dict[int, ZoneStripDestination]] = {}
        conflicted: set[str] = set()
        for e in entries:
            dests = agg.setdefault(e.zone_label, {})
            sid = self._station_id_by_name(e.delivery_station_name)
            existing = dests.get(sid)
            if existing:
                existing.scu_amount += e.scu_amount
            else:
                dests[sid] = ZoneStripDestination(
                    station_id=sid,
                    station_name=e.delivery_station_name,
                    color=color_map.get(e.delivery_station_name, "#888888"),
                    scu_amount=e.scu_amount,
                )
            if e.is_conflicted:
                conflicted.add(e.zone_label)

        # Pre-compute partner-color lookup per (group_id, station_id) so we
        # can render stripes in the conflicting destination's color rather
        # than a generic red.
        groups = self._last_result.conflict_groups if self._last_result else []
        partner_colors_for_dest: dict[int, list[str]] = {}
        for grp in groups:
            for d in grp.destinations:
                others = []
                for od in grp.destinations:
                    if od.delivery_station_id == d.delivery_station_id:
                        continue
                    c = color_map.get(od.delivery_station_name)
                    if c and c not in others:
                        others.append(c)
                bucket = partner_colors_for_dest.setdefault(d.delivery_station_id, [])
                for c in others:
                    if c not in bucket:
                        bucket.append(c)

        strips: list[ZoneStrip] = []
        for r in zone_rows:
            label = r["zone_label"]
            dests_dict = agg.get(label, {})
            dests_sorted = sorted(
                dests_dict.values(),
                key=lambda d: -d.scu_amount,
            )
            # Strip-level partner colors: union across destinations in this strip
            partners: list[str] = []
            for d in dests_sorted:
                for c in partner_colors_for_dest.get(d.station_id, []):
                    if c not in partners:
                        partners.append(c)
            strips.append(ZoneStrip(
                zone_label=label,
                bay=r["bay_label"],
                cube_offset_x=r["cube_offset_x"],
                cube_offset_y=r["cube_offset_y"],
                width_units=r["width_units"],
                length_units=r["length_units"],
                height_units=r["height_units"],
                scu_capacity=r["scu_capacity"],
                used_scu=sum(d.scu_amount for d in dests_sorted),
                destinations=dests_sorted,
                is_conflicted=label in conflicted,
                ship_forward_y=r["ship_forward_y"],
                conflict_partner_colors=partners,
            ))
        return strips

    def _station_id_by_name(self, name: str) -> int:
        row = self.conn.execute(
            "SELECT id FROM stations WHERE name = ?", (name,)
        ).fetchone()
        return row["id"] if row else 0

    def _busiest_stop_number(self) -> int:
        if not self._last_result or not self._last_result.snapshots:
            return 0
        return max(
            self._last_result.snapshots.keys(),
            key=lambda k: sum(e.scu_amount for e in self._last_result.snapshots[k]),
        )

    def get_snapshot_entries(self, stop_number: int):
        if not self._last_result:
            return []
        return self._last_result.snapshots.get(stop_number, [])

    def total_scu_in_use(self) -> int:
        if not self.workday_id:
            return 0
        row = self.conn.execute(
            """
            SELECT COALESCE(SUM(cl.scu_amount), 0) AS total
            FROM cargo_lines cl
            JOIN contracts ct ON ct.id = cl.contract_id
            WHERE ct.workday_id = ?
            """,
            (self.workday_id,),
        ).fetchone()
        return int(row["total"])

    def total_scu_capacity(self) -> int:
        if not self.workday_id:
            return 696
        row = self.conn.execute(
            """
            SELECT sh.total_scu
            FROM workdays w
            JOIN ships sh ON sh.id = w.ship_id
            WHERE w.id = ?
            """,
            (self.workday_id,),
        ).fetchone()
        return int(row["total_scu"]) if row else 696

    def has_been_computed(self) -> bool:
        """True once the current workday has had at least one successful
        compute. Drives the Compute-vs-Recompute label on the banner."""
        if not self.workday_id:
            return False
        row = self.conn.execute(
            "SELECT last_computed_at FROM workdays WHERE id = ?",
            (self.workday_id,),
        ).fetchone()
        return bool(row and row["last_computed_at"])

    # ── route navigation ────────────────────────────────────────────────

    def complete_current_stop(self) -> dict | None:
        if not self._last_result:
            return None
        self._current_stop_index = min(
            self._current_stop_index + 1,
            len(self._last_result.route_stops),
        )
        self._emit_progress()
        self.route_changed.emit()
        if self._current_stop_index < len(self._last_result.route_stops):
            stop = self._last_result.route_stops[self._current_stop_index]
            return {"stop_number": stop.stop_number, "station_name": stop.station_name}
        return None

    def skip_stop(self, station_id: int) -> None:
        # Stub for v1 — full skip support requires manual stop edits
        if not self.workday_id:
            return

    # ── manual zone control ─────────────────────────────────────────────

    def set_zone_destination(self, zone_label: str, station_id: int | None) -> None:
        # Recorded as a hint via app_settings; full implementation extends
        # zone_assignment.  For v1 we just mark the plan dirty.
        self._set_dirty()

    def move_zone_destination(self, source_zone: str, target_zone: str) -> None:
        """Move/swap an entire zone's cargo to another zone.

        If the target zone is empty, the cargo simply relocates. If both
        zones are occupied, their contents are swapped. The moved rows
        are flagged is_manual_override=1 so the next recompute respects
        them; plan_dirty is set so the user can recompute when ready.
        """
        self._require_workday()
        if source_zone == target_zone:
            return

        placeholder = "__SWAP__"
        # Stage source cargo on a placeholder label
        self.conn.execute(
            """
            UPDATE zone_assignments
            SET primary_zone_label = ?, is_manual_override = 1
            WHERE workday_id = ? AND primary_zone_label = ?
            """,
            (placeholder, self.workday_id, source_zone),
        )
        # Move target cargo into the source slot
        self.conn.execute(
            """
            UPDATE zone_assignments
            SET primary_zone_label = ?, is_manual_override = 1
            WHERE workday_id = ? AND primary_zone_label = ?
            """,
            (source_zone, self.workday_id, target_zone),
        )
        # Move staged source cargo into the target slot
        self.conn.execute(
            """
            UPDATE zone_assignments
            SET primary_zone_label = ?
            WHERE workday_id = ? AND primary_zone_label = ?
            """,
            (target_zone, self.workday_id, placeholder),
        )
        self.conn.commit()

        # Update the in-memory snapshots so the bay canvas reflects the
        # swap immediately without waiting for a recompute.
        if self._last_result:
            for entries in self._last_result.snapshots.values():
                for e in entries:
                    if e.zone_label == source_zone:
                        e.zone_label = placeholder
                    elif e.zone_label == target_zone:
                        e.zone_label = source_zone
                for e in entries:
                    if e.zone_label == placeholder:
                        e.zone_label = target_zone

        self._set_dirty()
        self.contracts_changed.emit()
        self.route_changed.emit()

    def merge_zone_into(self, source_zone: str, target_zone: str) -> None:
        """Move every cargo line in *source_zone* INTO *target_zone* without
        evicting anything that's already there. Source ends up empty;
        target ends up with both sets of cargo (mixed) — i.e. the
        Merge option in the Zone Detail prompt."""
        self._require_workday()
        if source_zone == target_zone:
            return
        self.conn.execute(
            """
            UPDATE zone_assignments
            SET primary_zone_label = ?, is_manual_override = 1,
                notes = COALESCE(notes, '') ||
                        ' [merged from ' || ? || ' by user]'
            WHERE workday_id = ? AND primary_zone_label = ?
            """,
            (target_zone, source_zone, self.workday_id, source_zone),
        )
        self.conn.commit()

        # In-memory snapshot patch so the bay canvas reflects the merge
        # without waiting for a recompute.
        if self._last_result:
            for entries in self._last_result.snapshots.values():
                for e in entries:
                    if e.zone_label == source_zone:
                        e.zone_label = target_zone

        self._set_dirty()
        self.contracts_changed.emit()
        self.route_changed.emit()

    def move_cargo(self, cargo_line_id: int, target_zone: str) -> None:
        """Move a single cargo line's pallets to *target_zone* as a
        manual override. The move is pin-survives-recompute: it sets
        is_manual_override=1 and rebuilds the pallet_breakdown so the
        moved row reflects the cargo line's full palletization
        (covering split-across-zones cases by collapsing them into one).
        """
        self._require_workday()
        cl = self.conn.execute(
            """
            SELECT cl.scu_amount, ct.max_pallet_size
            FROM cargo_lines cl
            JOIN contracts ct ON ct.id = cl.contract_id
            WHERE cl.id = ?
            """,
            (cargo_line_id,),
        ).fetchone()
        if not cl:
            return

        from .planner.palletizer import palletize, palletize_summary
        pallets = palletize(cl["scu_amount"], cl["max_pallet_size"])
        summary = palletize_summary(pallets)

        # move_cargo relocates the WHOLE cargo line — a line split
        # across several zones collapses into the one target. Reject
        # the move up front when the line can't physically live in a
        # single zone, instead of silently producing an overflowing
        # pin that the next recompute just discards.
        zone = self.conn.execute(
            """
            SELECT z.scu_capacity, z.width_units, z.length_units,
                   z.height_units
            FROM ship_zones z
            JOIN workdays w ON w.ship_id = z.ship_id
            WHERE w.id = ? AND z.zone_label = ?
            """,
            (self.workday_id, target_zone),
        ).fetchone()
        if zone is None:
            raise ToolError(f"Zone {target_zone} not found on this ship.")
        if cl["scu_amount"] > zone["scu_capacity"]:
            raise ToolError(
                f"This cargo line is {cl['scu_amount']} SCU — bigger than "
                f"zone {target_zone}'s {zone['scu_capacity']} SCU capacity. "
                f"It's too large to pin to a single zone; it has to stay "
                f"split across multiple zones."
            )
        from .planner.physical_packer import can_fit
        if not can_fit(zone["width_units"], zone["length_units"],
                       zone["height_units"], pallets):
            raise ToolError(
                f"This cargo line's pallets don't physically pack into "
                f"zone {target_zone} "
                f"({zone['width_units']}x{zone['length_units']}x"
                f"{zone['height_units']}). Pick a zone with a better fit."
            )

        # Wipe any previous rows for this cargo line (it might have
        # been split across zones via overflow). Replace with a single
        # row in the new zone.
        self.conn.execute(
            "DELETE FROM zone_assignments WHERE cargo_line_id = ? AND workday_id = ?",
            (cargo_line_id, self.workday_id),
        )
        # The whole line is pinned to one zone — record it as a single
        # partial-pin piece covering every pallet so move_cargo and
        # move_cargo_pallets share one representation.
        pin_zones = json.dumps([{"zone": target_zone, "sizes": list(pallets)}])
        self.conn.execute(
            """
            INSERT INTO zone_assignments
              (workday_id, cargo_line_id, primary_zone_label,
               pallet_breakdown, is_manual_override, pin_zones, notes)
            VALUES (?, ?, ?, ?, 1, ?, 'Moved by user from Zone Detail')
            """,
            (self.workday_id, cargo_line_id, target_zone, summary, pin_zones),
        )
        self.conn.commit()

        # Update in-memory snapshots so the bay canvas reflects the
        # move immediately without waiting for a recompute.
        if self._last_result:
            for entries in self._last_result.snapshots.values():
                for e in entries:
                    if e.cargo_line_id == cargo_line_id:
                        e.zone_label = target_zone

        self._set_dirty()
        self.contracts_changed.emit()
        self.route_changed.emit()

    def move_cargo_pallets(
        self, cargo_line_id: int, source_zone: str, target_zone: str,
    ) -> None:
        """Move only the portion of a cargo line currently sitting in
        *source_zone* into *target_zone*, as a partial pin.

        Unlike move_cargo (which pins the WHOLE line to one zone), this
        lets the rest of a split line stay auto-placed. Multiple
        partial pins per line are allowed.
        """
        self._require_workday()
        cl = self.conn.execute(
            """
            SELECT cl.scu_amount, ct.max_pallet_size
            FROM cargo_lines cl
            JOIN contracts ct ON ct.id = cl.contract_id
            WHERE cl.id = ?
            """,
            (cargo_line_id,),
        ).fetchone()
        if not cl:
            return

        # Which pallets of this line currently sit in source_zone?
        moved_sizes: list[int] | None = None
        if self._last_result:
            for entries in self._last_result.snapshots.values():
                for e in entries:
                    if (e.cargo_line_id == cargo_line_id
                            and e.zone_label == source_zone):
                        moved_sizes = _parse_breakdown(e.pallet_breakdown)
                        break
                if moved_sizes is not None:
                    break
        if not moved_sizes:
            raise ToolError(f"No cargo from this line is in {source_zone}.")

        # Validate the moved piece physically fits the target zone.
        zone = self.conn.execute(
            """
            SELECT z.scu_capacity, z.width_units, z.length_units,
                   z.height_units
            FROM ship_zones z
            JOIN workdays w ON w.ship_id = z.ship_id
            WHERE w.id = ? AND z.zone_label = ?
            """,
            (self.workday_id, target_zone),
        ).fetchone()
        if zone is None:
            raise ToolError(f"Zone {target_zone} not found on this ship.")
        if sum(moved_sizes) > zone["scu_capacity"]:
            raise ToolError(
                f"The pallets from {source_zone} total {sum(moved_sizes)} "
                f"SCU — bigger than zone {target_zone}'s "
                f"{zone['scu_capacity']} SCU capacity."
            )
        from .planner.physical_packer import can_fit
        if not can_fit(zone["width_units"], zone["length_units"],
                       zone["height_units"], moved_sizes):
            raise ToolError(
                f"These pallets don't physically pack into zone "
                f"{target_zone} "
                f"({zone['width_units']}x{zone['length_units']}x"
                f"{zone['height_units']}). Pick a zone with a better fit."
            )

        # Build the new pin_zones list from any existing partial pins.
        existing_rows = self.conn.execute(
            "SELECT pin_zones FROM zone_assignments "
            "WHERE cargo_line_id = ? AND workday_id = ?",
            (cargo_line_id, self.workday_id),
        ).fetchall()
        pieces: list[dict] = []
        for r in existing_rows:
            if r["pin_zones"]:
                try:
                    pieces = json.loads(r["pin_zones"])
                except (ValueError, TypeError):
                    pieces = []
                break
        # Redefine the source and target zones — drop their old pieces.
        pieces = [
            p for p in pieces
            if p.get("zone") not in (source_zone, target_zone)
        ]
        pieces.append({"zone": target_zone, "sizes": list(moved_sizes)})

        from .planner.palletizer import palletize, palletize_summary
        full_pallets = palletize(cl["scu_amount"], cl["max_pallet_size"])
        summary = palletize_summary(full_pallets)

        self.conn.execute(
            "DELETE FROM zone_assignments WHERE cargo_line_id = ? AND workday_id = ?",
            (cargo_line_id, self.workday_id),
        )
        self.conn.execute(
            """
            INSERT INTO zone_assignments
              (workday_id, cargo_line_id, primary_zone_label,
               pallet_breakdown, is_manual_override, pin_zones, notes)
            VALUES (?, ?, ?, ?, 1, ?,
                    'Partial move by user from Zone Detail')
            """,
            (self.workday_id, cargo_line_id, target_zone, summary,
             json.dumps(pieces)),
        )
        self.conn.commit()

        # Patch in-memory snapshots: move only this line's source_zone
        # piece to target_zone so the bay canvas updates immediately.
        if self._last_result:
            for entries in self._last_result.snapshots.values():
                for e in entries:
                    if (e.cargo_line_id == cargo_line_id
                            and e.zone_label == source_zone):
                        e.zone_label = target_zone

        self._set_dirty()
        self.contracts_changed.emit()
        self.route_changed.emit()

    # ── pallet locks (per-pallet fixed placement) ───────────────────────

    def lock_pallet(
        self,
        cargo_line_id: int,
        pallet_index: int,
        zone_label: str,
        cube_x: int,
        cube_y: int,
        cube_z: int,
        orientation: int = 0,
    ) -> None:
        """Lock a single pallet of a cargo line to a specific cube.

        Identity is ``(cargo_line_id, pallet_index)``; pallet_index is
        the 0-based position in the deterministic palletize() output.
        Re-locking the same pallet replaces the previous lock (upsert
        via INSERT OR REPLACE).

        ``orientation`` is 0 for the pallet's natural WxL footprint or
        1 for a 90-degree horizontal rotation (W and L swap). The
        bounds check below honors the rotation so a user can pin a
        16-SCU pallet sideways when the zone is narrower than its
        natural length.

        Validates that the zone exists on the active workday's ship,
        the cube is in-bounds, and the pallet_index is within range
        for the cargo line. Raises ToolError on validation failures.
        """
        self._require_workday()

        # 1. Validate the cargo line and pallet_index.
        cl = self.conn.execute(
            """
            SELECT cl.scu_amount, ct.max_pallet_size, ct.workday_id
            FROM cargo_lines cl
            JOIN contracts ct ON ct.id = cl.contract_id
            WHERE cl.id = ?
            """,
            (cargo_line_id,),
        ).fetchone()
        if not cl:
            raise ToolError(f"Cargo line {cargo_line_id} not found.")
        if cl["workday_id"] != self.workday_id:
            raise ToolError(
                f"Cargo line {cargo_line_id} is not part of the active "
                f"workday."
            )
        from .planner.palletizer import palletize
        pallets = palletize(cl["scu_amount"], cl["max_pallet_size"])
        if not (0 <= pallet_index < len(pallets)):
            raise ToolError(
                f"pallet_index {pallet_index} out of range for cargo line "
                f"{cargo_line_id} (has {len(pallets)} pallets: indices "
                f"0..{len(pallets) - 1})."
            )

        # 2. Validate the zone + cube bounds.
        zone = self.conn.execute(
            """
            SELECT z.width_units, z.length_units, z.height_units
            FROM ship_zones z
            JOIN workdays w ON w.ship_id = z.ship_id
            WHERE w.id = ? AND z.zone_label = ?
            """,
            (self.workday_id, zone_label),
        ).fetchone()
        if zone is None:
            raise ToolError(
                f"Zone {zone_label!r} not found on the active workday's ship."
            )

        # The pallet's own footprint must also fit at the requested
        # origin — anchor must leave room for w/l/h cubes inside the
        # zone bounds.
        from .planner.physical_packer import box_for
        box = box_for(pallets[pallet_index])
        pw, pl, ph = box["width"], box["length"], box["height"]
        if orientation == 1:
            # User requested a horizontal rotation. Only valid for
            # rotatable footprints; for non-rotatable (square) ones it
            # is a no-op so we silently accept it.
            if box.get("rotatable"):
                pw, pl = pl, pw
            elif pw != pl:
                raise ToolError(
                    f"Pallet size {pallets[pallet_index]} SCU is not "
                    f"rotatable — orientation=1 is invalid."
                )
        elif pw > zone["width_units"] and box.get("rotatable") and pl <= zone["width_units"]:
            # Auto-rotate when the natural footprint doesn't fit the
            # zone width (legacy behavior preserved for orientation=0).
            pw, pl = pl, pw
        if cube_x < 0 or cube_y < 0 or cube_z < 0:
            raise ToolError(
                f"Cube coordinates must be non-negative; got "
                f"({cube_x}, {cube_y}, {cube_z})."
            )
        if cube_x + pw > zone["width_units"]:
            raise ToolError(
                f"Pallet footprint {pw}x{pl}x{ph} at cube_x={cube_x} "
                f"exceeds zone {zone_label} width ({zone['width_units']})."
            )
        if cube_y + pl > zone["length_units"]:
            raise ToolError(
                f"Pallet footprint {pw}x{pl}x{ph} at cube_y={cube_y} "
                f"exceeds zone {zone_label} length ({zone['length_units']})."
            )
        if cube_z + ph > zone["height_units"]:
            raise ToolError(
                f"Pallet footprint {pw}x{pl}x{ph} at cube_z={cube_z} "
                f"exceeds zone {zone_label} height ({zone['height_units']})."
            )

        # 3. Upsert.
        self.conn.execute(
            """
            INSERT OR REPLACE INTO pallet_locks
                (workday_id, cargo_line_id, pallet_index,
                 zone_label, cube_x, cube_y, cube_z, orientation)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (self.workday_id, cargo_line_id, pallet_index,
             zone_label, cube_x, cube_y, cube_z, int(orientation)),
        )
        self.conn.commit()

        self._set_dirty()
        self.contracts_changed.emit()
        self.route_changed.emit()

    def unlock_pallet(self, cargo_line_id: int, pallet_index: int) -> None:
        """Remove a pallet lock. No-op if no lock exists."""
        self._require_workday()
        self.conn.execute(
            """
            DELETE FROM pallet_locks
            WHERE workday_id = ? AND cargo_line_id = ? AND pallet_index = ?
            """,
            (self.workday_id, cargo_line_id, pallet_index),
        )
        self.conn.commit()
        self._set_dirty()
        self.contracts_changed.emit()
        self.route_changed.emit()

    def list_pallet_locks(self) -> list[sqlite3.Row]:
        """Every pallet_locks row for the active workday."""
        if not self.workday_id:
            return []
        return self.conn.execute(
            """
            SELECT workday_id, cargo_line_id, pallet_index,
                   zone_label, cube_x, cube_y, cube_z, orientation
            FROM pallet_locks
            WHERE workday_id = ?
            ORDER BY cargo_line_id, pallet_index
            """,
            (self.workday_id,),
        ).fetchall()

    def clear_pallet_locks(self) -> None:
        """Delete every pallet lock for the active workday."""
        self._require_workday()
        self.conn.execute(
            "DELETE FROM pallet_locks WHERE workday_id = ?",
            (self.workday_id,),
        )
        self.conn.commit()
        self._set_dirty()
        self.contracts_changed.emit()
        self.route_changed.emit()

    # ── pallet holding (force-push waiting area) ────────────────────────

    def push_pallets_to_holding(
        self,
        pallets: list[tuple[int, int]],
        notes: str = "",
    ) -> None:
        """Move *pallets* into the holding table.

        Each entry is a ``(cargo_line_id, pallet_index)`` tuple. For
        each pallet the existing pallet_locks row (if any) is removed
        and a pallet_holding row is inserted in its place. Holding
        pallets are treated as NOT loaded by the planner — they no
        longer count toward any zone's SCU and don't render in the
        3D view; they show up in the holding sidebar instead.
        """
        self._require_workday()
        if not pallets:
            return
        for cargo_line_id, pallet_index in pallets:
            # Drop any lock the pallet had — force-push always evicts.
            self.conn.execute(
                """
                DELETE FROM pallet_locks
                WHERE workday_id = ?
                  AND cargo_line_id = ?
                  AND pallet_index = ?
                """,
                (self.workday_id, cargo_line_id, pallet_index),
            )
            # Insert into holding (upsert so re-pushing is a no-op).
            self.conn.execute(
                """
                INSERT OR REPLACE INTO pallet_holding
                    (workday_id, cargo_line_id, pallet_index, notes)
                VALUES (?, ?, ?, ?)
                """,
                (
                    self.workday_id,
                    cargo_line_id,
                    pallet_index,
                    notes or None,
                ),
            )
        self.conn.commit()
        _log.info(
            "push_pallets_to_holding: moved %d pallet(s) to holding",
            len(pallets),
        )
        self._set_dirty()
        self.contracts_changed.emit()
        self.route_changed.emit()

    def remove_from_holding(
        self, cargo_line_id: int, pallet_index: int,
    ) -> None:
        """Take a pallet off the holding table.

        Used after the user successfully drags a holding pallet back
        to a zone (which independently creates a pallet_locks row via
        ``lock_pallet``). No-op if the pallet wasn't in holding.
        """
        self._require_workday()
        self.conn.execute(
            """
            DELETE FROM pallet_holding
            WHERE workday_id = ?
              AND cargo_line_id = ?
              AND pallet_index = ?
            """,
            (self.workday_id, cargo_line_id, pallet_index),
        )
        self.conn.commit()
        self._set_dirty()
        self.contracts_changed.emit()
        self.route_changed.emit()

    def list_holding_pallets(self) -> list[sqlite3.Row]:
        """Return every pallet currently in holding for the active
        workday, joined to the cargo_lines / contracts / commodities /
        delivery-station rows the holding sidebar needs to render.

        Ordered by destination then cargo_line_id for stable grouping.
        """
        if not self.workday_id:
            return []
        return self.conn.execute(
            """
            SELECT ph.cargo_line_id, ph.pallet_index, ph.notes,
                   cl.scu_amount AS cargo_line_scu,
                   ct.contract_number, ct.max_pallet_size,
                   ds.id   AS delivery_station_id,
                   ds.name AS delivery_station_name,
                   ds.color_hex AS delivery_color,
                   cm.name AS commodity_name
            FROM pallet_holding ph
            JOIN cargo_lines  cl ON cl.id = ph.cargo_line_id
            JOIN contracts    ct ON ct.id = cl.contract_id
            JOIN stations     ds ON ds.id = cl.delivery_station_id
            JOIN commodities  cm ON cm.id = cl.commodity_id
            WHERE ph.workday_id = ?
            ORDER BY ds.name, ph.cargo_line_id, ph.pallet_index
            """,
            (self.workday_id,),
        ).fetchall()

    def is_pallet_in_holding(
        self, cargo_line_id: int, pallet_index: int,
    ) -> bool:
        """True iff ``(cargo_line_id, pallet_index)`` is in holding for
        the active workday.

        Used by force-push prep to defensively skip pallets that are
        already in holding — they aren't physically on the ship so they
        can't be "displaced" by a new drop.
        """
        if not self.workday_id:
            return False
        row = self.conn.execute(
            """
            SELECT 1 FROM pallet_holding
            WHERE workday_id = ?
              AND cargo_line_id = ?
              AND pallet_index = ?
            """,
            (self.workday_id, cargo_line_id, pallet_index),
        ).fetchone()
        return row is not None

    def auto_place_holding_pallets(self) -> dict[tuple[int, int], str]:
        """Try to lock each holding pallet to a free cube somewhere.

        For each pallet in holding, walks every zone on the active
        workday's ship and asks the physical packer for a free
        ``(zone, x, y, z)`` that respects existing locked pallets. On
        success the pallet is persisted via ``lock_pallet`` and
        removed from holding. On failure (no zone has room for the
        pallet's footprint) it stays in holding and the reason is
        recorded.

        Returns a dict of ``(cargo_line_id, pallet_index) -> reason``
        for every pallet that COULDN'T be placed.
        """
        self._require_workday()
        from .planner.palletizer import palletize
        from .planner.physical_packer import box_for, place_in_grid

        holding_rows = self.list_holding_pallets()
        if not holding_rows:
            return {}

        # Pull zone metadata once; we'll rebuild the per-zone grid
        # as we go so successive placements stack naturally.
        zone_rows = self.conn.execute(
            """
            SELECT z.zone_label, z.width_units, z.length_units,
                   z.height_units
            FROM ship_zones z
            JOIN workdays w ON w.ship_id = z.ship_id
            WHERE w.id = ?
            ORDER BY z.load_order, z.zone_label
            """,
            (self.workday_id,),
        ).fetchall()
        zones = [dict(r) for r in zone_rows]
        if not zones:
            return {
                (r["cargo_line_id"], r["pallet_index"]):
                    "No zones on the active workday's ship."
                for r in holding_rows
            }

        # Build initial occupancy from existing pallet locks. Each
        # locked pallet stamps its footprint into the grid and reserved
        # set so auto-place avoids them. Zones with no locks get an
        # all-zero grid.
        grids: dict[str, list[list[int]]] = {}
        reserved: dict[str, set[tuple[int, int, int]]] = {}
        for z in zones:
            grids[z["zone_label"]] = [
                [0] * z["length_units"] for _ in range(z["width_units"])
            ]
            reserved[z["zone_label"]] = set()
        for lock in self.list_pallet_locks():
            zlabel = lock["zone_label"]
            grid = grids.get(zlabel)
            if grid is None:
                continue
            # Look up the lock's footprint.
            cl = self.conn.execute(
                "SELECT scu_amount, ct.max_pallet_size "
                "FROM cargo_lines cl JOIN contracts ct ON ct.id = cl.contract_id "
                "WHERE cl.id = ?",
                (lock["cargo_line_id"],),
            ).fetchone()
            if cl is None:
                continue
            pallets = palletize(cl["scu_amount"], cl["max_pallet_size"])
            if lock["pallet_index"] >= len(pallets):
                continue
            box = box_for(pallets[lock["pallet_index"]])
            lw, ll, lh = box["width"], box["length"], box["height"]
            if lock["orientation"] == 1 and box.get("rotatable"):
                lw, ll = ll, lw
            lx, ly, lz = lock["cube_x"], lock["cube_y"], lock["cube_z"]
            for dx in range(lw):
                for dy in range(ll):
                    top = lz + lh
                    if 0 <= lx + dx < len(grid) and 0 <= ly + dy < len(grid[0]):
                        if grid[lx + dx][ly + dy] < top:
                            grid[lx + dx][ly + dy] = top
                    for dz in range(lh):
                        reserved[zlabel].add(
                            (lx + dx, ly + dy, lz + dz)
                        )

        failures: dict[tuple[int, int], str] = {}
        placed: list[tuple[int, int]] = []

        for row in holding_rows:
            cl_id = row["cargo_line_id"]
            p_idx = row["pallet_index"]
            cl = self.conn.execute(
                "SELECT scu_amount, ct.max_pallet_size "
                "FROM cargo_lines cl JOIN contracts ct ON ct.id = cl.contract_id "
                "WHERE cl.id = ?",
                (cl_id,),
            ).fetchone()
            if cl is None:
                failures[(cl_id, p_idx)] = "Cargo line missing"
                continue
            pallets = palletize(cl["scu_amount"], cl["max_pallet_size"])
            if p_idx >= len(pallets):
                failures[(cl_id, p_idx)] = "Pallet index out of range"
                continue
            size = pallets[p_idx]
            box = box_for(size)
            nw, nl, nh = box["width"], box["length"], box["height"]

            placed_here: tuple[str, int, int, int, int] | None = None
            for z in zones:
                zlabel = z["zone_label"]
                zw, zl, zh = (
                    z["width_units"], z["length_units"], z["height_units"],
                )
                # Try the natural footprint, falling back to a
                # horizontal rotation when allowed.
                attempts: list[tuple[int, int, int, int]] = []
                attempts.append((nw, nl, nh, 0))
                if box.get("rotatable") and (nw != nl):
                    attempts.append((nl, nw, nh, 1))
                for w, l, h, orient in attempts:
                    if w > zw or l > zl or h > zh:
                        continue
                    grid_copy = [list(col) for col in grids[zlabel]]
                    spot = place_in_grid(
                        grid_copy, w, l, h, zw, zl, zh,
                        reserved_cubes=reserved[zlabel],
                    )
                    if spot is not None:
                        # Commit the chosen grid copy so subsequent
                        # placements stack on top of this pallet.
                        grids[zlabel] = grid_copy
                        x, y, zc = spot
                        for dx in range(w):
                            for dy in range(l):
                                for dz in range(h):
                                    reserved[zlabel].add(
                                        (x + dx, y + dy, zc + dz)
                                    )
                        placed_here = (zlabel, x, y, zc, orient)
                        break
                if placed_here is not None:
                    break

            if placed_here is None:
                failures[(cl_id, p_idx)] = (
                    f"No zone has room for a {size} SCU pallet."
                )
                continue

            zlabel, x, y, zc, orient = placed_here
            try:
                self.lock_pallet(
                    cl_id, p_idx, zlabel, x, y, zc,
                    orientation=orient,
                )
            except ToolError as exc:
                failures[(cl_id, p_idx)] = str(exc)
                continue
            placed.append((cl_id, p_idx))

        for cl_id, p_idx in placed:
            self.remove_from_holding(cl_id, p_idx)

        _log.info(
            "auto_place_holding_pallets: placed %d/%d (failures=%d)",
            len(placed), len(holding_rows), len(failures),
        )
        return failures

    def recompute_around_locks(self) -> None:
        """Trigger a recompute that explicitly honors all current
        pallet_locks.

        Thin wrapper over ``recompute()`` whose only job is to surface
        the user-visible intent in cargo_manager.log — the underlying
        planner already respects locked pallets at every stop, so this
        is the "Re-optimize remaining stops" button after a force-push
        operation.
        """
        self._require_workday()
        n_locks = 0
        cl_ids: set[int] = set()
        if self.workday_id:
            for r in self.list_pallet_locks():
                n_locks += 1
                cl_ids.add(r["cargo_line_id"])
        _log.info(
            "recompute_around_locks: honoring %d pallet_locks across "
            "%d cargo lines",
            n_locks, len(cl_ids),
        )
        self.recompute()

    # ── manual stops ────────────────────────────────────────────────────

    def add_manual_stop(
        self, station_id: int, after_station_id: int | None = None,
    ) -> int:
        """Insert a user-defined extra stop into the active workday's route.

        *after_station_id* names the scheduled station the manual stop
        should follow. Passing ``None`` inserts at the very start of
        the route. Returns the new manual_stops.id.
        """
        self._require_workday()
        cur = self.conn.execute(
            """
            INSERT INTO manual_stops
                (workday_id, station_id, after_station_id, sort_order, notes)
            VALUES (
                ?, ?, ?,
                COALESCE(
                    (SELECT MAX(sort_order) + 1 FROM manual_stops
                     WHERE workday_id = ?),
                    0
                ),
                NULL
            )
            """,
            (self.workday_id, station_id, after_station_id, self.workday_id),
        )
        ms_id = cur.lastrowid
        self.conn.commit()
        self._set_dirty()
        self.route_changed.emit()
        self.contracts_changed.emit()
        return ms_id

    def remove_manual_stop(self, manual_stop_id: int) -> None:
        """Delete a manual stop from the active workday's route."""
        self._require_workday()
        self.conn.execute(
            "DELETE FROM manual_stops WHERE id = ? AND workday_id = ?",
            (manual_stop_id, self.workday_id),
        )
        self.conn.commit()
        self._set_dirty()
        self.route_changed.emit()
        self.contracts_changed.emit()

    def list_manual_stops(self) -> list[sqlite3.Row]:
        """Manual stops for the active workday with joined station info."""
        if not self.workday_id:
            return []
        return self.conn.execute(
            """
            SELECT ms.id, ms.station_id, ms.after_station_id, ms.sort_order,
                   ms.notes,
                   s.name AS station_name,
                   ap.name AS after_station_name
            FROM manual_stops ms
            JOIN stations s ON s.id = ms.station_id
            LEFT JOIN stations ap ON ap.id = ms.after_station_id
            WHERE ms.workday_id = ?
            ORDER BY ms.sort_order, ms.id
            """,
            (self.workday_id,),
        ).fetchall()

    # ── voice tool dispatch ──────────────────────────────────────────────

    def dispatch_tool(self, tool_name: str, args: dict) -> str:
        """Execute a tool call from the LLM; return a confirmation string."""
        match tool_name:
            case "start_workday":
                origin_id = self._resolve_station(args["origin_station"])
                fd = args.get("final_destination")
                fd_id = self._resolve_station(fd) if fd else None
                rr = bool(args.get("round_robin", False))
                wid = self.start_workday(origin_id, fd_id, rr)
                return f"Workday {wid} started from {args['origin_station']}."
            case "resume_workday":
                wd = self.find_open_workday()
                if not wd:
                    return "No open workday to resume."
                self.resume_workday(wd["id"])
                return f"Workday {wd['id']} resumed."
            case "end_workday":
                self.end_workday()
                return "Workday ended."
            case "set_origin":
                self.set_origin(self._resolve_station(args["station_name"]))
                return f"Origin set to {args['station_name']}."
            case "set_final_destination":
                sn = args.get("station_name")
                self.set_final_destination(self._resolve_station(sn) if sn else None)
                return f"Final destination set to {sn or '(none)'}."
            case "set_round_robin":
                self.set_round_robin(bool(args["enabled"]))
                return f"Round robin {'on' if args['enabled'] else 'off'}."
            case "recompute_plan":
                self.recompute()
                return "Recomputing…"
            case "add_contract":
                cid = self.add_contract(args)
                return f"Contract {self._contract_number_for(cid)} added."
            case "remove_contract":
                n = int(args["contract_number"])
                self.remove_contract(n)
                return f"Contract {n} removed."
            case "edit_contract":
                n = int(args["contract_number"])
                self.edit_contract(n, args)
                return f"Contract {n} updated."
            case "list_contracts":
                cs = self.list_contracts()
                if not cs:
                    return "No contracts."
                return "; ".join(
                    f"#{c['contract_number']} {c['pickup_name']} ({c['total_scu']} SCU)"
                    for c in cs
                )
            case "clear_contracts":
                self.clear_contracts()
                return "All contracts cleared."
            case "undo":
                ok = self.undo()
                return "Undone." if ok else "Nothing to undo."
            case "complete_current_stop":
                nxt = self.complete_current_stop()
                if nxt:
                    return f"Stop complete. Next: {nxt['station_name']}."
                return "Route complete."
            case "skip_stop":
                self.skip_stop(self._resolve_station(args["station_name"]))
                return f"Skipped {args['station_name']}."
            case "list_capabilities":
                return ("I can start/end workdays, add/edit/remove contracts, "
                        "recompute routes, navigate stops, and explain conflicts.")
            case _:
                raise ToolError(f"Unknown or unimplemented tool: {tool_name}")

    def build_context_snapshot(self) -> str:
        """Compact text snapshot of current state for the LLM system prompt."""
        if not self.workday_id:
            return "WORKDAY: none"
        wd = self.conn.execute(
            """
            SELECT s.name AS origin_name,
                   fd.name AS final_name,
                   w.round_robin
            FROM workdays w
            JOIN stations s ON s.id = w.origin_station_id
            LEFT JOIN stations fd ON fd.id = w.final_destination_station_id
            WHERE w.id = ?
            """,
            (self.workday_id,),
        ).fetchone()
        lines = [
            f"WORKDAY: origin={wd['origin_name']}  "
            f"final={wd['final_name'] or '(none)'}  "
            f"round_robin={'on' if wd['round_robin'] else 'off'}"
        ]
        if self._last_result:
            n = len(self._last_result.route_stops)
            cur = max(1, min(self._current_stop_index + 1, n))
            if n:
                stop = self._last_result.route_stops[cur - 1]
                lines.append(f"STOP: {cur} of {n}  current={stop.station_name} ({stop.action})")
        else:
            lines.append("STOP: not computed — recompute required")

        contracts = self.list_contracts()
        if contracts:
            lines.append("")
            lines.append("CONTRACTS:")
            conflict_cl: set[int] = set()
            if self._last_result:
                for grp in self._last_result.conflict_groups:
                    conflict_cl.update(grp.cargo_line_ids)
            for c in contracts:
                for d in c["deliveries"]:
                    flag = "  ⚠CONFLICT" if d["id"] in conflict_cl else ""
                    lines.append(
                        f"  #{c['contract_number']}  {c['pickup_name']} | "
                        f"{d['commodity_name']} {d['scu_amount']} SCU → "
                        f"{d['delivery_name']}   max={c['max_pallet_size']}{flag}"
                    )
        else:
            lines.append("CONTRACTS: none")

        if self._last_result and self._last_result.conflict_groups:
            lines.append("")
            lines.append("CONFLICTS:")
            for grp in self._last_result.conflict_groups:
                lines.append(
                    f"  G{grp.group_id}  {grp.pickup_station_name} × {grp.commodity_name}"
                )
                for dest in grp.destinations:
                    ambig = ", ".join(f"{dest.pallet_counts[s]}×{s}" for s in dest.ambiguous_sizes)
                    lines.append(f"      {dest.delivery_station_name}: conflict {ambig or '—'}")

        return "\n".join(lines)

    # ── helpers ──────────────────────────────────────────────────────────

    def _require_workday(self) -> None:
        if not self.workday_id:
            raise ToolError("No active workday")

    def _set_dirty(self) -> None:
        self.conn.execute(
            "UPDATE workdays SET plan_dirty = 1 WHERE id = ?", (self.workday_id,)
        )
        self.conn.commit()
        self.plan_dirty_changed.emit(True)
        self.scu_usage.emit(self.total_scu_in_use(), self.total_scu_capacity())

    def export_plan_pdf(self, path: str) -> None:
        """Write the current computed plan to *path* as a PDF."""
        from .pdf_export import export_plan_pdf
        export_plan_pdf(self, path)

    def _emit_progress(self) -> None:
        total = len(self._last_result.route_stops) if self._last_result else 0
        cur = min(self._current_stop_index + 1, total) if total else 0
        self.stop_progress.emit(cur, total)
        self.scu_usage.emit(self.total_scu_in_use(), self.total_scu_capacity())

    def _resolve_station(self, ref: int | str) -> int:
        if isinstance(ref, int):
            return ref
        try:
            sid, _ = canonical_station(ref, self.conn)
            return sid
        except LookupError as e:
            raise ToolError(str(e)) from e

    def _resolve_commodity(self, ref: int | str) -> int:
        if isinstance(ref, int):
            return ref
        try:
            cid, _ = canonical_commodity(ref, self.conn)
            return cid
        except LookupError as e:
            raise ToolError(str(e)) from e

    def _contract_number_for(self, contract_id: int) -> int:
        row = self.conn.execute(
            "SELECT contract_number FROM contracts WHERE id = ?", (contract_id,)
        ).fetchone()
        return row["contract_number"] if row else 0


# ── module-level helpers ──────────────────────────────────────────────────


def _parse_breakdown(text: str | None) -> list[int]:
    """Parse '6×8 + 1×4 + 1×2' → [8,8,8,8,8,8,4,2]."""
    if not text:
        return []
    out: list[int] = []
    for part in text.replace(" ", "").split("+"):
        if "×" in part or "x" in part:
            sep = "×" if "×" in part else "x"
            count_str, size_str = part.split(sep)
            try:
                count = int(count_str)
                size = int(size_str)
            except ValueError:
                continue
            out.extend([size] * count)
    return out
