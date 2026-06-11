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

from PySide6.QtCore import QBuffer, QIODevice, QPoint, QRect, Qt, QThread, Signal
from PySide6.QtGui import QColor, QImage, QMouseEvent, QPainter, QPen, QPixmap
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


def _list_sources(diagnostics: dict | None = None) -> list[dict]:
    """Enumerate available capture sources.

    Returns a list of dicts shaped:
        {"label": "Monitor 1", "kind": "monitor", "rect": {...}}
        {"label": "Star Citizen", "kind": "window", "rect": {...}}

    Window enumeration uses pygetwindow; if it isn't installed, only
    monitors are returned. mss is required for either.

    When *diagnostics* is a dict, the function fills it with counts
    explaining what was found vs filtered out — used by the Settings
    tab to tell the user "saw N raw windows, M had titles, K passed
    the size filter" instead of silently degrading.
    """
    sources: list[dict] = []
    diag = diagnostics if isinstance(diagnostics, dict) else {}
    diag.setdefault("monitors", 0)
    diag.setdefault("raw_windows", 0)
    diag.setdefault("titled_windows", 0)
    diag.setdefault("sized_windows", 0)
    diag.setdefault("pygetwindow_error", "")

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
        diag["monitors"] = len(sct.monitors) - 1

    try:
        import pygetwindow as gw
    except ImportError as exc:
        diag["pygetwindow_error"] = f"pygetwindow not installed: {exc}"
        return sources

    try:
        windows = gw.getAllWindows()
    except Exception as exc:                                # noqa: BLE001
        # pygetwindow's macOS / Linux stubs raise NotImplementedError;
        # surface the reason so the Settings UI can show it.
        diag["pygetwindow_error"] = str(exc)
        return sources

    diag["raw_windows"] = len(windows)

    # NB: we deliberately don't filter on `isVisible` — on pygetwindow
    # 0.0.9 it's a bound method (always truthy under getattr), and the
    # property's semantics vary by platform anyway. The size filter
    # (> 0) is enough to drop genuinely-invalid handles while still
    # showing fullscreen apps like Star Citizen.
    for w in windows:
        title = getattr(w, "title", "") or ""
        if not title.strip():
            continue
        diag["titled_windows"] += 1
        try:
            width = int(getattr(w, "width", 0) or 0)
            height = int(getattr(w, "height", 0) or 0)
        except Exception:                                   # noqa: BLE001
            continue
        if width <= 0 or height <= 0:
            continue
        diag["sized_windows"] += 1
        try:
            left = int(getattr(w, "left", 0) or 0)
            top  = int(getattr(w, "top", 0) or 0)
        except Exception:                                   # noqa: BLE001
            left = top = 0
        sources.append({
            "label": title,
            "kind": "window",
            "rect": {
                "left":   left,
                "top":    top,
                "width":  width,
                "height": height,
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


def resolve_saved_source(saved: dict) -> dict | None:
    """Take a persisted source spec from settings and re-resolve it to
    a fresh ``{label, kind, rect}`` ready for ``_grab_rect``.

    Monitor entries match by ``index`` (defaults to 1 if missing).
    Window entries match by title (case-insensitive substring) and
    pick up the window's CURRENT bounds — so the saved source still
    works after the user moves or resizes the Star Citizen window.

    Returns None when the saved source can't be re-resolved (monitor
    removed, window closed). Callers should fall back to prompting.
    """
    if not saved:
        return None
    sources = _list_sources()
    kind = saved.get("kind")
    if kind == "monitor":
        idx = int(saved.get("index", 1))
        for s in sources:
            if s["kind"] == "monitor" and s["label"] == f"Monitor {idx}":
                return s
        return None
    if kind == "window":
        wanted = (saved.get("title") or saved.get("label") or "").strip().lower()
        if not wanted:
            return None
        for s in sources:
            if s["kind"] == "window" and wanted in s["label"].lower():
                return s
        return None
    return None


def grab_source(saved: dict) -> QImage | None:
    """Grab a screenshot of the saved source, or None if it can't be
    resolved or the capture libs are missing. Never raises."""
    ok, _ = _capture_libs_available()
    if not ok:
        return None
    src = resolve_saved_source(saved)
    if src is None:
        return None
    try:
        img = _grab_rect(src["rect"])
    except Exception:                                   # noqa: BLE001
        return None
    if img.isNull():
        return None
    return img


def source_to_settings_value(src: dict) -> dict:
    """Persistable form of a source dict — strips the volatile rect
    (window positions change) but keeps enough to re-resolve later."""
    kind = src.get("kind")
    if kind == "monitor":
        # "Monitor 1" → index 1.
        try:
            idx = int(src["label"].split()[-1])
        except (IndexError, ValueError):
            idx = 1
        return {"kind": "monitor", "label": src["label"], "index": idx}
    if kind == "window":
        return {
            "kind": "window",
            "label": src.get("label", ""),
            "title": src.get("label", ""),
        }
    return {}


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


class CroppablePreview(QLabel):
    """Preview label that lets the user drag a selection rectangle over
    the captured image to focus a vision parse on a sub-region.

    Stars Citizen typically runs fullscreen, so capturing the SC window
    grabs the whole monitor. Without a crop tool the vision API has to
    parse a 4K screenshot dominated by HUD, sky, and other UI elements
    instead of the contract panel. Letting the user draw a rectangle
    around just the contract panel keeps the parse fast and accurate.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlignment(
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft
        )
        self.setStyleSheet("QLabel { border: 1px dashed #555; padding: 8px; }")
        self.setText("No capture yet.")
        self.setProperty("muted", True)
        self.setMouseTracking(True)
        self._original_image: QImage | None = None
        self._displayed_pixmap: QPixmap | None = None
        # Selection rect is in DISPLAYED pixmap coordinates (after scaling).
        self._sel_start: QPoint | None = None
        self._sel_end: QPoint | None = None
        self._sel_active: bool = False
        # Where the pixmap actually sits inside the label, in label coords
        # — used to translate mouse events into pixmap coords.
        self._pix_origin: QPoint = QPoint(8, 8)

    def set_image(self, image: QImage, displayed: QPixmap) -> None:
        """Show *displayed* (a scaled QPixmap) while remembering *image*
        (the original full-resolution QImage) for crop math."""
        self._original_image = image
        self._displayed_pixmap = displayed
        self._sel_start = self._sel_end = None
        self._sel_active = False
        # Size the label to the pixmap so coordinates are 1:1 inside it.
        self.setText("")
        self.setMinimumSize(
            displayed.width() + 16, displayed.height() + 16,
        )
        self.update()

    def clear_selection(self) -> None:
        self._sel_start = self._sel_end = None
        self._sel_active = False
        self.update()

    def has_selection(self) -> bool:
        if self._sel_start is None or self._sel_end is None:
            return False
        r = QRect(self._sel_start, self._sel_end).normalized()
        return r.width() >= 8 and r.height() >= 8

    def selected_image(self) -> QImage | None:
        """Return the cropped QImage if there's a selection, else the
        full captured image. None if nothing has been captured yet."""
        if self._original_image is None or self._original_image.isNull():
            return None
        if not self.has_selection() or self._displayed_pixmap is None:
            return self._original_image
        # Translate the displayed-coordinate selection back to original
        # image coordinates via the displayed/original scale ratio.
        sel = QRect(self._sel_start, self._sel_end).normalized()
        scale_x = self._original_image.width() / self._displayed_pixmap.width()
        scale_y = self._original_image.height() / self._displayed_pixmap.height()
        src = QRect(
            int(sel.x() * scale_x),
            int(sel.y() * scale_y),
            int(sel.width() * scale_x),
            int(sel.height() * scale_y),
        )
        src = src.intersected(self._original_image.rect())
        if src.isEmpty():
            return self._original_image
        return self._original_image.copy(src)

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        if self._displayed_pixmap is None:
            return
        p = QPainter(self)
        p.drawPixmap(self._pix_origin, self._displayed_pixmap)
        if self.has_selection():
            sel = QRect(self._sel_start, self._sel_end).normalized()
            # Translate selection to label space for drawing.
            label_sel = sel.translated(self._pix_origin)
            # Dim everything outside the selection.
            pix_rect = QRect(
                self._pix_origin,
                self._displayed_pixmap.size(),
            )
            overlay = QColor(0, 0, 0, 110)
            for r in self._regions_outside(pix_rect, label_sel):
                p.fillRect(r, overlay)
            p.setPen(QPen(QColor("#26b6d4"), 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(label_sel)
        p.end()

    @staticmethod
    def _regions_outside(outer: QRect, inner: QRect) -> list[QRect]:
        """Return up-to-4 rects covering outer \\ inner for the dimming
        overlay."""
        out = []
        if inner.top() > outer.top():
            out.append(QRect(
                outer.left(), outer.top(),
                outer.width(), inner.top() - outer.top(),
            ))
        if inner.bottom() < outer.bottom():
            out.append(QRect(
                outer.left(), inner.bottom() + 1,
                outer.width(), outer.bottom() - inner.bottom(),
            ))
        if inner.left() > outer.left():
            out.append(QRect(
                outer.left(), inner.top(),
                inner.left() - outer.left(), inner.height(),
            ))
        if inner.right() < outer.right():
            out.append(QRect(
                inner.right() + 1, inner.top(),
                outer.right() - inner.right(), inner.height(),
            ))
        return out

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if (event.button() != Qt.MouseButton.LeftButton
                or self._displayed_pixmap is None):
            return
        # Translate to pixmap coords.
        pt = event.position().toPoint() - self._pix_origin
        if not QRect(QPoint(0, 0), self._displayed_pixmap.size()).contains(pt):
            return
        self._sel_start = pt
        self._sel_end = pt
        self._sel_active = True
        self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if not self._sel_active or self._displayed_pixmap is None:
            return
        pt = event.position().toPoint() - self._pix_origin
        # Clamp to pixmap bounds.
        pt.setX(max(0, min(pt.x(), self._displayed_pixmap.width() - 1)))
        pt.setY(max(0, min(pt.y(), self._displayed_pixmap.height() - 1)))
        self._sel_end = pt
        self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self._sel_active = False
        # If the click didn't drag (tiny rect), treat as a clear.
        if not self.has_selection():
            self._sel_start = self._sel_end = None
        self.update()


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

        # Preview with drag-to-crop selection. Star Citizen typically
        # runs fullscreen so capturing the SC window grabs the whole
        # monitor; the user can drag a rectangle on the preview to
        # focus the vision parse on just the contract panel.
        self.preview_label = CroppablePreview()
        self.preview_label.setMinimumHeight(200)
        root.addWidget(self.preview_label, 1)

        crop_row = QHBoxLayout()
        self.crop_hint = QLabel(
            "💡 Drag a rectangle on the preview to crop to just the "
            "contract panel before parsing."
        )
        self.crop_hint.setProperty("muted", True)
        self.crop_hint.setWordWrap(True)
        crop_row.addWidget(self.crop_hint, 1)
        self.clear_crop_btn = QPushButton("Clear selection")
        self.clear_crop_btn.setProperty("flat", True)
        self.clear_crop_btn.clicked.connect(self.preview_label.clear_selection)
        crop_row.addWidget(self.clear_crop_btn)
        root.addLayout(crop_row)

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
        # Pre-select whatever the user previously saved in Settings →
        # Capture so the rapid-fire workflow always lands on the right
        # source without an extra click.
        self._restore_saved_source()

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

    def _restore_saved_source(self) -> None:
        settings = getattr(self.controller, "settings", None)
        if settings is None:
            return
        saved = settings.get("screen_capture_source") or {}
        if not saved:
            return
        wanted_kind = saved.get("kind")
        wanted_label = (saved.get("label") or "").lower()
        wanted_title = (saved.get("title") or wanted_label).lower()
        for i in range(self.source_combo.count()):
            src = self.source_combo.itemData(i)
            if not src or src.get("kind") != wanted_kind:
                continue
            label = (src.get("label") or "").lower()
            if wanted_kind == "monitor" and label == wanted_label:
                self.source_combo.setCurrentIndex(i)
                return
            if wanted_kind == "window" and wanted_title in label:
                self.source_combo.setCurrentIndex(i)
                return

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
        # CroppablePreview owns both the original image (for accurate
        # crop coords) and the displayed scaled pixmap.
        self.preview_label.set_image(image, pix)

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

        # Send only the user-selected crop if they dragged one;
        # otherwise the full capture.
        image_to_parse = self.preview_label.selected_image()
        if image_to_parse is None or image_to_parse.isNull():
            self.status_label.setText("Capture a screenshot first.")
            return
        try:
            png = _qimage_to_png_bytes(image_to_parse)
        except Exception as e:
            QMessageBox.warning(
                self, "Encoding failed",
                f"Could not encode captured image as PNG: {e}",
            )
            return
        cropped = self.preview_label.has_selection()
        size_note = (
            f"{image_to_parse.width()}×{image_to_parse.height()} (cropped)"
            if cropped else
            f"{image_to_parse.width()}×{image_to_parse.height()}"
        )

        self.parse_btn.setEnabled(False)
        self.capture_btn.setEnabled(False)
        self.status_label.setText(f"Parsing {size_note}…")

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
