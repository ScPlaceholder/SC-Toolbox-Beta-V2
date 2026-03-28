"""
SCWindow – holographic HUD-style window.

Translucent dark background with glowing cyan border lines, corner brackets,
and scan-line texture.  Looks like a projected MobiGlas interface floating
over the game.
"""

from __future__ import annotations
import logging
from typing import Optional

from PySide6.QtCore import Qt, QPoint, QSize, QTimer
from PySide6.QtGui import (
    QGuiApplication, QPainter, QColor, QPen, QBrush, QLinearGradient,
)
from PySide6.QtWidgets import QMainWindow, QWidget, QVBoxLayout

from shared.qt.theme import P

log = logging.getLogger(__name__)

_GRIP = 6
_EDGE_W = 1            # main border line width
_BRACKET_LEN = 18      # corner bracket arm length
_BRACKET_W = 2         # corner bracket line width
_GLOW_PASSES = 3       # number of glow bloom passes
_SCANLINE_SPACING = 2  # pixels between scan lines
_SCANLINE_ALPHA = 8    # 0-255, very subtle


class _HoloSurface(QWidget):
    """Paints the holographic HUD surface: translucent bg, glowing edges,
    corner brackets, and scan-line texture."""

    def __init__(self, parent=None, accent: str = ""):
        super().__init__(parent)
        self._accent_hex = accent or P.accent
        self._accent = QColor(self._accent_hex)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setStyleSheet("background: transparent;")

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, False)
        w, h = self.width(), self.height()
        r = self.rect()

        # ── 1. Translucent dark fill ──
        bg = QColor(P.bg_primary)
        bg.setAlpha(210)  # ~82% opaque — game shows through slightly
        painter.fillRect(r, bg)

        # ── 2. Scan lines ──
        scan_color = QColor(255, 255, 255, _SCANLINE_ALPHA)
        painter.setPen(QPen(scan_color, 1))
        y = 0
        while y < h:
            painter.drawLine(0, y, w, y)
            y += _SCANLINE_SPACING

        # ── 3. Glow bloom passes (outer to inner, decreasing alpha) ──
        for i in range(_GLOW_PASSES, 0, -1):
            glow = QColor(self._accent)
            glow.setAlpha(int(12 * i))  # 36, 24, 12
            painter.setPen(QPen(glow, 1))
            offset = i
            painter.drawRect(offset, offset, w - 1 - 2 * offset, h - 1 - 2 * offset)

        # ── 4. Main border line ──
        edge = QColor(self._accent)
        edge.setAlpha(140)
        painter.setPen(QPen(edge, _EDGE_W))
        painter.drawRect(0, 0, w - 1, h - 1)

        # ── 5. Top edge bright glow bar ──
        top_glow = QLinearGradient(0, 0, 0, 6)
        gc = QColor(self._accent)
        gc.setAlpha(50)
        top_glow.setColorAt(0.0, gc)
        gc2 = QColor(self._accent)
        gc2.setAlpha(0)
        top_glow.setColorAt(1.0, gc2)
        painter.fillRect(1, 1, w - 2, 6, top_glow)

        # ── 6. Corner brackets ──
        bracket_color = QColor(self._accent)
        bracket_color.setAlpha(220)
        pen = QPen(bracket_color, _BRACKET_W)
        painter.setPen(pen)
        bl = _BRACKET_LEN

        # Top-left
        painter.drawLine(0, 0, bl, 0)
        painter.drawLine(0, 0, 0, bl)
        # Top-right
        painter.drawLine(w - 1, 0, w - 1 - bl, 0)
        painter.drawLine(w - 1, 0, w - 1, bl)
        # Bottom-left
        painter.drawLine(0, h - 1, bl, h - 1)
        painter.drawLine(0, h - 1, 0, h - 1 - bl)
        # Bottom-right
        painter.drawLine(w - 1, h - 1, w - 1 - bl, h - 1)
        painter.drawLine(w - 1, h - 1, w - 1, h - 1 - bl)

        # ── 7. Corner bracket glow (bloom around brackets) ──
        bglow = QColor(self._accent)
        bglow.setAlpha(30)
        painter.setPen(QPen(bglow, _BRACKET_W + 4))
        # Top-left glow
        painter.drawLine(0, 0, bl, 0)
        painter.drawLine(0, 0, 0, bl)
        # Top-right glow
        painter.drawLine(w - 1, 0, w - 1 - bl, 0)
        painter.drawLine(w - 1, 0, w - 1, bl)
        # Bottom-left glow
        painter.drawLine(0, h - 1, bl, h - 1)
        painter.drawLine(0, h - 1, 0, h - 1 - bl)
        # Bottom-right glow
        painter.drawLine(w - 1, h - 1, w - 1 - bl, h - 1)
        painter.drawLine(w - 1, h - 1, w - 1, h - 1 - bl)

        painter.end()
        super().paintEvent(event)


class SCWindow(QMainWindow):
    """Frameless, always-on-top holographic HUD window."""

    def __init__(
        self,
        title: str = "SC Toolbox",
        width: int = 1000,
        height: int = 700,
        min_w: int = 400,
        min_h: int = 200,
        opacity: float = 0.95,
        always_on_top: bool = True,
        accent: str = "",
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)

        flags = Qt.FramelessWindowHint | Qt.WindowDoesNotAcceptFocus
        if always_on_top:
            flags |= Qt.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setWindowTitle(title)
        self.setMinimumSize(QSize(min_w, min_h))
        self.resize(width, height)
        self.setWindowOpacity(max(0.3, min(1.0, opacity)))

        self._central = _HoloSurface(self, accent=accent)
        self.setCentralWidget(self._central)
        self._layout = QVBoxLayout(self._central)
        self._layout.setContentsMargins(1, 1, 1, 1)  # 1px inside the border
        self._layout.setSpacing(0)

        self._resizing = False
        self._resize_edge = None
        self._drag_pos = QPoint()
        self.setMouseTracking(True)

    @property
    def content_layout(self) -> QVBoxLayout:
        return self._layout

    def set_opacity(self, value: float) -> None:
        self.setWindowOpacity(max(0.3, min(1.0, value)))

    def toggle_visibility(self) -> None:
        if self.isVisible():
            self.hide()
        else:
            self.show()
            self.raise_()

    def schedule(self, delay_ms: int, fn) -> None:
        QTimer.singleShot(delay_ms, fn)

    def move_to(self, x: int, y: int) -> None:
        screen = QGuiApplication.primaryScreen()
        if screen:
            geom = screen.availableGeometry()
            x = max(geom.x(), min(x, geom.right() - self.width()))
            y = max(geom.y(), min(y, geom.bottom() - self.height()))
        self.move(x, y)

    def restore_geometry_from_args(self, x: int, y: int, w: int, h: int, opacity: float) -> None:
        screen = QGuiApplication.primaryScreen()
        if screen:
            sg = screen.availableGeometry()
            x = max(sg.x(), min(x, sg.right() - w))
            y = max(sg.y(), min(y, sg.bottom() - h))
        self.resize(w, h)
        self.move(x, y)
        self.set_opacity(opacity)

    def get_geometry_dict(self, prefix: str = "") -> dict:
        pos = self.pos()
        size = self.size()
        return {
            f"{prefix}x": pos.x(),
            f"{prefix}y": pos.y(),
            f"{prefix}w": size.width(),
            f"{prefix}h": size.height(),
            f"{prefix}opacity": self.windowOpacity(),
        }

    # ── Resize handling ──

    def _edge_at(self, pos: QPoint) -> Optional[str]:
        r = self.rect()
        x, y = pos.x(), pos.y()
        on_left = x < _GRIP
        on_right = x > r.width() - _GRIP
        on_top = y < _GRIP
        on_bottom = y > r.height() - _GRIP
        if on_top and on_left: return "tl"
        if on_top and on_right: return "tr"
        if on_bottom and on_left: return "bl"
        if on_bottom and on_right: return "br"
        if on_top: return "t"
        if on_bottom: return "b"
        if on_left: return "l"
        if on_right: return "r"
        return None

    _EDGE_CURSORS = {
        "t": Qt.SizeVerCursor, "b": Qt.SizeVerCursor,
        "l": Qt.SizeHorCursor, "r": Qt.SizeHorCursor,
        "tl": Qt.SizeFDiagCursor, "br": Qt.SizeFDiagCursor,
        "tr": Qt.SizeBDiagCursor, "bl": Qt.SizeBDiagCursor,
    }

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            edge = self._edge_at(event.position().toPoint())
            if edge:
                self._resizing = True
                self._resize_edge = edge
                self._drag_pos = event.globalPosition().toPoint()
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._resizing and self._resize_edge:
            delta = event.globalPosition().toPoint() - self._drag_pos
            self._drag_pos = event.globalPosition().toPoint()
            geom = self.geometry()
            edge = self._resize_edge
            mw, mh = self.minimumWidth(), self.minimumHeight()
            if "r" in edge: geom.setWidth(max(mw, geom.width() + delta.x()))
            if "b" in edge: geom.setHeight(max(mh, geom.height() + delta.y()))
            if "l" in edge:
                nw = max(mw, geom.width() - delta.x())
                if nw != geom.width(): geom.setLeft(geom.left() + (geom.width() - nw))
            if "t" in edge:
                nh = max(mh, geom.height() - delta.y())
                if nh != geom.height(): geom.setTop(geom.top() + (geom.height() - nh))
            self.setGeometry(geom)
            event.accept()
            return
        edge = self._edge_at(event.position().toPoint())
        if edge:
            self.setCursor(self._EDGE_CURSORS.get(edge, Qt.ArrowCursor))
        else:
            self.unsetCursor()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._resizing:
            self._resizing = False
            self._resize_edge = None
            event.accept()
            return
        super().mouseReleaseEvent(event)
