"""RoutePanel — right pane listing route stops with View Load buttons."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from ..widgets.stop_card import StopCard


class RoutePanel(QWidget):
    detailed_plan_requested  = Signal()

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        # Side panels grow with window width; lower minimum so they
        # collapse cleanly on small displays.
        self.setMinimumWidth(280)

        root = QVBoxLayout(self)
        # Inset content past the 22 px rounded corners so headers and
        # buttons don't poke into the cut-out area.
        root.setContentsMargins(18, 22, 18, 18)
        root.setSpacing(8)

        # Header
        header = QHBoxLayout()
        title = QLabel("ROUTE")
        title.setProperty("heading", True)
        header.addWidget(title)
        header.addStretch(1)
        self.add_stop_btn = QPushButton("+ Add Stop")
        self.add_stop_btn.setToolTip(
            "Insert an unscheduled extra stop into the current route "
            "(e.g. fly to Baijini and unload)"
        )
        self.add_stop_btn.clicked.connect(self._open_manual_stop_dialog)
        header.addWidget(self.add_stop_btn)
        self.detail_btn = QPushButton("Detailed Plan")
        self.detail_btn.setToolTip(
            "Open the full per-stop plan in a single window"
        )
        self.detail_btn.clicked.connect(self.detailed_plan_requested.emit)
        header.addWidget(self.detail_btn)
        root.addLayout(header)

        # Scrollable list (vertical-only)
        self.list_widget = QWidget()
        self.list_layout = QVBoxLayout(self.list_widget)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.setSpacing(6)
        self.list_layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setWidget(self.list_widget)
        root.addWidget(scroll, 1)

        self.empty_label = QLabel("No route — add a contract and Recompute.")
        self.empty_label.setProperty("muted", True)
        root.addWidget(self.empty_label)

        self.refresh()

    def refresh(self) -> None:
        # Properly delete (don't just orphan) old stop cards. setParent(None)
        # would re-promote the QFrame to a top-level window — visible ones
        # stay onscreen as ghost popups, leaking one per stop per recompute.
        for i in reversed(range(self.list_layout.count() - 1)):
            item = self.list_layout.takeAt(i)
            if item is None:
                continue
            w = item.widget()
            if w is not None:
                w.hide()
                w.deleteLater()

        result = self.controller.get_last_result()

        # The compute trigger lives in the recompute banner at the
        # bottom of the window now, so we only need to nudge the user
        # toward the right verb here. After the first successful
        # compute the banner reads "Recompute"; before that, "Compute".
        verb = "Recompute" if self.controller.has_been_computed() else "Compute"
        self.empty_label.setText(f"No route — add a contract and {verb}.")

        if not result or not result.route_stops:
            self.empty_label.show()
            return
        self.empty_label.hide()

        # Build conflict cargo line set
        conflict_cl_ids: set[int] = set()
        for grp in result.conflict_groups:
            conflict_cl_ids.update(grp.cargo_line_ids)

        cur_idx = self.controller._current_stop_index

        for idx, stop in enumerate(result.route_stops):
            unload_summary = ""
            load_summary = ""
            conflict_note = ""

            # Pre-fetch destination names for any cargo lines at this stop
            cl_ids = [r.cargo_line_id for r in stop.unloads + stop.loads]
            dest_names = self._delivery_names(cl_ids)

            if stop.unloads:
                lines = []
                for ref in stop.unloads:
                    flag = " ⚠" if ref.cargo_line_id in conflict_cl_ids else ""
                    lines.append(
                        f"  {ref.scu_amount} SCU {ref.commodity_name}"
                        f"  [#{ref.contract_number}]{flag}"
                    )
                unload_summary = "Unload:\n" + "\n".join(lines)

            if stop.loads:
                lines = []
                for ref in stop.loads:
                    flag = " ⚠" if ref.cargo_line_id in conflict_cl_ids else ""
                    dest = dest_names.get(ref.cargo_line_id, "")
                    arrow = f" → {dest}" if dest else ""
                    lines.append(
                        f"  {ref.scu_amount} SCU {ref.commodity_name}"
                        f"{arrow}  [#{ref.contract_number}]{flag}"
                    )
                load_summary = "Load:\n" + "\n".join(lines)

            for grp in result.conflict_groups:
                stop_cl_ids = (
                    {r.cargo_line_id for r in stop.loads}
                    | {r.cargo_line_id for r in stop.unloads}
                )
                if stop_cl_ids & set(grp.cargo_line_ids):
                    conflict_note = (
                        f"Group {grp.group_id}: {grp.pickup_station_name} × "
                        f"{grp.commodity_name}"
                    )
                    break

            card = StopCard(
                stop_number=stop.stop_number,
                station_name=stop.station_name,
                action=stop.action,
                unload_summary=unload_summary,
                load_summary=load_summary,
                conflict_note=conflict_note,
                is_current=(idx == cur_idx),
                distance_km=getattr(stop, "distance_from_prev_km", None),
            )
            self.list_layout.insertWidget(self.list_layout.count() - 1, card)

    def _open_manual_stop_dialog(self) -> None:
        # Late import so the route panel doesn't pull in the dialog
        # module at startup (and to avoid a circular import via the
        # add_contract helpers the dialog reuses).
        from ..dialogs.manual_stop import ManualStopDialog
        if not self.controller.workday_id:
            return
        dlg = ManualStopDialog(self.controller, parent=self)
        dlg.exec()

    def _delivery_names(self, cargo_line_ids: list[int]) -> dict[int, str]:
        if not cargo_line_ids:
            return {}
        placeholders = ",".join("?" * len(cargo_line_ids))
        rows = self.controller.conn.execute(
            f"""
            SELECT cl.id, s.name AS delivery_name
            FROM cargo_lines cl
            JOIN stations s ON s.id = cl.delivery_station_id
            WHERE cl.id IN ({placeholders})
            """,
            cargo_line_ids,
        ).fetchall()
        return {r["id"]: r["delivery_name"] for r in rows}
