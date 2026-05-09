"""ContractCard — single contract row in ContractsPanel."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
)


class ContractCard(QFrame):
    edit_requested   = Signal(int)   # contract_number
    remove_requested = Signal(int)
    selected         = Signal(int)

    def __init__(self, contract: dict, *, has_conflict: bool = False, parent=None):
        super().__init__(parent)
        self.contract = contract
        self.contract_number = contract["contract_number"]

        self.setObjectName("card")
        self.setProperty("conflict", has_conflict)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)

        info = QVBoxLayout()
        deliveries = contract["deliveries"]
        dest_summary = " + ".join(
            sorted({d["delivery_name"] for d in deliveries})
        )
        title = QLabel(
            f"#{contract['contract_number']}  {contract['pickup_name']} → {dest_summary}"
        )
        title.setProperty("heading", False)
        info.addWidget(title)

        commodity_lines = []
        for d in deliveries:
            commodity_lines.append(
                f"{d['scu_amount']} SCU {d['commodity_name']} → {d['delivery_name']}"
            )
        muted = QLabel("\n".join(commodity_lines) +
                       f"\nmax {contract['max_pallet_size']} SCU pallet")
        muted.setProperty("muted", True)
        info.addWidget(muted)

        layout.addLayout(info, 1)

        btns = QVBoxLayout()
        edit = QPushButton("✎")
        edit.setProperty("flat", True)
        edit.setToolTip("Edit contract")
        edit.clicked.connect(lambda: self.edit_requested.emit(self.contract_number))
        remove = QPushButton("✕")
        remove.setProperty("flat", True)
        remove.setToolTip("Remove contract")
        remove.clicked.connect(lambda: self.remove_requested.emit(self.contract_number))
        btns.addWidget(edit)
        btns.addWidget(remove)
        btns.addStretch(1)
        layout.addLayout(btns)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        super().mousePressEvent(event)
        self.selected.emit(self.contract_number)
