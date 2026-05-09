"""Custom status bar — mic state, stop progress, SCU usage, API health."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QWidget


class StatusBarWidget(QWidget):
    settings_clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 2, 8, 2)
        layout.setSpacing(8)

        self.mic = QLabel("🎤 off")
        layout.addWidget(self.mic)
        layout.addWidget(self._sep())

        self.stop = QLabel("Stop —")
        layout.addWidget(self.stop)
        layout.addWidget(self._sep())

        self.scu = QLabel("0 / 696 SCU")
        layout.addWidget(self.scu)

        layout.addStretch(1)

        self.api = QLabel("●")
        self.api.setToolTip("OpenAI reachable")
        self.api.setStyleSheet("color: #888888;")
        layout.addWidget(self.api)

        gear = QPushButton("⚙")
        gear.setProperty("flat", True)
        gear.clicked.connect(self.settings_clicked.emit)
        layout.addWidget(gear)

    def _sep(self) -> QFrame:
        f = QFrame()
        f.setFrameShape(QFrame.Shape.VLine)
        f.setStyleSheet("color: #3a4894;")
        return f

    def set_mic(self, state: str) -> None:
        labels = {
            "listening": "🎤 listening",
            "muted":     "🎤 muted",
            "cancelled": "🎤 cancelled",
            "off":       "🎤 off",
        }
        self.mic.setText(labels.get(state, f"🎤 {state}"))

    def set_progress(self, current: int, total: int) -> None:
        if total:
            self.stop.setText(f"Stop {current} of {total}")
        else:
            self.stop.setText("Stop —")

    def set_scu(self, used: int, total: int) -> None:
        self.scu.setText(f"{used} / {total} SCU")

    def set_api_health(self, ok: bool) -> None:
        self.api.setStyleSheet(f"color: {'#7bbf3f' if ok else '#e74c3c'};")
        self.api.setToolTip("OpenAI reachable" if ok else "OpenAI unreachable")
