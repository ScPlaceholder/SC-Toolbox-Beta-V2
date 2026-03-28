"""
SCButton – QPushButton with hover glow animation.

On hover, a coloured QGraphicsDropShadowEffect fades in.
On leave, it fades out.  The glow colour defaults to the brand accent
but can be set per-button (e.g., per-tool accent colour).
"""

from __future__ import annotations
from typing import Optional

from PySide6.QtCore import Qt, QPropertyAnimation, QEasingCurve
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import QPushButton, QWidget, QGraphicsDropShadowEffect

from shared.qt.theme import P


class SCButton(QPushButton):
    """Push button with animated hover glow."""

    def __init__(
        self,
        text: str = "",
        parent: Optional[QWidget] = None,
        glow_color: str = "",
        glow_radius: int = 12,
    ):
        super().__init__(text, parent)
        self.setCursor(Qt.PointingHandCursor)

        self._glow = QGraphicsDropShadowEffect(self)
        self._glow.setColor(QColor(glow_color or P.accent))
        self._glow.setBlurRadius(0)
        self._glow.setOffset(0, 0)
        self.setGraphicsEffect(self._glow)

        self._max_radius = glow_radius

        self._anim = QPropertyAnimation(self._glow, b"blurRadius", self)
        self._anim.setDuration(150)
        self._anim.setEasingCurve(QEasingCurve.InOutQuad)

    def set_glow_color(self, color: str) -> None:
        self._glow.setColor(QColor(color))

    def enterEvent(self, event):
        self._anim.stop()
        self._anim.setStartValue(self._glow.blurRadius())
        self._anim.setEndValue(self._max_radius)
        self._anim.start()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._anim.stop()
        self._anim.setStartValue(self._glow.blurRadius())
        self._anim.setEndValue(0)
        self._anim.start()
        super().leaveEvent(event)
