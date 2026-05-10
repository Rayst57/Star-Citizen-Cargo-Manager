"""
Audio cues for PTT start/stop.

Plays Windows' built-in speech-recognition WAVs ("Speech On.wav" and
"Speech Off.wav" in %SystemRoot%\\Media) so the pilot gets a
recognisable audible confirmation when the mic toggles. Falls back to
silence on non-Windows platforms or if the WAV file isn't installed.
"""

from __future__ import annotations

import os
import sys


def play_speech_on() -> None:
    """Play the system 'Speech On' cue if available."""
    _play_async(_resolve_path("Speech On.wav"))


def play_speech_off() -> None:
    """Play the system 'Speech Off' cue if available."""
    _play_async(_resolve_path("Speech Off.wav"))


def _resolve_path(filename: str) -> str | None:
    if sys.platform != "win32":
        return None
    system_root = os.environ.get("SystemRoot") or r"C:\Windows"
    path = os.path.join(system_root, "Media", filename)
    return path if os.path.exists(path) else None


def _play_async(path: str | None) -> None:
    if not path:
        return
    try:
        import winsound
    except ImportError:
        return
    try:
        # SND_ASYNC returns immediately so we don't block the UI thread;
        # SND_NODEFAULT keeps the system silent if the WAV is missing
        # instead of playing the default Windows ding.
        winsound.PlaySound(
            path, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT,
        )
    except Exception:
        pass
