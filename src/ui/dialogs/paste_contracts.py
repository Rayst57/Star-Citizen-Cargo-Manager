"""
PasteContractsDialog — paste free-form dictation, parse into contracts,
review the parsed result, then commit.

Workflow:
  1. User pastes (or types) text in the input area.
  2. Clicks "Parse" → spawns a worker thread that calls OpenAI.
  3. Parsed contracts appear as previewable rows with checkboxes; the
     user can deselect anything that looks wrong.
  4. "Apply selected" → controller.add_contract(...) for each kept row.

The dialog is read-only on app state until Apply is pressed — Parse
just shows what WOULD be added.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QFrame, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QSizePolicy, QTextEdit, QVBoxLayout, QWidget, QMessageBox,
)

from ...app_controller import AppController, ToolError
from ...voice.parse_contracts import parse_contracts_text


class _ParseThread(QThread):
    """Worker that runs the OpenAI call. Receives plain values only —
    no SQLite connection is touched here, since SQLite refuses
    connections opened on a different thread."""

    parsed = Signal(list)        # list[dict] of add_contract args
    failed = Signal(str)

    def __init__(
        self,
        api_key: str,
        model: str,
        stations: list[str],
        commodities: list[str],
        text: str,
    ):
        super().__init__()
        self.api_key = api_key
        self.model = model
        self.stations = stations
        self.commodities = commodities
        self.text = text

    def run(self) -> None:
        try:
            contracts = parse_contracts_text(
                self.api_key, self.model,
                self.stations, self.commodities,
                self.text,
            )
            self.parsed.emit(contracts)
        except Exception as e:
            self.failed.emit(f"{type(e).__name__}: {e}")


class PasteContractsDialog(QDialog):
    def __init__(self, controller: AppController, parent=None):
        super().__init__(parent)
        self.controller = controller
        self._thread: _ParseThread | None = None
        self._parsed: list[dict] = []
        self._row_checkboxes: list[QCheckBox] = []
        # PTT recorder is lazy-created on first mic press so the openai
        # / sounddevice imports stay out of cold-start time.
        self._ptt = None

        self.setWindowTitle("Paste & Parse Contracts")
        self.setMinimumSize(720, 580)

        root = QVBoxLayout(self)

        # Header
        header = QHBoxLayout()
        title = QLabel("Paste contract dictation")
        title.setProperty("heading", True)
        header.addWidget(title)
        header.addStretch(1)
        close_btn = QPushButton("✕")
        close_btn.setProperty("flat", True)
        close_btn.clicked.connect(self.reject)
        header.addWidget(close_btn)
        root.addLayout(header)

        hint = QLabel(
            "Dictate contracts however you like (Windows Voice Typing, "
            "Notepad, phone, etc.) and paste the text below. The parser "
            "matches station / commodity names against this workday's "
            "known list. One contract = one pickup + one or more "
            "deliveries; multiple pickups = multiple contracts."
        )
        hint.setProperty("muted", True)
        hint.setWordWrap(True)
        root.addWidget(hint)

        self.input = QTextEdit()
        self.input.setPlaceholderText(
            "Example:\n"
            "Pick up 55 SCU of Tungsten at Yellow Core for Everus Harbor, "
            "max 8.\nPick up 27 SCU of Tungsten at Yellow Core for Baijini "
            "Point, max 8.\nGrab 96 SCU of Aluminum at Shallow Fields for "
            "Port Tressler, max 32."
        )
        self.input.setMinimumHeight(140)
        root.addWidget(self.input, 1)

        # Parse row — Mic toggle for dictation, Parse button, status.
        parse_row = QHBoxLayout()
        self.mic_btn = QPushButton("🎤 PTT")
        self.mic_btn.setCheckable(True)
        self.mic_btn.setToolTip(
            "Push-to-talk: click once to start dictating, click again to "
            "stop. Each dictation event appends a new paragraph above. "
            "Plays the Windows Speech On / Speech Off cue on toggle."
        )
        self.mic_btn.toggled.connect(self._on_mic_toggled)
        parse_row.addWidget(self.mic_btn)
        self.parse_btn = QPushButton("Parse →")
        self.parse_btn.clicked.connect(self._on_parse_clicked)
        parse_row.addWidget(self.parse_btn)
        self.status_label = QLabel("")
        self.status_label.setProperty("muted", True)
        parse_row.addWidget(self.status_label, 1)
        root.addLayout(parse_row)

        # Preview area
        preview_label = QLabel("Parsed contracts (review before applying)")
        preview_label.setProperty("heading", True)
        root.addWidget(preview_label)

        self.preview_holder = QWidget()
        self.preview_layout = QVBoxLayout(self.preview_holder)
        self.preview_layout.setContentsMargins(0, 0, 0, 0)
        self.preview_layout.setSpacing(4)
        self.preview_layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(self.preview_holder)
        scroll.setMinimumHeight(180)
        root.addWidget(scroll, 2)

        # Footer buttons
        footer = QHBoxLayout()
        footer.addStretch(1)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setProperty("flat", True)
        self.cancel_btn.clicked.connect(self.reject)
        footer.addWidget(self.cancel_btn)
        self.apply_btn = QPushButton("Apply selected")
        self.apply_btn.clicked.connect(self._on_apply_clicked)
        self.apply_btn.setEnabled(False)
        footer.addWidget(self.apply_btn)
        root.addLayout(footer)

    # ── parse ─────────────────────────────────────────────────────────

    def _on_parse_clicked(self) -> None:
        text = self.input.toPlainText().strip()
        if not text:
            self.status_label.setText("Nothing to parse.")
            return
        if self._thread and self._thread.isRunning():
            return

        if not self.controller.api_key:
            self.status_label.setText(
                "OpenAI API key not set. Open Settings → OpenAI to add one."
            )
            return

        # Extract station + commodity names HERE on the main thread so the
        # worker doesn't need to touch the SQLite connection (cross-thread
        # use is forbidden by default).
        stations = [
            r["name"] for r in self.controller.conn.execute(
                "SELECT name FROM stations "
                "WHERE is_active = 1 AND is_gateway = 0 "
                "ORDER BY sort_order"
            ).fetchall()
        ]
        commodities = [
            r["name"] for r in self.controller.conn.execute(
                "SELECT name FROM commodities WHERE is_active = 1 ORDER BY name"
            ).fetchall()
        ]

        self.parse_btn.setEnabled(False)
        self.apply_btn.setEnabled(False)
        self.status_label.setText("Parsing…")
        self._clear_preview()

        self._thread = _ParseThread(
            self.controller.api_key,
            self.controller.settings.get("model"),
            stations, commodities, text,
        )
        self._thread.parsed.connect(self._on_parsed)
        self._thread.failed.connect(self._on_parse_failed)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()

    def _on_parsed(self, contracts: list) -> None:
        self.parse_btn.setEnabled(True)
        self._parsed = contracts
        self._row_checkboxes = []
        if not contracts:
            self.status_label.setText(
                "No contracts found. Try rephrasing or check station "
                "/ commodity names."
            )
            self.apply_btn.setEnabled(False)
            return

        self.status_label.setText(
            f"Parsed {len(contracts)} contract"
            f"{'s' if len(contracts) != 1 else ''}. "
            "Untick any you don't want before applying."
        )
        for idx, c in enumerate(contracts):
            self.preview_layout.insertWidget(idx, self._build_preview_card(c))
        self.apply_btn.setEnabled(True)

    def _on_parse_failed(self, msg: str) -> None:
        self.parse_btn.setEnabled(True)
        self.apply_btn.setEnabled(False)
        self.status_label.setText(f"Parse failed: {msg}")

    # ── dictation (PTT) ───────────────────────────────────────────────

    def _on_mic_toggled(self, checked: bool) -> None:
        if checked:
            self._start_dictation()
        else:
            self._stop_dictation()

    def _start_dictation(self) -> None:
        if not self.controller.api_key:
            self.status_label.setText(
                "OpenAI API key not set. Open Settings → OpenAI to add one."
            )
            self.mic_btn.setChecked(False)
            return
        if self._ptt is None:
            from ...voice.ptt_recorder import PTTRecorder
            self._ptt = PTTRecorder(self.controller.api_key, parent=self)
            self._ptt.transcript.connect(self._on_dictation_transcript)
            self._ptt.error.connect(self._on_dictation_error)
            self._ptt.state.connect(self._on_dictation_state)
        from ...voice.sound_cues import play_speech_on
        play_speech_on()
        self._ptt.start_recording()

    def _stop_dictation(self) -> None:
        if self._ptt is None:
            return
        from ...voice.sound_cues import play_speech_off
        play_speech_off()
        self._ptt.stop_recording()

    def _on_dictation_state(self, state: str) -> None:
        if state == "recording":
            self.mic_btn.setText("● Recording…")
            self.status_label.setText("Recording. Click the mic again to stop.")
        elif state == "transcribing":
            self.mic_btn.setText("🎤 Transcribing…")
            self.mic_btn.setEnabled(False)
            self.status_label.setText("Transcribing your dictation…")
        else:
            self.mic_btn.setEnabled(True)
            self.mic_btn.setText("🎤 PTT")

    def _on_dictation_transcript(self, text: str) -> None:
        text = text.strip()
        if not text:
            self.status_label.setText("Empty transcript — try again.")
            self.mic_btn.setChecked(False)
            return
        # Each dictation event = a new paragraph below the existing text.
        existing = self.input.toPlainText()
        if existing and not existing.endswith("\n\n"):
            existing = existing.rstrip() + "\n\n"
        self.input.setPlainText(existing + text)
        cursor = self.input.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self.input.setTextCursor(cursor)
        self.status_label.setText("Dictation added. Click Parse when ready.")
        self.mic_btn.setChecked(False)

    def _on_dictation_error(self, msg: str) -> None:
        self.status_label.setText(f"Dictation error: {msg}")
        self.mic_btn.setChecked(False)
        self.mic_btn.setEnabled(True)

    def _clear_preview(self) -> None:
        # Drop everything except the trailing stretch — properly delete
        # so old preview cards don't become ghost top-level windows.
        for i in reversed(range(self.preview_layout.count() - 1)):
            item = self.preview_layout.takeAt(i)
            if item is None:
                continue
            w = item.widget()
            if w is not None:
                w.hide()
                w.deleteLater()
        self._row_checkboxes = []

    def _build_preview_card(self, contract: dict) -> QFrame:
        card = QFrame()
        card.setObjectName("card")
        # Yellow box around contracts that came back missing a piece —
        # so the user knows they need attention before clicking Apply.
        issues = self._contract_issues(contract)
        if issues:
            card.setStyleSheet(
                "QFrame#card { background-color: #2a2208; "
                "border: 2px solid #ffcc00; border-radius: 3px; }"
            )
        layout = QHBoxLayout(card)
        layout.setContentsMargins(8, 6, 8, 6)

        cb = QCheckBox()
        cb.setChecked(not issues)   # don't pre-check incomplete rows
        cb.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        layout.addWidget(cb)
        self._row_checkboxes.append(cb)

        details = QVBoxLayout()
        title = QLabel(
            f"Pickup: {contract.get('pickup_station', '?')}  ·  "
            f"max {contract.get('max_pallet_size', 8)} SCU pallet"
        )
        title.setStyleSheet("font-weight: bold;")
        title.setWordWrap(True)
        details.addWidget(title)
        for d in contract.get("deliveries") or []:
            line = QLabel(
                f"  → {d.get('scu', '?')} SCU "
                f"{d.get('commodity', '?')} → {d.get('destination', '?')}"
            )
            line.setProperty("muted", True)
            line.setWordWrap(True)
            details.addWidget(line)
        if issues:
            warn = QLabel(
                "<span style='color:#ffcc00;font-weight:bold;'>⚠ Needs "
                "attention:</span> " + "; ".join(issues)
            )
            warn.setTextFormat(Qt.TextFormat.RichText)
            warn.setWordWrap(True)
            details.addWidget(warn)
        layout.addLayout(details, 1)

        return card

    def _contract_issues(self, contract: dict) -> list[str]:
        """Reasons this parsed contract isn't ready to apply.

        Returns a list of short human-readable issues. Empty list = OK.
        These are surface-level checks; the actual add_contract call
        will still validate against the workday's stations/commodities.
        """
        issues: list[str] = []
        if not contract.get("pickup_station"):
            issues.append("missing pickup station")
        deliveries = contract.get("deliveries") or []
        if not deliveries:
            issues.append("no deliveries parsed")
            return issues
        for i, d in enumerate(deliveries, 1):
            if not d.get("destination"):
                issues.append(f"delivery {i}: missing destination")
            if not d.get("commodity"):
                issues.append(f"delivery {i}: missing commodity")
            scu = d.get("scu")
            if not scu or (isinstance(scu, (int, float)) and scu <= 0):
                issues.append(f"delivery {i}: missing or zero SCU")
        return issues

    # ── apply ─────────────────────────────────────────────────────────

    def _on_apply_clicked(self) -> None:
        added = 0
        errors: list[str] = []
        for idx, contract in enumerate(self._parsed):
            cb = self._row_checkboxes[idx] if idx < len(self._row_checkboxes) else None
            if not cb or not cb.isChecked():
                continue
            try:
                self.controller.add_contract(contract)
                added += 1
            except ToolError as e:
                errors.append(f"#{idx + 1}: {e}")
            except Exception as e:
                errors.append(f"#{idx + 1}: {type(e).__name__}: {e}")

        if errors:
            QMessageBox.warning(
                self,
                f"{added} added, {len(errors)} skipped",
                "Some contracts could not be added:\n\n" + "\n".join(errors),
            )
        if added or not errors:
            self.accept()
