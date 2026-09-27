"""Calendar tab — a real day/month/year calendar that drills into datapoints.

Two views:

* **Month** — a conventional month grid (weekday columns × week rows) where each
  day cell is a heat-coloured block labelled with that day's play time.
* **Year**  — a GitHub-style contribution heatmap of the whole year (week
  columns × weekday rows) with clickable month labels.

Every element is interactive.  Clicking a **day**, a **weekday column header**, a
**month label**, or the **period title** repopulates the right-hand detail panel
with the matching breakdown plus a mini bar chart, so the calendar "breaks out"
into datapoints on the fly.
"""
from __future__ import annotations

import calendar as _cal
from datetime import date, timedelta
from typing import Optional

from PySide6.QtCore import Qt, Signal, QRect
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QFontMetrics
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QFrame,
)

from shared.qt.theme import P
from core import formatting as fmt
from core.analytics import WEEKDAY_NAMES, MONTH_NAMES, iter_hour_segments
from core.log_scanner import Session
from ui.charts import BarChart, Bar

ACCENT = "#44ccff"

# Discrete heat buckets (GitHub-style): none → max.
_HEAT = ["#141a26", "#16384a", "#1f6f8c", "#2fa6cf", "#44ccff"]


def _heat(secs: float, vmax: float) -> QColor:
    if secs <= 0:
        return QColor(_HEAT[0])
    t = secs / vmax if vmax > 0 else 0.0
    if t < 0.15:
        i = 1
    elif t < 0.40:
        i = 2
    elif t < 0.70:
        i = 3
    else:
        i = 4
    return QColor(_HEAT[i])


def _text_on(secs: float, vmax: float) -> QColor:
    return QColor(P.bg_deepest) if (vmax > 0 and secs / vmax >= 0.7) else QColor(P.fg)


class CalendarGrid(QWidget):
    """Custom-painted month grid / year heatmap with click + hover hit-testing."""

    day_clicked = Signal(object)      # date
    day_hovered = Signal(object)      # date | None
    weekday_clicked = Signal(int)     # 0=Mon .. 6=Sun
    month_clicked = Signal(int, int)  # (year, month)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._by_day: dict[date, float] = {}
        self._vmax = 1.0
        self._mode = "month"
        self._year = 2026
        self._month = 6
        self._selected: Optional[date] = None
        self._hover: Optional[date] = None
        self._day_cells: list[tuple[QRect, date]] = []
        self._weekday_cells: list[tuple[QRect, int]] = []
        self._month_labels: list[tuple[QRect, tuple[int, int]]] = []
        self.setMouseTracking(True)
        self.setMinimumHeight(280)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    # ── data / view ──
    def set_data(self, by_day: dict[date, float], vmax: float) -> None:
        self._by_day = by_day
        self._vmax = max(vmax, 1.0)
        self.update()

    def set_view(self, mode: str, year: int, month: Optional[int] = None) -> None:
        self._mode = mode
        self._year = year
        if month:
            self._month = month
        self.update()

    def set_selected(self, d: Optional[date]) -> None:
        self._selected = d
        self.update()

    def mode(self) -> str:
        return self._mode

    def current(self) -> tuple[int, int]:
        return self._year, self._month

    # ── hit-testing ──
    def _hit_day(self, pt) -> Optional[date]:
        for rect, d in self._day_cells:
            if rect.contains(pt):
                return d
        return None

    def mousePressEvent(self, ev):
        if ev.button() != Qt.LeftButton:
            return super().mousePressEvent(ev)
        pt = ev.position().toPoint()
        for rect, ym in self._month_labels:
            if rect.contains(pt):
                self.month_clicked.emit(ym[0], ym[1])
                return
        for rect, wd in self._weekday_cells:
            if rect.contains(pt):
                self.weekday_clicked.emit(wd)
                return
        d = self._hit_day(pt)
        if d is not None:
            self.day_clicked.emit(d)
        ev.accept()

    def mouseMoveEvent(self, ev):
        pt = ev.position().toPoint()
        d = self._hit_day(pt)
        clickable = d is not None or any(r.contains(pt) for r, _ in self._weekday_cells) \
            or any(r.contains(pt) for r, _ in self._month_labels)
        self.setCursor(Qt.PointingHandCursor if clickable else Qt.ArrowCursor)
        if d != self._hover:
            self._hover = d
            self.day_hovered.emit(d)
            self.update()
        super().mouseMoveEvent(ev)

    def leaveEvent(self, ev):
        if self._hover is not None:
            self._hover = None
            self.day_hovered.emit(None)
            self.update()
        super().leaveEvent(ev)

    # ── painting ──
    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, False)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        p.fillRect(self.rect(), QColor(P.bg_primary))
        self._day_cells.clear()
        self._weekday_cells.clear()
        self._month_labels.clear()
        if self._mode == "month":
            self._paint_month(p)
        else:
            self._paint_year(p)
        p.end()

    def _paint_month(self, p: QPainter) -> None:
        pad = 8
        w = self.width() - 2 * pad
        hdr_h = 22
        weeks = _cal.Calendar(firstweekday=0).monthdayscalendar(self._year, self._month)
        nrows = max(1, len(weeks))
        cell_w = w / 7
        body_top = pad + hdr_h + 4
        cell_h = max(28, (self.height() - body_top - pad) / nrows)

        # Weekday header
        p.setFont(QFont("Consolas", 8, QFont.Bold))
        for i in range(7):
            r = QRect(int(pad + i * cell_w), pad, int(cell_w), hdr_h)
            self._weekday_cells.append((r, i))
            p.setPen(QColor(P.fg_dim))
            p.drawText(r, Qt.AlignCenter, WEEKDAY_NAMES[i])

        today = self._today()
        for wi, week in enumerate(weeks):
            for di, daynum in enumerate(week):
                if daynum == 0:
                    continue
                d = date(self._year, self._month, daynum)
                secs = self._by_day.get(d, 0.0)
                x = int(pad + di * cell_w)
                y = int(body_top + wi * cell_h)
                cell = QRect(x + 1, y + 1, int(cell_w) - 3, int(cell_h) - 3)
                self._day_cells.append((cell, d))
                p.fillRect(cell, _heat(secs, self._vmax))
                # day number
                p.setFont(QFont("Consolas", 8))
                p.setPen(_text_on(secs, self._vmax) if secs > 0 else QColor(P.fg_dim))
                p.drawText(cell.adjusted(5, 3, -4, 0), Qt.AlignLeft | Qt.AlignTop, str(daynum))
                if secs > 0:
                    p.setFont(QFont("Consolas", 9, QFont.Bold))
                    p.setPen(_text_on(secs, self._vmax))
                    p.drawText(cell.adjusted(0, 0, -5, -3),
                               Qt.AlignRight | Qt.AlignBottom, fmt.fmt_short(secs))
                if d == today:
                    p.setPen(QPen(QColor(P.fg_dim), 1, Qt.DotLine))
                    p.drawRect(cell)
                if d == self._hover and d != self._selected:
                    p.setPen(QPen(QColor(P.fg_bright), 1))
                    p.drawRect(cell)
                if d == self._selected:
                    p.setPen(QPen(QColor(ACCENT), 2))
                    p.drawRect(cell.adjusted(1, 1, -1, -1))

    def _paint_year(self, p: QPainter) -> None:
        pad = 8
        gutter = 30   # weekday labels
        top = 24      # month labels
        jan1 = date(self._year, 1, 1)
        dec31 = date(self._year, 12, 31)
        start = jan1 - timedelta(days=jan1.weekday())  # Monday on/before Jan 1
        total_weeks = ((dec31 - start).days // 7) + 1
        avail_w = self.width() - gutter - pad
        cw = max(7, min(18, avail_w / total_weeks))
        ch = max(7, min(18, (self.height() - top - pad) / 7))
        size = int(min(cw, ch))
        gap = 2

        # Weekday labels (Mon/Wed/Fri)
        p.setFont(QFont("Consolas", 7))
        p.setPen(QColor(P.fg_dim))
        for wd in (0, 2, 4):
            yy = int(top + wd * (size + gap) + size / 2)
            p.drawText(2, yy - 6, gutter - 6, 12, Qt.AlignRight | Qt.AlignVCenter,
                       WEEKDAY_NAMES[wd])

        # Day cells
        today = self._today()
        d = jan1
        while d <= dec31:
            col = (d - start).days // 7
            row = d.weekday()
            x = int(gutter + col * (size + gap))
            y = int(top + row * (size + gap))
            cell = QRect(x, y, size, size)
            secs = self._by_day.get(d, 0.0)
            self._day_cells.append((cell, d))
            p.fillRect(cell, _heat(secs, self._vmax))
            if d == today:
                p.setPen(QPen(QColor(P.fg_dim), 1, Qt.DotLine))
                p.drawRect(cell)
            if d == self._hover and d != self._selected:
                p.setPen(QPen(QColor(P.fg_bright), 1))
                p.drawRect(cell)
            if d == self._selected:
                p.setPen(QPen(QColor(ACCENT), 2))
                p.drawRect(cell)
            d += timedelta(days=1)

        # Month labels (clickable)
        p.setFont(QFont("Consolas", 8, QFont.Bold))
        p.setPen(QColor(P.fg_dim))
        for m in range(1, 13):
            first = date(self._year, m, 1)
            col = (first - start).days // 7
            x = int(gutter + col * (size + gap))
            r = QRect(x, 2, int(3 * (size + gap)) + 8, 16)
            self._month_labels.append((QRect(x, 2, max(24, size + gap), 18), (self._year, m)))
            p.drawText(r, Qt.AlignLeft | Qt.AlignVCenter, MONTH_NAMES[m])

        # Hover tooltip
        if self._hover is not None:
            self._paint_tip(p, self._hover)

    def _paint_tip(self, p: QPainter, d: date) -> None:
        secs = self._by_day.get(d, 0.0)
        text = f"{d:%a %d %b %Y}   {fmt.fmt_short(secs) if secs > 0 else 'no play'}"
        p.setFont(QFont("Consolas", 8, QFont.Bold))
        fmm = QFontMetrics(p.font())
        tw = fmm.horizontalAdvance(text) + 12
        th = fmm.height() + 6
        tx = min(max(8, self._mouse_x() - tw // 2), self.width() - tw - 4)
        box = QRect(int(tx), 2, int(tw), int(th))
        bg = QColor(P.bg_header)
        bg.setAlpha(245)
        p.fillRect(box, bg)
        p.setPen(QPen(QColor(ACCENT), 1))
        p.drawRect(box)
        p.setPen(QColor(P.fg_bright))
        p.drawText(box, Qt.AlignCenter, text)

    def _mouse_x(self) -> int:
        return self.mapFromGlobal(self.cursor().pos()).x()

    @staticmethod
    def _today() -> Optional[date]:
        try:
            return date.today()
        except Exception:
            return None


class CalendarTab(QWidget):
    """Assembles the calendar grid, navigation, and the drill-down detail panel."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._by_day: dict[date, float] = {}
        self._sessions_by_day: dict[date, list[Session]] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        # ── Navigation header ──
        nav = QHBoxLayout()
        nav.setSpacing(6)
        self._prev = self._nav_btn("◀")
        self._prev.clicked.connect(lambda: self._step(-1))
        nav.addWidget(self._prev)

        self._title = QPushButton("—")
        self._title.setCursor(Qt.PointingHandCursor)
        self._title.setToolTip("Show a summary for this period")
        self._title.setStyleSheet(
            f"QPushButton {{ font-family: Electrolize, Consolas; font-size: 12pt; font-weight: bold;"
            f" color: {P.fg_bright}; background: transparent; border: none; padding: 2px 10px; }}"
            f"QPushButton:hover {{ color: {ACCENT}; }}")
        self._title.clicked.connect(self._show_period_summary)
        nav.addWidget(self._title)

        self._next = self._nav_btn("▶")
        self._next.clicked.connect(lambda: self._step(1))
        nav.addWidget(self._next)
        nav.addStretch(1)

        self._btn_month = self._mode_btn("Month")
        self._btn_year = self._mode_btn("Year")
        self._btn_month.clicked.connect(lambda: self._set_mode("month"))
        self._btn_year.clicked.connect(lambda: self._set_mode("year"))
        nav.addWidget(self._btn_month)
        nav.addWidget(self._btn_year)
        latest = self._nav_btn("Latest", wide=True)
        latest.clicked.connect(self._jump_latest)
        nav.addWidget(latest)
        root.addLayout(nav)

        # ── Body: grid | detail ──
        body = QHBoxLayout()
        body.setSpacing(10)
        self._grid = CalendarGrid(self)
        self._grid.day_clicked.connect(self._on_day_clicked)
        self._grid.day_hovered.connect(self._on_day_hovered)
        self._grid.weekday_clicked.connect(self._on_weekday_clicked)
        self._grid.month_clicked.connect(self._on_month_clicked)
        body.addWidget(self._grid, 3)

        detail = QFrame(self)
        detail.setStyleSheet(f"QFrame {{ background: {P.bg_card}; border: 1px solid {P.border}; }}")
        detail.setFixedWidth(330)
        dl = QVBoxLayout(detail)
        dl.setContentsMargins(12, 10, 12, 10)
        dl.setSpacing(6)
        self._d_title = QLabel("—")
        self._d_title.setStyleSheet(
            f"font-family: Electrolize, Consolas; font-size: 11pt; font-weight: bold;"
            f" color: {ACCENT}; background: transparent; border: none;")
        dl.addWidget(self._d_title)
        self._d_stats = QLabel("")
        self._d_stats.setWordWrap(True)
        self._d_stats.setAlignment(Qt.AlignTop)
        self._d_stats.setTextFormat(Qt.RichText)
        self._d_stats.setStyleSheet(
            f"font-family: Consolas; font-size: 8pt; color: {P.fg}; background: transparent; border: none;")
        dl.addWidget(self._d_stats)
        self._d_chart = BarChart(fit_width=True, value_fmt=fmt.fmt_short)
        self._d_chart.setMinimumHeight(150)
        self._d_chart.bar_clicked.connect(self._on_detail_bar)
        dl.addWidget(self._d_chart, 1)
        body.addWidget(detail)
        root.addLayout(body, 1)

        self._detail_scope = ("none",)
        self._set_mode("month", silent=True)

    # ── styling helpers ──
    @staticmethod
    def _nav_btn(text: str, wide: bool = False) -> QPushButton:
        b = QPushButton(text)
        b.setCursor(Qt.PointingHandCursor)
        pad = "4px 12px" if wide else "4px 9px"
        b.setStyleSheet(
            f"QPushButton {{ font-family: Consolas; font-size: 9pt; font-weight: bold;"
            f" color: {P.fg}; background: {P.bg_input}; border: 1px solid {P.border};"
            f" padding: {pad}; }}"
            f"QPushButton:hover {{ color: {ACCENT}; border-color: {ACCENT}; }}")
        return b

    def _mode_btn(self, text: str) -> QPushButton:
        b = QPushButton(text)
        b.setCursor(Qt.PointingHandCursor)
        b.setCheckable(True)
        return b

    def _restyle_modes(self) -> None:
        for b, key in ((self._btn_month, "month"), (self._btn_year, "year")):
            on = (self._grid.mode() == key)
            b.setChecked(on)
            if on:
                b.setStyleSheet(
                    f"QPushButton {{ font-family: Consolas; font-size: 8pt; font-weight: bold;"
                    f" color: {P.bg_deepest}; background: {ACCENT}; border: 1px solid {ACCENT};"
                    f" padding: 4px 12px; }}")
            else:
                b.setStyleSheet(
                    f"QPushButton {{ font-family: Consolas; font-size: 8pt; font-weight: bold;"
                    f" color: {P.fg_dim}; background: transparent; border: 1px solid {P.border};"
                    f" padding: 4px 12px; }}"
                    f"QPushButton:hover {{ color: {P.fg_bright}; border-color: {ACCENT}; }}")

    # ── data entry ──
    def set_data(self, by_day: dict[date, float], sessions_by_day: dict[date, list[Session]],
                 chans_by_day: dict | None = None) -> None:
        self._by_day = by_day
        self._sessions_by_day = sessions_by_day
        #: {date: {channel: seconds}}, split at midnight the same way `by_day` is. Optional so an
        #: older caller still works — but see `_show_day`: when it is absent the channel figures
        #: fall back to whole-session sums, which is the very mismatch this was added to remove.
        self._chans_by_day = chans_by_day or {}
        vmax = max(by_day.values(), default=1.0)
        self._grid.set_data(by_day, vmax)
        if by_day:
            latest = max(by_day.keys())
            self._grid.set_view(self._grid.mode(), latest.year, latest.month)
            self._grid.set_selected(latest)
            self._refresh_title()
            self._show_day(latest)
        else:
            self._refresh_title()
            self._d_title.setText("No data")
            self._d_stats.setText("Scan a Star Citizen folder to populate the calendar.")
            self._d_chart.clear()

    def show_day(self, d: date) -> None:
        """Public: jump the calendar to *d* in month view and select it."""
        self._grid.set_view("month", d.year, d.month)
        self._restyle_modes()
        self._grid.set_selected(d)
        self._refresh_title()
        self._show_day(d)

    # ── navigation ──
    def _set_mode(self, mode: str, silent: bool = False) -> None:
        yr, mo = self._grid.current()
        self._grid.set_view(mode, yr, mo)
        self._restyle_modes()
        self._refresh_title()
        if not silent:
            # Re-show the current selection's detail in the new framing.
            sel = self._grid._selected
            if mode == "year":
                self._show_year(yr)
            elif sel is not None:
                self._show_day(sel)
            else:
                self._show_month(yr, mo)

    def _step(self, direction: int) -> None:
        yr, mo = self._grid.current()
        if self._grid.mode() == "year":
            yr += direction
            self._grid.set_view("year", yr, mo)
            self._refresh_title()
            self._show_year(yr)
        else:
            mo += direction
            if mo < 1:
                mo = 12
                yr -= 1
            elif mo > 12:
                mo = 1
                yr += 1
            self._grid.set_view("month", yr, mo)
            self._refresh_title()
            self._show_month(yr, mo)

    def _jump_latest(self) -> None:
        if not self._by_day:
            return
        latest = max(self._by_day.keys())
        self._grid.set_view(self._grid.mode(), latest.year, latest.month)
        self._grid.set_selected(latest)
        self._refresh_title()
        if self._grid.mode() == "year":
            self._show_year(latest.year)
        else:
            self._show_day(latest)

    def _refresh_title(self) -> None:
        yr, mo = self._grid.current()
        if self._grid.mode() == "year":
            self._title.setText(str(yr))
        else:
            self._title.setText(f"{MONTH_NAMES[mo]} {yr}")

    # ── grid interaction ──
    def _on_day_clicked(self, d: date) -> None:
        if self._grid.mode() == "year":
            # Drill from the heatmap into that day's month.
            self._grid.set_view("month", d.year, d.month)
            self._restyle_modes()
            self._refresh_title()
        self._grid.set_selected(d)
        self._show_day(d)

    def _on_day_hovered(self, d) -> None:
        pass  # tooltip is painted by the grid

    def _on_weekday_clicked(self, wd: int) -> None:
        self._show_weekday(wd)

    def _on_month_clicked(self, year: int, month: int) -> None:
        self._grid.set_view("month", year, month)
        self._restyle_modes()
        self._refresh_title()
        self._show_month(year, month)

    def _on_detail_bar(self, key: str) -> None:
        # The mini-chart bars drill one level deeper.
        scope = self._detail_scope
        try:
            if scope[0] == "month":  # per-day bars → that day
                self._on_day_clicked(date.fromisoformat(key))
            elif scope[0] == "year":  # per-month bars → that month
                yr, mo = int(key[:4]), int(key[5:7])
                self._on_month_clicked(yr, mo)
        except (ValueError, IndexError):
            pass

    # ── detail panel renderers ──
    def _show_day(self, d: date) -> None:
        self._detail_scope = ("day", d)
        secs = self._by_day.get(d, 0.0)
        sessions = self._sessions_by_day.get(d, [])
        self._d_title.setText(f"{d:%A  %d %b %Y}")
        if secs <= 0 and not sessions:
            self._d_stats.setText(
                f"<span style='color:{P.fg_dim}'>No play recorded on this day.</span>")
            self._d_chart.clear()
            return
        longest = max((s.duration_seconds for s in sessions), default=0)
        # ⛔ Day-local seconds per channel, so this line AGREES with the Total above it. Summing
        #   whole `s.duration_seconds` here grouped sessions by START date and credited the whole
        #   of an overnight session to the day it began — a ~4h total printed beside a ~48h
        #   channel line, which is the most double-count-LOOKING thing in this app and is not a
        #   double count at all. See analytics.channel_seconds_by_day.
        chans = dict(self._chans_by_day.get(d, {}))
        if not chans:
            # No split map supplied. Say so with whole-session sums rather than showing nothing —
            # but this is the old, disagreeing figure, and it is only reachable from a caller that
            # has not been updated.
            for s in sessions:
                chans[s.channel] = chans.get(s.channel, 0.0) + s.duration_seconds
        lines = [
            self._stat("Total", fmt.fmt_short(secs)),
            self._stat("Sessions", str(len(sessions))),
            self._stat("Longest", fmt.fmt_short(longest)),
            self._stat("Channels", ", ".join(f"{k} {fmt.fmt_short(v)}" for k, v in
                                              sorted(chans.items(), key=lambda kv: -kv[1])) or "—"),
        ]
        # ⚠ "Started today", not "ran today". A session that began yesterday and crossed midnight
        #   contributes to Total and Channels above and is deliberately NOT listed here — it is not
        #   this day's session. Saying which is meant is the difference between a caveat and a
        #   discrepancy the reader has to explain to themselves.
        lines.append("<br><b style='color:%s'>Sessions started today</b>" % P.fg_dim)
        for s in sorted(sessions, key=lambda x: x.start_local):
            lines.append(f"&nbsp;{s.start_local:%H:%M}–{s.end_local:%H:%M} · "
                         f"{fmt.fmt_short(s.duration_seconds)} · {s.channel}")
        self._d_stats.setText("<br>".join(lines))
        # Mini chart: this day's hour-of-day distribution.
        hours = [0.0] * 24
        for s in sessions:
            for seg_start, sec in iter_hour_segments(s.start_local, s.end_local):
                if seg_start.date() == d:
                    hours[seg_start.hour] += sec
        self._d_chart.set_mode(True)
        self._d_chart.set_bars([
            Bar(label=self._hour_label(i) if i % 6 == 0 else "", value=hours[i], key=str(i))
            for i in range(24)])

    def _show_month(self, year: int, month: int) -> None:
        self._detail_scope = ("month", year, month)
        days = {d: v for d, v in self._by_day.items() if d.year == year and d.month == month}
        total = sum(days.values())
        active = len([v for v in days.values() if v > 0])
        self._d_title.setText(f"{MONTH_NAMES[month]} {year}")
        if not days:
            self._d_stats.setText(f"<span style='color:{P.fg_dim}'>No play this month.</span>")
            self._d_chart.clear()
            return
        best = max(days.items(), key=lambda kv: kv[1])
        ndays = _cal.monthrange(year, month)[1]
        self._d_stats.setText("<br>".join([
            self._stat("Total", fmt.fmt_short(total)),
            self._stat("Active days", f"{active} of {ndays}"),
            self._stat("Avg / active day", fmt.fmt_short(total / active if active else 0)),
            self._stat("Best day", f"{best[0]:%a %d} · {fmt.fmt_short(best[1])}"),
            self._stat("Sessions", str(sum(len(self._sessions_by_day.get(d, []))
                                           for d in days))),
            f"<br><span style='color:{P.fg_dim}'>Click a bar to open that day.</span>",
        ]))
        self._d_chart.set_mode(True)
        bars = []
        for day in range(1, ndays + 1):
            dd = date(year, month, day)
            v = self._by_day.get(dd, 0.0)
            bars.append(Bar(label=str(day) if day % 5 == 0 or day == 1 else "",
                            value=v, key=dd.isoformat(), accent=(dd == best[0] and v > 0)))
        self._d_chart.set_bars(bars)

    def _show_year(self, year: int) -> None:
        self._detail_scope = ("year", year)
        months = {}
        for d, v in self._by_day.items():
            if d.year == year:
                months[d.month] = months.get(d.month, 0.0) + v
        total = sum(months.values())
        active = len([d for d in self._by_day if d.year == year and self._by_day[d] > 0])
        self._d_title.setText(str(year))
        if not months:
            self._d_stats.setText(f"<span style='color:{P.fg_dim}'>No play this year.</span>")
            self._d_chart.clear()
            return
        best = max(months.items(), key=lambda kv: kv[1])
        self._d_stats.setText("<br>".join([
            self._stat("Total", fmt.fmt_hours(total)),
            self._stat("Active days", str(active)),
            self._stat("Best month", f"{MONTH_NAMES[best[0]]} · {fmt.fmt_short(best[1])}"),
            self._stat("Avg / month", fmt.fmt_short(total / len(months))),
            f"<br><span style='color:{P.fg_dim}'>Click a bar to open that month.</span>",
        ]))
        self._d_chart.set_mode(True)
        self._d_chart.set_bars([
            Bar(label=MONTH_NAMES[m], value=months.get(m, 0.0), key=f"{year:04d}-{m:02d}",
                accent=(m == best[0]))
            for m in range(1, 13)])

    def _show_weekday(self, wd: int) -> None:
        yr, mo = self._grid.current()
        in_year = self._grid.mode() == "year"
        self._detail_scope = ("weekday", wd)
        if in_year:
            days = {d: v for d, v in self._by_day.items() if d.year == yr and d.weekday() == wd}
            scope_txt = str(yr)
        else:
            days = {d: v for d, v in self._by_day.items()
                    if d.year == yr and d.month == mo and d.weekday() == wd}
            scope_txt = f"{MONTH_NAMES[mo]} {yr}"
        total = sum(days.values())
        played = [v for v in days.values() if v > 0]
        full = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][wd]
        self._d_title.setText(f"{full}s · {scope_txt}")
        if not played:
            self._d_stats.setText(f"<span style='color:{P.fg_dim}'>No {full} play in {scope_txt}.</span>")
            self._d_chart.clear()
            return
        self._d_stats.setText("<br>".join([
            self._stat("Total", fmt.fmt_short(total)),
            self._stat(f"{full}s played", str(len(played))),
            self._stat("Avg", fmt.fmt_short(total / len(played))),
            self._stat("Best", fmt.fmt_short(max(played))),
        ]))
        self._d_chart.set_mode(True)
        items = sorted(days.items())
        self._d_chart.set_bars([
            Bar(label=f"{d:%d %b}" if i % max(1, len(items) // 8) == 0 else "",
                value=v, key=d.isoformat())
            for i, (d, v) in enumerate(items)])

    def _show_period_summary(self) -> None:
        if self._grid.mode() == "year":
            self._show_year(self._grid.current()[0])
        else:
            yr, mo = self._grid.current()
            self._show_month(yr, mo)

    # ── helpers ──
    @staticmethod
    def _stat(label: str, value: str) -> str:
        return (f"<span style='color:{P.fg_dim}'>{label}:</span> "
                f"<b style='color:{P.fg_bright}'>{value}</b>")

    @staticmethod
    def _hour_label(h: int) -> str:
        h %= 24
        if h == 0:
            return "12a"
        if h == 12:
            return "12p"
        return f"{h}a" if h < 12 else f"{h - 12}p"
