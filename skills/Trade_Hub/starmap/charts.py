"""Lightweight QPainter charts for the commodity detail page.

A multi-series time LineChart and a BarChart, themed to match the toolbox. No
charting library — pure QPainter, render-on-demand (no timers)."""
from __future__ import annotations

import datetime
from typing import Callable, List, Optional, Tuple

from PySide6.QtCore import Qt, QPointF, QRectF
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QWidget

from shared.qt.theme import P

Series = dict   # {"points": [(x, y)], "color": str, "width": float, "name": str}


def _date_label(epoch: float) -> str:
    try:
        return datetime.date.fromtimestamp(epoch).strftime("%b %d")
    except (OSError, ValueError, OverflowError):
        return ""


class LineChart(QWidget):
    def __init__(self, title: str = "", subtitle: str = "", parent=None) -> None:
        super().__init__(parent)
        self._title = title
        self._subtitle = subtitle
        self._series: List[Series] = []
        self._yfmt: Callable[[float], str] = lambda v: f"{v:,.0f}"
        self._time_x = True
        self.setMinimumHeight(190)

    def set_series(self, series: List[Series], yfmt: Optional[Callable] = None,
                   time_x: bool = True) -> None:
        self._series = series or []
        if yfmt:
            self._yfmt = yfmt
        self._time_x = time_x
        self.update()

    def paintEvent(self, _ev) -> None:
        w, h = self.width(), self.height()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.fillRect(self.rect(), QColor(P.bg_secondary))

        ft = QFont(); ft.setPointSizeF(10.5); ft.setBold(True); p.setFont(ft)
        p.setPen(QColor(P.fg))
        p.drawText(12, 20, self._title)
        if self._subtitle:
            fs = QFont(); fs.setPointSizeF(8.5); p.setFont(fs)
            p.setPen(QColor(P.fg_dim))
            sw = p.fontMetrics().horizontalAdvance(self._subtitle)
            p.drawText(w - sw - 12, 19, self._subtitle)

        ml, mr, mt, mb = 58, 14, 32, 26
        plot = QRectF(ml, mt, max(10, w - ml - mr), max(10, h - mt - mb))

        pts_all = [pt for s in self._series for pt in s.get("points", [])]
        if len(pts_all) < 2:
            p.setPen(QColor(P.fg_disabled))
            msg = "no history yet" if not pts_all else "1 snapshot so far —\nneed 2+ days for a trend"
            p.drawText(plot, Qt.AlignCenter | Qt.TextWordWrap, msg)
            p.end()
            return

        xs = [pt[0] for pt in pts_all]
        ys = [pt[1] for pt in pts_all]
        xmin, xmax = min(xs), max(xs)
        ymin, ymax = min(ys), max(ys)
        if xmax == xmin:
            xmax = xmin + 1
        rng = (ymax - ymin) or max(1.0, abs(ymax) * 0.1)
        ymin -= rng * 0.12
        ymax += rng * 0.12

        def sx(x: float) -> float:
            return plot.left() + (x - xmin) / (xmax - xmin) * plot.width()

        def sy(y: float) -> float:
            return plot.bottom() - (y - ymin) / (ymax - ymin) * plot.height()

        # Y grid + labels
        fg = QFont(); fg.setPointSizeF(7.5); p.setFont(fg)
        for i in range(5):
            yy = ymin + (ymax - ymin) * i / 4.0
            py = sy(yy)
            pen = QPen(QColor(P.border)); pen.setWidthF(1.0)
            p.setPen(pen)
            p.drawLine(QPointF(plot.left(), py), QPointF(plot.right(), py))
            p.setPen(QColor(P.fg_dim))
            p.drawText(QRectF(0, py - 8, ml - 6, 16),
                       Qt.AlignRight | Qt.AlignVCenter, self._yfmt(yy))

        # X labels
        p.setPen(QColor(P.fg_dim))
        for i in range(4):
            xx = xmin + (xmax - xmin) * i / 3.0
            lbl = _date_label(xx) if self._time_x else f"{xx:,.0f}"
            px = sx(xx)
            p.drawText(QRectF(px - 34, plot.bottom() + 4, 68, 16),
                       Qt.AlignCenter, lbl)

        # Series
        for s in self._series:
            pts = s.get("points", [])
            if not pts:
                continue
            col = QColor(s.get("color", P.accent))
            qpts = [QPointF(sx(x), sy(y)) for (x, y) in pts]
            if len(qpts) >= 2:
                pen = QPen(col); pen.setWidthF(s.get("width", 2.2))
                pen.setJoinStyle(Qt.RoundJoin); pen.setCapStyle(Qt.RoundCap)
                p.setPen(pen)
                p.drawPolyline(QPolygonF(qpts))
            p.setPen(Qt.NoPen); p.setBrush(col)
            for q in qpts:
                p.drawEllipse(q, 2.6, 2.6)
        p.end()


class BarChart(QWidget):
    def __init__(self, title: str = "", color: str = "", parent=None) -> None:
        super().__init__(parent)
        self._title = title
        self._color = color or P.purple
        self._bars: List[Tuple[str, float]] = []
        self.setMinimumHeight(190)

    def set_bars(self, bars: List[Tuple[str, float]]) -> None:
        self._bars = bars or []
        self.update()

    def paintEvent(self, _ev) -> None:
        w, h = self.width(), self.height()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.fillRect(self.rect(), QColor(P.bg_secondary))

        ft = QFont(); ft.setPointSizeF(10.5); ft.setBold(True); p.setFont(ft)
        p.setPen(QColor(P.fg))
        p.drawText(12, 20, self._title)

        ml, mr, mt, mb = 44, 14, 32, 28
        plot = QRectF(ml, mt, max(10, w - ml - mr), max(10, h - mt - mb))
        if not self._bars:
            p.setPen(QColor(P.fg_disabled))
            p.drawText(plot, Qt.AlignCenter, "no reports yet")
            p.end()
            return

        vmax = (max((v for _l, v in self._bars), default=1.0) or 1.0) * 1.18  # headroom
        n = len(self._bars)
        gap = 10.0
        bw = min(64.0, (plot.width() - gap * (n - 1)) / n if n else plot.width())
        x0 = plot.left() + (plot.width() - (bw * n + gap * (n - 1))) / 2.0     # centre the bars
        fg = QFont(); fg.setPointSizeF(7.5); p.setFont(fg)
        for i, (label, v) in enumerate(self._bars):
            x = x0 + i * (bw + gap)
            bh = (v / vmax) * plot.height()
            rect = QRectF(x, plot.bottom() - bh, bw, bh)
            p.setPen(Qt.NoPen); p.setBrush(QColor(self._color))
            p.drawRoundedRect(rect, 2, 2)
            p.setPen(QColor(P.fg_dim))
            p.drawText(QRectF(x - 6, plot.bottom() + 4, bw + 12, 16),
                       Qt.AlignCenter, label)
            if v:
                p.setPen(QColor(P.fg))
                p.drawText(QRectF(x - 6, rect.top() - 15, bw + 12, 14),
                           Qt.AlignCenter, f"{v:,.0f}")
        p.end()
