"""ContractsPanel — left pane listing active contracts."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QVBoxLayout, QWidget,
)

from ..widgets.contract_card import ContractCard


class ContractsPanel(QWidget):
    add_requested      = Signal()
    edit_requested     = Signal(int)   # contract_number
    remove_requested   = Signal(int)
    contract_selected  = Signal(int)

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.setMinimumWidth(280)
        self.setMaximumWidth(360)

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)

        # Header
        header = QHBoxLayout()
        title = QLabel("CONTRACTS")
        title.setProperty("heading", True)
        header.addWidget(title)
        header.addStretch(1)
        add_btn = QPushButton("+ Add")
        add_btn.clicked.connect(self.add_requested.emit)
        header.addWidget(add_btn)
        root.addLayout(header)

        # Scroll area with the cards
        self.list_widget = QWidget()
        self.list_layout = QVBoxLayout(self.list_widget)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.setSpacing(6)
        self.list_layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.list_widget)
        root.addWidget(scroll, 1)

        # SCU summary footer
        self.scu_label = QLabel("0 / 696 SCU")
        self.scu_label.setProperty("muted", True)
        root.addWidget(self.scu_label)

        # initial paint
        self.refresh()

    def refresh(self) -> None:
        """Rebuild the contract cards from the controller's current state."""
        # Clear existing cards (keep the trailing stretch)
        for i in reversed(range(self.list_layout.count() - 1)):
            item = self.list_layout.itemAt(i)
            if item and item.widget():
                item.widget().setParent(None)

        contracts = self.controller.list_contracts()

        # Build conflict set
        conflict_cl_ids: set[int] = set()
        result = self.controller.get_last_result()
        if result:
            for grp in result.conflict_groups:
                conflict_cl_ids.update(grp.cargo_line_ids)

        for c in contracts:
            has_conflict = any(d["id"] in conflict_cl_ids for d in c["deliveries"])
            card = ContractCard(c, has_conflict=has_conflict)
            card.edit_requested.connect(self.edit_requested.emit)
            card.remove_requested.connect(self.remove_requested.emit)
            card.selected.connect(self.contract_selected.emit)
            # Insert before the trailing stretch
            self.list_layout.insertWidget(self.list_layout.count() - 1, card)

        # Update SCU summary
        used = self.controller.total_scu_in_use()
        total = self.controller.total_scu_capacity()
        self.scu_label.setText(f"{used} / {total} SCU")
