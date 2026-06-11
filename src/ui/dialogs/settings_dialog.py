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
        tabs.addTab(self._capture_tab(), "Capture")
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

    def _capture_tab(self) -> QWidget:
        """Source picker + Quick Capture global hotkey."""
        import sys

        from ..dialogs.screen_capture import (
            _capture_libs_available, _list_sources, source_to_settings_value,
        )

        w = QWidget()
        f = QFormLayout(w)

        # Lib status banner — surfaces the exact missing package(s) and
        # the active Python so the user can pip-install into the right
        # interpreter. mss is required; pygetwindow gates window
        # capture; keyboard gates the global hotkey.
        def _probe(modname: str) -> bool:
            try:
                __import__(modname)
                return True
            except Exception:                               # noqa: BLE001
                return False

        self._lib_status = {
            "mss":         _probe("mss"),
            "pygetwindow": _probe("pygetwindow"),
            "keyboard":    _probe("keyboard"),
        }
        missing = [n for n, ok in self._lib_status.items() if not ok]
        status_lines = [
            ("✅" if ok else "⛔") + f"  {n}"
            for n, ok in self._lib_status.items()
        ]
        status_text = "Capture libraries:   " + "    ".join(status_lines)
        if missing:
            if getattr(sys, "frozen", False):
                # Frozen .exe — pip-installing into the embedded Python
                # isn't possible. The bundle should have shipped these
                # already; the .exe needs to be rebuilt.
                status_text += (
                    f"\n\nMissing in this build: {', '.join(missing)}.\n"
                    f"This is a packaging bug — rebuild the .exe from "
                    f"the current source (the spec lists every "
                    f"required package). After installing the new "
                    f"build, reopen Settings."
                )
            else:
                status_text += (
                    f"\n\nInstall the missing one(s) into THIS Python:\n"
                    f"  {sys.executable} -m pip install {' '.join(missing)}\n\n"
                    f"Then close and reopen Settings to refresh."
                )
        status = QLabel(status_text)
        status.setStyleSheet(
            "color: #ff8a3c;" if missing else "color: #9fd8ec;"
        )
        status.setWordWrap(True)
        status.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        f.addRow("", status)

        # Saved source — re-enumerated each time the dialog opens so a
        # newly-launched Star Citizen window shows up immediately.
        source_row = QHBoxLayout()
        self._capture_source_combo = QComboBox()
        self._capture_sources: list[dict] = []
        self._enum_diag: dict = {}
        if self._lib_status["mss"]:
            try:
                self._capture_sources = _list_sources(self._enum_diag)
            except Exception as exc:                        # noqa: BLE001
                self._capture_source_combo.setEnabled(False)
                self._capture_source_combo.addItem(
                    f"(could not enumerate sources: {exc})",
                )
            for src in self._capture_sources:
                self._capture_source_combo.addItem(
                    src["label"], userData=src,
                )
        else:
            self._capture_source_combo.setEnabled(False)
            self._capture_source_combo.addItem("(install mss first)")
        source_row.addWidget(self._capture_source_combo, 1)
        refresh_btn = QPushButton("↻ Refresh")
        refresh_btn.setProperty("flat", True)
        refresh_btn.setToolTip(
            "Re-scan for monitors and windows. Use after launching "
            "Star Citizen so the SC window appears in the list."
        )
        refresh_btn.clicked.connect(self._refresh_capture_sources)
        source_row.addWidget(refresh_btn)
        source_w = QWidget()
        source_w.setLayout(source_row)
        f.addRow("Screen capture source", source_w)

        # Enumeration diagnostics — say exactly what was found so the
        # user can tell "did Refresh do anything?" at a glance.
        self._enum_label = QLabel(self._format_enum_diag())
        self._enum_label.setProperty("muted", True)
        self._enum_label.setWordWrap(True)
        self._enum_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        f.addRow("", self._enum_label)

        # Restore the saved selection (if it still exists in this run).
        saved = self.settings.get("screen_capture_source") or {}
        if saved and self._capture_sources:
            self._select_saved_source(saved)

        if (self._lib_status["mss"]
                and not self._lib_status["pygetwindow"]):
            hint = QLabel(
                "⚠ Only monitor sources are listed because pygetwindow "
                "isn't available — install it (see above) to capture a "
                "specific window like Star Citizen."
            )
            hint.setStyleSheet("color: #ffb060;")
            hint.setWordWrap(True)
            f.addRow("", hint)

        # Persist the chosen source even without the user hitting Save —
        # the to-settings shape strips volatile fields (window rects).
        self._source_to_settings = source_to_settings_value

        # Global hotkey: typed manually OR captured by pressing keys
        # while the line edit has focus.
        hotkey_row = QHBoxLayout()
        self._hotkey_edit = QLineEdit(
            self.settings.get("hotkey_quick_capture") or ""
        )
        self._hotkey_edit.setPlaceholderText(
            "e.g. ctrl+shift+c   (leave blank to disable)"
        )
        hotkey_row.addWidget(self._hotkey_edit, 1)
        capture_btn = QPushButton("Press a combo…")
        capture_btn.setEnabled(self._lib_status["keyboard"])
        capture_btn.clicked.connect(self._capture_hotkey_combo)
        hotkey_row.addWidget(capture_btn)
        clear_btn = QPushButton("Clear")
        clear_btn.setProperty("flat", True)
        clear_btn.clicked.connect(lambda: self._hotkey_edit.setText(""))
        hotkey_row.addWidget(clear_btn)
        hotkey_w = QWidget()
        hotkey_w.setLayout(hotkey_row)
        f.addRow("Quick Capture hotkey", hotkey_w)

        # Live bind status — the hotkey is registered at app start AND
        # whenever the user saves Settings; this label reports which
        # combo (if any) is currently armed in the OS keyboard hook.
        hk = getattr(self.controller, "_quick_capture_hotkey", None)
        bound = hk.combo() if hk is not None else ""
        if bound:
            bind_text = f"✅ Hotkey active: '{bound}' (rebinds on Save)"
            bind_color = "#9fd8ec"
        else:
            bind_text = (
                "⛔ No hotkey is currently armed. Enter a combo above "
                "and click Save."
            )
            bind_color = "#ff8a3c"
        bind_status = QLabel(bind_text)
        bind_status.setStyleSheet(f"color: {bind_color};")
        bind_status.setWordWrap(True)
        f.addRow("", bind_status)

        # "Test capture now" — bypasses the hotkey and runs the same
        # grab + queue path so the user can confirm the pipeline works
        # without alt-tabbing into SC and back.
        test_btn = QPushButton("Test capture now")
        test_btn.setToolTip(
            "Grabs the saved source immediately and queues it, the "
            "exact path the global hotkey takes."
        )
        test_btn.clicked.connect(self._test_quick_capture)
        f.addRow("", test_btn)

        hint = QLabel(
            "While in Star Citizen, press the hotkey to silently snap "
            "the saved source — captures stack in the 📸 badge at the "
            "top of the main window, and a status bar message confirms "
            "each successful snap."
        )
        hint.setProperty("muted", True)
        hint.setWordWrap(True)
        f.addRow("", hint)

        return w

    def _test_quick_capture(self) -> None:
        """Run the same grab-and-queue path the global hotkey uses,
        so the user can verify the setup without alt-tabbing."""
        from PySide6.QtWidgets import QMessageBox
        from ..dialogs.screen_capture import grab_source

        saved = self.settings.get("screen_capture_source") or {}
        if not saved:
            QMessageBox.information(
                self, "No source saved",
                "Pick a source from the dropdown above and click Save "
                "first. Then re-open Settings and Test again.",
            )
            return
        img, reason = grab_source(saved, return_reason=True)
        if img is None or img.isNull():
            QMessageBox.warning(self, "Capture failed", reason)
            return
        label = saved.get("label", "")
        self.controller.capture_queue.push(img, source_label=label)
        body = (
            f"Captured '{label}' ({img.width()}×{img.height()}). "
            f"Queue depth: {len(self.controller.capture_queue)}."
        )
        if reason:
            # grab_source surfaces a reason on partial-success too —
            # e.g. "fell back to Monitor 1 because the window wouldn't
            # grab directly". Show it so the user knows.
            body += f"\n\nNote: {reason}"
        QMessageBox.information(self, "Test capture queued", body)

    def _refresh_capture_sources(self) -> None:
        from ..dialogs.screen_capture import _list_sources
        prev_data = self._capture_source_combo.currentData()
        self._capture_source_combo.clear()
        self._enum_diag = {}
        try:
            self._capture_sources = _list_sources(self._enum_diag)
        except Exception as exc:                            # noqa: BLE001
            self._capture_source_combo.setEnabled(False)
            self._capture_source_combo.addItem(
                f"(could not enumerate sources: {exc})",
            )
            self._enum_label.setText(f"Enumeration failed: {exc}")
            return
        self._capture_source_combo.setEnabled(True)
        for src in self._capture_sources:
            self._capture_source_combo.addItem(src["label"], userData=src)
        # Try to keep the previously-selected source highlighted.
        if isinstance(prev_data, dict):
            self._select_saved_source(self._source_to_settings(prev_data))
        self._enum_label.setText(self._format_enum_diag())

    def _format_enum_diag(self) -> str:
        """Human-readable summary of the last enumeration so the user
        can see whether the Refresh button picked anything new up."""
        d = self._enum_diag
        if not d:
            return ""
        parts = [f"Found {d.get('monitors', 0)} monitor(s)"]
        if d.get("pygetwindow_error"):
            parts.append(
                f"⛔ pygetwindow: {d['pygetwindow_error']}"
            )
        else:
            parts.append(
                f"{d.get('sized_windows', 0)} window(s) "
                f"({d.get('titled_windows', 0)} titled, "
                f"{d.get('raw_windows', 0)} total raw handles)"
            )
        suffix = ""
        if (d.get("raw_windows", 0) > 0
                and d.get("sized_windows", 0) == 0):
            suffix = (
                "\nNo windows passed the size filter. If Star Citizen "
                "is running in fullscreen exclusive mode, capture the "
                "monitor instead — the OS doesn't expose SC as a "
                "regular window in that mode. Switch SC to "
                "Borderless / Fullscreen Windowed to grab it by name."
            )
        return "Enumerated: " + " · ".join(parts) + suffix

    def _select_saved_source(self, saved: dict) -> None:
        wanted_kind = saved.get("kind")
        wanted_label = (saved.get("label") or "").lower()
        wanted_title = (saved.get("title") or wanted_label).lower()
        # Exact match — see screen_capture.resolve_saved_source for the
        # "Star Citizen" / "Star Citizen Cargo Manager" footgun this
        # avoids. Returns silently if the saved window is no longer
        # there so the combo keeps its current selection rather than
        # snapping to whichever same-prefix window was first.
        for i in range(self._capture_source_combo.count()):
            src = self._capture_source_combo.itemData(i)
            if not src or src.get("kind") != wanted_kind:
                continue
            label = (src.get("label") or "").lower()
            if wanted_kind == "monitor" and label == wanted_label:
                self._capture_source_combo.setCurrentIndex(i)
                return
            if wanted_kind == "window" and label == wanted_title:
                self._capture_source_combo.setCurrentIndex(i)
                return

    def _capture_hotkey_combo(self) -> None:
        """Listen for one keypress and store its combo string."""
        import sys
        from PySide6.QtWidgets import QMessageBox
        try:
            import keyboard
        except Exception as exc:                            # noqa: BLE001
            if getattr(sys, "frozen", False):
                body = (
                    "The `keyboard` package isn't in this build of the "
                    "app.\n\nRebuild the .exe from the current source "
                    "— the spec already lists it as a required "
                    "package. After installing the new build, reopen "
                    f"Settings.\n\nImport error: {exc}"
                )
            else:
                body = (
                    "The `keyboard` package is needed to bind a "
                    "global hotkey.\n\n"
                    f"Install it into THIS Python:\n  "
                    f"{sys.executable} -m pip install keyboard\n\n"
                    f"Then close and reopen Settings.\n\n"
                    f"Import error: {exc}"
                )
            QMessageBox.warning(
                self, "Keyboard library not installed", body,
            )
            return
        self._hotkey_edit.setPlaceholderText("Press the keys now…")
        self._hotkey_edit.setText("")
        try:
            # read_hotkey returns the normalised combo string for the
            # next key combination, blocking the dialog for a moment
            # — fine because the user invited it by clicking.
            combo = keyboard.read_hotkey(suppress=False)
        except Exception as exc:                            # noqa: BLE001
            self._hotkey_edit.setPlaceholderText(
                f"(capture failed: {exc})"
            )
            return
        self._hotkey_edit.setText(combo)

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

        # Capture tab — saved source + Quick Capture hotkey.
        src = self._capture_source_combo.currentData()
        if isinstance(src, dict):
            self.settings.set(
                "screen_capture_source", self._source_to_settings(src),
            )
        new_combo = self._hotkey_edit.text().strip()
        old_combo = self.settings.get("hotkey_quick_capture") or ""
        self.settings.set("hotkey_quick_capture", new_combo)
        # Rebind the live hotkey if the main window registered one.
        if new_combo != old_combo:
            hk = getattr(self.controller, "_quick_capture_hotkey", None)
            if hk is not None:
                hk.set_combo(new_combo)

        self.accept()
