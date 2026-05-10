"""
DetailedPlanDialog — single popup showing every stop's tasks in detail.

Replaces the per-stop "View Load" popup that opened one window per stop.
Now there's exactly one button (in the Route panel header), one popup,
and the user scrolls through all stops in a single dialog.
"""

from __future__ import annotations

from collections import Counter

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton,
    QScrollArea, QVBoxLayout, QWidget, QFrame,
)

from ...app_controller import AppController
from ...planner.palletizer import VALID_SIZES, palletize


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
        cl_to_max_pallet: dict[int, int] = {}

        # Lookup zone + delivery name + max pallet size for each cargo line
        rows = controller.conn.execute(
            """
            SELECT cl.id, s.name AS dest_name, za.primary_zone_label AS zone_label,
                   ct.max_pallet_size
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
            cl_to_max_pallet[r["id"]] = r["max_pallet_size"]

        # Reverse-lookup: destination name → zone label, used in
        # conflict notes so we can tell the pilot exactly where the
        # partner-destination's cargo lives.
        dest_to_zone: dict[str, str] = {}
        for cl_id, dest_name in cl_to_dest.items():
            zone = cl_to_zone.get(cl_id)
            if zone and dest_name not in dest_to_zone:
                dest_to_zone[dest_name] = zone
        self._dest_to_zone = dest_to_zone

        # Per-cargo-line conflict info. Each partner entry carries the
        # data the pilot needs to act on the conflict at delivery: which
        # zone the partner's pallets reload to, the partner's contract
        # number to fulfill, and how many pallets of each ambiguous size
        # the partner is owed (i.e. what gets reloaded after this
        # contract's portion is unloaded).
        partner_pallet_info = controller.conn.execute(
            """
            SELECT cl.id AS cl_id, ct.contract_number, ct.max_pallet_size,
                   cl.scu_amount
            FROM cargo_lines cl
            JOIN contracts ct ON ct.id = cl.contract_id
            WHERE ct.workday_id = ?
            """,
            (controller.workday_id,),
        ).fetchall()
        cl_pallet_meta: dict[int, tuple[int, dict[int, int]]] = {}
        for r in partner_pallet_info:
            counts = Counter(palletize(r["scu_amount"], r["max_pallet_size"]))
            cl_pallet_meta[r["cl_id"]] = (r["contract_number"], dict(counts))

        cl_conflict_info: dict[int, tuple[set[int], list[dict]]] = {}
        for grp in result.conflict_groups:
            amb_sizes = set(grp.ambiguous_sizes)
            for d in grp.destinations:
                partners: list[dict] = []
                for od in grp.destinations:
                    if od.delivery_station_id == d.delivery_station_id:
                        continue
                    for ocl_id in od.cargo_line_ids:
                        contract_num, p_counts = cl_pallet_meta.get(
                            ocl_id, (None, {})
                        )
                        partners.append({
                            "name": od.delivery_station_name,
                            "zone": cl_to_zone.get(ocl_id, "?"),
                            "contract": contract_num,
                            "pallets_by_size": p_counts,
                        })
                for cl_id in d.cargo_line_ids:
                    cl_conflict_info[cl_id] = (amb_sizes, partners)
        self._cl_conflict_info = cl_conflict_info
        self._cl_to_max_pallet = cl_to_max_pallet

        # Scrollable list of per-stop sections
        list_widget = QWidget()
        list_layout = QVBoxLayout(list_widget)
        list_layout.setContentsMargins(0, 0, 0, 0)
        list_layout.setSpacing(8)

        n_stops = len(result.route_stops)
        for idx, stop in enumerate(result.route_stops):
            # Naming: first stop = "Initial Departure", last = "Final
            # Destination", everything in between = "Stop 1, 2, …".
            if idx == 0:
                stop_title = "Initial Departure"
            elif idx == n_stops - 1:
                stop_title = "Final Destination"
            else:
                stop_title = f"Stop {idx}"
            list_layout.addWidget(
                self._build_stop_section(
                    stop, stop_title, conflict_cl_ids, cl_to_dest,
                    cl_to_zone, result,
                )
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
        stop_title: str,
        conflict_cl_ids: set[int],
        cl_to_dest: dict[int, str],
        cl_to_zone: dict[int, str],
        result,
    ) -> QFrame:
        card = QFrame()
        card.setObjectName("card")
        stop_cl_ids = {r.cargo_line_id for r in stop.loads + stop.unloads}
        is_conflict_stop = bool(stop_cl_ids & conflict_cl_ids)
        card.setProperty("conflict", is_conflict_stop)

        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(4)

        # Header — label by role (Initial Departure / Stop N / Final
        # Destination) plus the station name and the action.
        h = QLabel(f"{stop_title}: {stop.station_name} — {stop.action}")
        h.setProperty("heading", True)
        h.setWordWrap(True)
        layout.addWidget(h)

        # Unload section — pallet-level breakdown with ambiguity markers
        if stop.unloads:
            layout.addWidget(self._section_label("Unload"))
            for ref in stop.unloads:
                zone = cl_to_zone.get(ref.cargo_line_id, "?")
                lbl = QLabel(
                    f"  {zone} → {ref.scu_amount} SCU {ref.commodity_name}"
                    f"  [Contract {ref.contract_number}]"
                )
                lbl.setWordWrap(True)
                lbl.setStyleSheet("font-weight: bold;")
                layout.addWidget(lbl)
                self._add_pallet_breakdown(layout, ref.cargo_line_id, action="deliver")

        else:
            layout.addWidget(self._muted("Unload: none"))

        # Load section — pallet-level breakdown with ambiguity markers
        if stop.loads:
            layout.addWidget(self._section_label("Load"))
            for ref in stop.loads:
                zone = cl_to_zone.get(ref.cargo_line_id, "?")
                dest = cl_to_dest.get(ref.cargo_line_id, "?")
                lbl = QLabel(
                    f"  {zone} → {ref.scu_amount} SCU {ref.commodity_name} "
                    f"→ {dest}  [Contract {ref.contract_number}]"
                )
                lbl.setWordWrap(True)
                lbl.setStyleSheet("font-weight: bold;")
                layout.addWidget(lbl)
                self._add_pallet_breakdown(layout, ref.cargo_line_id, action="load")
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
                lbl.setStyleSheet("color: #ff8a3c;")
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

    def _add_pallet_breakdown(
        self,
        layout: QVBoxLayout,
        cargo_line_id: int,
        *,
        action: str,
    ) -> None:
        """Show the pallet count×size breakdown for a cargo line, with
        an asterisk on ambiguous (conflict-group) sizes and a footer
        line per ambiguous size telling the pilot what to expect."""
        max_pallet = self._cl_to_max_pallet.get(cargo_line_id)
        scu = self.controller.conn.execute(
            "SELECT scu_amount FROM cargo_lines WHERE id = ?", (cargo_line_id,)
        ).fetchone()
        if not scu or not max_pallet:
            return
        pallets = palletize(scu["scu_amount"], max_pallet)
        counts = Counter(pallets)
        amb_sizes, partners = self._cl_conflict_info.get(cargo_line_id, (set(), []))

        # Pallet line
        parts = []
        for size in VALID_SIZES:
            if counts[size] > 0:
                star = "*" if size in amb_sizes else ""
                parts.append(f"{counts[size]}×{size} SCU{star}")
        breakdown_text = "    Pallets: " + " + ".join(parts) if parts else ""
        lbl = QLabel(breakdown_text)
        lbl.setWordWrap(True)
        layout.addWidget(lbl)

        # Per-ambiguous-size note in plain language.
        #   load    → "1×4 SCU pallet — sizing conflict with X. Conflict
        #              will be resolved at delivery."
        #   deliver → "1×4 SCU pallet — known conflict. After unloading
        #              this contract, M×4 SCU shall be reloaded into
        #              zone Z to fulfill Contract N."
        for size in sorted(amb_sizes, reverse=True):
            n_here = counts[size]
            if n_here == 0:
                continue

            # Distinct partner names for the load summary.
            partner_names = sorted({p["name"] for p in partners})
            partner_text = (
                " / ".join(partner_names)
                if partner_names else "another destination"
            )

            if action == "load":
                msg = (
                    f"      ⚠ {n_here}×{size} SCU pallet"
                    f"{'s' if n_here != 1 else ''} — sizing conflict "
                    f"with {partner_text}. Pick up as normal; the "
                    f"conflict is resolved at delivery."
                )
                note = QLabel(msg)
                note.setWordWrap(True)
                note.setStyleSheet("color: #ff8a3c;")
                layout.addWidget(note)
                continue

            # ── deliver ─────────────────────────────────────────────
            # For each partner that has pallets of this ambiguous size,
            # write one line: how many of size SCU get reloaded, into
            # which zone, for which contract.
            header = QLabel(
                f"      ⚠ {n_here}×{size} SCU pallet"
                f"{'s' if n_here != 1 else ''} — known conflict with "
                f"{partner_text}."
            )
            header.setWordWrap(True)
            header.setStyleSheet("color: #ff8a3c;")
            layout.addWidget(header)

            wrote_any_partner = False
            for p in partners:
                p_count = p["pallets_by_size"].get(size, 0)
                if p_count <= 0:
                    continue
                wrote_any_partner = True
                zone_text = (
                    f"zone {p['zone']}" if p["zone"] and p["zone"] != "?"
                    else "the partner's assigned zone"
                )
                contract_text = (
                    f"Contract {p['contract']}"
                    if p["contract"] is not None
                    else "the partner contract"
                )
                line = QLabel(
                    f"        After this contract is unloaded, the "
                    f"remaining {p_count}×{size} SCU shall be reloaded "
                    f"into {zone_text} to fulfill {contract_text} "
                    f"({p['name']})."
                )
                line.setWordWrap(True)
                line.setStyleSheet("color: #ff8a3c;")
                layout.addWidget(line)

            if not wrote_any_partner:
                # Shouldn't happen — if THIS dest has ambiguous pallets
                # of this size, the partner had matching ones — but be
                # safe rather than render a header with no follow-up.
                fallback = QLabel(
                    "        Resolve the ambiguous pallets at the "
                    "elevator before continuing."
                )
                fallback.setWordWrap(True)
                fallback.setStyleSheet("color: #ff8a3c;")
                layout.addWidget(fallback)

    def _section_label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet("font-weight: bold; color: #5be4ff; margin-top: 4px;")
        return lbl

    def _muted(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setProperty("muted", True)
        return lbl
