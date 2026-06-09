"""
HoldingTableWidget — sidebar listing pallets waiting for placement.

After a force-push drag-drop displaces pallets from a zone, they land
in the controller's pallet_holding table. This widget renders them as
a sidebar grouped by destination, with the same colour-by-destination
palette the BayCanvas uses. Each row exposes a "Pick up" button whose
signal the parent connects to start a drag-to-place workflow back into
the 3D view, and the header carries an "Auto-place all" button that
asks the controller to find homes for everything in holding.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QMessageBox, QPushButton, QScrollArea,
    QVBoxLayout, QWidget,
)


_DEFAULT_COLOR = "#3a4894"


def _luminance(hex_color: str) -> float:
    """0-1 perceived luminance for a #RRGGBB string. Used to pick a
    readable text colour on top of the destination tint."""
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return 0.5
    try:
        r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return 0.5
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255.0


class HoldingTableWidget(QWidget):
    """Sidebar listing pallets waiting for placement.

    Re-renders itself whenever the controller emits ``contracts_changed``
    or ``route_changed`` (the two signals that follow any holding-table
    mutation), and offers an explicit ``refresh()`` call for callers
    that want a manual rebuild.
    """

    # Emitted when the user clicks "Pick up" on a holding-pallet row.
    # The parent widget connects this to the drag-to-place workflow that
    # eventually re-locks the pallet into a zone (via lock_pallet) and
    # removes it from holding (via remove_from_holding).
    pallet_picked = Signal(int, int)  # (cargo_line_id, pallet_index)

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # ── Header ──────────────────────────────────────────────────
        header = QHBoxLayout()
        self.count_label = QLabel("Holding (0)")
        self.count_label.setProperty("heading", True)
        header.addWidget(self.count_label, 1)

        self.auto_btn = QPushButton("Auto-place all")
        self.auto_btn.setToolTip(
            "Try to find a free spot for every holding pallet."
        )
        self.auto_btn.clicked.connect(self._on_auto_place)
        header.addWidget(self.auto_btn)

        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.setProperty("flat", True)
        self.refresh_btn.clicked.connect(self.refresh)
        header.addWidget(self.refresh_btn)
        root.addLayout(header)

        # ── Scroll body ─────────────────────────────────────────────
        self._body = QWidget()
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(0, 0, 0, 0)
        self._body_layout.setSpacing(6)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        scroll.setWidget(self._body)
        self._scroll_area = scroll
        root.addWidget(scroll, 1)

        # Reconnect to controller signals — the holding sidebar must
        # mirror the controller's state at all times so the user
        # always sees the current waiting list.
        try:
            self.controller.contracts_changed.connect(self.refresh)
            self.controller.route_changed.connect(self.refresh)
        except AttributeError:
            # Allows tests to pass in a minimal controller stub that
            # doesn't carry Qt signals. The widget still works for
            # programmatic refresh() calls.
            pass

        self.refresh()

    # ── public API ──────────────────────────────────────────────────

    def refresh(self) -> None:
        """Rebuild the body from the controller's current holding list."""
        # Drop any existing children. We replace the body widget
        # outright so the previous children become unreachable via
        # findChildren() immediately — deleteLater() alone leaves them
        # parented under self for the rest of the event-loop tick,
        # which trips assertions in tests that count rendered groups.
        old_body = self._body
        new_body = QWidget()
        new_layout = QVBoxLayout(new_body)
        new_layout.setContentsMargins(0, 0, 0, 0)
        new_layout.setSpacing(6)
        self._body = new_body
        self._body_layout = new_layout
        # Swap the scroll area's widget so the new body becomes
        # findChildren()-visible and the old one detaches.
        scroll = self._scroll_area
        scroll.takeWidget()
        scroll.setWidget(new_body)
        old_body.setParent(None)
        old_body.deleteLater()

        rows = list(self.controller.list_holding_pallets())
        self.count_label.setText(f"Holding ({len(rows)})")

        if not rows:
            empty = QLabel("No pallets waiting for placement.")
            empty.setProperty("muted", True)
            empty.setWordWrap(True)
            self._body_layout.addWidget(empty)
            self._body_layout.addStretch(1)
            return

        # Group by destination (delivery_station_name). Iteration is
        # deterministic because list_holding_pallets() returns rows
        # ordered by destination already.
        groups: dict[str, list] = {}
        order: list[str] = []
        for r in rows:
            dest = r["delivery_station_name"]
            if dest not in groups:
                groups[dest] = []
                order.append(dest)
            groups[dest].append(r)

        for dest in order:
            self._body_layout.addWidget(
                self._build_group(dest, groups[dest])
            )
        self._body_layout.addStretch(1)

    # ── builders ────────────────────────────────────────────────────

    def _build_group(self, destination: str, rows: list) -> QFrame:
        first = rows[0]
        color = first["delivery_color"] or _DEFAULT_COLOR
        frame = QFrame()
        frame.setObjectName("destinationGroup")
        # Apply a tinted background card around the whole destination
        # group so the user can pick out each cluster at a glance.
        text_color = "#111" if _luminance(color) > 0.6 else "#f0f0f0"
        frame.setStyleSheet(
            f"QFrame#destinationGroup {{ "
            f"background-color: {color}33; "
            f"border: 1px solid {color}; "
            f"border-radius: 4px; "
            f"}} "
            f"QLabel[groupHeader='true'] {{ "
            f"color: {text_color}; "
            f"background-color: {color}; "
            f"padding: 4px 6px; "
            f"border-radius: 3px; "
            f"font-weight: bold; "
            f"}}"
        )
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        header_label = QLabel(f"→ {destination}  ({len(rows)})")
        header_label.setProperty("groupHeader", True)
        header_label.setProperty("destinationName", destination)
        layout.addWidget(header_label)

        for r in rows:
            layout.addWidget(self._build_row(r, color))

        return frame

    def _build_row(self, row, color: str) -> QFrame:
        frame = QFrame()
        frame.setObjectName("card")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(4, 3, 4, 3)
        layout.setSpacing(6)

        # Colour swatch — keeps each row visually anchored to its
        # destination even when scrolling has hidden the group header.
        swatch = QFrame()
        swatch.setFixedSize(14, 14)
        swatch.setStyleSheet(
            f"background-color: {color}; "
            f"border-radius: 2px; "
            f"border: 1px solid #00000044;"
        )
        layout.addWidget(swatch)

        # Pallet size + commodity is the primary identifier; the
        # contract number is rendered muted next to it.
        size = self._pallet_size_for(row)
        size_text = f"{size} SCU" if size is not None else "? SCU"
        info = QLabel(
            f"<b>{size_text}</b> {row['commodity_name']}  "
            f"<span style='color:#888'>#{row['contract_number']}</span>"
        )
        info.setWordWrap(True)
        layout.addWidget(info, 1)

        pick_btn = QPushButton("→ Pick up")
        pick_btn.setProperty("flat", True)
        pick_btn.setToolTip(
            "Pick this pallet up to drag-and-drop it back into a zone."
        )
        cl_id = int(row["cargo_line_id"])
        p_idx = int(row["pallet_index"])
        pick_btn.clicked.connect(
            lambda _=False, c=cl_id, p=p_idx: self.pallet_picked.emit(c, p)
        )
        layout.addWidget(pick_btn)
        return frame

    def _pallet_size_for(self, row) -> int | None:
        """Resolve the pallet's SCU size by re-palletizing the cargo line.

        The holding row doesn't carry per-pallet sizes, so we derive
        them from the cargo_line's scu + max_pallet_size by re-running
        the deterministic palletize() function used everywhere else.
        """
        try:
            from ...planner.palletizer import palletize
        except ImportError:
            return None
        pallets = palletize(
            int(row["cargo_line_scu"]),
            int(row["max_pallet_size"]),
        )
        idx = int(row["pallet_index"])
        if 0 <= idx < len(pallets):
            return pallets[idx]
        return None

    # ── slots ───────────────────────────────────────────────────────

    def _on_auto_place(self) -> None:
        failures = self.controller.auto_place_holding_pallets()
        n_fail = len(failures)
        # Refresh before showing the summary so the post-placement
        # state is visible behind the modal popup.
        self.refresh()
        if n_fail:
            QMessageBox.information(
                self,
                "Auto-place finished",
                f"Couldn't place {n_fail} pallet(s); "
                f"they're still in holding.",
            )
        else:
            QMessageBox.information(
                self,
                "Auto-place finished",
                "All holding pallets were placed successfully.",
            )
