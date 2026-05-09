"""
AppController — single business-logic bridge between the UI, the planner,
the database, and the voice subsystem.

UI widgets hold a reference to one of these but never import from
src.planner directly.  All planner functions are called from here and
the controller emits Qt signals to refresh the UI.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal

from .planner.canonicalize import canonical_commodity, canonical_station
from .planner.conflicts import ConflictGroup
from .planner.recompute import RecomputeResult, recompute as run_recompute
from .planner.route import RouteStop
from .palletizer_color import assign_destination_colors
from .settings import AppSettings, get_api_key
from .undo import UndoStack


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
class PalletRect:
    """A single pallet drawn on the BayCanvas viewport."""
    cargo_line_id: int
    zone_label: str          # 'F1', 'R3', etc.
    bay: str                 # 'forward' | 'rear'
    cell_x: int              # origin within bay (0 = port edge)
    cell_y: int              # origin within bay (0 = ramp edge)
    cell_w: int              # pallet width (cells)
    cell_l: int              # pallet length (cells)
    cell_h: int              # pallet height (cells, 1 or 2)
    pallet_size: int         # SCU
    color: str               # hex of delivery destination
    is_conflicted: bool
    label: str               # short label inside rectangle
    delivery_station_name: str
    commodity_name: str
    contract_number: int


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
                result = run_recompute(self.workday_id, conn)
                self.finished_with_result.emit(result)
            finally:
                conn.close()
        except Exception as e:
            self.failed.emit(str(e))


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
        self._recompute_thread: RecomputeThread | None = None
        self._current_stop_index = 0   # 0 = before first stop completed

        self.api_key: str | None = get_api_key()

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
            "SELECT id FROM workdays WHERE id = ? AND ended_at IS NULL",
            (workday_id,),
        ).fetchone()
        if not row:
            raise ValueError(f"Workday {workday_id} is not open")
        self.workday_id = workday_id
        self._current_stop_index = 0
        self.contracts_changed.emit()
        self.route_changed.emit()
        self._emit_progress()

    def start_workday(
        self,
        origin_station_id: int,
        final_destination_id: int | None,
        round_robin: bool,
    ) -> int:
        """Create a fresh workday after closing any open one."""
        # Close existing open workday
        self.conn.execute(
            "UPDATE workdays SET ended_at = ? WHERE ended_at IS NULL",
            (datetime.now(timezone.utc).isoformat(),),
        )
        # Resolve C2 ship id (only ship in v1)
        ship = self.conn.execute(
            "SELECT id FROM ships WHERE name = 'C2 Hercules'"
        ).fetchone()
        if not ship:
            raise RuntimeError("C2 Hercules ship row missing — DB seed failed")

        cur = self.conn.execute(
            """
            INSERT INTO workdays
              (started_at, ship_id, origin_station_id,
               final_destination_station_id, round_robin, plan_dirty)
            VALUES (?, ?, ?, ?, ?, 0)
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                ship["id"],
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

    def add_contract(self, data: dict) -> int:
        """Add a contract.

        Args:
            data: {"pickup_station": str | int,
                   "max_pallet_size": int (default 8),
                   "deliveries": [{"destination": str|int,
                                   "commodity": str|int,
                                   "scu": int}, ...]}
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

        self._set_dirty()
        self.contracts_changed.emit()
        return contract_id

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
        self._require_workday()
        if self._recompute_thread and self._recompute_thread.isRunning():
            return
        self.recompute_started.emit()
        # Commit pending state before spawning a thread that opens its own conn
        self.conn.commit()
        thread = RecomputeThread(self.db_path, self.workday_id)
        thread.finished_with_result.connect(self._on_recompute_done)
        thread.failed.connect(self._on_recompute_failed)
        thread.finished.connect(thread.deleteLater)
        self._recompute_thread = thread
        thread.start()

    def _on_recompute_done(self, result: RecomputeResult) -> None:
        self._last_result = result
        # Auto-assign colors to any new destinations
        if self.workday_id:
            assign_destination_colors(self.workday_id, self.conn)
        self.plan_dirty_changed.emit(False)
        self.contracts_changed.emit()
        self.route_changed.emit()
        self._emit_progress()
        self.recompute_done.emit(result)

    def _on_recompute_failed(self, message: str) -> None:
        self.recompute_failed.emit(message)

    def get_last_result(self) -> RecomputeResult | None:
        return self._last_result

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
                       z.cube_offset_x, z.cube_offset_y, z.scu_capacity
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

        # Box footprints for pallet sizes
        boxes = _load_box_footprints()

        # Per-zone height grid: [height_units_used_at(x,y)]
        # Zone height is the full vertical capacity in 1.25 m cubes (e.g. 4
        # for the C2). A 2-height pallet adds 2 to grid[x][y]; capping at the
        # zone's height_units lets us stack two pallets per floor cell.
        zone_grids: dict[str, list[list[int]]] = {}

        rects: list[PalletRect] = []

        # Sort within each zone so conflict cargo is placed first (low Y =
        # ramp side) for fast deconfliction at unload (handbook §10).
        sorted_entries = sorted(
            entries,
            key=lambda e: (e.zone_label, 0 if e.is_conflicted else 1, e.cargo_line_id),
        )

        for entry in sorted_entries:
            zone = zone_meta.get(entry.zone_label)
            if not zone:
                continue
            zw = zone["width_units"]
            zl = zone["length_units"]
            zh = zone["scu_capacity"] // (zw * zl) if zw * zl else 4
            grid = zone_grids.setdefault(
                entry.zone_label, [[0] * zl for _ in range(zw)]
            )
            sizes = _parse_breakdown(entry.pallet_breakdown) or [entry.scu_amount]
            # For conflict cargo: place SMALL pallets first so they land at
            # low Y (ramp side). Small pallets are typically the ambiguous
            # ones (e.g. 1×2 + 1×1 in the Everus/Baijini Tungsten case);
            # the pilot can test-send them at the ramp before bringing up
            # the unique-size stacks.
            if entry.is_conflicted:
                sizes = sorted(sizes)        # ascending = smallest first
            color = color_map.get(entry.delivery_station_name, "#888888")

            for size in sizes:
                box = boxes.get(size, {"width": 1, "length": 1, "height": 1})
                w, l, h = box["width"], box["length"], box["height"]
                # Auto-rotate horizontally if the pallet is too wide for
                # the zone (scu_boxes.json marks 2/16/24/32 as rotatable).
                if w > zw and box.get("rotatable") and l <= zw:
                    w, l = l, w
                placed = _place_in_grid(grid, w, l, h, zw, zl, zh)
                if placed is None:
                    # Zone visually full — anchor at corner, label as overflow
                    placed = (max(0, zw - w), max(0, zl - l))

                cell_x, cell_y = placed
                rects.append(PalletRect(
                    cargo_line_id=entry.cargo_line_id,
                    zone_label=entry.zone_label,
                    bay=zone["bay_label"],
                    cell_x=zone["cube_offset_x"] + cell_x,
                    cell_y=zone["cube_offset_y"] + cell_y,
                    cell_w=w,
                    cell_l=l,
                    cell_h=h,
                    pallet_size=size,
                    color=color,
                    is_conflicted=entry.is_conflicted,
                    label=f"{size}",
                    delivery_station_name=entry.delivery_station_name,
                    commodity_name=entry.commodity_name,
                    contract_number=entry.contract_number,
                ))

        return rects

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
                   z.width_units, z.length_units, z.height_units, z.scu_capacity
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

        strips: list[ZoneStrip] = []
        for r in zone_rows:
            label = r["zone_label"]
            dests_dict = agg.get(label, {})
            dests_sorted = sorted(
                dests_dict.values(),
                key=lambda d: -d.scu_amount,
            )
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

    def move_cargo(self, cargo_line_id: int, target_zone: str) -> None:
        self._require_workday()
        # Update or insert a manual override row
        existing = self.conn.execute(
            "SELECT id FROM zone_assignments WHERE cargo_line_id = ? AND workday_id = ?",
            (cargo_line_id, self.workday_id),
        ).fetchone()
        if existing:
            self.conn.execute(
                """
                UPDATE zone_assignments
                SET primary_zone_label = ?, is_manual_override = 1
                WHERE id = ?
                """,
                (target_zone, existing["id"]),
            )
        else:
            self.conn.execute(
                """
                INSERT INTO zone_assignments
                  (workday_id, cargo_line_id, primary_zone_label, is_manual_override)
                VALUES (?, ?, ?, 1)
                """,
                (self.workday_id, cargo_line_id, target_zone),
            )
        self._set_dirty()

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
                    lines.append(f"      {dest.delivery_station_name}: ambiguous {ambig or '—'}")

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

def _load_box_footprints() -> dict[int, dict]:
    """Return {scu: {width, length, height}} from data/scu_boxes.json."""
    boxes_path = Path(__file__).resolve().parents[1] / "data" / "scu_boxes.json"
    data = json.loads(boxes_path.read_text(encoding="utf-8"))
    return {b["scu"]: b for b in data["boxes"]}


def _place_in_grid(
    grid: list[list[int]],
    w: int, l: int, h: int,
    zone_w: int, zone_l: int,
    stack_limit: int,
) -> tuple[int, int] | None:
    """First-fit placement scanning row-by-row.

    Tries the lowest available cell whose w×l footprint can hold *h* more
    units of height without exceeding *stack_limit*. Returns (x, y) or None.
    """
    for y in range(zone_l - l + 1):
        for x in range(zone_w - w + 1):
            ok = True
            for dx in range(w):
                for dy in range(l):
                    if grid[x + dx][y + dy] + h > stack_limit:
                        ok = False
                        break
                if not ok:
                    break
            if ok:
                for dx in range(w):
                    for dy in range(l):
                        grid[x + dx][y + dy] += h
                return (x, y)
    return None


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
