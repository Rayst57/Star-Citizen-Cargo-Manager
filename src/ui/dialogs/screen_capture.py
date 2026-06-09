"""
ScreenCaptureDialog — grab a region of a monitor or window, send it
to OpenAI's vision API, and emit the parsed contract dict so the
caller (AddContractDialog) can prefill its fields.

Parallel to the "Paste & Parse" dictation dialog: that one takes free-
form text, this one takes a screenshot.

The mss / pygetwindow imports are LAZY (inside helper functions) so
the app still starts cleanly when the libs aren't installed — the
dialog detects this and shows a friendly install message.
"""

from __future__ import annotations

from PySide6.QtCore import QBuffer, QIODevice, Qt, QThread, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QComboBox, QDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton,
    QVBoxLayout,
)


# Max preview thumbnail width in pixels — full 4K screenshots are
# unusably huge in a dialog, so scale down for display only. The raw
# QImage is kept at full resolution for the API call.
PREVIEW_MAX_WIDTH = 600


def _capture_libs_available() -> tuple[bool, str]:
    """Return (available, missing_libs_message). mss is required;
    pygetwindow is optional (needed for window enumeration on Windows
    but monitor capture works without it).

    The diagnostic message names which lib failed AND shows the active
    Python so the user can tell whether the install went to the same
    interpreter the app is running."""
    import sys
    missing: list[str] = []
    try:
        import mss  # noqa: F401
    except ImportError:
        missing.append("mss")
    try:
        import pygetwindow  # noqa: F401
    except ImportError:
        missing.append("pygetwindow")
    if "mss" in missing:
        # mss is required — without it we have nothing.
        miss_list = " ".join(missing)
        return False, (
            "Screen capture libraries missing.\n\n"
            f"  Install:  pip install {miss_list}\n"
            f"  Python:   {sys.executable}\n\n"
            "Tip: make sure the install goes to the SAME Python the "
            "app is running from (the path above). If you already "
            "installed, click ↻ Refresh below."
        )
    return True, ""


def _list_sources() -> list[dict]:
    """Enumerate available capture sources.

    Returns a list of dicts shaped:
        {"label": "Monitor 1", "kind": "monitor", "rect": {...}}
        {"label": "Star Citizen", "kind": "window", "rect": {...}}

    Window enumeration uses pygetwindow; if it isn't installed, only
    monitors are returned. mss is required for either.
    """
    sources: list[dict] = []

    import mss
    with mss.mss() as sct:
        # sct.monitors[0] is the union "all monitors" — skip it.
        for i, m in enumerate(sct.monitors[1:], start=1):
            sources.append({
                "label": f"Monitor {i}",
                "kind": "monitor",
                "rect": {
                    "left":   m["left"],
                    "top":    m["top"],
                    "width":  m["width"],
                    "height": m["height"],
                },
            })

    try:
        import pygetwindow as gw
    except ImportError:
        return sources

    try:
        windows = gw.getAllWindows()
    except Exception:
        # pygetwindow's macOS / Linux stubs raise NotImplementedError;
        # silently fall back to monitor-only mode.
        return sources

    for w in windows:
        title = getattr(w, "title", "") or ""
        if not title.strip():
            continue
        # isVisible / visible — name varies by platform. Default to
        # showing the window if the attr isn't present.
        visible = getattr(w, "isVisible", getattr(w, "visible", True))
        if not visible:
            continue
        width = getattr(w, "width", 0) or 0
        height = getattr(w, "height", 0) or 0
        if width <= 0 or height <= 0:
            continue
        sources.append({
            "label": title,
            "kind": "window",
            "rect": {
                "left":   int(getattr(w, "left", 0) or 0),
                "top":    int(getattr(w, "top", 0) or 0),
                "width":  int(width),
                "height": int(height),
            },
        })
    return sources


def _grab_rect(rect: dict) -> QImage:
    """Capture the given screen rect as a QImage."""
    import mss
    with mss.mss() as sct:
        shot = sct.grab(rect)
        # mss returns BGRA bytes. QImage.Format_ARGB32 is little-endian
        # BGRA on the wire, which matches mss's layout.
        # Copy so the QImage owns its buffer once mss releases it.
        return QImage(
            bytes(shot.bgra),
            shot.width,
            shot.height,
            QImage.Format.Format_ARGB32,
        ).copy()


def _qimage_to_png_bytes(image: QImage) -> bytes:
    """Encode a QImage as PNG bytes for the vision API."""
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buf, "PNG")
    data = bytes(buf.data())
    buf.close()
    return data


class _ParseThread(QThread):
    """Worker that runs the OpenAI vision call off the UI thread."""

    parsed = Signal(dict)
    failed = Signal(str)

    def __init__(self, image_bytes: bytes, api_key: str):
        super().__init__()
        self.image_bytes = image_bytes
        self.api_key = api_key

    def run(self) -> None:
        try:
            # Lazy import so tests that don't exercise the parse path
            # don't need the openai client mocked at module load.
            from ...vision_parser import parse_contract_from_image
            data = parse_contract_from_image(self.image_bytes, self.api_key)
            self.parsed.emit(data)
        except Exception as e:
            self.failed.emit(str(e))


class ScreenCaptureDialog(QDialog):
    """Capture a region of the screen and parse it into a contract."""

    contract_parsed = Signal(dict)

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self._captured_image: QImage | None = None
        self._thread: _ParseThread | None = None

        self.setWindowTitle("Capture from Screen")
        self.setMinimumWidth(640)

        root = QVBoxLayout(self)

        # Header
        title = QLabel("Capture a contract screenshot")
        title.setProperty("heading", True)
        root.addWidget(title)

        # Source selector + Capture button.
        source_row = QHBoxLayout()
        source_row.addWidget(QLabel("Source:"))
        self.source_combo = QComboBox()
        source_row.addWidget(self.source_combo, 1)
        self.capture_btn = QPushButton("Capture")
        self.capture_btn.clicked.connect(self._on_capture_clicked)
        source_row.addWidget(self.capture_btn)
        root.addLayout(source_row)

        # Preview thumbnail
        self.preview_label = QLabel("No capture yet.")
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setMinimumHeight(200)
        self.preview_label.setProperty("muted", True)
        self.preview_label.setStyleSheet(
            "QLabel { border: 1px dashed #555; padding: 8px; }"
        )
        root.addWidget(self.preview_label, 1)

        # Status line
        self.status_label = QLabel("")
        self.status_label.setProperty("muted", True)
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)

        # Footer buttons
        footer = QHBoxLayout()
        footer.addStretch(1)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setProperty("flat", True)
        self.cancel_btn.clicked.connect(self.reject)
        footer.addWidget(self.cancel_btn)
        self.parse_btn = QPushButton("Parse with AI")
        self.parse_btn.setEnabled(False)
        self.parse_btn.clicked.connect(self._on_parse_clicked)
        footer.addWidget(self.parse_btn)
        root.addLayout(footer)

        # Populate sources — or show the install hint if mss is missing.
        self._populate_sources()

    # ── source list ────────────────────────────────────────────────────

    def _populate_sources(self) -> None:
        ok, msg = _capture_libs_available()
        if not ok:
            self.source_combo.setEnabled(False)
            self.capture_btn.setEnabled(False)
            self.status_label.setText(msg)
            return
        try:
            sources = _list_sources()
        except Exception as e:
            self.source_combo.setEnabled(False)
            self.capture_btn.setEnabled(False)
            self.status_label.setText(f"Could not list capture sources: {e}")
            return

        if not sources:
            self.source_combo.setEnabled(False)
            self.capture_btn.setEnabled(False)
            self.status_label.setText("No monitors or windows found to capture.")
            return

        for src in sources:
            self.source_combo.addItem(src["label"], userData=src)

    # ── capture ────────────────────────────────────────────────────────

    def _on_capture_clicked(self) -> None:
        src = self.source_combo.currentData()
        if not src:
            self.status_label.setText("Pick a source to capture.")
            return
        try:
            image = _grab_rect(src["rect"])
        except Exception as e:
            QMessageBox.warning(
                self, "Capture failed",
                f"Could not capture {src['label']}: {e}",
            )
            return
        if image.isNull():
            QMessageBox.warning(
                self, "Capture failed",
                f"Captured image of {src['label']} was empty.",
            )
            return
        self._captured_image = image
        self._show_preview(image)
        self.parse_btn.setEnabled(True)
        self.status_label.setText(
            f"Captured {image.width()}×{image.height()} from {src['label']}. "
            "Click 'Parse with AI' to extract contract data."
        )

    def _show_preview(self, image: QImage) -> None:
        pix = QPixmap.fromImage(image)
        if pix.width() > PREVIEW_MAX_WIDTH:
            pix = pix.scaledToWidth(
                PREVIEW_MAX_WIDTH,
                Qt.TransformationMode.SmoothTransformation,
            )
        self.preview_label.setPixmap(pix)
        self.preview_label.setText("")

    # ── parse ──────────────────────────────────────────────────────────

    def _on_parse_clicked(self) -> None:
        if self._captured_image is None or self._captured_image.isNull():
            self.status_label.setText("Capture a screenshot first.")
            return
        if self._thread is not None and self._thread.isRunning():
            return

        api_key = getattr(self.controller, "api_key", None)
        if not api_key:
            QMessageBox.warning(
                self, "Missing API key",
                "OpenAI API key not set. Open Settings → OpenAI to add one.",
            )
            return

        try:
            png = _qimage_to_png_bytes(self._captured_image)
        except Exception as e:
            QMessageBox.warning(
                self, "Encoding failed",
                f"Could not encode captured image as PNG: {e}",
            )
            return

        self.parse_btn.setEnabled(False)
        self.capture_btn.setEnabled(False)
        self.status_label.setText("Parsing…")

        self._thread = _ParseThread(png, api_key)
        self._thread.parsed.connect(self._on_parsed)
        self._thread.failed.connect(self._on_parse_failed)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()

    def _on_parsed(self, data: dict) -> None:
        self.contract_parsed.emit(data)
        self.accept()

    def _on_parse_failed(self, msg: str) -> None:
        self.parse_btn.setEnabled(True)
        self.capture_btn.setEnabled(True)
        self.status_label.setText("")
        QMessageBox.warning(
            self, "Parse failed",
            f"Could not parse contract from screenshot:\n\n{msg}",
        )
