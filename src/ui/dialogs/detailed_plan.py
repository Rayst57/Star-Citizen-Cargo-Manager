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

        # Conflict warning banner. If THIS stop loads or unloads cargo
        # that's part of a conflict group, the pilot has to track each
        # pallet carefully (the elevator can't tell ambiguous sizes
        # apart). Flagging the stop loudly helps avoid mistakes.
        stop_conflict_groups = [
            g for g in result.conflict_groups
            if any(cl in stop_cl_ids for cl in g.cargo_line_ids)
        ]
        if stop_conflict_groups:
            warn = QLabel(
                "<span style='color:#ff3030;font-weight:bold;'>⚠ WARNING:</span>"
                "  this stop touches conflict cargo — track every "
                "pallet at the elevator (see Conflict notes below)."
            )
            warn.setTextFormat(Qt.TextFormat.RichText)
            warn.setWordWrap(True)
            warn.setStyleSheet("background-color: #2a0d0d; padding: 4px 8px; "
                               "border: 1px solid #ff3030; border-radius: 3px;")
            layout.addWidget(warn)

        # Unload section — grouped by zone so the pilot has a single
        # action per zone instead of one row per cargo line.
        if stop.unloads:
            layout.addWidget(self._section_label("Unload"))
            self._render_zone_grouped(
                layout, stop.unloads, cl_to_zone, cl_to_dest, action="deliver",
            )
        else:
            layout.addWidget(self._muted("Unload: none"))

        # Transload section — planner's consolidation recommendation.
        # Shown between Unload and Load so the pilot can choose to merge
        # same-destination cargo into fewer zones before the next pickup
        # arrives. Skipping is fine; the rest of the plan still works.
        moves = getattr(result, "transload_moves", {}).get(stop.stop_number, [])
        if moves:
            layout.addWidget(self._section_label("Transload (optional consolidation)"))
            for m in moves:
                line = QLabel(
                    f"  Move {m.scu_amount} SCU {m.commodity_name}  "
                    f"{m.from_zone}  →  {m.to_zone}  "
                    f"(→ {m.delivery_station_name})  "
                    f"[Contract {m.contract_number}]"
                )
                line.setWordWrap(True)
                line.setStyleSheet("color: #a0e0ff;")
                layout.addWidget(line)
                if m.pallet_breakdown:
                    pl = QLabel(f"      Pallets: {m.pallet_breakdown}")
                    pl.setWordWrap(True)
                    pl.setProperty("muted", True)
                    layout.addWidget(pl)

        # Load section — same zone-grouped layout. All cargo lines that
        # land in the same zone (e.g. Contract 2's two AD-bound lines
        # both going to F2) are shown under a single zone header.
        if stop.loads:
            layout.addWidget(self._section_label("Load"))
            self._render_zone_grouped(
                layout, stop.loads, cl_to_zone, cl_to_dest, action="load",
            )
        else:
            layout.addWidget(self._muted("Load: none"))

        # Conflict notes (which group this stop touches)
        if stop_conflict_groups:
            layout.addWidget(self._section_label("Conflict notes"))
            for g in stop_conflict_groups:
                names = " / ".join(d.delivery_station_name for d in g.destinations)
                amb = " + ".join(f"1×{s}" for s in g.ambiguous_sizes)
                lbl = QLabel(
                    f"  ⚠ Group {g.group_id} — {g.pickup_station_name} × "
                    f"{g.commodity_name}\n"
                    f"      destinations: {names}\n"
                    f"      conflict sizes: {amb}"
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

        # Advisories — surface any planner findings for this stop so the
        # user can consider a manual adjustment. Lazily fetched once per
        # dialog open and cached on the controller.
        try:
            advisories = self.controller.compute_advisories().get(
                stop.stop_number, [],
            )
        except Exception:
            advisories = []
        if advisories:
            from ..widgets.advisory_panel import _AdvisoryCard
            layout.addWidget(
                self._section_label("Advisories (consider manual adjustment)")
            )
            for adv in advisories:
                layout.addWidget(_AdvisoryCard(adv))

        return card

    def _render_zone_grouped(
        self,
        layout: QVBoxLayout,
        refs: list,
        cl_to_zone: dict[int, str],
        cl_to_dest: dict[int, str],
        *,
        action: str,
    ) -> None:
        """Render load/unload entries grouped by zone — one header per
        zone, all the cargo lines going into/coming out of that zone
        listed under it. So Contract 2's two AD-bound lines that both
        land in F2 read as a single F2 action.
        """
        # Preserve route order within each zone (stable_sort).
        from collections import OrderedDict
        by_zone: "OrderedDict[str, list]" = OrderedDict()
        for ref in refs:
            zone = cl_to_zone.get(ref.cargo_line_id, "?")
            by_zone.setdefault(zone, []).append(ref)

        for zone, zone_refs in by_zone.items():
            total_scu = sum(r.scu_amount for r in zone_refs)
            dests = []
            for r in zone_refs:
                d = cl_to_dest.get(r.cargo_line_id, "?")
                if d not in dests:
                    dests.append(d)
            dest_str = " + ".join(dests)
            header = QLabel(
                f"  {zone}  →  {dest_str}  ({total_scu} SCU total)"
            )
            header.setWordWrap(True)
            header.setStyleSheet("font-weight: bold; color: #5be4ff;")
            layout.addWidget(header)

            for ref in zone_refs:
                d = cl_to_dest.get(ref.cargo_line_id, "?")
                # When the zone header already names the destination,
                # the per-line summary can drop the dest to read tighter.
                if action == "load":
                    line = QLabel(
                        f"      {ref.scu_amount} SCU {ref.commodity_name}"
                        f"  [Contract {ref.contract_number}]"
                    )
                else:
                    line = QLabel(
                        f"      {ref.scu_amount} SCU {ref.commodity_name}"
                        f"  [Contract {ref.contract_number}]"
                    )
                line.setWordWrap(True)
                layout.addWidget(line)
                self._add_pallet_breakdown(
                    layout, ref.cargo_line_id, action=action,
                )

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
                    "        Resolve the conflict pallets at the "
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
