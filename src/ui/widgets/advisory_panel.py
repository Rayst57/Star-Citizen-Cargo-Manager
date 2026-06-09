"""
AdvisoryPanel — surfaces planner advisories for the selected stop.

The panel is a thin display layer over ``controller.compute_advisories()``.
It refreshes automatically whenever a recompute completes (subscribe to
``controller.recompute_done``) and is told which stop to display via
``set_stop(stop_number)`` from whoever owns the stop-selector.

Each advisory renders as a card showing:
    [severity icon]  Summary headline
                     Detail body (wrapped)
                     Zones: [F1] [R2] …
                     Lines: [cl#12] [cl#15] …
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QScrollArea, QVBoxLayout, QWidget,
)


_SEVERITY_ICON = {
    "info":  ("ℹ", "#5fb3ff"),
    "warn":  ("⚠", "#ffb347"),
    "error": ("⛔", "#ff5a5a"),
}


class AdvisoryPanel(QWidget):
    """A scrollable list of advisories for the currently selected stop.

    Lifecycle:
        1. Caller instantiates AdvisoryPanel(controller).
        2. Caller wires controller.recompute_done -> panel.refresh().
        3. Caller calls panel.set_stop(stop_number) whenever the user
           picks a different stop.
    """

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self._stop_number: int | None = None
        # Cache the last advisories dict so set_advisories() bypasses
        # controller (used by tests that bypass the real planner).
        self._injected_advisories: dict | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        self.header = QLabel("Advisories")
        self.header.setProperty("heading", True)
        self.header.setWordWrap(True)
        root.addWidget(self.header)

        # Scrollable advisory list
        self._list_widget = QWidget()
        self._list_layout = QVBoxLayout(self._list_widget)
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._list_layout.setSpacing(6)
        self._list_layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff,
        )
        scroll.setWidget(self._list_widget)
        root.addWidget(scroll, 1)

        self._empty_label = QLabel("No advisories — plan looks clean.")
        self._empty_label.setProperty("muted", True)
        self._empty_label.setWordWrap(True)
        root.addWidget(self._empty_label)

        # Subscribe to recompute_done. We use a lambda so that the
        # signal's payload (the RecomputeResult) doesn't get passed
        # through to refresh() as a positional arg.
        try:
            controller.recompute_done.connect(lambda _r: self.refresh())
        except AttributeError:
            # Allow tests / contexts where controller has no Qt signals.
            pass

        self.refresh()

    # ── public API ───────────────────────────────────────────────────────

    def set_stop(self, stop_number: int | None) -> None:
        """Display advisories for *stop_number* (None hides them)."""
        if stop_number == self._stop_number:
            return
        self._stop_number = stop_number
        self.refresh()

    def set_advisories(self, advisories: dict) -> None:
        """Inject an advisory dict directly, bypassing the controller.

        Used by tests so we can hand the panel synthetic Advisory data
        without spinning up a full planner run.
        """
        self._injected_advisories = advisories
        self.refresh()

    def refresh(self) -> None:
        """Rebuild the advisory cards for the current stop."""
        # Clear existing cards (but keep the trailing stretch).
        while self._list_layout.count() > 1:
            item = self._list_layout.takeAt(0)
            if item is None:
                continue
            w = item.widget()
            if w is not None:
                w.hide()
                w.deleteLater()

        # Resolve advisories source.
        if self._injected_advisories is not None:
            all_advisories = self._injected_advisories
        else:
            try:
                all_advisories = self.controller.compute_advisories()
            except Exception:
                all_advisories = {}

        # Resolve the stop being displayed.
        stop_num = self._stop_number
        station_name = ""
        result = (
            self.controller.get_last_result()
            if hasattr(self.controller, "get_last_result") else None
        )
        if stop_num is not None and result is not None:
            for s in result.route_stops:
                if s.stop_number == stop_num:
                    station_name = s.station_name
                    break

        if stop_num is None:
            self.header.setText("Advisories")
        else:
            label = f"Stop {stop_num}"
            if station_name:
                label += f": {station_name}"
            self.header.setText(f"Advisories for {label}")

        advisories = (
            all_advisories.get(stop_num, []) if stop_num is not None else []
        )

        if not advisories:
            self._empty_label.show()
            return
        self._empty_label.hide()

        for adv in advisories:
            card = _AdvisoryCard(adv)
            self._list_layout.insertWidget(self._list_layout.count() - 1, card)


class _AdvisoryCard(QFrame):
    """One advisory rendered as a card with severity icon + detail."""

    def __init__(self, advisory, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        # Map severity → property so QSS can style it.
        self.setProperty("severity", advisory.severity)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(4)

        icon, color = _SEVERITY_ICON.get(advisory.severity, ("•", "#888"))

        # Header row: severity icon + summary
        header_row = QHBoxLayout()
        header_row.setSpacing(6)
        icon_lbl = QLabel(icon)
        icon_lbl.setStyleSheet(f"color: {color}; font-weight: bold;")
        header_row.addWidget(icon_lbl)
        summary = QLabel(advisory.summary)
        summary.setWordWrap(True)
        summary.setStyleSheet("font-weight: bold;")
        header_row.addWidget(summary, 1)
        layout.addLayout(header_row)

        # Detail body
        if advisory.detail:
            body = QLabel(advisory.detail)
            body.setWordWrap(True)
            body.setProperty("muted", True)
            body.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse,
            )
            layout.addWidget(body)

        # Affected zones
        if advisory.affected_zones:
            zones_row = QHBoxLayout()
            zones_row.setSpacing(4)
            zones_row.addWidget(QLabel("Affected zones:"))
            for z in advisory.affected_zones:
                badge = QLabel(z)
                badge.setStyleSheet(
                    "border: 1px solid #5be4ff; border-radius: 3px; "
                    "padding: 0 4px; color: #5be4ff;"
                )
                zones_row.addWidget(badge)
            zones_row.addStretch(1)
            layout.addLayout(zones_row)

        # Affected cargo lines
        if advisory.affected_cargo_lines:
            cls_row = QHBoxLayout()
            cls_row.setSpacing(4)
            cls_row.addWidget(QLabel("Affected cargo lines:"))
            for cl in advisory.affected_cargo_lines:
                badge = QLabel(f"cl#{cl}")
                badge.setStyleSheet(
                    "border: 1px solid #ff8a3c; border-radius: 3px; "
                    "padding: 0 4px; color: #ff8a3c;"
                )
                cls_row.addWidget(badge)
            cls_row.addStretch(1)
            layout.addLayout(cls_row)
