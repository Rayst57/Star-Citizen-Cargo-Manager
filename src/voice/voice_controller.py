"""
VoiceController — orchestrates wake word, PTT, mic capture, and STT/LLM threads.

For v1 this is a thin scaffolding layer that defers heavy lifting to
the wake_word, ptt_*, recorder, and llm_thread modules.  It boots
gracefully if any of those modules / their underlying libraries are
missing (e.g. running on a system without sounddevice or pvporcupine).
"""

from __future__ import annotations

import re

from PySide6.QtCore import QObject, Signal


RESERVED_PHRASES: dict[str, str] = {
    "stand by giant": "mute",
    "cancel that":    "abort",
}


class VoiceController(QObject):
    transcript_ready = Signal(str)
    state_changed    = Signal(str)        # 'listening' | 'muted' | 'cancelled' | 'off'
    error            = Signal(str)

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self._enabled = False

    # ── lifecycle ──────────────────────────────────────────────────────

    def start(self) -> None:
        """Initialise wake word + PTT listeners based on settings."""
        mode = self.controller.settings.get("listening_mode")
        if mode == "off":
            self.state_changed.emit("off")
            return

        if not self.controller.api_key:
            self.error.emit("OpenAI API key not set — voice disabled.")
            self.state_changed.emit("off")
            return

        # Probe optional dependencies. Each failure degrades silently to a
        # specific user-visible message; the app stays usable via manual entry.
        try:
            import pvporcupine  # noqa: F401
            import sounddevice  # noqa: F401
        except ImportError as e:
            self.error.emit(f"Voice dependency missing: {e.name}. Install requirements.txt.")
            self.state_changed.emit("off")
            return

        self._enabled = True
        self.state_changed.emit("listening")

    def stop(self) -> None:
        self._enabled = False
        self.state_changed.emit("off")

    # ── transcript pipeline ───────────────────────────────────────────

    def handle_transcript(self, text: str) -> None:
        """Dispatch a transcript: reserved phrase or LLM."""
        normalized = re.sub(r"[^a-z ]", "", text.lower()).strip()
        for phrase, action in RESERVED_PHRASES.items():
            if phrase in normalized:
                if action == "mute":
                    self.stop()
                elif action == "abort":
                    self.state_changed.emit("cancelled")
                return

        self.transcript_ready.emit(text)
