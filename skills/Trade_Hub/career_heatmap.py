"""Career commodity heatmap — one tile per commodity the trader has run,
coloured green→red by net result and labelled with finished (✓) / failed (✕)
counts. Click a tile to open that commodity's page. Pure QPainter heat grid.
"""
from __future__ import annotations

import math
from typing import List, Optional

from PySide6.QtCore import Qt, QRect, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from shared.qt.theme import P

_GREEN = ["#10241a", "#15452c", "#1d6e3f", "#27a85a", "#39e07a"]
_RED = ["#241010", "#4a1714", "#7a221c", "#a52f25", "#cf3d2e"]
_TILE_W = 170
_TILE_H = 60
_GAP = 6


def _short(v: float) -> str:
    a = abs(v)
    sign = "−" if v < 0 else ""
    if a >= 1e9:
        return f"{sign}{a / 1e9:.1f}B"
    if a >= 1e6:
        return f"{sign}{a / 1e6:.1f}M"
    if a >= 1e3:
        return f"{sign}{a / 1e3:.0f}k"
    return f"{sign}{a:.0f}"


def _heat(net: float, vmax: float) -> QColor:
    pal = _GREEN if net >= 0 else _RED
    if net == 0 or vmax <= 0:
        return QColor(pal[0])
    t = min(1.0, abs(net) / vmax)
    i = 1 if t < 0.15 else 2 if t < 0.40 else 3 if t < 0.70 else 4
    return QColor(pal[i])


class CareerHeatmap(QWidget):
    commodity_clicked = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._rows: List[dict] = []
        self._vmax = 1.0
        self._cols = 4
        self._tiles: list = []
        self._hover: Optional[str] = None
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMinimumHeight(80)

    def set_data(self, rows: List[dict]) -> None:
        self._rows = rows or []
        self._vmax = max((abs(r["net"]) for r in self._rows), default=1.0)
        self._recompute()
        self.update()

    def resizeEvent(self, ev) -> None:
        super().resizeEvent(ev)
        self._recompute()

    def _recompute(self) -> None:
        self._cols = max(1, (self.width() + _GAP) // (_TILE_W + _GAP))
        n = len(self._rows)
        rows_needed = math.ceil(n / self._cols) if n else 1
        self.setMinimumHeight(rows_needed * (_TILE_H + _GAP) + _GAP if n else 80)

    def _hit(self, pt) -> Optional[dict]:
        for rect, r in self._tiles:
            if rect.contains(pt):
                return r
        return None

    def mouseMoveEvent(self, ev) -> None:
        r = self._hit(ev.position().toPoint())
        self.setCursor(Qt.PointingHandCursor if r else Qt.ArrowCursor)
        name = r["commodity"] if r else None
        if name != self._hover:
            self._hover = name
            self.update()

    def mouseReleaseEvent(self, ev) -> None:
        if ev.button() == Qt.LeftButton:
            r = self._hit(ev.position().toPoint())
            if r:
                self.commodity_clicked.emit(r["commodity"])

    def leaveEvent(self, ev) -> None:
        if self._hover is not None:
            self._hover = None
            self.update()

    def paintEvent(self, ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.fillRect(self.rect(), QColor(P.bg_primary))
        self._tiles = []
        if not self._rows:
            p.setPen(QColor(P.fg_disabled))
            p.drawText(self.rect(), Qt.AlignCenter,
                       "No commodity runs yet — Complete or fail a route to populate this.")
            p.end()
            return
        cols = self._cols
        tw = (self.width() - _GAP - cols * _GAP) / cols if cols else self.width()
        for i, r in enumerate(self._rows):
            x = int(_GAP + (i % cols) * (tw + _GAP))
            y = int(_GAP + (i // cols) * (_TILE_H + _GAP))
            rect = QRect(x, y, int(tw), _TILE_H)
            self._tiles.append((rect, r))
            p.fillRect(rect, _heat(r["net"], self._vmax))
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor(P.fg_bright), 1.6) if r["commodity"] == self._hover
                     else QPen(QColor(P.border), 1))
            p.drawRect(rect)
            strong = self._vmax > 0 and abs(r["net"]) / self._vmax > 0.45
            txt = QColor(P.bg_deepest) if strong else QColor(P.fg_bright)
            inner = rect.adjusted(7, 5, -7, -5)
            p.setFont(QFont("Consolas", 9, QFont.Bold))
            fm = QFontMetrics(p.font())
            p.setPen(txt)
            p.drawText(inner, Qt.AlignLeft | Qt.AlignTop,
                       fm.elidedText(r["commodity"], Qt.ElideRight, inner.width() - 2))
            p.drawText(inner, Qt.AlignRight | Qt.AlignBottom, _short(r["net"]))
            p.setFont(QFont("Consolas", 8))
            sub = QColor(txt); sub.setAlpha(205); p.setPen(sub)
            p.drawText(inner, Qt.AlignLeft | Qt.AlignBottom, f"✓{r['finished']}  ✕{r['failed']}")
        p.end()
