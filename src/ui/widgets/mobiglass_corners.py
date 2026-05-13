"""
MobiglassCornerOverlay — paints the four cyan corner-taper accents
over a parent widget, matching the mockup at mockups/mobiglass-sketch.html.

QSS can't do `mask-image: radial-gradient(...)`, so the taper is
done in QPainter:

    1. Render each corner as an L-shape stroke into a temporary
       QPixmap.
    2. Apply a radial alpha gradient with CompositionMode_DestinationIn
       so the stroke is fully opaque near the corner and fades to
       transparent further along each leg.

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
    QBrush, QColor, QPainter, QPainterPath, QPen, QPixmap, QRadialGradient,
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
        # Each corner gets its own pre-rendered pixmap so the
        # radial-gradient alpha mask works cleanly via DestinationIn.
        w, h = self.width(), self.height()
        if w < 4 or h < 4:
            return

        # Shrink the corner footprint for short containers (e.g. the
        # top bar) so the fade completes within the container height
        # instead of bleeding past — same trick the mockup pulls for
        # `.topbar .corner`.
        max_dim = min(w, h)
        size = min(self.CORNER_SIZE, max_dim)
        # Scale the fade thresholds in proportion to the corner size.
        scale = size / self.CORNER_SIZE
        solid_r = self.SOLID_RADIUS * scale
        fade_r = self.FADE_RADIUS * scale
        radius = min(self.CORNER_RADIUS, size / 2)

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        for which in ("tl", "tr", "bl", "br"):
            pix = self._render_corner(size, radius, solid_r, fade_r, which)
            if which == "tl":
                p.drawPixmap(0, 0, pix)
            elif which == "tr":
                p.drawPixmap(w - size, 0, pix)
            elif which == "bl":
                p.drawPixmap(0, h - size, pix)
            else:
                p.drawPixmap(w - size, h - size, pix)
        p.end()

    def _render_corner(
        self,
        size: int,
        radius: float,
        solid_r: float,
        fade_r: float,
        which: str,
    ) -> QPixmap:
        pix = QPixmap(size, size)
        pix.fill(Qt.GlobalColor.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # 1. Stroke the L-shape that hugs the rounded corner. Each
        #    corner picks two legs + one quarter arc.
        pen = QPen(self._color, self.STROKE_WIDTH, Qt.PenStyle.SolidLine,
                   Qt.PenCapStyle.FlatCap, Qt.PenJoinStyle.MiterJoin)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)

        path = QPainterPath()
        offset = self.STROKE_WIDTH / 2     # so the stroke stays inside the pix
        if which == "tl":
            # leg down ←→ arc ←→ leg right
            path.moveTo(offset, size)
            path.lineTo(offset, radius)
            path.arcTo(QRectF(offset, offset, 2 * radius, 2 * radius), 180, 90)
            path.lineTo(size, offset)
            grad_center = (0.0, 0.0)
        elif which == "tr":
            path.moveTo(0, offset)
            path.lineTo(size - radius, offset)
            path.arcTo(QRectF(size - 2 * radius - offset, offset,
                              2 * radius, 2 * radius), 90, -90)
            path.lineTo(size - offset, size)
            grad_center = (float(size), 0.0)
        elif which == "bl":
            path.moveTo(offset, 0)
            path.lineTo(offset, size - radius)
            path.arcTo(QRectF(offset, size - 2 * radius - offset,
                              2 * radius, 2 * radius), 180, -90)
            path.lineTo(size, size - offset)
            grad_center = (0.0, float(size))
        else:   # br
            path.moveTo(size, offset)
            path.lineTo(size - radius, size - offset)
            # easier as two lines + arc:
            path = QPainterPath()
            path.moveTo(size - offset, 0)
            path.lineTo(size - offset, size - radius)
            path.arcTo(QRectF(size - 2 * radius - offset,
                              size - 2 * radius - offset,
                              2 * radius, 2 * radius), 0, -90)
            path.lineTo(0, size - offset)
            grad_center = (float(size), float(size))
        p.drawPath(path)

        # 2. Apply a radial-gradient alpha mask so the stroke fades
        #    out as it moves away from the corner. DestinationIn keeps
        #    only the parts of what's already drawn where this gradient
        #    has alpha.
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
        grad = QRadialGradient(grad_center[0], grad_center[1], fade_r)
        grad.setColorAt(0.0, QColor(0, 0, 0, 255))
        grad.setColorAt(min(1.0, solid_r / fade_r), QColor(0, 0, 0, 255))
        grad.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.fillRect(0, 0, size, size, QBrush(grad))
        p.end()

        return pix
