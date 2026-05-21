"""PDF export for the computed loading plan.

Modeled after the in-app Detailed Plan dialog: one card per stop with
zone-grouped Unload / Transload / Load sections, conflict warnings,
and the onboard snapshot after each stop. Text-only — easier to read
on a phone in the cockpit than a tiny diagram, and prints cleanly.

Implementation uses Qt's built-in QPdfWriter + QPainter so there's no
extra dependency beyond PySide6.
"""

from __future__ import annotations

from collections import Counter, OrderedDict
from typing import TYPE_CHECKING

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import (
    QBrush, QColor, QFont, QFontMetrics, QPageLayout, QPageSize,
    QPainter, QPdfWriter, QPen,
)

from .planner.palletizer import VALID_SIZES, palletize

if TYPE_CHECKING:
    from .app_controller import AppController


# Page geometry — letter paper at 150 DPI.
_DPI = 150
_PAGE_W = int(8.5 * _DPI)
_PAGE_H = int(11 * _DPI)
_MARGIN = 75               # ~0.5 inch
_CONTENT_W = _PAGE_W - 2 * _MARGIN
_BOTTOM = _PAGE_H - _MARGIN

# Palette — matches the in-app Detailed Plan styling.
_INK = QColor("#0d2330")
_ACCENT = QColor("#26b6d4")          # cyan headings
_HEADER = QColor("#0a5d6b")          # darker cyan for stop title
_ZONE_HEAD = QColor("#1b7a8a")       # zone-group header
_MUTED = QColor("#5d7280")
_WARN = QColor("#c83b3b")
_TRANSLOAD = QColor("#1d6f8a")
_CONFLICT = QColor("#c8631b")


class _Cursor:
    """Tracks current page-Y and starts a new page when content runs
    out of room. Centralises word-wrap drawing so every text line uses
    the same vertical budgeting logic."""

    def __init__(self, painter: QPainter, writer: QPdfWriter):
        self.painter = painter
        self.writer = writer
        self.y = _MARGIN

    def new_page(self) -> None:
        self.writer.newPage()
        self.y = _MARGIN

    def space(self, px: int) -> None:
        self.y += px

    def need(self, px: int) -> None:
        """Start a new page if *px* won't fit in the remaining space."""
        if self.y + px > _BOTTOM:
            self.new_page()

    def text(
        self,
        s: str,
        *,
        font: QFont,
        color: QColor = _INK,
        indent: int = 0,
        gap_after: int = 4,
    ) -> None:
        """Draw word-wrapped text starting at the current cursor.
        Advances the cursor past the drawn block + gap_after pixels."""
        if not s:
            self.space(gap_after)
            return
        self.painter.setFont(font)
        self.painter.setPen(QPen(color))
        # Measure required height via QFontMetrics with the same width.
        fm = QFontMetrics(font)
        # Reserve up to 6 lines of text per draw; if a single piece of
        # text would exceed that, page-break before measuring.
        max_h = 6 * fm.lineSpacing()
        if self.y + fm.lineSpacing() > _BOTTOM:
            self.new_page()
        rect = QRect(
            _MARGIN + indent, self.y,
            _CONTENT_W - indent, _BOTTOM - self.y,
        )
        bounding = self.painter.boundingRect(
            rect,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
            | Qt.TextFlag.TextWordWrap,
            s,
        )
        # If the wrapped block won't fit on this page, page-break first
        # (unless we're already at the top of a fresh page — then just
        # let it overflow).
        if bounding.height() > _BOTTOM - self.y and self.y > _MARGIN + 1:
            self.new_page()
            rect = QRect(
                _MARGIN + indent, self.y,
                _CONTENT_W - indent, _BOTTOM - self.y,
            )
            bounding = self.painter.boundingRect(
                rect,
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
                | Qt.TextFlag.TextWordWrap,
                s,
            )
        self.painter.drawText(
            rect,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
            | Qt.TextFlag.TextWordWrap,
            s,
        )
        self.y += bounding.height() + gap_after

    def rule(self, color: QColor = _MUTED) -> None:
        """Thin horizontal divider."""
        self.painter.setPen(QPen(color, 1))
        self.painter.drawLine(
            _MARGIN, self.y, _PAGE_W - _MARGIN, self.y,
        )
        self.y += 8


# ── Public entry point ────────────────────────────────────────────────

def export_plan_pdf(controller: "AppController", path: str) -> None:
    result = controller._last_result
    if result is None or not result.route_stops:
        raise RuntimeError("No computed plan to export — recompute first.")

    ship_row = controller.conn.execute(
        "SELECT s.name, s.manufacturer, s.total_scu "
        "FROM ships s JOIN workdays w ON w.ship_id = s.id "
        "WHERE w.id = ?",
        (controller.workday_id,),
    ).fetchone()
    ship_name = ship_row["name"] if ship_row else "—"
    ship_total = ship_row["total_scu"] if ship_row else 0

    # Lookup tables that mirror what DetailedPlanDialog builds.
    cl_to_dest: dict[int, str] = {}
    cl_to_zone: dict[int, str] = {}
    cl_to_max_pallet: dict[int, int] = {}
    for r in controller.conn.execute(
        """
        SELECT cl.id, s.name AS dest_name,
               za.primary_zone_label AS zone_label,
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
    ).fetchall():
        cl_to_dest[r["id"]] = r["dest_name"]
        cl_to_zone[r["id"]] = r["zone_label"] or "?"
        cl_to_max_pallet[r["id"]] = r["max_pallet_size"]

    conflict_cl_ids: set[int] = set()
    for grp in result.conflict_groups:
        conflict_cl_ids.update(grp.cargo_line_ids)

    writer = QPdfWriter(path)
    writer.setPageSize(QPageSize(QPageSize.PageSizeId.Letter))
    writer.setPageOrientation(QPageLayout.Orientation.Portrait)
    writer.setResolution(_DPI)
    writer.setTitle(f"{ship_name} loading plan")

    painter = QPainter(writer)
    try:
        cursor = _Cursor(painter, writer)
        _render_cover(cursor, ship_name, ship_total, result)

        n_stops = len(result.route_stops)
        for idx, stop in enumerate(result.route_stops):
            cursor.new_page()
            if idx == 0:
                title = "Initial Departure"
            elif idx == n_stops - 1:
                title = "Final Destination"
            else:
                title = f"Stop {idx}"
            _render_stop(
                cursor, controller, stop, title, result,
                cl_to_dest, cl_to_zone, cl_to_max_pallet,
                conflict_cl_ids,
            )
    finally:
        painter.end()


# ── Cover page ─────────────────────────────────────────────────────────

def _render_cover(cursor, ship_name, ship_total, result) -> None:
    cursor.text(
        "STAR CITIZEN CARGO MANAGER",
        font=QFont("Segoe UI", 22, QFont.Weight.Bold),
        color=_ACCENT,
        gap_after=8,
    )
    cursor.text(
        "Loading Plan",
        font=QFont("Segoe UI", 18, QFont.Weight.Bold),
        gap_after=16,
    )

    cursor.text(
        f"Ship: {ship_name}  ({ship_total} SCU capacity)",
        font=QFont("Segoe UI", 12),
        color=_MUTED,
        gap_after=2,
    )
    cursor.text(
        f"{len(result.route_stops)} stops  •  "
        f"{sum(len(v) for v in result.transload_moves.values())} "
        f"transload move(s)",
        font=QFont("Segoe UI", 12),
        color=_MUTED,
        gap_after=24,
    )

    cursor.text(
        "Route",
        font=QFont("Segoe UI", 14, QFont.Weight.Bold),
        gap_after=8,
    )
    n_stops = len(result.route_stops)
    for idx, stop in enumerate(result.route_stops):
        if idx == 0:
            tag = "Initial Departure"
        elif idx == n_stops - 1:
            tag = "Final Destination"
        else:
            tag = f"Stop {idx}"
        n_loads = len(stop.loads)
        n_unloads = len(stop.unloads)
        line = (
            f"{stop.stop_number:>2}.  {tag}: {stop.station_name}  "
            f"({stop.action})  —  "
            f"loads {n_loads}, unloads {n_unloads}"
        )
        cursor.text(
            line,
            font=QFont("Segoe UI", 11),
            indent=10,
            gap_after=2,
        )


# ── Per-stop page ──────────────────────────────────────────────────────

def _render_stop(
    cursor, controller, stop, title, result,
    cl_to_dest, cl_to_zone, cl_to_max_pallet,
    conflict_cl_ids,
) -> None:
    # Title
    cursor.text(
        f"{title}: {stop.station_name}",
        font=QFont("Segoe UI", 20, QFont.Weight.Bold),
        color=_HEADER,
        gap_after=2,
    )
    cursor.text(
        f"Action: {stop.action}",
        font=QFont("Segoe UI", 11),
        color=_MUTED,
        gap_after=10,
    )
    cursor.rule()

    stop_cl_ids = {r.cargo_line_id for r in stop.loads + stop.unloads}
    stop_conflict_groups = [
        g for g in result.conflict_groups
        if any(cl in stop_cl_ids for cl in g.cargo_line_ids)
    ]
    if stop_conflict_groups:
        cursor.text(
            "⚠ WARNING: this stop touches conflict cargo — track every "
            "pallet at the elevator (see Conflict notes below).",
            font=QFont("Segoe UI", 11, QFont.Weight.Bold),
            color=_WARN,
            gap_after=14,
        )

    # Unload
    if stop.unloads:
        _section_header(cursor, "Unload")
        _render_zone_grouped(
            cursor, controller, stop.unloads,
            cl_to_zone, cl_to_dest, cl_to_max_pallet,
            action="deliver",
        )
    else:
        cursor.text(
            "Unload: none",
            font=QFont("Segoe UI", 11, QFont.Weight.Normal),
            color=_MUTED,
            gap_after=12,
        )

    # Transload
    moves = result.transload_moves.get(stop.stop_number, [])
    if moves:
        _section_header(cursor, "Transload (optional consolidation)")
        for m in moves:
            cursor.text(
                f"Move {m.scu_amount} SCU {m.commodity_name}  "
                f"{m.from_zone} → {m.to_zone}  "
                f"(→ {m.delivery_station_name})  "
                f"[Contract {m.contract_number}]",
                font=QFont("Segoe UI", 11),
                color=_TRANSLOAD,
                indent=10,
                gap_after=2,
            )
            if m.pallet_breakdown:
                cursor.text(
                    f"Pallets: {m.pallet_breakdown}",
                    font=QFont("Segoe UI", 10),
                    color=_MUTED,
                    indent=30,
                    gap_after=6,
                )

    # Load
    if stop.loads:
        _section_header(cursor, "Load")
        _render_zone_grouped(
            cursor, controller, stop.loads,
            cl_to_zone, cl_to_dest, cl_to_max_pallet,
            action="load",
        )
    else:
        cursor.text(
            "Load: none",
            font=QFont("Segoe UI", 11, QFont.Weight.Normal),
            color=_MUTED,
            gap_after=12,
        )

    # Conflict notes
    if stop_conflict_groups:
        _section_header(cursor, "Conflict notes")
        for g in stop_conflict_groups:
            names = " / ".join(d.delivery_station_name for d in g.destinations)
            amb = " + ".join(f"1×{s}" for s in g.ambiguous_sizes)
            cursor.text(
                f"⚠ Group {g.group_id} — {g.pickup_station_name} × "
                f"{g.commodity_name}",
                font=QFont("Segoe UI", 11, QFont.Weight.Bold),
                color=_CONFLICT,
                indent=10,
                gap_after=2,
            )
            cursor.text(
                f"destinations: {names}",
                font=QFont("Segoe UI", 10),
                color=_CONFLICT,
                indent=20,
                gap_after=2,
            )
            cursor.text(
                f"conflict sizes: {amb}",
                font=QFont("Segoe UI", 10),
                color=_CONFLICT,
                indent=20,
                gap_after=8,
            )

    # Onboard after this stop
    snapshot = result.snapshots.get(stop.stop_number, [])
    _section_header(cursor, "Onboard after this stop")
    if snapshot:
        # Group by zone for tidier reading.
        by_zone: "OrderedDict[str, list]" = OrderedDict()
        for e in snapshot:
            by_zone.setdefault(e.zone_label, []).append(e)
        for zone, entries in by_zone.items():
            zone_scu = sum(e.scu_amount for e in entries)
            cursor.text(
                f"{zone}:  {zone_scu} SCU",
                font=QFont("Segoe UI", 11, QFont.Weight.Bold),
                color=_ZONE_HEAD,
                indent=10,
                gap_after=2,
            )
            for e in entries:
                tag = "  ⚠" if e.is_conflicted else ""
                cursor.text(
                    f"{e.scu_amount} SCU {e.commodity_name} "
                    f"→ {e.delivery_station_name}{tag}",
                    font=QFont("Segoe UI", 10),
                    color=_MUTED,
                    indent=30,
                    gap_after=2,
                )
    else:
        cursor.text(
            "Empty",
            font=QFont("Segoe UI", 11, QFont.Weight.Normal),
            color=_MUTED,
            indent=10,
            gap_after=6,
        )


def _section_header(cursor, text: str) -> None:
    cursor.space(4)
    cursor.text(
        text,
        font=QFont("Segoe UI", 13, QFont.Weight.Bold),
        color=_ACCENT,
        gap_after=6,
    )


def _render_zone_grouped(
    cursor, controller, refs, cl_to_zone, cl_to_dest, cl_to_max_pallet,
    *, action: str,
) -> None:
    """Match DetailedPlanDialog._render_zone_grouped: one header per
    zone, all cargo lines into/out of that zone listed under it."""
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
        cursor.text(
            f"{zone}  →  {dest_str}  ({total_scu} SCU total)",
            font=QFont("Segoe UI", 12, QFont.Weight.Bold),
            color=_ZONE_HEAD,
            indent=10,
            gap_after=4,
        )
        for ref in zone_refs:
            cursor.text(
                f"{ref.scu_amount} SCU {ref.commodity_name}  "
                f"[Contract {ref.contract_number}]",
                font=QFont("Segoe UI", 11),
                indent=30,
                gap_after=2,
            )
            _render_pallet_breakdown(
                cursor, ref, cl_to_max_pallet,
            )
        cursor.space(6)


def _render_pallet_breakdown(cursor, ref, cl_to_max_pallet) -> None:
    max_pallet = cl_to_max_pallet.get(ref.cargo_line_id)
    if not max_pallet:
        return
    pallets = palletize(ref.scu_amount, max_pallet)
    counts = Counter(pallets)
    parts = []
    for size in VALID_SIZES:
        if counts[size] > 0:
            parts.append(f"{counts[size]}×{size} SCU")
    if parts:
        cursor.text(
            "Pallets: " + " + ".join(parts),
            font=QFont("Segoe UI", 10),
            color=_MUTED,
            indent=50,
            gap_after=4,
        )
