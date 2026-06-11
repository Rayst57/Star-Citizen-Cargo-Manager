"""
Quick Capture — global hotkey + capture queue for the rapid-fire
"click contract in SC, press hotkey, repeat" workflow.

When the user presses the configured hotkey, this module:

  1. Grabs a screenshot of the saved capture source (no UI).
  2. Pushes it onto an in-memory queue (this app session only —
     screenshots are not persisted across launches).
  3. Updates a badge in the main window with the queue depth.

The user reviews the queue at their leisure: click the badge to open
a review dialog showing thumbnails, parse each one into an Add
Contract dialog when ready.

Hotkey listening uses the cross-platform ``keyboard`` package, which
runs its own background thread. We marshal the callback back onto the
Qt main thread via a queued signal so all UI work happens there.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QImage

_log = logging.getLogger("cargo_manager")


@dataclass
class QueuedCapture:
    """One pending capture awaiting parse."""
    id: int                       # monotonically increasing within a session
    image: QImage
    captured_at: float = field(default_factory=time.time)
    source_label: str = ""


class CaptureQueue(QObject):
    """In-memory FIFO queue of pending screenshots.

    Lives on the controller; the main window listens for ``changed``
    to keep the badge in sync, and the review dialog binds to the
    same signal so opening/removing entries refreshes the list.
    """

    # Emitted any time the queue mutates (push or pop) — carries the
    # new queue depth so badge updates don't have to re-read.
    changed = Signal(int)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._items: list[QueuedCapture] = []
        self._next_id = 1

    def push(self, image: QImage, source_label: str = "") -> QueuedCapture:
        item = QueuedCapture(
            id=self._next_id,
            image=image,
            source_label=source_label,
        )
        self._next_id += 1
        self._items.append(item)
        self.changed.emit(len(self._items))
        return item

    def items(self) -> list[QueuedCapture]:
        return list(self._items)

    def remove(self, item_id: int) -> bool:
        for i, it in enumerate(self._items):
            if it.id == item_id:
                del self._items[i]
                self.changed.emit(len(self._items))
                return True
        return False

    def clear(self) -> None:
        if not self._items:
            return
        self._items.clear()
        self.changed.emit(0)

    def __len__(self) -> int:
        return len(self._items)


class QuickCaptureHotkey(QObject):
    """Bridge from the global ``keyboard`` listener to a Qt signal
    delivered on the main thread.

    Owns the lifecycle of the OS hotkey: rebind on settings change,
    unbind on shutdown. Fail-soft: if the ``keyboard`` package isn't
    importable (no admin on Linux, missing wheel, etc.) the hotkey is
    a no-op and the user can still use the screenshot button in
    AddContract.
    """

    # Emitted on the GUI thread when the hotkey fires.
    triggered = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._combo: str = ""
        self._handle: object | None = None

    def set_combo(self, combo: str) -> bool:
        """Bind *combo* (e.g. ``"ctrl+shift+c"``). Empty string = unbind.

        Returns True if the bind succeeded (or was a clean unbind),
        False if the keyboard lib couldn't register the combo.
        """
        # Unbind first so a re-bind to the same combo refreshes cleanly.
        self._unbind()
        combo = (combo or "").strip()
        self._combo = combo
        if not combo:
            return True

        try:
            import keyboard
        except Exception as exc:                            # noqa: BLE001
            _log.info("quick_capture: keyboard import failed: %s", exc)
            return False

        try:
            # suppress=False so the keypress still reaches the focused
            # app (Star Citizen) — we want the hotkey to be observable
            # by our app without blocking the game's own bindings.
            self._handle = keyboard.add_hotkey(
                combo,
                lambda: self.triggered.emit(),
                suppress=False,
                trigger_on_release=False,
            )
        except Exception as exc:                            # noqa: BLE001
            _log.info(
                "quick_capture: bind '%s' failed: %s", combo, exc,
            )
            self._handle = None
            return False
        _log.info("quick_capture: bound hotkey '%s'", combo)
        return True

    def combo(self) -> str:
        return self._combo

    def _unbind(self) -> None:
        if self._handle is None:
            return
        try:
            import keyboard
            keyboard.remove_hotkey(self._handle)
        except Exception:                                   # noqa: BLE001
            pass
        self._handle = None

    def shutdown(self) -> None:
        self._unbind()


def install_quick_capture(controller, main_window) -> QuickCaptureHotkey | None:
    """Wire the global hotkey to push a screenshot of the saved
    source onto the controller's CaptureQueue.

    Returns the hotkey object (or None when no combo is configured)
    so callers can keep it alive and rebind on settings change.
    """
    combo = (controller.settings.get("hotkey_quick_capture") or "").strip()
    hk = QuickCaptureHotkey(parent=main_window)

    def _toast(msg: str, ms: int = 2500) -> None:
        """Flash a brief status-bar message so the user can see the
        hotkey landed even when the cargo manager isn't focused."""
        sb = getattr(main_window, "statusBar", None)
        if callable(sb):
            try:
                sb().showMessage(msg, ms)
            except Exception:                               # noqa: BLE001
                pass

    def _on_triggered() -> None:
        from .dialogs.screen_capture import grab_source
        saved = controller.settings.get("screen_capture_source") or {}
        if not saved:
            _toast("⛔ Quick Capture: no source saved — pick one in Settings → Capture")
            _log.info("quick_capture: hotkey fired but no source saved")
            return
        img = grab_source(saved)
        if img is None or img.isNull():
            label = saved.get("label", "?")
            _toast(
                f"⛔ Quick Capture: '{label}' isn't running. Open SC, "
                f"or pick a monitor in Settings."
            )
            _log.info(
                "quick_capture: hotkey fired but capture source "
                "couldn't be resolved (saved=%r)", saved,
            )
            return
        label = saved.get("label", "") if isinstance(saved, dict) else ""
        controller.capture_queue.push(img, source_label=label)
        _toast(
            f"📸 Captured from '{label}' — "
            f"{len(controller.capture_queue)} in queue"
        )
        _log.info(
            "quick_capture: queued screenshot from '%s' (depth=%d)",
            label, len(controller.capture_queue),
        )

    # Connect with QueuedConnection so the slot always runs on the
    # main (GUI) thread even though the hotkey lib fires from its own
    # background thread.
    hk.triggered.connect(_on_triggered, Qt.ConnectionType.QueuedConnection)
    hk.set_combo(combo)
    return hk
