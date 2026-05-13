"""
MobiglassCornerOverlay — paints the four cyan corner-taper accents
over a parent widget, matching the mockup at mockups/mobiglass-sketch.html.

QSS can't do `mask-image: radial-gradient(...)`, so the taper is done
in QPainter. The trick: stroke each L-shape with a pen whose brush is
a QRadialGradient centered on the corner anchor. The stroke is fully
opaque close to the corner and fades to fully transparent further
along each leg — same effect as the HTML mockup's radial mask, but
without any compositing pass (which was producing aliased hard-ended
strokes on the previous QPixmap-based implementation).

The overlay sizes to its parent via an event filter and is set
WA_TransparentForMouseEvents so it doesn't block clicks on the
underlying widget. It also reparents itself to the END of the
parent's child list (raise_()) so it draws on top.

Usage:
    MobiglassCornerOverlay(parent_widget)
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QRectF, Qt
from PySide6.QtGui import (
    QBrush, QColor, QPainter, QPainterPath, QPen, QRadialGradient,
)
from PySide6.QtWidgets import QWidget


class MobiglassCornerOverlay(QWidget):
    # Geometry — kept in sync with the HTML mockup.
    CORNER_SIZE = 90      # square footprint for each corner span
    CORNER_RADIUS = 22    # matches the panel's border-radius
    STROKE_WIDTH = 3
    SOLID_RADIUS = 32     # stroke is fully opaque within this radius
    FADE_RADIUS = 88      # ...fades to fully transparent by here

    def __init__(self, parent: QWidget, color: QColor | str = "#7befff"):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self._color = QColor(color) if not isinstance(color, QColor) else color
        # Track parent size + restack ourselves on top.
        parent.installEventFilter(self)
        self.setGeometry(0, 0, parent.width(), parent.height())
        self.raise_()

    # ── lifecycle ──────────────────────────────────────────────────────

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: N802
        if obj is self.parent() and event.type() in (
            QEvent.Type.Resize, QEvent.Type.Show, QEvent.Type.ChildAdded,
        ):
            p = self.parentWidget()
            if p is not None:
                self.setGeometry(0, 0, p.width(), p.height())
                self.raise_()
        return False

    # ── painting ───────────────────────────────────────────────────────

    def paintEvent(self, _event) -> None:  # noqa: N802
        w, h = self.width(), self.height()
        if w < 4 or h < 4:
            return

        # Shrink the corner footprint for short containers (e.g. the
        # top bar) so the fade completes within the container instead
        # of bleeding past — same trick the mockup pulls for
        # `.topbar .corner`.
        max_dim = min(w, h)
        size = min(self.CORNER_SIZE, max_dim)
        scale = size / self.CORNER_SIZE
        solid_r = self.SOLID_RADIUS * scale
        fade_r = self.FADE_RADIUS * scale
        radius = min(self.CORNER_RADIUS, size / 2)
        offset = self.STROKE_WIDTH / 2

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setBrush(Qt.BrushStyle.NoBrush)

        for cx, cy, path in (
            (0.0,      0.0,      self._tl_path(size, radius, offset)),
            (float(w), 0.0,      self._tr_path(w, size, radius, offset)),
            (0.0,      float(h), self._bl_path(h, size, radius, offset)),
            (float(w), float(h), self._br_path(w, h, size, radius, offset)),
        ):
            p.setPen(self._gradient_pen(cx, cy, solid_r, fade_r))
            p.drawPath(path)

        p.end()

    def _gradient_pen(
        self,
        cx: float,
        cy: float,
        solid_r: float,
        fade_r: float,
    ) -> QPen:
        # Cyan close to the corner, alpha 0 by `fade_r` away — sampled
        # per-pixel by the pen as it strokes the L-shape, so the line
        # itself fades out naturally without any masking pass.
        transparent = QColor(self._color)
        transparent.setAlpha(0)
        grad = QRadialGradient(cx, cy, fade_r)
        grad.setColorAt(0.0, self._color)
        grad.setColorAt(min(1.0, solid_r / fade_r), self._color)
        grad.setColorAt(1.0, transparent)
        return QPen(QBrush(grad), self.STROKE_WIDTH, Qt.PenStyle.SolidLine,
                    Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)

    # ── Corner paths (Qt angle convention: 0°=3 o'clock, +ve sweep=CCW) ──

    def _tl_path(
        self, size: float, radius: float, offset: float,
    ) -> QPainterPath:
        # Anchor at (0, 0). 9 o'clock → 12 o'clock, CW (sweep -90).
        path = QPainterPath()
        path.moveTo(offset, size)
        path.lineTo(offset, radius + offset)
        path.arcTo(QRectF(offset, offset, 2 * radius, 2 * radius), 180, -90)
        path.lineTo(size, offset)
        return path

    def _tr_path(
        self, w: int, size: float, radius: float, offset: float,
    ) -> QPainterPath:
        # Anchor at (w, 0). 12 o'clock → 3 o'clock, CW (sweep -90).
        path = QPainterPath()
        path.moveTo(w - size, offset)
        path.lineTo(w - radius - offset, offset)
        path.arcTo(QRectF(w - 2 * radius - offset, offset,
                          2 * radius, 2 * radius), 90, -90)
        path.lineTo(w - offset, size)
        return path

    def _bl_path(
        self, h: int, size: float, radius: float, offset: float,
    ) -> QPainterPath:
        # Anchor at (0, h). 9 o'clock → 6 o'clock, CCW (sweep +90).
        path = QPainterPath()
        path.moveTo(offset, h - size)
        path.lineTo(offset, h - radius - offset)
        path.arcTo(QRectF(offset, h - 2 * radius - offset,
                          2 * radius, 2 * radius), 180, 90)
        path.lineTo(size, h - offset)
        return path

    def _br_path(
        self, w: int, h: int, size: float, radius: float, offset: float,
    ) -> QPainterPath:
        # Anchor at (w, h). 3 o'clock → 6 o'clock, CW (sweep -90).
        path = QPainterPath()
        path.moveTo(w - offset, h - size)
        path.lineTo(w - offset, h - radius - offset)
        path.arcTo(QRectF(w - 2 * radius - offset, h - 2 * radius - offset,
                          2 * radius, 2 * radius), 0, -90)
        path.lineTo(w - size, h - offset)
        return path
