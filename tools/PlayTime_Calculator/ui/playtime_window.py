"""PlayTime Calculator — main window.

A MobiGlas-style holographic window that scans Star Citizen's Game.log files,
computes total play time, and presents an interactive breakdown: a headline
total switchable between time formats, highlight cards, a time-of-day
distribution, a per-day/week/month/year trend chart, and a sortable session
log.  Every chart and card is interactive — hover for exact figures, click to
drill into the underlying sessions.
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta
from typing import Optional

from PySide6.QtCore import Qt, QThread, Signal, QTimer
from PySide6.QtGui import QFont, QColor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame,
    QGridLayout, QScrollArea, QTabWidget, QTableWidget, QTableWidgetItem,
    QHeaderView, QLineEdit, QFileDialog, QCheckBox, QSpinBox, QSizePolicy,
    QAbstractItemView, QProgressBar, QComboBox,
)

from shared.qt.base_window import SCWindow
from shared.qt.title_bar import SCTitleBar
from shared.qt.theme import P

from core import log_scanner, analytics, settings as st, formatting as fmt
from core.analytics import WEEKDAY_NAMES, MONTH_NAMES, Analytics
from core.log_scanner import Session
from ui.charts import BarChart, Bar
from ui.calendar_view import CalendarTab
from ui.fun_stats_tab import FunStatsTab, FunStatsWorker
from ui.career_tab import CareerTab
from ui.injuries_tab import InjuriesTab

log = logging.getLogger(__name__)

ACCENT = "#44ccff"

# Currency code -> display symbol, for the cost-per-hour readout.
_CURRENCIES = [
    ("USD", "$"), ("EUR", "€"), ("GBP", "£"), ("CAD", "C$"), ("AUD", "A$"),
    ("NZD", "NZ$"), ("JPY", "¥"), ("CNY", "¥"), ("INR", "₹"), ("KRW", "₩"),
    ("BRL", "R$"), ("MXN", "Mex$"), ("RUB", "₽"), ("CHF", "CHF"), ("SEK", "kr"),
    ("NOK", "kr"), ("DKK", "kr"), ("PLN", "zł"), ("CZK", "Kč"), ("ZAR", "R"),
    ("SGD", "S$"), ("HKD", "HK$"), ("TRY", "₺"), ("AED", "AED"), ("THB", "฿"),
    ("PHP", "₱"),
]
_CURRENCY_SYMBOLS = {code: sym for code, sym in _CURRENCIES}
# Glyphs that render reliably across the app's font stack (and font fallback).
# Rarer symbols (₹ ₩ ₽ ฿ ₱ ₺ zł Kč …) tofu in some fonts, so for those we show
# the ASCII ISO code instead — always legible, and unambiguous.
_SAFE_SYMBOLS = {"$", "€", "£", "¥"}


# ══════════════════════════════════════════════════════════════════════════════
# Background scan worker
# ══════════════════════════════════════════════════════════════════════════════

class ScanWorker(QThread):
    progress = Signal(int, int)
    done = Signal(list)

    def __init__(self, folder: str, recurse: bool, parent=None) -> None:
        super().__init__(parent)
        self._folder = folder
        self._recurse = recurse
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        try:
            sessions = log_scanner.scan(
                self._folder,
                recurse=self._recurse,
                progress_cb=lambda d, t: self.progress.emit(d, t),
                cancel_cb=lambda: self._cancel,
            )
        except Exception:
            log.exception("playtime: scan failed")
            sessions = []
        self.done.emit(sessions)


# ══════════════════════════════════════════════════════════════════════════════
# Small reusable widgets
# ══════════════════════════════════════════════════════════════════════════════

class StatCard(QFrame):
    """A clickable highlight card: title, big value, sub-caption."""
    clicked = Signal(str)

    def __init__(self, card_id: str, title: str, accent: str = ACCENT, parent=None) -> None:
        super().__init__(parent)
        self._id = card_id
        self._accent = accent
        self.setObjectName("statCard")
        self.setCursor(Qt.PointingHandCursor)
        self._apply_border(P.border_card)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 9, 12, 9)
        lay.setSpacing(2)

        self._title = QLabel(title.upper(), self)
        self._title.setStyleSheet(
            f"font-family: Consolas; font-size: 7pt; font-weight: bold; "
            f"letter-spacing: 1px; color: {P.fg_dim}; background: transparent;")
        lay.addWidget(self._title)

        self._value = QLabel("—", self)
        self._value.setStyleSheet(
            f"font-family: Electrolize, Consolas; font-size: 16pt; font-weight: bold; "
            f"color: {accent}; background: transparent;")
        lay.addWidget(self._value)

        self._sub = QLabel("", self)
        self._sub.setStyleSheet(
            f"font-family: Consolas; font-size: 7pt; color: {P.fg_dim}; "
            f"background: transparent;")
        lay.addWidget(self._sub)

    def _apply_border(self, color: str) -> None:
        self.setStyleSheet(
            f"#statCard {{ background: {P.bg_card}; border: 1px solid {color}; }}")

    def set(self, value: str, sub: str = "") -> None:
        self._value.setText(value)
        self._sub.setText(sub)

    def enterEvent(self, ev):
        self._apply_border(self._accent)
        super().enterEvent(ev)

    def leaveEvent(self, ev):
        self._apply_border(P.border_card)
        super().leaveEvent(ev)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self.clicked.emit(self._id)
            ev.accept()
            return
        super().mousePressEvent(ev)


class SegmentedToggle(QWidget):
    """A row of exclusive pill buttons (e.g. Hours / Days / Calendar)."""
    changed = Signal(str)

    def __init__(self, options: list[tuple[str, str]], accent: str = ACCENT, parent=None) -> None:
        super().__init__(parent)
        self._accent = accent
        self._buttons: dict[str, QPushButton] = {}
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        for key, label in options:
            b = QPushButton(label, self)
            b.setCursor(Qt.PointingHandCursor)
            b.setCheckable(True)
            b.clicked.connect(lambda _=False, k=key: self._select(k))
            self._buttons[key] = b
            lay.addWidget(b)
        self._active = options[0][0] if options else ""
        self._restyle()

    def _select(self, key: str) -> None:
        if key == self._active:
            self._restyle()
            return
        self._active = key
        self._restyle()
        self.changed.emit(key)

    def set_active(self, key: str, silent: bool = True) -> None:
        if key not in self._buttons:
            return
        self._active = key
        self._restyle()
        if not silent:
            self.changed.emit(key)

    def active(self) -> str:
        return self._active

    def _restyle(self) -> None:
        for key, b in self._buttons.items():
            on = key == self._active
            b.setChecked(on)
            if on:
                b.setStyleSheet(
                    f"QPushButton {{ font-family: Consolas; font-size: 8pt; font-weight: bold;"
                    f" color: {P.bg_deepest}; background: {self._accent};"
                    f" border: 1px solid {self._accent}; padding: 4px 12px; }}")
            else:
                b.setStyleSheet(
                    f"QPushButton {{ font-family: Consolas; font-size: 8pt; font-weight: bold;"
                    f" color: {P.fg_dim}; background: transparent;"
                    f" border: 1px solid {P.border}; padding: 4px 12px; }}"
                    f"QPushButton:hover {{ color: {P.fg_bright}; border-color: {self._accent}; }}")


class _NumItem(QTableWidgetItem):
    """Table item that sorts by a stored numeric key rather than its text."""
    def __init__(self, text: str, sort_value: float) -> None:
        super().__init__(text)
        self._sv = sort_value

    def __lt__(self, other):
        if isinstance(other, _NumItem):
            return self._sv < other._sv
        return super().__lt__(other)


def _h2(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(
        f"font-family: Electrolize, Consolas; font-size: 10pt; font-weight: bold; "
        f"letter-spacing: 1px; color: {P.fg_bright}; background: transparent;")
    return lbl


def _btn(text: str, accent: str = ACCENT) -> QPushButton:
    b = QPushButton(text)
    b.setCursor(Qt.PointingHandCursor)
    b.setStyleSheet(
        f"QPushButton {{ font-family: Consolas; font-size: 8pt; font-weight: bold;"
        f" color: {accent}; background: transparent; border: 1px solid {accent};"
        f" border-radius: 3px; padding: 4px 12px; }}"
        f"QPushButton:hover {{ background: rgba(68,204,255,0.15); }}")
    return b


# ══════════════════════════════════════════════════════════════════════════════
# Main window
# ══════════════════════════════════════════════════════════════════════════════

class PlayTimeWindow(SCWindow):
    """The PlayTime Calculator window."""

    def __init__(self, geometry, hotkey_text: str = "", cmd_file: Optional[str] = None) -> None:
        super().__init__(
            title="PlayTime",
            width=geometry.w, height=geometry.h,
            min_w=720, min_h=520,
            opacity=geometry.opacity, accent=ACCENT,
        )
        self.restore_geometry_from_args(
            geometry.x, geometry.y, geometry.w, geometry.h, geometry.opacity)
        self._standalone = not cmd_file or cmd_file == os.devnull

        self._settings = st.load_settings()
        self._fmt = self._settings.get("time_format", "hours")
        self._cap_hours = float(self._settings.get("session_cap_hours", 0) or 0)
        self._spent = float(self._settings.get("total_spent", 0) or 0)
        self._currency = str(self._settings.get("currency", "USD"))
        if self._currency not in _CURRENCY_SYMBOLS:
            self._currency = "USD"
        self._granularity = "month"

        self._sessions: list[Session] = []
        #: The AFK-capped view of `_sessions`, and THE ONLY LIST ANY DISPLAY PATH MAY READ.
        #:
        #: ⛔ 2026-09-26: `_recompute` built this as a LOCAL called `used`, fed it to
        #:   `build_analytics` and to the calendar's day grouping, and then five display paths
        #:   went back to the raw `_sessions` behind its back — the Sessions table and all four
        #:   trend drill-downs. Turn the cap on and the headline said one thing while the table
        #:   under it and every bar you clicked said another, with no indication which was which.
        #: ⚠ It currently changes nothing on this machine, because `session_cap_hours` is 0 and
        #:   the two lists are then the same object. That is exactly why it was safe to sit there:
        #:   the defect is INVISIBLE until someone turns the cap on, and the moment they do, the
        #:   numbers disagree everywhere at once. Fixed before enabling the cap, not after.
        #: ⚠ Kept as a separate attribute rather than capping `_sessions` in place, because the
        #:   uncapped list is the honest record of what was scanned and the cap is a DISPLAY
        #:   policy. Overwriting the source would make the raw figure unrecoverable.
        self._used: list[Session] = []
        self._analytics = Analytics()
        self._worker: Optional[ScanWorker] = None
        # Fun Stats + Career + Injuries share one heavy full-content scan, owned here.
        self._fun_worker: Optional[FunStatsWorker] = None
        self._fun_scan_done = False

        # ── Title bar ──
        self._title_bar = SCTitleBar(
            window=self, title="PLAY TIME", accent_color=ACCENT,
            hotkey_text=hotkey_text, show_minimize=True,
            extra_buttons=[("Tutorial", self._show_tutorial)],
        )
        self._title_bar.minimize_clicked.connect(self.showMinimized)
        self._title_bar.close_clicked.connect(self._on_close)
        self.content_layout.addWidget(self._title_bar)

        self._build_controls()
        self._build_headline()
        self._build_tabs()

        # ── Kick off the initial scan ──
        QTimer.singleShot(120, self._initial_scan)

    # ── Control bar ──────────────────────────────────────────────────────────

    def _build_controls(self) -> None:
        bar = QWidget(self)
        bar.setStyleSheet(f"background: {P.bg_header};")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(12, 6, 12, 6)
        lay.setSpacing(8)

        folder_icon = QLabel("\U0001f4c1", bar)
        folder_icon.setStyleSheet("background: transparent; font-size: 11pt;")
        lay.addWidget(folder_icon)

        self._folder_lbl = QLabel("(no folder linked)", bar)
        self._folder_lbl.setStyleSheet(
            f"font-family: Consolas; font-size: 8pt; color: {P.fg_dim}; background: transparent;")
        self._folder_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self._folder_lbl)

        link_btn = _btn("Link Folder")
        link_btn.clicked.connect(self._link_folder)
        lay.addWidget(link_btn)

        self._rescan_btn = _btn("↻ Rescan")
        self._rescan_btn.clicked.connect(self._rescan)
        lay.addWidget(self._rescan_btn)

        lay.addStretch(1)

        # AFK trim control
        self._cap_chk = QCheckBox("Trim AFK over", bar)
        self._cap_chk.setStyleSheet(
            f"QCheckBox {{ font-family: Consolas; font-size: 8pt; color: {P.fg_dim}; background: transparent; }}")
        self._cap_chk.setChecked(self._cap_hours > 0)
        self._cap_chk.toggled.connect(self._on_cap_toggled)
        lay.addWidget(self._cap_chk)

        self._cap_spin = QSpinBox(bar)
        self._cap_spin.setRange(1, 72)
        self._cap_spin.setValue(int(self._cap_hours) if self._cap_hours > 0 else 12)
        self._cap_spin.setSuffix(" h")
        self._cap_spin.setFixedWidth(60)
        self._cap_spin.valueChanged.connect(self._on_cap_changed)
        lay.addWidget(self._cap_spin)

        self.content_layout.addWidget(bar)

    # ── Headline ──────────────────────────────────────────────────────────────

    def _build_headline(self) -> None:
        panel = QWidget(self)
        panel.setStyleSheet(f"background: {P.bg_primary};")
        lay = QHBoxLayout(panel)
        lay.setContentsMargins(16, 10, 16, 8)
        lay.setSpacing(14)

        left = QVBoxLayout()
        left.setSpacing(0)
        cap = QLabel("TOTAL PLAY TIME", panel)
        cap.setStyleSheet(
            f"font-family: Consolas; font-size: 8pt; font-weight: bold; letter-spacing: 2px;"
            f" color: {P.fg_dim}; background: transparent;")
        left.addWidget(cap)

        self._total_lbl = QLabel("—", panel)
        self._total_lbl.setStyleSheet(
            f"font-family: Electrolize, Consolas; font-size: 30pt; font-weight: bold;"
            f" color: {ACCENT}; background: transparent;")
        left.addWidget(self._total_lbl)

        self._alt_lbl = QLabel("", panel)
        self._alt_lbl.setStyleSheet(
            f"font-family: Consolas; font-size: 8pt; color: {P.fg}; background: transparent;")
        left.addWidget(self._alt_lbl)

        self._sub_lbl = QLabel("", panel)
        self._sub_lbl.setStyleSheet(
            f"font-family: Consolas; font-size: 8pt; color: {P.fg_dim}; background: transparent;")
        left.addWidget(self._sub_lbl)
        lay.addLayout(left, 1)

        # ── Cost per hour ──
        cost = QVBoxLayout()
        cost.setSpacing(0)
        cost_cap = QLabel("COST / HOUR", panel)
        cost_cap.setStyleSheet(
            f"font-family: Consolas; font-size: 8pt; font-weight: bold; letter-spacing: 2px;"
            f" color: {P.fg_dim}; background: transparent;")
        cost.addWidget(cost_cap)

        self._cost_lbl = QLabel("$ —", panel)
        # Consolas + Segoe UI fallback (NOT Electrolize, which renders a "bird"
        # placeholder for currency glyphs like ₹ ₩ ₽ it doesn't carry).
        self._cost_lbl.setStyleSheet(
            f"font-family: Consolas, 'Segoe UI', monospace; font-size: 28pt; font-weight: bold;"
            f" color: {P.green}; background: transparent;")
        cost.addWidget(self._cost_lbl)

        spent_row = QHBoxLayout()
        spent_row.setSpacing(4)
        spent_lbl = QLabel("Total spent", panel)
        spent_lbl.setStyleSheet(
            f"font-family: Consolas; font-size: 8pt; color: {P.fg_dim}; background: transparent;")
        spent_row.addWidget(spent_lbl)

        self._currency_combo = QComboBox(panel)
        self._currency_combo.setFixedWidth(78)
        self._currency_combo.setCursor(Qt.PointingHandCursor)
        self._currency_combo.setToolTip("Select your currency")
        for code, sym in _CURRENCIES:
            label = f"{code}  {sym}" if (sym.isascii() or sym in _SAFE_SYMBOLS) else code
            self._currency_combo.addItem(label, code)
        idx = self._currency_combo.findData(self._currency)
        if idx >= 0:
            self._currency_combo.setCurrentIndex(idx)
        self._currency_combo.currentIndexChanged.connect(self._on_currency_changed)
        spent_row.addWidget(self._currency_combo)

        self._spent_input = QLineEdit(panel)
        self._spent_input.setFixedWidth(96)
        self._spent_input.setPlaceholderText("e.g. 1200")
        self._spent_input.setToolTip("Total real money you've spent on Star Citizen "
                                     "(pledges, ships, subs). Saved between patches.")
        if self._spent > 0:
            self._spent_input.setText(f"{self._spent:g}")
        # Save on finish/focus-out AND live as you type, so the value persists
        # no matter how the window is closed.
        self._spent_input.editingFinished.connect(self._on_spent_changed)
        self._spent_input.textChanged.connect(self._on_spent_live)
        spent_row.addWidget(self._spent_input)
        spent_row.addStretch(1)
        cost.addLayout(spent_row)

        self._cost_sub = QLabel("enter your lifetime spend", panel)
        self._cost_sub.setStyleSheet(
            f"font-family: Consolas, 'Segoe UI', monospace; font-size: 7pt;"
            f" color: {P.fg_disabled}; background: transparent;")
        cost.addWidget(self._cost_sub)
        lay.addLayout(cost)

        # Format toggle (top-right)
        right = QVBoxLayout()
        right.setSpacing(6)
        right.addStretch(1)
        fmt_row = QHBoxLayout()
        fmt_row.addStretch(1)
        fmt_lbl = QLabel("FORMAT", panel)
        fmt_lbl.setStyleSheet(
            f"font-family: Consolas; font-size: 7pt; font-weight: bold; letter-spacing: 1px;"
            f" color: {P.fg_dim}; background: transparent;")
        fmt_row.addWidget(fmt_lbl)
        self._fmt_toggle = SegmentedToggle(
            [("hours", "Hours"), ("days", "Days"), ("calendar", "Calendar")])
        self._fmt_toggle.set_active(self._fmt)
        self._fmt_toggle.changed.connect(self._set_format)
        fmt_row.addWidget(self._fmt_toggle)
        right.addLayout(fmt_row)

        self._progress = QProgressBar(panel)
        self._progress.setFixedHeight(6)
        self._progress.setTextVisible(False)
        self._progress.setRange(0, 100)
        self._progress.hide()
        right.addWidget(self._progress)
        right.addStretch(1)
        lay.addLayout(right)

        self.content_layout.addWidget(panel)

        sep = QFrame(self)
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"background: {P.border};")
        self.content_layout.addWidget(sep)
        self._update_cost()

    # ── Cost per hour ─────────────────────────────────────────────────────────

    def _currency_symbol(self) -> str:
        return _CURRENCY_SYMBOLS.get(self._currency, "$")

    def _disp_unit(self) -> str:
        """Currency prefix for the amount: the symbol when it renders reliably,
        else the ASCII ISO code.  Trailing space for multi-char units."""
        sym = _CURRENCY_SYMBOLS.get(self._currency, "$")
        unit = sym if (sym.isascii() or sym in _SAFE_SYMBOLS) else self._currency
        return unit if len(unit) == 1 else unit + " "

    @staticmethod
    def _parse_spend(text: str):
        """Parse a money string to a float, or None if not parseable."""
        raw = "".join(c for c in text if c.isdigit() or c == ".")
        try:
            return max(0.0, float(raw)) if raw else 0.0
        except ValueError:
            return None

    def _persist_spend(self) -> None:
        """Write the current spend box to settings (no-op if unchanged/garbage)."""
        if not hasattr(self, "_spent_input"):
            return
        val = self._parse_spend(self._spent_input.text())
        if val is None:
            return
        if val != self._spent or self._settings.get("total_spent") != val:
            self._spent = val
            self._settings["total_spent"] = val
            st.save_settings(self._settings)

    def _on_spent_live(self) -> None:
        """Live update (and save) as the user types, so it persists immediately."""
        val = self._parse_spend(self._spent_input.text())
        if val is None:
            return
        self._spent = val
        self._settings["total_spent"] = val
        st.save_settings(self._settings)
        self._update_cost()

    def _on_spent_changed(self) -> None:
        self._persist_spend()
        self._update_cost()

    def _on_currency_changed(self) -> None:
        code = self._currency_combo.currentData()
        if code:
            self._currency = code
            self._settings["currency"] = code
            st.save_settings(self._settings)
            self._update_cost()

    def _update_cost(self) -> None:
        u = self._disp_unit()
        hrs = self._analytics.total_seconds / 3600.0 if self._analytics else 0.0
        if self._spent > 0 and hrs > 0:
            self._cost_lbl.setText(f"{u}{self._spent / hrs:,.2f}")
            cap = "  (AFK-trimmed)" if self._cap_hours > 0 else ""
            self._cost_sub.setText(f"{u}{self._spent:,.0f} ÷ {hrs:,.0f} h played{cap}")
        elif self._spent > 0:
            self._cost_lbl.setText(f"{u}—")
            self._cost_sub.setText("scan your logs to compute")
        else:
            self._cost_lbl.setText(f"{u}—")
            self._cost_sub.setText("enter your lifetime spend")

    # ── Tabs ────────────────────────────────────────────────────────────────

    def _build_tabs(self) -> None:
        self._tabs = QTabWidget(self)
        self._tabs.setDocumentMode(True)
        self._build_overview_tab()
        self._build_trends_tab()
        self._calendar_tab = CalendarTab(self)
        self._tabs.addTab(self._calendar_tab, "Calendar")
        self._fun_tab = FunStatsTab(on_request_scan=self._ensure_fun_scan, parent=self)
        self._tabs.addTab(self._fun_tab, "Fun Stats")
        self._career_tab = CareerTab(on_request_scan=self._ensure_fun_scan, parent=self)
        self._tabs.addTab(self._career_tab, "Career")
        self._injuries_tab = InjuriesTab(on_request_scan=self._ensure_fun_scan, parent=self)
        self._tabs.addTab(self._injuries_tab, "Injuries")
        self._build_sessions_tab()
        self.content_layout.addWidget(self._tabs, stretch=1)

    def _build_overview_tab(self) -> None:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(10)

        # Highlight cards grid
        grid = QGridLayout()
        grid.setSpacing(8)
        self._cards: dict[str, StatCard] = {}
        specs = [
            ("longest", "Longest Session"),
            ("day", "Most Played Day"),
            ("week", "Most Played Week"),
            ("month", "Most Played Month"),
            ("streak", "Longest Streak"),
            ("avg_day", "Avg / Active Day"),
            ("busiest_hour", "Busiest Hour"),
            ("busiest_dow", "Busiest Weekday"),
        ]
        for i, (cid, title) in enumerate(specs):
            card = StatCard(cid, title)
            card.clicked.connect(self._on_card_clicked)
            self._cards[cid] = card
            grid.addWidget(card, i // 4, i % 4)
        for c in range(4):
            grid.setColumnStretch(c, 1)
        outer.addLayout(grid)

        # Time-of-day distribution
        outer.addWidget(_h2("Time of Day  —  when you play"))
        self._hour_chart = BarChart(
            fit_width=True, value_fmt=fmt.fmt_short, y_title="play time")
        self._hour_chart.setMinimumHeight(170)
        self._hour_chart.bar_clicked.connect(self._on_hour_clicked)
        outer.addWidget(self._hour_chart, 2)

        self._hour_detail = QLabel("Hover a bar for the exact total; click to break it down.", page)
        self._hour_detail.setStyleSheet(
            f"font-family: Consolas; font-size: 8pt; color: {P.fg_dim}; background: transparent;")
        outer.addWidget(self._hour_detail)

        # Weekday + channel split
        outer.addWidget(_h2("By Weekday  &  Release Channel"))
        split = QHBoxLayout()
        split.setSpacing(10)
        self._dow_chart = BarChart(fit_width=True, value_fmt=fmt.fmt_short)
        self._dow_chart.setMinimumHeight(150)
        split.addWidget(self._dow_chart, 1)
        self._chan_chart = BarChart(fit_width=True, value_fmt=fmt.fmt_short)
        self._chan_chart.setMinimumHeight(150)
        split.addWidget(self._chan_chart, 1)
        outer.addLayout(split, 1)

        self._tabs.addTab(page, "Overview")

    def _build_trends_tab(self) -> None:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(8)

        row = QHBoxLayout()
        row.addWidget(_h2("Play Time Over Time"))
        row.addStretch(1)
        self._gran_toggle = SegmentedToggle(
            [("day", "Day"), ("week", "Week"), ("month", "Month"), ("year", "Year")])
        self._gran_toggle.set_active(self._granularity)
        self._gran_toggle.changed.connect(self._set_granularity)
        row.addWidget(self._gran_toggle)
        outer.addLayout(row)

        self._trend_scroll = QScrollArea(page)
        self._trend_scroll.setWidgetResizable(True)
        self._trend_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._trend_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._trend_scroll.setStyleSheet(
            f"QScrollArea {{ background: {P.bg_primary}; border: 1px solid {P.border}; }}")
        self._trend_chart = BarChart(fit_width=True, value_fmt=fmt.fmt_short)
        self._trend_chart.bar_clicked.connect(self._on_trend_clicked)
        self._trend_scroll.setWidget(self._trend_chart)
        outer.addWidget(self._trend_scroll, 2)

        self._trend_detail = QLabel(
            "Tip: hover a bar for its total · click a bar to list that period's sessions.", page)
        self._trend_detail.setWordWrap(True)
        self._trend_detail.setAlignment(Qt.AlignTop)
        self._trend_detail.setStyleSheet(
            f"font-family: Consolas; font-size: 8pt; color: {P.fg}; background: {P.bg_card};"
            f" border: 1px solid {P.border}; padding: 8px;")
        self._trend_detail.setMinimumHeight(110)
        outer.addWidget(self._trend_detail, 1)

        self._tabs.addTab(page, "Trends")

    def _build_sessions_tab(self) -> None:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(8)

        row = QHBoxLayout()
        row.addWidget(_h2("Session Log"))
        row.addStretch(1)
        filt_lbl = QLabel("Filter:", page)
        filt_lbl.setStyleSheet(
            f"font-family: Consolas; font-size: 8pt; color: {P.fg_dim}; background: transparent;")
        row.addWidget(filt_lbl)
        self._filter = QLineEdit(page)
        self._filter.setPlaceholderText("date, channel, build… (e.g. 2026-05, LIVE)")
        self._filter.setFixedWidth(260)
        self._filter.textChanged.connect(self._apply_filter)
        row.addWidget(self._filter)
        outer.addLayout(row)

        self._table = QTableWidget(page)
        self._table.setColumnCount(6)
        self._table.setHorizontalHeaderLabels(
            ["Date", "Start", "End", "Duration", "Channel", "Build"])
        self._table.setSortingEnabled(True)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        hh = self._table.horizontalHeader()
        hh.setStretchLastSection(True)
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        outer.addWidget(self._table, 1)

        self._sessions_count = QLabel("", page)
        self._sessions_count.setStyleSheet(
            f"font-family: Consolas; font-size: 8pt; color: {P.fg_dim}; background: transparent;")
        outer.addWidget(self._sessions_count)

        self._tabs.addTab(page, "Sessions")

    # ══ Scanning ══════════════════════════════════════════════════════════════

    def _initial_scan(self) -> None:
        folder = log_scanner.get_or_detect_folder()
        if not folder:
            self._folder_lbl.setText("(no Star Citizen folder found — click Link Folder)")
            self._set_total_text("No folder", "Link your Star Citizen install to begin.")
            return
        self._start_scan(folder)

    def _start_scan(self, folder: str) -> None:
        if self._worker and self._worker.isRunning():
            return
        self._settings["sc_folder"] = folder.replace("\\", "/")
        st.save_settings(self._settings)
        self._folder_lbl.setText(self._shorten(folder))
        self._rescan_btn.setEnabled(False)
        # New/relinked logs invalidate the career/fun scan; refresh if visible.
        self._fun_scan_done = False
        if self._tabs.currentWidget() in (self._fun_tab, self._career_tab,
                                          self._injuries_tab):
            self._ensure_fun_scan(force=True)
        self._progress.show()
        self._progress.setValue(0)
        recurse = bool(self._settings.get("scan_subfolders", True))
        self._worker = ScanWorker(folder, recurse)
        self._worker.progress.connect(self._on_progress)
        self._worker.done.connect(self._on_scan_done)
        self._worker.start()

    def _on_progress(self, done: int, total: int) -> None:
        if total > 0:
            self._progress.setValue(int(done * 100 / total))
        self._set_total_text("Scanning…", f"Reading log {done} of {total}")

    def _on_scan_done(self, sessions: list) -> None:
        self._sessions = sessions
        self._rescan_btn.setEnabled(True)
        self._progress.hide()
        self._recompute()

    # ══ Fun Stats + Career shared scan ═════════════════════════════════════════

    def _ensure_fun_scan(self, force: bool = False) -> None:
        """Start the heavy full-content scan once; feed both tabs the result."""
        if self._fun_worker and self._fun_worker.isRunning():
            return
        if self._fun_scan_done and not force:
            return
        folder = self._settings.get("sc_folder", "")
        if not folder:
            return
        self._fun_worker = FunStatsWorker(folder)
        self._fun_worker.progress.connect(self._on_fun_progress)
        self._fun_worker.done.connect(self._on_fun_done)
        self._fun_worker.start()

    def _on_fun_progress(self, done: int, total: int) -> None:
        self._fun_tab.set_progress(done, total)
        self._career_tab.set_progress(done, total)
        self._injuries_tab.set_progress(done, total)

    def _on_fun_done(self, fs) -> None:
        self._fun_scan_done = True
        self._fun_tab.set_stats(fs)
        self._career_tab.set_stats(fs)
        self._injuries_tab.set_stats(fs)

    # ══ Recompute + render ════════════════════════════════════════════════════

    def _recompute(self) -> None:
        used = log_scanner.apply_cap(self._sessions, self._cap_hours) \
            if self._cap_hours > 0 else self._sessions
        self._used = used
        self._analytics = analytics.build_analytics(used)
        # Group sessions by their local start date for the calendar day detail.
        sessions_by_day: dict = {}
        for s in used:
            sessions_by_day.setdefault(s.start_local.date(), []).append(s)
        self._sessions_by_day = sessions_by_day
        self._render_headline()
        self._update_cost()
        self._render_overview()
        self._render_trends()
        self._render_sessions()
        self._calendar_tab.set_data(self._analytics.by_day, sessions_by_day,
                                    analytics.channel_seconds_by_day(used))
        self._injuries_tab.set_sessions(used)
        self._persist_summary()

    def _render_headline(self) -> None:
        a = self._analytics
        if a.is_empty:
            self._set_total_text("0 hrs", "No sessions found in the linked folder.")
            return
        self._total_lbl.setText(fmt.format_total(a.total_seconds, self._fmt))
        # Always show all three representations; the active one is brightened.
        reps = [
            ("hours", fmt.fmt_hours(a.total_seconds)),
            ("days", fmt.fmt_days(a.total_seconds)),
            ("calendar", fmt.fmt_calendar_long(a.total_seconds)),
        ]
        parts = []
        for key, text in reps:
            if key == self._fmt:
                parts.append(f"<b style='color:{ACCENT}'>{text}</b>")
            else:
                parts.append(text)
        self._alt_lbl.setText("  ·  ".join(parts))

        h = a.highlights
        rng = ""
        if h.first_session and h.last_session:
            rng = f"{h.first_session:%b %Y} – {h.last_session:%b %Y}"
        cap_note = f"  ·  AFK trimmed at {int(self._cap_hours)}h" if self._cap_hours > 0 else ""
        self._sub_lbl.setText(
            f"{a.session_count:,} sessions  ·  {h.active_days:,} active days  ·  {rng}{cap_note}")

    def _set_total_text(self, total: str, sub: str) -> None:
        self._total_lbl.setText(total)
        self._sub_lbl.setText(sub)
        self._alt_lbl.setText("")

    def _render_overview(self) -> None:
        a = self._analytics
        h = a.highlights
        if a.is_empty:
            for card in self._cards.values():
                card.set("—", "")
            self._hour_chart.clear()
            self._dow_chart.clear()
            self._chan_chart.clear()
            return

        if h.longest_session:
            ls = h.longest_session
            self._cards["longest"].set(
                fmt.fmt_hms(ls.duration_seconds), f"{ls.start_local:%d %b %Y} · {ls.channel}")
        if h.most_played_day:
            d, secs = h.most_played_day
            self._cards["day"].set(fmt.fmt_short(secs), f"{d:%a %d %b %Y}")
        if h.most_played_week:
            wk, secs = h.most_played_week
            self._cards["week"].set(fmt.fmt_short(secs), f"week of {wk:%d %b %Y}")
        if h.most_played_month:
            (yr, mo), secs = h.most_played_month
            self._cards["month"].set(fmt.fmt_short(secs), f"{MONTH_NAMES[mo]} {yr}")
        self._cards["streak"].set(
            f"{h.longest_streak} days", f"current streak: {h.current_streak} days")
        self._cards["avg_day"].set(
            fmt.fmt_short(h.avg_per_active_day), f"avg session: {fmt.fmt_short(h.avg_session)}")
        if h.busiest_hour:
            hr, secs = h.busiest_hour
            self._cards["busiest_hour"].set(self._hour_label(hr), fmt.fmt_short(secs))
        if h.busiest_weekday:
            dow, secs = h.busiest_weekday
            self._cards["busiest_dow"].set(WEEKDAY_NAMES[dow], fmt.fmt_short(secs))

        # Hour distribution
        peak = max(range(24), key=lambda i: a.by_hour[i]) if any(a.by_hour) else -1
        self._hour_chart.set_bars([
            Bar(label=self._hour_label(i) if i % 3 == 0 else "",
                value=a.by_hour[i], key=str(i), accent=(i == peak))
            for i in range(24)
        ])

        # Weekday
        self._dow_chart.set_bars([
            Bar(label=WEEKDAY_NAMES[i], value=a.by_weekday[i], key=str(i),
                accent=(h.busiest_weekday is not None and i == h.busiest_weekday[0]))
            for i in range(7)
        ])

        # Channel
        chans = sorted(a.by_channel.items(), key=lambda kv: kv[1], reverse=True)
        self._chan_chart.set_bars([
            Bar(label=name, value=secs, key=name) for name, secs in chans
        ])

    def _render_trends(self) -> None:
        a = self._analytics
        g = self._granularity
        bars: list[Bar] = []
        if g == "day":
            series = a.day_series()
            best = max((v for _, v in series), default=0)
            bars = [Bar(label=f"{d:%Y-%m-%d}", value=v, key=d.isoformat(),
                        accent=(v == best and v > 0)) for d, v in series]
            self._trend_chart.set_mode(False, bar_width=7)
        elif g == "week":
            series = a.week_series()
            best = max((v for _, v in series), default=0)
            bars = [Bar(label=f"{d:%Y-%m-%d}", value=v, key=d.isoformat(),
                        accent=(v == best and v > 0)) for d, v in series]
            self._trend_chart.set_mode(False, bar_width=11)
        elif g == "month":
            series = a.month_series()
            best = max((v for _, v in series), default=0)
            bars = [Bar(label=f"{MONTH_NAMES[mo]} {str(yr)[2:]}", value=v,
                        key=f"{yr:04d}-{mo:02d}", accent=(v == best and v > 0))
                    for (yr, mo), v in series]
            self._trend_chart.set_mode(True)
        else:  # year
            series = a.year_series()
            best = max((v for _, v in series), default=0)
            bars = [Bar(label=str(yr), value=v, key=str(yr), accent=(v == best and v > 0))
                    for yr, v in series]
            self._trend_chart.set_mode(True)
        self._trend_chart.set_bars(bars)

    def _render_sessions(self) -> None:
        self._table.setSortingEnabled(False)
        rows = self._used          # capped — see `_used`. NOT `_sessions`.
        self._table.setRowCount(len(rows))
        for r, s in enumerate(rows):
            ls = s.start_local
            le = s.end_local
            date_item = QTableWidgetItem(f"{ls:%Y-%m-%d}")
            start_item = QTableWidgetItem(f"{ls:%H:%M}")
            end_item = QTableWidgetItem(f"{le:%H:%M}")
            dur_item = _NumItem(fmt.fmt_short(s.duration_seconds), s.duration_seconds)
            chan_item = QTableWidgetItem(s.channel)
            build_item = QTableWidgetItem(s.build or "—")
            for it in (date_item, start_item, end_item, dur_item, chan_item, build_item):
                it.setFont(QFont("Consolas", 8))
            dur_item.setForeground(QColor(ACCENT))
            self._table.setItem(r, 0, date_item)
            self._table.setItem(r, 1, start_item)
            self._table.setItem(r, 2, end_item)
            self._table.setItem(r, 3, dur_item)
            self._table.setItem(r, 4, chan_item)
            self._table.setItem(r, 5, build_item)
        self._table.setSortingEnabled(True)
        self._table.sortItems(0, Qt.DescendingOrder)
        self._apply_filter()

    # ══ Interaction handlers ══════════════════════════════════════════════════

    def _set_format(self, fmt_key: str) -> None:
        self._fmt = fmt_key
        self._fmt_toggle.set_active(fmt_key)  # keep toggle in sync if called in code
        self._settings["time_format"] = fmt_key
        st.save_settings(self._settings)
        self._render_headline()
        self._persist_summary()

    def _set_granularity(self, g: str) -> None:
        self._granularity = g
        self._gran_toggle.set_active(g)  # keep toggle in sync if called in code
        self._render_trends()
        self._trend_detail.setText(
            "Tip: hover a bar for its total · click a bar to list that period's sessions.")

    def _on_cap_toggled(self, on: bool) -> None:
        self._cap_hours = float(self._cap_spin.value()) if on else 0.0
        self._settings["session_cap_hours"] = self._cap_hours
        st.save_settings(self._settings)
        self._recompute()

    def _on_cap_changed(self, val: int) -> None:
        if self._cap_chk.isChecked():
            self._cap_hours = float(val)
            self._settings["session_cap_hours"] = self._cap_hours
            st.save_settings(self._settings)
            self._recompute()

    def _show_tutorial(self) -> None:
        """Show (or raise) the tutorial popup."""
        # Imported here so a tutorial import error can never stop the window
        # from opening.
        from ui.tutorial_popup import TutorialPopup
        TutorialPopup(self)

    def _on_card_clicked(self, cid: str) -> None:
        if cid == "longest":
            # Sessions is index 6, not 5. Tab order is Overview 0, Trends 1,
            # Calendar 2, Fun Stats 3, Career 4, Injuries 5, Sessions 6 --
            # this said 5 (with the comment "Sessions") since Injuries was
            # inserted ahead of it, so the card sorted the sessions table and
            # then showed the user the Injuries tab.
            self._tabs.setCurrentIndex(6)  # Sessions
            self._table.sortItems(3, Qt.DescendingOrder)
        elif cid == "day":
            h = self._analytics.highlights
            if h.most_played_day:
                self._tabs.setCurrentIndex(2)  # Calendar — select that day
                self._calendar_tab.show_day(h.most_played_day[0])
        elif cid in ("week", "month"):
            self._tabs.setCurrentIndex(1)  # Trends
            self._gran_toggle.set_active(cid)
            self._set_granularity(cid)
        elif cid in ("busiest_hour", "busiest_dow", "avg_day", "streak"):
            self._tabs.setCurrentIndex(0)

    def _on_hour_clicked(self, key: str) -> None:
        try:
            hr = int(key)
        except ValueError:
            return
        secs = self._analytics.by_hour[hr]
        days = sum(1 for d, _ in self._analytics.by_day.items())  # active days overall
        # Count days that had play during this hour.
        self._hour_detail.setText(
            f"{self._hour_label(hr)}–{self._hour_label((hr + 1) % 24)}:  "
            f"{fmt.fmt_short(secs)} total play time in this hour of the day "
            f"({fmt.fmt_hours(secs)}).")

    def _on_trend_clicked(self, key: str) -> None:
        g = self._granularity
        match: list[Session] = []
        title = key
        if g == "day":
            try:
                d = date.fromisoformat(key)
            except ValueError:
                return
            match = [s for s in self._used if s.start_local.date() == d]
            title = f"{d:%A %d %b %Y}"
        elif g == "week":
            try:
                monday = date.fromisoformat(key)
            except ValueError:
                return
            sunday = monday + timedelta(days=6)
            match = [s for s in self._used if monday <= s.start_local.date() <= sunday]
            title = f"Week of {monday:%d %b} – {sunday:%d %b %Y}"
        elif g == "month":
            yr, mo = int(key[:4]), int(key[5:7])
            match = [s for s in self._used
                     if s.start_local.year == yr and s.start_local.month == mo]
            title = f"{MONTH_NAMES[mo]} {yr}"
        else:
            yr = int(key)
            match = [s for s in self._used if s.start_local.year == yr]
            title = str(yr)

        total = sum(s.duration_seconds for s in match)
        lines = [
            f"<b style='color:{ACCENT}'>{title}</b>  —  "
            f"{fmt.fmt_short(total)} across {len(match)} session(s)"
        ]
        for s in sorted(match, key=lambda x: x.duration_seconds, reverse=True)[:14]:
            lines.append(
                f"&nbsp;&nbsp;{s.start_local:%Y-%m-%d %H:%M}  ·  "
                f"{fmt.fmt_short(s.duration_seconds)}  ·  {s.channel}")
        if len(match) > 14:
            lines.append(f"&nbsp;&nbsp;… and {len(match) - 14} more")
        self._trend_detail.setText("<br>".join(lines))

    def _apply_filter(self) -> None:
        needle = self._filter.text().strip().lower() if hasattr(self, "_filter") else ""
        shown = 0
        for r in range(self._table.rowCount()):
            if not needle:
                self._table.setRowHidden(r, False)
                shown += 1
                continue
            hay = " ".join(
                self._table.item(r, c).text().lower()
                for c in range(self._table.columnCount())
                if self._table.item(r, c))
            match = needle in hay
            self._table.setRowHidden(r, not match)
            shown += 1 if match else 0
        self._sessions_count.setText(f"{shown:,} of {self._table.rowCount():,} sessions shown")

    # ══ Folder / persistence ══════════════════════════════════════════════════

    def _link_folder(self) -> None:
        start = self._settings.get("sc_folder", "") or os.path.expanduser("~")
        folder = QFileDialog.getExistingDirectory(
            self, "Select your Star Citizen folder (install root, LIVE, or PTU)", start)
        if folder:
            self._start_scan(folder)

    def _rescan(self) -> None:
        folder = self._settings.get("sc_folder", "")
        if folder:
            self._start_scan(folder)
        else:
            self._initial_scan()

    def _persist_summary(self) -> None:
        try:
            summary = analytics.build_summary(
                self._analytics, self._fmt, self._settings.get("sc_folder", ""))
            st.write_summary(summary)
        except Exception:
            log.exception("playtime: failed to persist summary")

    # ══ Helpers ═══════════════════════════════════════════════════════════════

    @staticmethod
    def _hour_label(h: int) -> str:
        h %= 24
        if h == 0:
            return "12a"
        if h == 12:
            return "12p"
        if h < 12:
            return f"{h}a"
        return f"{h - 12}p"

    @staticmethod
    def _shorten(path: str, maxlen: int = 48) -> str:
        path = path.replace("\\", "/")
        if len(path) <= maxlen:
            return path
        return "…" + path[-(maxlen - 1):]

    # ══ IPC / lifecycle ═══════════════════════════════════════════════════════

    def handle_ipc_command(self, cmd: dict) -> None:
        t = cmd.get("type", "")
        if t == "show":
            self.showNormal()
            self.raise_()
            self.activateWindow()
        elif t == "hide":
            self.hide()
        elif t == "quit":
            self._quit()

    def _on_close(self) -> None:
        self._persist_spend()
        if self._standalone:
            self._quit()
        else:
            self.hide()

    def hideEvent(self, event) -> None:
        # Safety net: persist the spend whenever the window is hidden, in case
        # an edit wasn't committed via editingFinished.
        self._persist_spend()
        super().hideEvent(event)

    def closeEvent(self, event) -> None:
        self._persist_spend()
        super().closeEvent(event)

    def _quit(self) -> None:
        self._persist_spend()
        if self._worker and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait(1500)
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        if app:
            app.quit()
