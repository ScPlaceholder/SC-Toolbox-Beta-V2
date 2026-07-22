"""Custom widgets for the Mouse Blocker tool.

BlockerHoloSurface — replaces SCWindow's default _HoloSurface.  Same
holographic edge treatment (border, corner brackets, scan grip marks)
but with no opaque body fill, brighter glow passes, and a heavier
border so the screen edge is unmistakable.

BlockerBody — fills the area below the title bar.  Paints a slowly
pulsing "MOUSE BLOCKER ACTIVE" headline plus a subtitle, and absorbs
every mouse / wheel event so accidental clicks never reach the game
underneath.
"""
from __future__ import annotations

from PySide6.QtCore import (
    Qt, QPropertyAnimation, QEasingCurve, Property, QRect, QAbstractAnimation,
)
from PySide6.QtGui import QPainter, QColor, QPen, QFont, QLinearGradient
from PySide6.QtWidgets import QWidget

from shared.qt.theme import P


# Match SCWindow's grip / bracket dimensions so the visual edge zone
# lines up with the actual draggable resize area.
_BRACKET_LEN = 22
_BRACKET_W = 3
_GRIP = 14


class BlockerHoloSurface(QWidget):
    """Frame surface for the blocker window.

    Drawn as the central widget of the BlockerWindow.  Body is fully
    transparent (no dark fill) so Star Citizen shows through; only the
    perimeter glow, corner brackets, and grip marks are painted.
    """

    def __init__(self, parent: QWidget | None = None, accent: str = "#ff3355"):
        super().__init__(parent)
        self._accent_hex = accent
        self._accent = QColor(accent)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setStyleSheet("background: transparent;")

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, False)
        w, h = self.width(), self.height()

        # NOTE: no full-rect alpha=2 hit-test fill here.  The title bar
        # paints the top strip opaque and BlockerBody covers everything
        # below it (and does its own alpha=2 fill), so the HoloSurface
        # only shows through the 1px contentsMargin rim — not worth
        # paying for a fullscreen fill on launch.

        # ── 1. Outer-to-inner glow bloom (2 passes — was 5) ──
        for i in range(2, 0, -1):
            glow = QColor(self._accent)
            glow.setAlpha(60 + 40 * (2 - i))  # 60, 100
            painter.setPen(QPen(glow, 1))
            o = i
            painter.drawRect(o, o, w - 1 - 2 * o, h - 1 - 2 * o)

        # ── 2. Main border line (bright) ──
        edge = QColor(self._accent)
        edge.setAlpha(230)
        painter.setPen(QPen(edge, 2))
        painter.drawRect(0, 0, w - 1, h - 1)

        # ── 3. Top + bottom edge bright glow gradients ──
        gc_strong = QColor(self._accent)
        gc_strong.setAlpha(90)
        gc_clear = QColor(self._accent)
        gc_clear.setAlpha(0)

        top_grad = QLinearGradient(0, 0, 0, 10)
        top_grad.setColorAt(0.0, gc_strong)
        top_grad.setColorAt(1.0, gc_clear)
        painter.fillRect(1, 1, w - 2, 10, top_grad)

        bot_grad = QLinearGradient(0, h - 11, 0, h - 1)
        bot_grad.setColorAt(0.0, gc_clear)
        bot_grad.setColorAt(1.0, gc_strong)
        painter.fillRect(1, h - 11, w - 2, 10, bot_grad)

        # Side glow strips (left + right)
        left_grad = QLinearGradient(0, 0, 10, 0)
        left_grad.setColorAt(0.0, gc_strong)
        left_grad.setColorAt(1.0, gc_clear)
        painter.fillRect(1, 1, 10, h - 2, left_grad)

        right_grad = QLinearGradient(w - 11, 0, w - 1, 0)
        right_grad.setColorAt(0.0, gc_clear)
        right_grad.setColorAt(1.0, gc_strong)
        painter.fillRect(w - 11, 1, 10, h - 2, right_grad)

        # ── 4. Corner brackets ──
        bl = _BRACKET_LEN
        corners = [
            (0, 0, bl, 0), (0, 0, 0, bl),                                  # TL
            (w - 1, 0, w - 1 - bl, 0), (w - 1, 0, w - 1, bl),              # TR
            (0, h - 1, bl, h - 1), (0, h - 1, 0, h - 1 - bl),              # BL
            (w - 1, h - 1, w - 1 - bl, h - 1),                             # BR
            (w - 1, h - 1, w - 1, h - 1 - bl),
        ]

        # Outer halo first (wide, faint)
        halo = QColor(self._accent)
        halo.setAlpha(80)
        painter.setPen(QPen(halo, _BRACKET_W + 6))
        for (x0, y0, x1, y1) in corners:
            painter.drawLine(x0, y0, x1, y1)

        # Inner bracket on top (sharp, bright)
        sharp = QColor(self._accent)
        sharp.setAlpha(255)
        painter.setPen(QPen(sharp, _BRACKET_W))
        for (x0, y0, x1, y1) in corners:
            painter.drawLine(x0, y0, x1, y1)

        # ── 5. Diagonal grip hash marks at each corner ──
        grip = QColor(self._accent)
        grip.setAlpha(140)
        painter.setPen(QPen(grip, 1))
        for d in range(3, _GRIP, 4):
            painter.drawLine(w - 1 - d, h - 1, w - 1, h - 1 - d)  # BR
            painter.drawLine(d, h - 1, 0, h - 1 - d)              # BL
            painter.drawLine(w - 1 - d, 0, w - 1, d)              # TR
            painter.drawLine(d, 0, 0, d)                          # TL

        painter.end()


class BlockerBody(QWidget):
    """Body widget for the Mouse Blocker.

    * Fully transparent visually — Star Citizen shows through unaltered.
    * Captures every mouse / wheel event so clicks don't pass to the game.
    * Paints a slowly pulsing 'MOUSE BLOCKER ACTIVE' headline plus a
      smaller subtitle line.
    """

    PULSE_MS = 2200  # one pulse cycle (dim → bright)

    def __init__(self, accent_color: str = "#ff3355", parent: QWidget | None = None):
        super().__init__(parent)
        self._accent = QColor(accent_color)
        self._pulse: float = 1.0  # 0.0 .. 1.0, drives glow intensity

        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_NoMousePropagation, True)
        self.setMouseTracking(True)
        self.setStyleSheet("background: transparent;")

        # Slow ping-pong pulse animation.
        # Note: setLoopCount(-1) makes Qt loop forward only and never
        # fires `finished`, so we run a single forward leg and flip
        # direction in the finished slot to get a triangle / sine wave.
        self._anim = QPropertyAnimation(self, b"pulse", self)
        self._anim.setDuration(self.PULSE_MS)
        self._anim.setStartValue(0.35)
        self._anim.setEndValue(1.0)
        self._anim.setEasingCurve(QEasingCurve.InOutSine)
        self._anim.setLoopCount(1)
        self._anim.finished.connect(self._reverse_anim)

    # ── Custom Qt property so QPropertyAnimation can drive it ──
    def _get_pulse(self) -> float:
        return self._pulse

    def _set_pulse(self, v: float) -> None:
        self._pulse = max(0.0, min(1.0, float(v)))
        self.update()

    pulse = Property(float, _get_pulse, _set_pulse)

    def _reverse_anim(self) -> None:
        # QPropertyAnimation.setLoopCount(-1) loops forward only; manually
        # reverse direction so the value oscillates between start and end.
        d = self._anim.direction()
        new = (QAbstractAnimation.Backward
               if d == QAbstractAnimation.Forward
               else QAbstractAnimation.Forward)
        self._anim.setDirection(new)
        self._anim.start()

    def start(self) -> None:
        self._anim.setDirection(QAbstractAnimation.Forward)
        self._anim.start()

    def stop(self) -> None:
        self._anim.stop()

    # ── Mouse / wheel event absorption ──
    def mousePressEvent(self, event):
        event.accept()

    def mouseReleaseEvent(self, event):
        event.accept()

    def mouseMoveEvent(self, event):
        event.accept()

    def mouseDoubleClickEvent(self, event):
        event.accept()

    def wheelEvent(self, event):
        event.accept()

    # ── Painting ──
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.TextAntialiasing, True)

        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            painter.end()
            return

        # Near-invisible hit-test fill — see BlockerHoloSurface.paintEvent
        # for why this is required.  Without it, the empty middle of the
        # body has alpha=0 and DWM passes clicks through to the game.
        painter.fillRect(self.rect(), QColor(0, 0, 0, 2))

        # Headline font size scales gently with window width so it still
        # looks reasonable at smaller sizes.
        head_pt = max(14, min(36, w // 52))

        head_font = QFont("Electrolize")
        head_font.setStyleHint(QFont.Monospace)
        head_font.setFamilies(["Electrolize", "Consolas", "monospace"])
        head_font.setPointSize(head_pt)
        head_font.setBold(True)
        head_font.setLetterSpacing(QFont.AbsoluteSpacing, max(4, head_pt // 5))
        painter.setFont(head_font)

        text = "MOUSE BLOCKER ACTIVE"
        full_rect = self.rect()

        # Compact radial halo for the headline — 2 radii × 4 cardinal
        # directions = 8 text draws (was 4 × 8 = 32) so the 60Hz pulse
        # animation stays cheap on a fullscreen surface.
        halo_color = QColor(self._accent)
        halo_color.setAlpha(int(70 * self._pulse))
        painter.setPen(halo_color)
        for r in (4, 2):
            for dx, dy in ((-r, 0), (r, 0), (0, -r), (0, r)):
                painter.drawText(full_rect.adjusted(dx, dy, dx, dy),
                                 Qt.AlignCenter, text)

        # Bright headline on top
        bright = QColor(self._accent)
        bright.setAlpha(int(255 * (0.55 + 0.45 * self._pulse)))
        painter.setPen(bright)
        painter.drawText(full_rect, Qt.AlignCenter, text)

        # Subtitle below the headline
        sub_pt = max(9, head_pt // 4)
        sub_font = QFont("Consolas")
        sub_font.setStyleHint(QFont.Monospace)
        sub_font.setFamilies(["Consolas", "monospace"])
        sub_font.setPointSize(sub_pt)
        sub_font.setLetterSpacing(QFont.AbsoluteSpacing, 3)
        painter.setFont(sub_font)

        sub_color = QColor(P.green)
        sub_color.setAlpha(int(255 * (0.7 + 0.3 * self._pulse)))
        painter.setPen(sub_color)

        sub_y = h // 2 + head_pt + 18
        sub_rect = QRect(0, sub_y, w, sub_pt * 3)
        painter.drawText(sub_rect, Qt.AlignHCenter | Qt.AlignTop,
                         "Star Citizen input is currently blocked")

        painter.end()
