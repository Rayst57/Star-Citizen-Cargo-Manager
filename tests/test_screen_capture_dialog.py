"""Tests for ScreenCaptureDialog — soft-fail when mss is missing,
monitor enumeration when it's present.

All mss / pygetwindow access is mocked. The Qt platform is offscreen.
"""

from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from src.ui.dialogs.screen_capture import ScreenCaptureDialog


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_dialog_opens_without_mss_installed(qapp, monkeypatch):
    """When mss isn't available the dialog should still open and show
    a friendly install hint rather than crashing."""
    # Hide any real mss the dev box might have.
    monkeypatch.setitem(sys.modules, "mss", None)

    controller = SimpleNamespace(api_key=None)
    dlg = ScreenCaptureDialog(controller)
    try:
        status = dlg.status_label.text()
        assert "Install screen capture support" in status
        assert "pip install mss" in status
        # Capture is disabled — there's nothing to capture without mss.
        assert not dlg.capture_btn.isEnabled()
        assert not dlg.source_combo.isEnabled()
    finally:
        dlg.deleteLater()


def test_dialog_lists_monitors(qapp, monkeypatch):
    """With mss returning 2 monitors (+ the all-monitors union at idx 0)
    the source combo should expose exactly 2 monitor entries."""
    fake_monitors = [
        # mss.monitors[0] is the union of all monitors — _list_sources
        # skips it.
        {"left": 0, "top": 0, "width": 3840, "height": 1080},
        {"left": 0, "top": 0, "width": 1920, "height": 1080},
        {"left": 1920, "top": 0, "width": 1920, "height": 1080},
    ]

    class FakeMSS:
        def __init__(self):
            self.monitors = fake_monitors
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            return False
        def grab(self, rect):
            raise AssertionError("grab should not be called in this test")

    fake_mss_module = SimpleNamespace(mss=FakeMSS)
    monkeypatch.setitem(sys.modules, "mss", fake_mss_module)
    # Force pygetwindow to be unavailable so window enumeration is
    # skipped and we count only monitors.
    monkeypatch.setitem(sys.modules, "pygetwindow", None)

    controller = SimpleNamespace(api_key=None)
    dlg = ScreenCaptureDialog(controller)
    try:
        labels = [
            dlg.source_combo.itemText(i)
            for i in range(dlg.source_combo.count())
        ]
        monitor_labels = [l for l in labels if l.startswith("Monitor")]
        assert monitor_labels == ["Monitor 1", "Monitor 2"]
        assert dlg.capture_btn.isEnabled()
    finally:
        dlg.deleteLater()
