"""
Application settings backed by the SQLite app_settings table plus
keyring (Windows Credential Manager) for the OpenAI API key.

Defaults match docs/app_init_voice.md §AppSettings.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any


KEYRING_SERVICE = "Star Citizen Cargo Manager"
KEYRING_API_KEY = "openai_api_key"


DEFAULTS: dict[str, str] = {
    "stt_engine":            "openai",
    "listening_mode":        "wake_word",
    "wake_phrase":           "Hey Giant",
    "wake_sensitivity":      "0.6",
    "ptt_binds":             "[]",
    "input_device":          "",
    "input_gain":            "0.0",
    "noise_suppression":     "0",
    "activation_threshold":  "-45.0",
    "tts_enabled":           "0",
    "tts_voice":             "alloy",
    "tts_speed":             "1.0",
    "model":                 "gpt-4o",
    "use_realtime_api":      "0",
    "theme":                 "default",
    "window_opacity":        "1.0",
    "always_on_top":         "0",
    "hotkey_recompute":      "[]",
    "hotkey_cancel":         "Escape",
}


def _coerce(key: str, raw: str) -> Any:
    if key in {"wake_sensitivity", "input_gain", "activation_threshold",
               "tts_speed", "window_opacity"}:
        try:
            return float(raw)
        except (TypeError, ValueError):
            return float(DEFAULTS[key])
    if key in {"noise_suppression", "tts_enabled", "use_realtime_api",
               "always_on_top"}:
        return raw not in ("", "0", "false", "False")
    if key in {"ptt_binds", "hotkey_recompute"}:
        try:
            return json.loads(raw or "[]")
        except json.JSONDecodeError:
            return []
    return raw


class AppSettings:
    """Read/write wrapper around the app_settings key/value table."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._ensure_defaults()

    def _ensure_defaults(self) -> None:
        for key, value in DEFAULTS.items():
            self.conn.execute(
                "INSERT OR IGNORE INTO app_settings (key, value) VALUES (?, ?)",
                (key, value),
            )
        self.conn.commit()

    def get(self, key: str) -> Any:
        row = self.conn.execute(
            "SELECT value FROM app_settings WHERE key = ?", (key,)
        ).fetchone()
        raw = row["value"] if row else DEFAULTS.get(key, "")
        return _coerce(key, raw)

    def set(self, key: str, value: Any) -> None:
        if isinstance(value, bool):
            raw = "1" if value else "0"
        elif isinstance(value, (list, dict)):
            raw = json.dumps(value)
        else:
            raw = str(value)
        self.conn.execute(
            """
            INSERT INTO app_settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, raw),
        )
        self.conn.commit()

    def all(self) -> dict[str, Any]:
        return {key: self.get(key) for key in DEFAULTS}


# ── OpenAI API key (Windows Credential Manager) ──────────────────────────

def get_api_key() -> str | None:
    """Return the stored OpenAI API key, or None if not set / keyring unavailable.

    Catches BaseException because keyring backend probing can hit pyo3
    PanicException on systems without a working credential store.
    """
    try:
        import keyring
        return keyring.get_password(KEYRING_SERVICE, KEYRING_API_KEY)
    except BaseException:
        return None


def set_api_key(key: str) -> bool:
    """Persist the OpenAI API key. Returns True on success."""
    try:
        import keyring
        keyring.set_password(KEYRING_SERVICE, KEYRING_API_KEY, key)
        return True
    except BaseException:
        return False


def clear_api_key() -> bool:
    try:
        import keyring
        keyring.delete_password(KEYRING_SERVICE, KEYRING_API_KEY)
        return True
    except BaseException:
        return False
