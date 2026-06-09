"""
ForcePushDialog — confirms displacement of pallets during 3D drag-drop.

When the user drops a pallet onto a cube that's already occupied by one
or more other pallets, the BayCanvas asks the controller to identify
which pallets would have to move out of the way. Those candidates are
shown here with per-row "Keep" checkboxes so the user can refuse to
displace specific pallets (which aborts the drop).

After a successful accept(), ``confirmed_displacements`` is the subset
of displaced_pallets the user agreed to push to the holding table.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFrame, QHBoxLayout, QLabel,
    QScrollArea, QVBoxLayout, QWidget,
)


class ForcePushDialog(QDialog):
    """Confirm displacement of pallets before a force-push lock.

    Shows the incoming pallet and the list of pallets that would be
    pushed to the holding table to make room for it. Each displaced
    pallet has a "Keep" checkbox; unchecking one means that pallet
    must stay where it is — accepting the dialog in that state will
    still proceed, but the kept pallet is excluded from
    ``confirmed_displacements`` so the caller can decide whether to
    abort the drop.
    """

    def __init__(
        self,
        controller,
        *,
        incoming_pallet: dict,
        displaced_pallets: list[dict],
        parent=None,
    ):
        super().__init__(parent)
        self.controller = controller
        self.incoming_pallet = incoming_pallet
        self.displaced_pallets = list(displaced_pallets)
        # Populated on accept().
        self.confirmed_displacements: list[tuple[int, int]] = []
        # Per-row "Displace" checkboxes — UI default is checked (the
        # pallet WILL be pushed). We expose them as _checkboxes for
        # the test suite to introspect.
        self._checkboxes: list[tuple[QCheckBox, dict]] = []

        n = len(self.displaced_pallets)
        self.setWindowTitle("Push pallets to holding?")
        self.setMinimumWidth(440)

        root = QVBoxLayout(self)

        # ── Header ──────────────────────────────────────────────────
        header = QLabel(
            f"Push <b>{n}</b> pallet(s) to holding to fit this drop?"
        )
        header.setProperty("heading", True)
        header.setWordWrap(True)
        root.addWidget(header)

        if incoming_pallet:
            incoming = QLabel(
                f"Incoming: <b>{incoming_pallet.get('size', '?')} SCU</b> → "
                f"{incoming_pallet.get('destination', '(unknown)')}"
            )
            incoming.setProperty("muted", True)
            incoming.setWordWrap(True)
            root.addWidget(incoming)

        # ── Displaced-pallets list ──────────────────────────────────
        body_label = QLabel("These pallets will be moved to holding:")
        body_label.setProperty("muted", True)
        root.addWidget(body_label)

        list_widget = QWidget()
        list_layout = QVBoxLayout(list_widget)
        list_layout.setContentsMargins(0, 0, 0, 0)
        list_layout.setSpacing(4)

        for pallet in self.displaced_pallets:
            list_layout.addWidget(self._build_row(pallet))
        list_layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        scroll.setMinimumHeight(140)
        scroll.setWidget(list_widget)
        root.addWidget(scroll, 1)

        hint = QLabel(
            "Uncheck a pallet to keep it where it is. If you keep "
            "anything in the drop zone the drop won't fit — the caller "
            "should treat that as a cancel."
        )
        hint.setProperty("muted", True)
        hint.setWordWrap(True)
        root.addWidget(hint)

        # ── Buttons ────────────────────────────────────────────────
        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        bb.button(QDialogButtonBox.StandardButton.Ok).setText(
            "Push to holding"
        )
        bb.accepted.connect(self._on_accept)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)

    # ── row builder ─────────────────────────────────────────────────

    def _build_row(self, pallet: dict) -> QFrame:
        frame = QFrame()
        frame.setObjectName("card")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(6)

        check = QCheckBox()
        check.setChecked(True)
        check.setToolTip(
            "When checked, this pallet will move to holding. "
            "Uncheck to keep it where it is."
        )
        layout.addWidget(check)

        size = pallet.get("size", "?")
        destination = pallet.get("destination", "(unknown)")
        commodity = pallet.get("commodity", "")
        contract_number = pallet.get("contract_number")

        info_bits = [f"<b>{size} SCU</b>"]
        if commodity:
            info_bits.append(commodity)
        info_bits.append(f"→ {destination}")
        if contract_number is not None:
            info_bits.append(
                f"<span style='color:#888'>#{contract_number}</span>"
            )
        label = QLabel("  ".join(info_bits))
        label.setWordWrap(True)
        layout.addWidget(label, 1)

        self._checkboxes.append((check, pallet))
        return frame

    # ── accept ──────────────────────────────────────────────────────

    def _on_accept(self) -> None:
        confirmed: list[tuple[int, int]] = []
        for check, pallet in self._checkboxes:
            if check.isChecked():
                confirmed.append(
                    (
                        int(pallet["cargo_line_id"]),
                        int(pallet["pallet_index"]),
                    )
                )
        self.confirmed_displacements = confirmed
        self.accept()
