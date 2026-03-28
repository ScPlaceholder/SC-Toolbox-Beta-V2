"""
HUD visual effect widgets – MobiGlas-style decorative elements.

  - HUDPanel: QFrame with painted corner brackets
  - GlowEffect: Utility to apply cyan glow to any widget
  - ScanlineOverlay: Atmospheric scan-line texture overlay
"""

from __future__ import annotations
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QPainter, QPen, QColor
from PySide6.QtWidgets import QFrame, QWidget, QGraphicsDropShadowEffect, QVBoxLayout

from shared.qt.theme import P


class HUDPanel(QFrame):
    """Panel with painted corner brackets and optional border glow.

    Corner brackets are L-shaped lines drawn at configurable corners,
    giving the MobiGlas holographic projection aesthetic.
    """

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        bracket_length: int = 12,
        bracket_color: str = "",
        show_brackets: bool = True,
        bg_color: str = "",
    ):
        super().__init__(parent)
        self._bracket_len = bracket_length
        self._bracket_color = QColor(bracket_color or P.border)
        self._show_brackets = show_brackets

        bg = bg_color or P.bg_card
        self.setStyleSheet(f"""
            HUDPanel {{
                background-color: {bg};
                border: none;
            }}
        """)

        self._inner_layout = QVBoxLayout(self)
        self._inner_layout.setContentsMargins(8, 8, 8, 8)
        self._inner_layout.setSpacing(4)

    @property
    def inner_layout(self) -> QVBoxLayout:
        return self._inner_layout

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self._show_brackets:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, False)
        pen = QPen(self._bracket_color, 1)
        painter.setPen(pen)

        w, h = self.width(), self.height()
        bl = self._bracket_len

        # Top-left bracket
        painter.drawLine(0, 0, bl, 0)
        painter.drawLine(0, 0, 0, bl)

        # Top-right bracket
        painter.drawLine(w - 1, 0, w - 1 - bl, 0)
        painter.drawLine(w - 1, 0, w - 1, bl)

        # Bottom-left bracket
        painter.drawLine(0, h - 1, bl, h - 1)
        painter.drawLine(0, h - 1, 0, h - 1 - bl)

        # Bottom-right bracket
        painter.drawLine(w - 1, h - 1, w - 1 - bl, h - 1)
        painter.drawLine(w - 1, h - 1, w - 1, h - 1 - bl)

        painter.end()


class GlowEffect:
    """Utility to apply a coloured glow (drop shadow) to any widget."""

    @staticmethod
    def apply(
        widget: QWidget,
        color: str = "",
        radius: int = 10,
        offset_x: int = 0,
        offset_y: int = 0,
    ) -> QGraphicsDropShadowEffect:
        effect = QGraphicsDropShadowEffect(widget)
        effect.setColor(QColor(color or P.accent))
        effect.setBlurRadius(radius)
        effect.setOffset(offset_x, offset_y)
        widget.setGraphicsEffect(effect)
        return effect

    @staticmethod
    def remove(widget: QWidget) -> None:
        widget.setGraphicsEffect(None)


class ScanlineOverlay(QWidget):
    """Transparent overlay painting horizontal scan lines for atmosphere.

    Place this over a content widget using a stacked layout or absolute
    positioning.  The overlay is mouse-transparent.
    """

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        line_spacing: int = 2,
        opacity: float = 0.04,
        color: str = "",
    ):
        super().__init__(parent)
        self._spacing = line_spacing
        self._opacity = opacity
        self._color = QColor(color or P.border)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setStyleSheet("background: transparent;")

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setOpacity(self._opacity)
        pen = QPen(self._color, 1)
        painter.setPen(pen)

        h = self.height()
        w = self.width()
        y = 0
        while y < h:
            painter.drawLine(0, y, w, y)
            y += self._spacing

        painter.end()
