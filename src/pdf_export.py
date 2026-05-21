"""PDF export for the computed loading plan.

Produces a cockpit cheat sheet: one page per stop with the bay state
diagram on top, then the unload / load / transload lists below.
Sized for letter-paper printing or on-screen reading on a phone in
the game.

Implementation uses Qt's built-in QPdfWriter + QPainter so there's no
extra dependency beyond PySide6.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QRect, QRectF, Qt
from PySide6.QtGui import (
    QBrush, QColor, QFont, QPageLayout, QPageSize, QPainter, QPdfWriter, QPen,
)

if TYPE_CHECKING:
    from .app_controller import AppController


# Page geometry constants — values are in QPdfWriter device pixels
# (resolution = 150 DPI below, so 150 px ≈ 1 inch).
_DPI = 150
_MARGIN = 60          # ~0.4 inch
_PAGE_W = int(8.5 * _DPI)
_PAGE_H = int(11 * _DPI)
_CONTENT_W = _PAGE_W - 2 * _MARGIN

# Colors — match the in-app palette so the printout reads like the UI.
_INK = QColor("#0d2330")          # near-black for text
_ACCENT = QColor("#26b6d4")       # heading cyan
_MUTED = QColor("#5d7280")        # subdued grey
_ZONE_BORDER = QColor("#26b6d4")
_EMPTY_FILL = QColor("#e6f1f5")
_ZONE_BG = QColor("#ffffff")


def export_plan_pdf(controller: "AppController", path: str) -> None:
    """Write the current computed plan to *path* as a PDF.

    Raises if no plan has been computed yet on the active workday.
    """
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

    # Destination colour map for the bay diagram fills. Colors live
    # on the stations table once they're auto-assigned per workday.
    colour_map = {
        r["name"]: r["color_hex"]
        for r in controller.conn.execute(
            "SELECT name, color_hex FROM stations "
            "WHERE color_hex IS NOT NULL"
        ).fetchall()
    }

    # Per-zone scu_capacity lookup — BayZoneGeom doesn't carry it.
    zone_caps = {
        r["zone_label"]: r["scu_capacity"]
        for r in controller.conn.execute(
            "SELECT z.zone_label, z.scu_capacity "
            "FROM ship_zones z "
            "JOIN workdays w ON w.ship_id = z.ship_id "
            "WHERE w.id = ?",
            (controller.workday_id,),
        ).fetchall()
    }

    writer = QPdfWriter(path)
    writer.setPageSize(QPageSize(QPageSize.PageSizeId.Letter))
    writer.setPageOrientation(QPageLayout.Orientation.Portrait)
    writer.setResolution(_DPI)
    writer.setTitle(f"{ship_name} loading plan")

    painter = QPainter(writer)
    try:
        # Cover page
        _render_cover(painter, ship_name, ship_total, result)

        # One page per stop
        for stop in result.route_stops:
            writer.newPage()
            _render_stop_page(
                painter, controller, stop, result, colour_map, zone_caps,
            )
    finally:
        painter.end()


# ── Cover page ─────────────────────────────────────────────────────────

def _render_cover(painter, ship_name, ship_total, result) -> None:
    y = _MARGIN

    painter.setPen(QPen(_ACCENT))
    painter.setFont(QFont("Segoe UI", 28, QFont.Weight.Bold))
    painter.drawText(_MARGIN, y + 40, "STAR CITIZEN CARGO MANAGER")
    y += 80

    painter.setPen(QPen(_INK))
    painter.setFont(QFont("Segoe UI", 18, QFont.Weight.Bold))
    painter.drawText(_MARGIN, y + 20, "Loading Plan")
    y += 60

    painter.setPen(QPen(_MUTED))
    painter.setFont(QFont("Segoe UI", 12))
    painter.drawText(_MARGIN, y + 20, f"Ship: {ship_name}  ({ship_total} SCU)")
    painter.drawText(_MARGIN, y + 40,
                     f"Stops: {len(result.route_stops)}  •  "
                     f"Snapshots: {len(result.snapshots)}")
    y += 80

    # Route summary
    painter.setPen(QPen(_INK))
    painter.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
    painter.drawText(_MARGIN, y + 20, "Route")
    y += 40

    painter.setFont(QFont("Segoe UI", 11))
    for stop in result.route_stops:
        if y > _PAGE_H - _MARGIN - 40:
            break
        line = (
            f"  {stop.stop_number:>2}.  {stop.station_name}  "
            f"({stop.action})  —  loads={stop.loads}, unloads={stop.unloads}"
        )
        painter.drawText(_MARGIN, y + 16, line)
        y += 22

    # Footer
    painter.setPen(QPen(_MUTED))
    painter.setFont(QFont("Segoe UI", 9))
    painter.drawText(
        _MARGIN, _PAGE_H - _MARGIN,
        "Loading doctrine: lower unload_priority zones drain first.",
    )


# ── Per-stop page ──────────────────────────────────────────────────────

def _render_stop_page(
    painter, controller, stop, result, colour_map, zone_caps,
) -> None:
    y = _MARGIN

    # Header
    painter.setPen(QPen(_ACCENT))
    painter.setFont(QFont("Segoe UI", 22, QFont.Weight.Bold))
    painter.drawText(
        _MARGIN, y + 30,
        f"Stop {stop.stop_number}: {stop.station_name}",
    )
    painter.setPen(QPen(_MUTED))
    painter.setFont(QFont("Segoe UI", 12))
    painter.drawText(
        _MARGIN, y + 56,
        f"{stop.action}  •  loads={stop.loads}, unloads={stop.unloads}",
    )
    y += 80

    # Bay diagram
    snapshot = result.snapshots.get(stop.stop_number, [])
    diagram_rect = QRect(_MARGIN, y, _CONTENT_W, 260)
    _draw_bay_diagram(
        painter, controller, snapshot, diagram_rect,
        colour_map, zone_caps,
    )
    y += 280

    # Loads, unloads, transloads
    transloads = result.transload_moves.get(stop.stop_number, [])
    y = _draw_section(
        painter, y, "Unloads",
        [_format_unload_line(e) for e in snapshot
         if False],  # snapshots are post-state; unloads enumerated below
    )

    # The snapshot is post-state, so derive unload list from the
    # previous stop's snapshot minus this one — simpler to read
    # straight from the cargo table by stop:
    unload_lines = _unload_lines_for_stop(controller, stop)
    if unload_lines:
        y = _draw_section(painter, y, f"Unload ({stop.unloads})", unload_lines)

    load_lines = _load_lines_for_stop(controller, stop)
    if load_lines:
        y = _draw_section(painter, y, f"Load ({stop.loads})", load_lines)

    if transloads:
        y = _draw_section(
            painter, y,
            f"Transload ({len(transloads)})",
            [
                f"  cl#{m.cargo_line_id}  {m.scu_amount} SCU "
                f"{m.commodity_name} → {m.delivery_station_name}  "
                f"({m.from_zone} → {m.to_zone})"
                for m in transloads
            ],
        )

    # Footer
    painter.setPen(QPen(_MUTED))
    painter.setFont(QFont("Segoe UI", 9))
    painter.drawText(
        _MARGIN, _PAGE_H - _MARGIN,
        f"— Stop {stop.stop_number} of {len(result.route_stops)} —",
    )


def _draw_section(painter, y, title, lines):
    if y > _PAGE_H - _MARGIN - 80:
        return y
    painter.setPen(QPen(_INK))
    painter.setFont(QFont("Segoe UI", 13, QFont.Weight.Bold))
    painter.drawText(_MARGIN, y + 18, title)
    y += 30
    painter.setFont(QFont("Segoe UI", 10))
    for line in lines:
        if y > _PAGE_H - _MARGIN - 30:
            painter.drawText(_MARGIN + 10, y + 14, "…")
            y += 18
            break
        painter.drawText(_MARGIN + 10, y + 14, line)
        y += 18
    return y + 10


def _unload_lines_for_stop(controller, stop) -> list[str]:
    rows = controller.conn.execute(
        """
        SELECT cl.id AS cl_id, cl.scu_amount, c.contract_number,
               cm.name AS commodity, s.name AS dest
        FROM cargo_lines cl
        JOIN contracts c ON c.id = cl.contract_id
        JOIN commodities cm ON cm.id = cl.commodity_id
        JOIN stations s ON s.id = cl.delivery_station_id
        WHERE c.workday_id = ?
          AND cl.delivery_station_id = ?
        ORDER BY cl.id
        """,
        (controller.workday_id, stop.station_id),
    ).fetchall()
    return [
        f"  cl#{r['cl_id']}  {r['scu_amount']} SCU "
        f"{r['commodity']} → {r['dest']}  (contract #{r['contract_number']})"
        for r in rows
    ] if stop.unloads else []


def _load_lines_for_stop(controller, stop) -> list[str]:
    rows = controller.conn.execute(
        """
        SELECT cl.id AS cl_id, cl.scu_amount, c.contract_number,
               cm.name AS commodity, s.name AS dest,
               za.primary_zone_label
        FROM cargo_lines cl
        JOIN contracts c ON c.id = cl.contract_id
        JOIN commodities cm ON cm.id = cl.commodity_id
        JOIN stations s ON s.id = cl.delivery_station_id
        LEFT JOIN zone_assignments za
               ON za.cargo_line_id = cl.id AND za.workday_id = ?
        WHERE c.workday_id = ?
          AND c.pickup_station_id = ?
        ORDER BY cl.id
        """,
        (controller.workday_id, controller.workday_id, stop.station_id),
    ).fetchall()
    return [
        f"  cl#{r['cl_id']}  {r['scu_amount']} SCU "
        f"{r['commodity']} → {r['dest']}  →  {r['primary_zone_label'] or '—'}  "
        f"(contract #{r['contract_number']})"
        for r in rows
    ] if stop.loads else []


def _format_unload_line(e):  # kept for future use
    return (
        f"  cl#{e.cargo_line_id}  {e.scu_amount} SCU "
        f"{e.commodity_name} → {e.delivery_station_name}  ({e.zone_label})"
    )


# ── Bay diagram ────────────────────────────────────────────────────────

def _draw_bay_diagram(
    painter, controller, snapshot, rect, colour_map, zone_caps,
) -> None:
    """Draw a top-down bay diagram inside *rect*: one column per zone,
    fill height proportional to SCU usage, label + SCU text overlaid.
    """
    bays = controller.get_bay_layout()
    if not bays:
        return

    # Map zone → list of (destination, scu).
    by_zone: dict[str, list] = {}
    for e in snapshot:
        by_zone.setdefault(e.zone_label, []).append(e)

    # Title + scu summary
    painter.setPen(QPen(_INK))
    painter.setFont(QFont("Segoe UI", 13, QFont.Weight.Bold))
    painter.drawText(rect.x(), rect.y() + 18, "Bay state at this stop")
    painter.setPen(QPen(_MUTED))
    painter.setFont(QFont("Segoe UI", 10))
    total_scu = sum(e.scu_amount for e in snapshot)
    painter.drawText(
        rect.x(), rect.y() + 36,
        f"Total onboard: {total_scu} SCU"
    )

    # Stack each bay vertically inside the rect.
    inner = QRect(rect.x(), rect.y() + 50, rect.width(), rect.height() - 50)
    bay_h = max(60, inner.height() // max(1, len(bays)) - 8)
    for i, bay in enumerate(bays):
        bay_top = inner.y() + i * (bay_h + 8)
        _draw_bay(
            painter, bay, by_zone, colour_map, zone_caps,
            QRect(inner.x(), bay_top, inner.width(), bay_h),
        )


def _draw_bay(painter, bay, by_zone, colour_map, zone_caps, rect) -> None:
    """Draw one bay as a horizontal row of zone columns."""
    # Bay label on the left
    label_w = 60
    painter.setPen(QPen(_INK))
    painter.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
    painter.drawText(
        QRect(rect.x(), rect.y(), label_w, rect.height()),
        Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
        bay.bay_label.title(),
    )

    # Zones occupy the rest, sized proportional to width_units
    zones = bay.zones
    if not zones:
        return
    total_w = sum(z.width_units for z in zones)
    zone_area_x = rect.x() + label_w
    zone_area_w = rect.width() - label_w

    x = zone_area_x
    for z in zones:
        col_w = zone_area_w * z.width_units // max(1, total_w)
        col_rect = QRect(x, rect.y(), col_w - 4, rect.height())
        _draw_zone_column(painter, z, by_zone.get(z.zone_label, []),
                          colour_map, zone_caps, col_rect)
        x += col_w


def _draw_zone_column(painter, zone, entries, colour_map, zone_caps, rect) -> None:
    # Outline
    painter.setPen(QPen(_ZONE_BORDER, 1.5))
    painter.setBrush(QBrush(_EMPTY_FILL))
    painter.drawRect(rect)

    cap = zone_caps.get(zone.zone_label, 1)
    # cap above is in cubes which equals SCU for our boxes. Use zone
    # geometry as the SCU capacity proxy — matches scu_capacity in
    # practice.
    used = sum(e.scu_amount for e in entries)
    fill_ratio = min(1.0, used / max(1, cap))

    # Fill from bottom (ramp end) upward.
    fill_h = int(rect.height() * fill_ratio)
    fill_rect = QRect(
        rect.x(), rect.y() + rect.height() - fill_h,
        rect.width(), fill_h,
    )

    if entries:
        # Sort by SCU descending so the largest destination dominates.
        sorted_entries = sorted(entries, key=lambda e: -e.scu_amount)
        primary = sorted_entries[0]
        c = QColor(colour_map.get(primary.delivery_station_name, "#888888"))
        c.setAlpha(220)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(c))
        painter.drawRect(fill_rect)

        # Mixed indicator
        if len({e.delivery_station_name for e in entries}) > 1:
            painter.setPen(QPen(QColor("#ff8a3c"), 1.5, Qt.PenStyle.DashLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(rect)

    # Zone label
    painter.setPen(QPen(_INK))
    painter.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
    painter.drawText(
        rect, Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter,
        zone.zone_label,
    )
    # SCU summary at the bottom
    painter.setFont(QFont("Segoe UI", 8))
    painter.drawText(
        rect, Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignHCenter,
        f"{used}/{cap}",
    )
    # Destination name centered if any
    if entries:
        sorted_entries = sorted(entries, key=lambda e: -e.scu_amount)
        dest = sorted_entries[0].delivery_station_name
        if len({e.delivery_station_name for e in entries}) > 1:
            dest = "MIXED"
        painter.setFont(QFont("Segoe UI", 8))
        painter.setPen(QPen(QColor("#ffffff")))
        painter.drawText(
            rect, Qt.AlignmentFlag.AlignCenter,
            dest,
        )
