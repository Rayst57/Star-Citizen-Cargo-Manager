"""
Single-step undo for contract mutations.

Captures a JSON snapshot of all contracts/cargo lines for a workday before each
mutation, then restores from the snapshot when undo() is called.

One level only — sufficient for "scratch that".
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone


class UndoStack:
    def __init__(self) -> None:
        self._snapshot: dict | None = None

    def capture(self, workday_id: int, conn: sqlite3.Connection) -> None:
        """Snapshot the current contract + cargo_line state for *workday_id*."""
        contracts = conn.execute(
            """
            SELECT id, contract_number, pickup_station_id, max_pallet_size,
                   parsed_confidence, status, notes, created_at, updated_at
            FROM contracts WHERE workday_id = ?
            """,
            (workday_id,),
        ).fetchall()
        cargo_lines = conn.execute(
            """
            SELECT cl.id, cl.contract_id, cl.line_number, cl.delivery_station_id,
                   cl.commodity_id, cl.scu_amount, cl.notes
            FROM cargo_lines cl
            JOIN contracts ct ON ct.id = cl.contract_id
            WHERE ct.workday_id = ?
            """,
            (workday_id,),
        ).fetchall()
        self._snapshot = {
            "workday_id": workday_id,
            "contracts": [dict(r) for r in contracts],
            "cargo_lines": [dict(r) for r in cargo_lines],
        }

    def can_undo(self) -> bool:
        return self._snapshot is not None

    def restore(self, conn: sqlite3.Connection) -> bool:
        """Restore the captured snapshot. Returns False if nothing to restore."""
        if not self._snapshot:
            return False
        wid = self._snapshot["workday_id"]
        now = datetime.now(timezone.utc).isoformat()

        conn.execute("DELETE FROM cargo_lines WHERE contract_id IN "
                     "(SELECT id FROM contracts WHERE workday_id = ?)", (wid,))
        conn.execute("DELETE FROM contracts WHERE workday_id = ?", (wid,))

        for c in self._snapshot["contracts"]:
            conn.execute(
                """
                INSERT INTO contracts
                  (id, workday_id, contract_number, pickup_station_id,
                   max_pallet_size, parsed_confidence, status, notes,
                   created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    c["id"], wid, c["contract_number"], c["pickup_station_id"],
                    c["max_pallet_size"], c["parsed_confidence"], c["status"],
                    c["notes"], c["created_at"], c["updated_at"] or now,
                ),
            )
        for cl in self._snapshot["cargo_lines"]:
            conn.execute(
                """
                INSERT INTO cargo_lines
                  (id, contract_id, line_number, delivery_station_id,
                   commodity_id, scu_amount, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (cl["id"], cl["contract_id"], cl["line_number"],
                 cl["delivery_station_id"], cl["commodity_id"],
                 cl["scu_amount"], cl["notes"]),
            )

        conn.execute("UPDATE workdays SET plan_dirty = 1 WHERE id = ?", (wid,))
        conn.commit()
        self._snapshot = None
        return True

    def clear(self) -> None:
        self._snapshot = None
