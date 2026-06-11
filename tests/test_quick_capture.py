"""Tests for the Quick Capture workflow.

Covers the parts that don't need a real OS-level keyboard listener:

1. CaptureQueue push/remove/clear emits ``changed`` with the new depth.
2. screen_capture.source_to_settings_value strips volatile fields so
   a saved monitor / window source persists in a stable form.
3. AppSettings round-trips the JSON-shaped screen_capture_source.
4. Wiring: an emitted ``triggered`` signal on QuickCaptureHotkey
   results in a screenshot being added to the queue (the grab itself
   is monkeypatched — we only verify the plumbing).
"""

from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication, QMainWindow

from src.app_controller import AppController
from src.db.init_db import initialize_database


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def controller(qapp, tmp_path):
    db_path = tmp_path / "test.db"
    conn = initialize_database(db_path)
    return AppController(conn, db_path)


def _solid_image(w=120, h=80, color=0xff_3399ff) -> QImage:
    img = QImage(w, h, QImage.Format.Format_ARGB32)
    img.fill(color)
    return img


# ── 1. CaptureQueue ──────────────────────────────────────────────────


def test_capture_queue_push_emits_changed(controller):
    q = controller.capture_queue
    seen: list[int] = []
    q.changed.connect(seen.append)

    a = q.push(_solid_image(), source_label="Monitor 1")
    b = q.push(_solid_image(), source_label="Monitor 1")
    assert len(q) == 2
    assert seen == [1, 2]
    assert a.id < b.id, "IDs must be monotonically increasing."


def test_capture_queue_remove_and_clear(controller):
    q = controller.capture_queue
    a = q.push(_solid_image())
    b = q.push(_solid_image())
    q.push(_solid_image())

    seen: list[int] = []
    q.changed.connect(seen.append)

    assert q.remove(b.id) is True
    assert q.remove(b.id) is False, "Second remove must be a no-op."
    ids = [it.id for it in q.items()]
    assert b.id not in ids
    assert a.id in ids

    q.clear()
    assert len(q) == 0
    # Two changed signals: one for remove(b), one for clear.
    assert seen == [2, 0]


# ── 2. Settings round-trip ───────────────────────────────────────────


def test_screen_capture_source_round_trip(controller):
    src = {"kind": "window", "label": "Star Citizen", "title": "Star Citizen"}
    controller.settings.set("screen_capture_source", src)
    got = controller.settings.get("screen_capture_source")
    assert got == src

    # Empty default coerces to {} rather than None or "".
    controller.settings.set("screen_capture_source", "")
    assert controller.settings.get("screen_capture_source") == {}


def test_source_to_settings_value_strips_volatile_fields():
    from src.ui.dialogs.screen_capture import source_to_settings_value
    monitor_src = {
        "label": "Monitor 2", "kind": "monitor",
        "rect": {"left": 1920, "top": 0, "width": 2560, "height": 1440},
    }
    assert source_to_settings_value(monitor_src) == {
        "kind": "monitor", "label": "Monitor 2", "index": 2,
    }
    window_src = {
        "label": "Star Citizen", "kind": "window",
        "rect": {"left": 100, "top": 100, "width": 1920, "height": 1080},
    }
    # Window position changes every time the user moves it; only the
    # title is stable enough to persist.
    saved = source_to_settings_value(window_src)
    assert "rect" not in saved
    assert saved["title"] == "Star Citizen"


# ── 3. Hotkey -> queue wiring ────────────────────────────────────────


def test_hotkey_trigger_pushes_to_queue(controller, monkeypatch):
    """Simulate the hotkey firing: install_quick_capture connects the
    signal, and when we emit it, a screenshot lands on the queue."""
    from src.ui import quick_capture
    from src.ui.dialogs import screen_capture as sc

    # Pretend the saved source resolves to a fixed test image.
    canned = _solid_image()
    monkeypatch.setattr(
        sc, "grab_source",
        lambda saved, return_reason=False: (
            (canned, "") if return_reason else canned
        ),
    )
    # Pretend a source is saved so install_quick_capture has something
    # to label the entry with.
    controller.settings.set("screen_capture_source", {
        "kind": "monitor", "label": "Monitor 1", "index": 1,
    })

    win = QMainWindow()
    hk = quick_capture.install_quick_capture(controller, win)
    assert hk is not None

    # Stub set_combo so the test isn't blocked by missing OS hotkey
    # privileges, then synthesise the signal.
    hk._handle = None
    hk.triggered.emit()
    # Queued connection — pump the event loop so the slot runs.
    QApplication.processEvents()

    assert len(controller.capture_queue) == 1
    item = controller.capture_queue.items()[0]
    assert item.source_label == "Monitor 1"
    assert not item.image.isNull()
    hk.shutdown()


def test_hotkey_trigger_no_op_when_grab_fails(controller, monkeypatch):
    from src.ui import quick_capture
    from src.ui.dialogs import screen_capture as sc

    monkeypatch.setattr(
        sc, "grab_source",
        lambda saved, return_reason=False: (
            (None, "test no-grab") if return_reason else None
        ),
    )

    win = QMainWindow()
    hk = quick_capture.install_quick_capture(controller, win)
    hk._handle = None
    hk.triggered.emit()
    QApplication.processEvents()

    assert len(controller.capture_queue) == 0
    hk.shutdown()
