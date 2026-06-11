"""
CaptureQueueDialog — review screenshots gathered by Quick Capture.

The user fills the queue by repeatedly pressing the global hotkey
while in Star Citizen. This dialog lists every pending capture as a
thumbnail row with three buttons:

    [ Parse ]   Pops AddContractDialog prefilled from the vision
                parse and removes the entry on a successful save.
    [ View ]    Opens a one-off ScreenCaptureDialog seeded with the
                queued image so the user can crop manually before
                parsing.
    [  ✕  ]     Drops the entry from the queue without parsing.

The dialog stays open across parses so the user can blast through
the entire queue without re-opening it each time.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QLabel, QMessageBox, QPushButton,
    QScrollArea, QVBoxLayout, QWidget,
)


THUMB_WIDTH = 220


class CaptureQueueDialog(QDialog):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.queue = controller.capture_queue

        self.setWindowTitle("Capture Queue")
        self.resize(640, 520)

        root = QVBoxLayout(self)

        header = QHBoxLayout()
        self.count_label = QLabel("0 pending")
        self.count_label.setProperty("heading", True)
        header.addWidget(self.count_label, 1)
        clear_btn = QPushButton("Clear all")
        clear_btn.setProperty("flat", True)
        clear_btn.clicked.connect(self._on_clear_all)
        header.addWidget(clear_btn)
        root.addLayout(header)

        # Scrolling body — rebuilt on every queue change.
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._body = QWidget()
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setSpacing(8)
        self._scroll.setWidget(self._body)
        root.addWidget(self._scroll, 1)

        footer = QHBoxLayout()
        footer.addStretch(1)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        footer.addWidget(close_btn)
        root.addLayout(footer)

        self.queue.changed.connect(self._rebuild)
        self._rebuild()

    # ── slots ──────────────────────────────────────────────────────────

    def _rebuild(self, _depth: int = -1) -> None:
        # Wipe and rebuild — simpler than tracking row identity, and
        # the queue is short (<20 typical).
        while self._body_layout.count():
            item = self._body_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

        items = self.queue.items()
        self.count_label.setText(
            f"{len(items)} pending" if items else "Queue is empty"
        )
        if not items:
            empty = QLabel(
                "Press the Quick Capture hotkey while a contract is "
                "open in Star Citizen — screenshots will stack here "
                "for parsing."
            )
            empty.setProperty("muted", True)
            empty.setWordWrap(True)
            self._body_layout.addWidget(empty)
            self._body_layout.addStretch(1)
            return

        for it in items:
            self._body_layout.addWidget(self._build_row(it))
        self._body_layout.addStretch(1)

    def _build_row(self, item) -> QFrame:
        frame = QFrame()
        frame.setObjectName("card")
        row = QHBoxLayout(frame)
        row.setSpacing(10)

        thumb = QLabel()
        pix = QPixmap.fromImage(item.image)
        if pix.width() > THUMB_WIDTH:
            pix = pix.scaledToWidth(
                THUMB_WIDTH, Qt.TransformationMode.SmoothTransformation,
            )
        thumb.setPixmap(pix)
        thumb.setFixedWidth(THUMB_WIDTH)
        row.addWidget(thumb)

        meta = QLabel(
            f"<b>Capture #{item.id}</b><br>"
            f"<span style='color:#888'>{item.image.width()}×"
            f"{item.image.height()} from "
            f"{item.source_label or 'saved source'}</span>"
        )
        meta.setWordWrap(True)
        meta.setAlignment(Qt.AlignmentFlag.AlignTop)
        row.addWidget(meta, 1)

        buttons = QVBoxLayout()
        parse_btn = QPushButton("Parse →")
        parse_btn.clicked.connect(
            lambda _=False, it=item: self._parse_item(it)
        )
        buttons.addWidget(parse_btn)
        view_btn = QPushButton("Crop / View")
        view_btn.setProperty("flat", True)
        view_btn.clicked.connect(
            lambda _=False, it=item: self._view_item(it)
        )
        buttons.addWidget(view_btn)
        drop_btn = QPushButton("✕ Discard")
        drop_btn.setProperty("flat", True)
        drop_btn.clicked.connect(
            lambda _=False, it=item: self.queue.remove(it.id)
        )
        buttons.addWidget(drop_btn)
        buttons_w = QWidget()
        buttons_w.setLayout(buttons)
        row.addWidget(buttons_w)

        return frame

    # ── per-item actions ───────────────────────────────────────────────

    def _parse_item(self, item) -> None:
        """Run the vision parser on the queued image; on success pop
        AddContractDialog and (when the user saves it) drop the entry
        from the queue."""
        from ...vision_parser import parse_contract_from_image
        from .add_contract import AddContractDialog
        from .screen_capture import _qimage_to_png_bytes

        api_key = getattr(self.controller, "api_key", None)
        if not api_key:
            QMessageBox.warning(
                self, "Missing API key",
                "OpenAI API key not set — open Settings → OpenAI.",
            )
            return
        try:
            png = _qimage_to_png_bytes(item.image)
            data = parse_contract_from_image(png, api_key)
        except Exception as exc:                            # noqa: BLE001
            QMessageBox.warning(self, "Parse failed", str(exc))
            return
        if not isinstance(data, dict) or "error" in data:
            QMessageBox.warning(
                self, "No contract found",
                str(data.get("error") if isinstance(data, dict) else data),
            )
            return

        dlg = AddContractDialog(self.controller, parent=self)
        dlg._apply_parsed_contract(data)
        if dlg.exec():
            try:
                self.controller.add_contract(dlg.value())
            except Exception as exc:                        # noqa: BLE001
                QMessageBox.warning(self, "Add contract failed", str(exc))
                return
            self.queue.remove(item.id)

    def _view_item(self, item) -> None:
        """Hand the image to a full ScreenCaptureDialog so the user
        can drag-crop and review before parsing."""
        from .screen_capture import ScreenCaptureDialog
        dlg = ScreenCaptureDialog(self.controller, parent=self)
        dlg._captured_image = item.image
        dlg._show_preview(item.image)
        dlg.parse_btn.setEnabled(True)
        dlg.status_label.setText(
            f"Loaded capture #{item.id} from the queue. Drag to crop "
            "if needed, then Parse."
        )

        def _on_parsed(data):
            from .add_contract import AddContractDialog
            ac = AddContractDialog(self.controller, parent=self)
            ac._apply_parsed_contract(data)
            if ac.exec():
                try:
                    self.controller.add_contract(ac.value())
                except Exception as exc:                    # noqa: BLE001
                    QMessageBox.warning(self, "Add contract failed", str(exc))
                    return
                self.queue.remove(item.id)

        dlg.contract_parsed.connect(_on_parsed)
        dlg.exec()

    def _on_clear_all(self) -> None:
        if not len(self.queue):
            return
        confirm = QMessageBox.question(
            self, "Clear capture queue",
            f"Discard all {len(self.queue)} pending screenshot(s)?",
        )
        if confirm == QMessageBox.StandardButton.Yes:
            self.queue.clear()
