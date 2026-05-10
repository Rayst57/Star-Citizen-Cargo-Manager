"""
DetailedPlanDialog — single popup showing every stop's tasks in detail.

Replaces the per-stop "View Load" popup that opened one window per stop.
Now there's exactly one button (in the Route panel header), one popup,
and the user scrolls through all stops in a single dialog.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton,
    QScrollArea, QVBoxLayout, QWidget, QFrame,
)

from ...app_controller import AppController


class DetailedPlanDialog(QDialog):
    def __init__(self, controller: AppController, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.setWindowTitle("Detailed Plan")
        self.setMinimumSize(820, 640)

        root = QVBoxLayout(self)
        root.setSpacing(6)

        # Header
        header = QHBoxLayout()
        title = QLabel("Detailed Plan — every stop")
        title.setProperty("heading", True)
        header.addWidget(title)
        header.addStretch(1)
        close_btn = QPushButton("✕")
        close_btn.setProperty("flat", True)
        close_btn.clicked.connect(self.reject)
        header.addWidget(close_btn)
        root.addLayout(header)

        result = controller.get_last_result()
        if not result or not result.route_stops:
            empty = QLabel("No route computed. Add contracts and click Recompute.")
            empty.setProperty("muted", True)
            root.addWidget(empty)
            return

        # Build conflict cargo line set
        conflict_cl_ids: set[int] = set()
        for grp in result.conflict_groups:
            conflict_cl_ids.update(grp.cargo_line_ids)
        cl_to_dest: dict[int, str] = {}
        cl_to_zone: dict[int, str] = {}

        # Lookup zone + delivery name for each cargo line
        rows = controller.conn.execute(
            """
            SELECT cl.id, s.name AS dest_name, za.primary_zone_label AS zone_label
            FROM cargo_lines cl
            JOIN stations s ON s.id = cl.delivery_station_id
            JOIN contracts ct ON ct.id = cl.contract_id
            LEFT JOIN zone_assignments za
                   ON za.cargo_line_id = cl.id
                  AND za.workday_id = ct.workday_id
            WHERE ct.workday_id = ?
            """,
            (controller.workday_id,),
        ).fetchall()
        for r in rows:
            cl_to_dest[r["id"]] = r["dest_name"]
            cl_to_zone[r["id"]] = r["zone_label"] or "?"

        # Scrollable list of per-stop sections
        list_widget = QWidget()
        list_layout = QVBoxLayout(list_widget)
        list_layout.setContentsMargins(0, 0, 0, 0)
        list_layout.setSpacing(8)

        for stop in result.route_stops:
            list_layout.addWidget(
                self._build_stop_section(stop, conflict_cl_ids, cl_to_dest,
                                          cl_to_zone, result)
            )
        list_layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(list_widget)
        root.addWidget(scroll, 1)

    # ── per-stop section ──────────────────────────────────────────────

    def _build_stop_section(
        self,
        stop,
        conflict_cl_ids: set[int],
        cl_to_dest: dict[int, str],
        cl_to_zone: dict[int, str],
        result,
    ) -> QFrame:
        card = QFrame()
        card.setObjectName("card")
        # Highlight stops whose cargo participates in a conflict group
        stop_cl_ids = {r.cargo_line_id for r in stop.loads + stop.unloads}
        is_conflict_stop = bool(stop_cl_ids & conflict_cl_ids)
        card.setProperty("conflict", is_conflict_stop)

        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(4)

        # Header
        h = QLabel(f"Stop {stop.stop_number}: {stop.station_name} — {stop.action}")
        h.setProperty("heading", True)
        h.setWordWrap(True)
        layout.addWidget(h)

        # Unload section
        if stop.unloads:
            layout.addWidget(self._section_label("Unload"))
            for ref in stop.unloads:
                tag = "  ⚠ CONFLICT" if ref.cargo_line_id in conflict_cl_ids else ""
                zone = cl_to_zone.get(ref.cargo_line_id, "?")
                lbl = QLabel(
                    f"  {zone} → {ref.scu_amount} SCU {ref.commodity_name}"
                    f"  [Contract {ref.contract_number}]{tag}"
                )
                lbl.setWordWrap(True)
                layout.addWidget(lbl)
        else:
            layout.addWidget(self._muted("Unload: none"))

        # Load section
        if stop.loads:
            layout.addWidget(self._section_label("Load"))
            for ref in stop.loads:
                tag = "  ⚠ CONFLICT" if ref.cargo_line_id in conflict_cl_ids else ""
                zone = cl_to_zone.get(ref.cargo_line_id, "?")
                dest = cl_to_dest.get(ref.cargo_line_id, "?")
                lbl = QLabel(
                    f"  {zone} → {ref.scu_amount} SCU {ref.commodity_name} "
                    f"→ {dest}  [Contract {ref.contract_number}]{tag}"
                )
                lbl.setWordWrap(True)
                layout.addWidget(lbl)
        else:
            layout.addWidget(self._muted("Load: none"))

        # Conflict notes (which group this stop touches)
        relevant_groups = [
            g for g in result.conflict_groups
            if any(cl in stop_cl_ids for cl in g.cargo_line_ids)
        ]
        if relevant_groups:
            layout.addWidget(self._section_label("Conflict notes"))
            for g in relevant_groups:
                names = " / ".join(d.delivery_station_name for d in g.destinations)
                amb = " + ".join(f"1×{s}" for s in g.ambiguous_sizes)
                lbl = QLabel(
                    f"  ⚠ Group {g.group_id} — {g.pickup_station_name} × "
                    f"{g.commodity_name}\n"
                    f"      destinations: {names}\n"
                    f"      ambiguous sizes: {amb}"
                )
                lbl.setStyleSheet("color: #ffbe20;")
                lbl.setWordWrap(True)
                layout.addWidget(lbl)

        # Loadout snapshot after this stop
        snapshot = result.snapshots.get(stop.stop_number, [])
        if snapshot:
            layout.addWidget(self._section_label("Onboard after this stop"))
            for e in snapshot:
                tag = "  ⚠" if e.is_conflicted else ""
                lbl = QLabel(
                    f"  {e.zone_label}: {e.scu_amount} SCU {e.commodity_name} "
                    f"→ {e.delivery_station_name}{tag}"
                )
                lbl.setProperty("muted", True)
                lbl.setWordWrap(True)
                layout.addWidget(lbl)
        else:
            layout.addWidget(self._muted("Onboard after this stop: empty"))

        return card

    def _section_label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet("font-weight: bold; color: #deb447; margin-top: 4px;")
        return lbl

    def _muted(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setProperty("muted", True)
        return lbl
