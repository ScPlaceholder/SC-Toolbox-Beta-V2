"""Career calendar — a month / year heat grid of net trade profit per day, in
the same visual style as the PlayTime calendar. Green = a profitable day, red =
a net-loss day; click a day to inspect it. Self-contained (no cross-tool deps).
"""
from __future__ import annotations

import calendar as _cal
from datetime import date, timedelta
from typing import Dict, Optional

from PySide6.QtCore import Qt, QRect, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from shared.qt.theme import P

_WD = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_MO = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_GREEN = ["#10241a", "#15452c", "#1d6e3f", "#27a85a", "#39e07a"]
_RED = "#b83b2d"


def _short(v: float) -> str:
    a = abs(v)
    if a >= 1e9:
        return f"{v / 1e9:.1f}B"
    if a >= 1e6:
        return f"{v / 1e6:.1f}M"
    if a >= 1e3:
        return f"{v / 1e3:.0f}k"
    return f"{v:.0f}"


def _heat(v: float, vmax: float) -> QColor:
    if v < 0:
        return QColor(_RED)
    if v <= 0 or vmax <= 0:
        return QColor(_GREEN[0])
    t = v / vmax
    i = 1 if t < 0.15 else 2 if t < 0.40 else 3 if t < 0.70 else 4
    return QColor(_GREEN[i])


def _today() -> Optional[date]:
    try:
        return date.today()
    except Exception:
        return None


class _Grid(QWidget):
    day_clicked = Signal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._by: Dict[date, float] = {}
        self._vmax = 1.0
        self._mode = "month"
        t = _today() or date(2026, 6, 1)
        self._year, self._month = t.year, t.month
        self._sel: Optional[date] = None
        self._hover: Optional[date] = None
        self._cells: list = []
        self.setMouseTracking(True)
        self.setMinimumHeight(240)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_data(self, by, vmax) -> None:
        self._by = by; self._vmax = max(vmax, 1.0); self.update()

    def set_view(self, mode, year, month=None) -> None:
        self._mode = mode; self._year = year
        if month:
            self._month = month
        self.update()

    def set_selected(self, d) -> None:
        self._sel = d; self.update()

    def mode(self) -> str:
        return self._mode

    def current(self):
        return self._year, self._month

    def _hit(self, pt) -> Optional[date]:
        for r, d in self._cells:
            if r.contains(pt):
                return d
        return None

    def mousePressEvent(self, ev) -> None:
        d = self._hit(ev.position().toPoint())
        if d is not None:
            self.day_clicked.emit(d)

    def mouseMoveEvent(self, ev) -> None:
        d = self._hit(ev.position().toPoint())
        self.setCursor(Qt.PointingHandCursor if d else Qt.ArrowCursor)
        if d != self._hover:
            self._hover = d; self.update()

    def leaveEvent(self, ev) -> None:
        if self._hover is not None:
            self._hover = None; self.update()

    def paintEvent(self, ev) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(P.bg_primary))
        self._cells = []
        (self._paint_month if self._mode == "month" else self._paint_year)(p)
        p.end()

    def _paint_month(self, p: QPainter) -> None:
        pad, hdr = 8, 22
        w = self.width() - 2 * pad
        weeks = _cal.Calendar(0).monthdayscalendar(self._year, self._month)
        nrows = max(1, len(weeks))
        cw = w / 7.0
        body = pad + hdr + 4
        ch = max(26, (self.height() - body - pad) / nrows)
        p.setFont(QFont("Consolas", 8, QFont.Bold)); p.setPen(QColor(P.fg_dim))
        for i in range(7):
            p.drawText(QRect(int(pad + i * cw), pad, int(cw), hdr), Qt.AlignCenter, _WD[i])
        today = _today()
        for wi, week in enumerate(weeks):
            for di, dn in enumerate(week):
                if dn == 0:
                    continue
                d = date(self._year, self._month, dn)
                v = self._by.get(d, 0.0)
                x, y = int(pad + di * cw), int(body + wi * ch)
                cell = QRect(x + 1, y + 1, int(cw) - 3, int(ch) - 3)
                self._cells.append((cell, d))
                p.fillRect(cell, _heat(v, self._vmax))
                p.setFont(QFont("Consolas", 8))
                p.setPen(QColor(P.fg_dim) if v == 0 else QColor(P.bg_deepest) if v > 0 else QColor("#ffd6d2"))
                p.drawText(cell.adjusted(5, 3, -4, 0), Qt.AlignLeft | Qt.AlignTop, str(dn))
                if v != 0:
                    p.setFont(QFont("Consolas", 9, QFont.Bold))
                    p.setPen(QColor(P.bg_deepest) if v > 0 else QColor("#ffd6d2"))
                    p.drawText(cell.adjusted(0, 0, -5, -3), Qt.AlignRight | Qt.AlignBottom, _short(v))
                if d == today:
                    p.setPen(QPen(QColor(P.fg_dim), 1, Qt.DotLine)); p.drawRect(cell)
                if d == self._hover and d != self._sel:
                    p.setPen(QPen(QColor(P.fg_bright), 1)); p.drawRect(cell)
                if d == self._sel:
                    p.setPen(QPen(QColor(P.tool_trade), 2)); p.drawRect(cell.adjusted(1, 1, -1, -1))

    def _paint_year(self, p: QPainter) -> None:
        pad, gutter, top = 8, 30, 24
        jan1, dec31 = date(self._year, 1, 1), date(self._year, 12, 31)
        start = jan1 - timedelta(days=jan1.weekday())
        weeks = ((dec31 - start).days // 7) + 1
        size = int(min(max(7, min(18, (self.width() - gutter - pad) / weeks)),
                       max(7, min(18, (self.height() - top - pad) / 7))))
        gap = 2
        p.setFont(QFont("Consolas", 7)); p.setPen(QColor(P.fg_dim))
        for wd in (0, 2, 4):
            yy = int(top + wd * (size + gap) + size / 2)
            p.drawText(2, yy - 6, gutter - 6, 12, Qt.AlignRight | Qt.AlignVCenter, _WD[wd])
        today = _today()
        d = jan1
        while d <= dec31:
            col = (d - start).days // 7
            x, y = int(gutter + col * (size + gap)), int(top + d.weekday() * (size + gap))
            cell = QRect(x, y, size, size)
            self._cells.append((cell, d))
            p.fillRect(cell, _heat(self._by.get(d, 0.0), self._vmax))
            if d == self._sel:
                p.setPen(QPen(QColor(P.tool_trade), 2)); p.drawRect(cell)
            elif d == self._hover:
                p.setPen(QPen(QColor(P.fg_bright), 1)); p.drawRect(cell)
            elif d == today:
                p.setPen(QPen(QColor(P.fg_dim), 1, Qt.DotLine)); p.drawRect(cell)
            d += timedelta(days=1)
        p.setFont(QFont("Consolas", 8, QFont.Bold)); p.setPen(QColor(P.fg_dim))
        for m in range(1, 13):
            col = (date(self._year, m, 1) - start).days // 7
            p.drawText(int(gutter + col * (size + gap)), 2, 40, 16,
                       Qt.AlignLeft | Qt.AlignVCenter, _MO[m])


class CareerCalendar(QWidget):
    day_selected = Signal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        root = QVBoxLayout(self); root.setContentsMargins(0, 0, 0, 0); root.setSpacing(4)
        nav = QHBoxLayout(); nav.setSpacing(6)
        self._prev = self._btn("◀"); self._prev.clicked.connect(lambda: self._step(-1)); nav.addWidget(self._prev)
        self._title = self._btn("—"); self._title.setMinimumWidth(150); nav.addWidget(self._title)
        self._next = self._btn("▶"); self._next.clicked.connect(lambda: self._step(1)); nav.addWidget(self._next)
        nav.addStretch(1)
        self._bmonth = self._btn("Month"); self._bmonth.clicked.connect(lambda: self._mode("month")); nav.addWidget(self._bmonth)
        self._byear = self._btn("Year"); self._byear.clicked.connect(lambda: self._mode("year")); nav.addWidget(self._byear)
        root.addLayout(nav)
        self._grid = _Grid(); self._grid.day_clicked.connect(self._on_day)
        root.addWidget(self._grid, 1)
        self._refresh_title()

    @staticmethod
    def _btn(t: str) -> QPushButton:
        b = QPushButton(t); b.setCursor(Qt.PointingHandCursor)
        b.setStyleSheet(
            f"QPushButton{{color:{P.fg}; background:{P.bg_input}; border:1px solid {P.border}; "
            f"padding:4px 10px; font-family:Consolas; font-size:9pt; font-weight:bold;}} "
            f"QPushButton:hover{{color:{P.tool_trade}; border-color:{P.tool_trade};}}")
        return b

    def set_data(self, by_day: Dict[date, float]) -> None:
        vmax = max((abs(v) for v in by_day.values()), default=1.0)
        self._grid.set_data(by_day, vmax)
        if by_day:
            latest = max(by_day)
            self._grid.set_view(self._grid.mode(), latest.year, latest.month)
        self._refresh_title()

    def _on_day(self, d) -> None:
        if self._grid.mode() == "year":
            self._grid.set_view("month", d.year, d.month)
            self._refresh_title()
        self._grid.set_selected(d)
        self.day_selected.emit(d)

    def _mode(self, m: str) -> None:
        yr, mo = self._grid.current()
        self._grid.set_view(m, yr, mo)
        self._refresh_title()

    def _step(self, dirn: int) -> None:
        yr, mo = self._grid.current()
        if self._grid.mode() == "year":
            yr += dirn
        else:
            mo += dirn
            if mo < 1:
                mo, yr = 12, yr - 1
            elif mo > 12:
                mo, yr = 1, yr + 1
        self._grid.set_view(self._grid.mode(), yr, mo)
        self._refresh_title()

    def _refresh_title(self) -> None:
        yr, mo = self._grid.current()
        self._title.setText(str(yr) if self._grid.mode() == "year" else f"{_MO[mo]} {yr}")
