"""
SettingsDialog — tabbed configuration for STT, voice, mic, OpenAI, appearance.

For v1 the bind capture / device probing is stubbed; the dialog persists
chosen values to AppSettings and the OpenAI key to the OS keyring.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QSlider, QTabWidget, QVBoxLayout, QWidget,
)
from PySide6.QtCore import Qt

from ...settings import get_api_key, set_api_key, clear_api_key


class SettingsDialog(QDialog):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.settings = controller.settings
        self.setWindowTitle("Settings")
        self.setMinimumWidth(520)

        root = QVBoxLayout(self)

        tabs = QTabWidget()
        tabs.addTab(self._voice_tab(), "Voice Input")
        tabs.addTab(self._mic_tab(), "Microphone")
        tabs.addTab(self._wake_tab(), "Wake Word")
        tabs.addTab(self._openai_tab(), "OpenAI")
        tabs.addTab(self._appearance_tab(), "Appearance")
        root.addWidget(tabs)

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save |
            QDialogButtonBox.StandardButton.Cancel
        )
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)

    # ── tabs ────────────────────────────────────────────────────────────

    def _voice_tab(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)

        self.listening_mode = QComboBox()
        self.listening_mode.addItems(["wake_word", "ptt", "ptt_toggle", "hybrid", "off"])
        self.listening_mode.setCurrentText(self.settings.get("listening_mode"))
        f.addRow("Listening mode", self.listening_mode)

        self.stt_engine = QComboBox()
        self.stt_engine.addItems(["openai", "windows"])
        self.stt_engine.setCurrentText(self.settings.get("stt_engine"))
        f.addRow("STT engine", self.stt_engine)

        ptt_row = QHBoxLayout()
        self.ptt_label = QLabel("(none bound)")
        ptt_row.addWidget(self.ptt_label, 1)
        bind_btn = QPushButton("Press input to bind…")
        bind_btn.clicked.connect(self._bind_ptt_stub)
        ptt_row.addWidget(bind_btn)
        ptt_w = QWidget()
        ptt_w.setLayout(ptt_row)
        f.addRow("PTT bind", ptt_w)

        return w

    def _mic_tab(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)

        self.input_device = QComboBox()
        self.input_device.addItem("(default)", userData="")
        for name in self._list_input_devices():
            self.input_device.addItem(name, userData=name)
        self.input_device.setCurrentText(self.settings.get("input_device") or "(default)")
        f.addRow("Input device", self.input_device)

        self.input_gain = QDoubleSpinBox()
        self.input_gain.setRange(-30.0, 30.0)
        self.input_gain.setSingleStep(0.5)
        self.input_gain.setSuffix(" dB")
        self.input_gain.setValue(float(self.settings.get("input_gain")))
        f.addRow("Input gain", self.input_gain)

        self.noise_suppression = QCheckBox("Enable noise suppression")
        self.noise_suppression.setChecked(bool(self.settings.get("noise_suppression")))
        f.addRow("", self.noise_suppression)

        self.activation_threshold = QDoubleSpinBox()
        self.activation_threshold.setRange(-90.0, 0.0)
        self.activation_threshold.setSingleStep(1.0)
        self.activation_threshold.setSuffix(" dBFS")
        self.activation_threshold.setValue(float(self.settings.get("activation_threshold")))
        f.addRow("Activation threshold", self.activation_threshold)

        return w

    def _wake_tab(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)

        self.sensitivity = QSlider(Qt.Orientation.Horizontal)
        self.sensitivity.setRange(0, 100)
        self.sensitivity.setValue(int(float(self.settings.get("wake_sensitivity")) * 100))
        f.addRow("Sensitivity", self.sensitivity)

        f.addRow(QLabel("Custom wake phrase: requires Picovoice Console — see docs."))
        return w

    def _openai_tab(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)

        self.api_key_edit = QLineEdit()
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        existing = get_api_key()
        if existing:
            self.api_key_edit.setPlaceholderText("(saved in Credential Manager — leave blank to keep)")
        f.addRow("API key", self.api_key_edit)

        clear_btn = QPushButton("Clear stored key")
        clear_btn.setProperty("flat", True)
        clear_btn.clicked.connect(lambda: clear_api_key())
        f.addRow("", clear_btn)

        self.model_combo = QComboBox()
        self.model_combo.addItems(["gpt-4o", "gpt-4o-mini", "gpt-4.1"])
        self.model_combo.setCurrentText(self.settings.get("model"))
        f.addRow("Model", self.model_combo)

        self.realtime = QCheckBox("Use Realtime API")
        self.realtime.setChecked(bool(self.settings.get("use_realtime_api")))
        f.addRow("", self.realtime)

        return w

    def _appearance_tab(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)

        self.opacity = QDoubleSpinBox()
        self.opacity.setRange(0.3, 1.0)
        self.opacity.setSingleStep(0.05)
        self.opacity.setValue(float(self.settings.get("window_opacity")))
        f.addRow("Window opacity", self.opacity)

        self.always_on_top = QCheckBox("Always on top")
        self.always_on_top.setChecked(bool(self.settings.get("always_on_top")))
        f.addRow("", self.always_on_top)

        # Planner fallback: switches the zone planner back to the
        # legacy strict-exclusion / consolidation logic that was needed
        # before CIG fixed pallet-ID and locked-destination. Off by
        # default; flip on only if CIG ever regresses.
        self.strict_conflict_mode = QCheckBox(
            "Strict pallet conflict mode (legacy / pre-CIG-fix)"
        )
        self.strict_conflict_mode.setChecked(
            bool(self.settings.get("strict_pallet_conflict_mode"))
        )
        self.strict_conflict_mode.setToolTip(
            "Use the older planner that treats indistinguishable pallets "
            "from different contracts as a hard conflict — keep this OFF "
            "unless the in-game pallet-ID / locked-destination behavior "
            "has regressed."
        )
        f.addRow("", self.strict_conflict_mode)

        return w

    # ── helpers ────────────────────────────────────────────────────────

    def _bind_ptt_stub(self) -> None:
        # Real bind capture lives in voice/ptt_*.py — stubbed for v1 dialog
        self.ptt_label.setText("(capture coming in v1 voice subsystem)")

    def _list_input_devices(self) -> list[str]:
        try:
            import sounddevice as sd
            return [
                d["name"] for d in sd.query_devices()
                if d.get("max_input_channels", 0) > 0
            ]
        except Exception:
            return []

    def _save(self) -> None:
        self.settings.set("listening_mode", self.listening_mode.currentText())
        self.settings.set("stt_engine", self.stt_engine.currentText())

        self.settings.set("input_device", self.input_device.currentData() or "")
        self.settings.set("input_gain", self.input_gain.value())
        self.settings.set("noise_suppression", self.noise_suppression.isChecked())
        self.settings.set("activation_threshold", self.activation_threshold.value())

        self.settings.set("wake_sensitivity", self.sensitivity.value() / 100.0)

        new_key = self.api_key_edit.text().strip()
        if new_key:
            set_api_key(new_key)
            self.controller.api_key = new_key
        self.settings.set("model", self.model_combo.currentText())
        self.settings.set("use_realtime_api", self.realtime.isChecked())

        self.settings.set("window_opacity", self.opacity.value())
        self.settings.set("always_on_top", self.always_on_top.isChecked())
        self.settings.set(
            "strict_pallet_conflict_mode",
            self.strict_conflict_mode.isChecked(),
        )

        self.accept()
