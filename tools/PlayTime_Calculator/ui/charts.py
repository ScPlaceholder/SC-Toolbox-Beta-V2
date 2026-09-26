"""Interactive custom-painted charts for the PlayTime Calculator.

``BarChart`` renders a row of value-scaled bars with hover tooltips and
click-to-drill.  It works in two modes:

* ``fit_width=True``  — bars stretch to fill the widget (hours, weekdays,
  months, years: a known, small bar count).
* ``fit_width=False`` — fixed bar width; the widget grows as wide as needed
  and is meant to live inside a horizontal ``QScrollArea`` (per-day view with
  hundreds of bars).

The aesthetic follows the SC Toolbox MobiGlas palette and the same manual
QPainter approach used by the Mining Signals chart.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Optional

from PySide6.QtCore import Qt, Signal, QRect
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QFontMetrics
from PySide6.QtWidgets import QWidget, QSizePolicy

from shared.qt.theme import P


@dataclass
class Bar:
    label: str          # x-axis label (may be blank when too dense)
    value: float        # bar magnitude (seconds)
    key: str = ""       # opaque id emitted on click (e.g. ISO date)
    accent: bool = False  # draw highlighted (e.g. the record holder)


def _value_color(t: float, accent: bool) -> QColor:
    """Blue→cyan ramp by normalised value ``t`` in [0,1]."""
    if accent:
        return QColor("#ffcc44")
    r = int(40 + t * 30)
    g = int(120 + t * 90)
    b = int(180 + t * 60)
    return QColor(r, g, min(255, b))


class BarChart(QWidget):
    """Value-scaled bar chart with hover tooltip + click-to-drill."""

    bar_clicked = Signal(str)   # emits Bar.key
    bar_hovered = Signal(int)   # emits index, or -1 when leaving

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        fit_width: bool = True,
        bar_width: int = 8,
        value_fmt: Optional[Callable[[float], str]] = None,
        axis_fmt: Optional[Callable[[float], str]] = None,
        y_title: str = "",
    ) -> None:
        super().__init__(parent)
        self._bars: list[Bar] = []
        self._fit_width = fit_width
        self._bar_w = bar_width
        self._value_fmt = value_fmt or (lambda v: f"{v:.0f}")
        self._axis_fmt = axis_fmt or (lambda v: f"{v / 3600:.0f}h")
        self._y_title = y_title
        self._hover = -1
        self._max = 0.0
        self.setMouseTracking(True)
        self.setMinimumHeight(180)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_mode(self, fit_width: bool, bar_width: Optional[int] = None) -> None:
        """Switch between fit-to-width and fixed-width (scrollable) layout."""
        self._fit_width = fit_width
        if bar_width is not None:
            self._bar_w = bar_width
        self.set_bars(self._bars)

    # ── geometry constants ──
    _PAD_L = 54     # left gutter for y labels
    _PAD_R = 12
    _PAD_T = 16
    _PAD_B = 30     # bottom gutter for x labels
    _GAP = 2        # gap between bars (fixed-width mode)

    def set_bars(self, bars: list[Bar]) -> None:
        self._bars = bars
        self._max = max((b.value for b in bars), default=0.0)
        self._hover = -1
        if self._fit_width:
            # Fill the viewport (no horizontal scroll).
            self.setMinimumWidth(0)
        else:
            # Drive a horizontal scrollbar from the enclosing scroll area.
            total = self._PAD_L + self._PAD_R + max(1, len(bars)) * (self._bar_w + self._GAP)
            self.setMinimumWidth(total)
        self.update()

    def clear(self) -> None:
        self.set_bars([])

    # ── layout helpers ──

    def _plot_rect(self) -> QRect:
        return QRect(
            self._PAD_L, self._PAD_T,
            max(1, self.width() - self._PAD_L - self._PAD_R),
            max(1, self.height() - self._PAD_T - self._PAD_B),
        )

    def _bar_geom(self, i: int) -> Optional[QRect]:
        if not self._bars or self._max <= 0:
            return None
        pr = self._plot_rect()
        n = len(self._bars)
        if self._fit_width:
            slot = pr.width() / n
            bw = max(2.0, slot - self._GAP)
            x = pr.left() + i * slot
        else:
            slot = self._bar_w + self._GAP
            bw = self._bar_w
            x = pr.left() + i * slot
        v = self._bars[i].value
        h = (v / self._max) * pr.height() if self._max > 0 else 0
        h = max(0, h)
        top = pr.bottom() - h
        return QRect(int(x), int(top), max(1, int(bw)), int(h))

    def _bar_at(self, px: int) -> int:
        if not self._bars:
            return -1
        pr = self._plot_rect()
        if px < pr.left() or px > pr.right():
            return -1
        n = len(self._bars)
        if self._fit_width:
            slot = pr.width() / n
        else:
            slot = self._bar_w + self._GAP
        idx = int((px - pr.left()) // slot)
        return idx if 0 <= idx < n else -1

    # ── interaction ──

    def mouseMoveEvent(self, ev):
        idx = self._bar_at(int(ev.position().x()))
        if idx != self._hover:
            self._hover = idx
            self.bar_hovered.emit(idx)
            self.setCursor(Qt.PointingHandCursor if idx >= 0 else Qt.ArrowCursor)
            self.update()
        super().mouseMoveEvent(ev)

    def leaveEvent(self, ev):
        if self._hover != -1:
            self._hover = -1
            self.bar_hovered.emit(-1)
            self.update()
        super().leaveEvent(ev)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            idx = self._bar_at(int(ev.position().x()))
            if idx >= 0:
                self.bar_clicked.emit(self._bars[idx].key or self._bars[idx].label)
                ev.accept()
                return
        super().mousePressEvent(ev)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self.update()

    # ── painting ──

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, False)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        p.fillRect(self.rect(), QColor(P.bg_primary))

        pr = self._plot_rect()
        if not self._bars or self._max <= 0:
            p.setPen(QColor(P.fg_dim))
            p.setFont(QFont("Consolas", 10))
            p.drawText(self.rect(), Qt.AlignCenter, "No data for this view.")
            p.end()
            return

        # ── Y gridlines + labels (0, 25, 50, 75, 100% of max) ──
        p.setFont(QFont("Consolas", 7))
        fm = QFontMetrics(p.font())
        for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
            y = int(pr.bottom() - frac * pr.height())
            p.setPen(QPen(QColor(P.border), 1))
            p.drawLine(pr.left(), y, pr.right(), y)
            p.setPen(QColor(P.fg_dim))
            label = self._axis_fmt(self._max * frac)
            p.drawText(2, y - 2, self._PAD_L - 6, 12, Qt.AlignRight | Qt.AlignVCenter, label)

        # ── Bars ──
        for i, b in enumerate(self._bars):
            rect = self._bar_geom(i)
            if rect is None:
                continue
            t = b.value / self._max if self._max else 0
            col = _value_color(t, b.accent)
            if i == self._hover:
                col = col.lighter(140)
            if rect.height() <= 0:
                # Still draw a 1px stub so zero days are visible on the axis.
                p.fillRect(rect.left(), pr.bottom() - 1, rect.width(), 1,
                           QColor(P.border))
            else:
                p.fillRect(rect, col)
            if b.accent:
                p.setPen(QPen(QColor("#ffcc44"), 1))
                p.drawRect(rect.adjusted(0, 0, -1, 0))

        # ── X labels (adaptive density) ──
        p.setPen(QColor(P.fg_dim))
        p.setFont(QFont("Consolas", 7))
        fm = QFontMetrics(p.font())
        n = len(self._bars)
        # How many labels can we fit?  Base spacing on the widest real label
        # so short hour labels pack densely and long dates thin out.
        approx_slot = pr.width() / n
        widest = max((fm.horizontalAdvance(b.label) for b in self._bars if b.label), default=10)
        # ceil, not floor: int() let labels 1.9 slots wide print on every bar.
        every = max(1, math.ceil((widest + 8) / approx_slot)) if approx_slot > 0 else 1
        for i, b in enumerate(self._bars):
            if not b.label:
                continue
            if n > 1 and i % every != 0 and i != self._hover:
                continue
            rect = self._bar_geom(i)
            if rect is None:
                continue
            cx = rect.center().x()
            tw = fm.horizontalAdvance(b.label)
            p.drawText(int(cx - tw / 2), pr.bottom() + 4, tw + 4, 14,
                       Qt.AlignHCenter | Qt.AlignTop, b.label)

        # ── Hover tooltip ──
        if 0 <= self._hover < n:
            self._paint_tooltip(p, pr)

        # ── Optional y-axis title ──
        if self._y_title:
            p.setPen(QColor(P.fg_dim))
            p.setFont(QFont("Consolas", 7, QFont.Bold))
            p.drawText(2, 2, 120, 12, Qt.AlignLeft, self._y_title)

        p.end()

    def _paint_tooltip(self, p: QPainter, pr: QRect) -> None:
        b = self._bars[self._hover]
        rect = self._bar_geom(self._hover)
        if rect is None:
            return
        title = b.label or b.key or ""
        value = self._value_fmt(b.value)
        lines = [t for t in (title, value) if t]
        p.setFont(QFont("Consolas", 8, QFont.Bold))
        fm = QFontMetrics(p.font())
        tw = max(fm.horizontalAdvance(t) for t in lines) + 12
        th = len(lines) * (fm.height()) + 8
        tx = rect.center().x() - tw // 2
        ty = max(pr.top(), rect.top() - th - 6)
        tx = max(pr.left(), min(tx, pr.right() - tw))
        box = QRect(int(tx), int(ty), int(tw), int(th))
        bg = QColor(P.bg_header)
        bg.setAlpha(245)
        p.fillRect(box, bg)
        p.setPen(QPen(QColor(P.accent), 1))
        p.drawRect(box)
        p.setPen(QColor(P.fg_bright))
        y = box.top() + 4
        for idx, t in enumerate(lines):
            p.setPen(QColor(P.accent) if idx == 0 else QColor(P.fg_bright))
            p.drawText(box.left() + 6, y, tw - 12, fm.height(),
                       Qt.AlignLeft | Qt.AlignVCenter, t)
            y += fm.height()
