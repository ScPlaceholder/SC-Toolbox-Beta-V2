#!/usr/bin/env python3
"""
Trade Hub — standalone PySide6 GUI process.
Launched by the WingmanAI skill via subprocess.
Fetches trade data from the UEX API.
"""
import json
import logging
import os
import queue
import sys
import threading
import time
import traceback
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, List, Optional, Tuple

# Bootstrap project root and skill directory
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..')))
from shared.app_bootstrap import bootstrap_skill  # noqa: E402
bootstrap_skill(__file__)

from PySide6.QtCore import Qt, QTimer, QUrl, Signal, QObject
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QSplitter, QFrame, QTabWidget, QLineEdit, QDialog, QScrollArea,
)

from shared.qt.theme import P, apply_theme
from shared.qt.base_window import SCWindow
from shared.qt.title_bar import SCTitleBar
from shared.qt.data_table import SCTable, SCTableModel, ColumnDef
from shared.qt.search_bar import SCSearchBar
from shared.qt.dropdown import SCComboBox
from shared.qt.hud_widgets import HUDPanel
from shared.qt.animated_button import SCButton
from shared.qt.ipc_thread import IPCWatcher
from shared.qt.fuzzy_combo import SCFuzzyCombo
from shared.ships import SHIP_PRESETS, scu_for_ship, QUICK_SHIPS
from shared.data_utils import parse_cli_args
from shared.i18n import s_ as _

from trade_hub_data import (
    Route, MultiRoute, FilterState, DataFetcher,
    COLUMNS, COLUMN_KEYS, LOOP_COLUMNS, LOOP_COLUMN_KEYS,
    MIXED_COLUMNS, MIXED_COLUMN_KEYS,
    apply_filters, sort_routes, find_multi_routes, sort_multi_routes,
    profit_tier, get_unique_commodities, fmt_distance, fmt_eta,
    load_config, save_config,
    calc_profit, set_calc_mode, get_calc_mode,
    set_market_mode, find_max_profit_routes,
)

# Platform-guarded Win32 imports
if sys.platform == 'win32':
    import ctypes
    import ctypes.wintypes
else:
    ctypes = None

# ── Logging ──────────────────────────────────────────────────────────────────
_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trade_hub.log")

def _setup_log():
    lg = logging.getLogger("TradeHub")
    lg.setLevel(logging.DEBUG)
    if not lg.handlers:
        fh = RotatingFileHandler(_LOG_PATH, maxBytes=1_500_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)-5s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
        lg.addHandler(fh)
    return lg

log = _setup_log()

# ── Win32 constants ──────────────────────────────────────────────────────────
if sys.platform == 'win32':
    _user32 = ctypes.windll.user32
    _kernel32 = ctypes.windll.kernel32
else:
    _user32 = _kernel32 = None

_HWND_TOPMOST = -1
_SWP_NOSIZE = 0x0001
_SWP_NOMOVE = 0x0002
_SWP_NOACTIVATE = 0x0010
_SW_RESTORE = 9
_WM_HOTKEY = 0x0312
_PM_REMOVE = 0x0001
_MOD_ALT = 0x0001
_MOD_CONTROL = 0x0002
_MOD_SHIFT = 0x0004
_MOD_WIN = 0x0008
_VK_MAP = {
    **{c: 0x41 + i for i, c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ")},
    **{str(i): 0x30 + i for i in range(10)},
    "F1": 0x70, "F2": 0x71, "F3": 0x72, "F4": 0x73,
    "F5": 0x74, "F6": 0x75, "F7": 0x76, "F8": 0x77,
    "F9": 0x78, "F10": 0x79, "F11": 0x7A, "F12": 0x7B,
}
_DEFAULT_HOTKEY = "ctrl+shift+t"


def _parse_hotkey(hk: str) -> Tuple[int, int]:
    mods = 0
    vk = 0
    for part in hk.upper().split("+"):
        part = part.strip()
        if part in ("CTRL", "CONTROL"):
            mods |= _MOD_CONTROL
        elif part == "SHIFT":
            mods |= _MOD_SHIFT
        elif part == "ALT":
            mods |= _MOD_ALT
        elif part in ("WIN", "WINDOWS"):
            mods |= _MOD_WIN
        else:
            vk = _VK_MAP.get(part, 0)
    return mods, vk


def _pin_btn_qss(pinned: bool) -> str:
    """Return the pin button stylesheet for pinned/unpinned state."""
    if pinned:
        return f"""
            QPushButton {{
                background-color: rgba(255, 204, 0, 80);
                color: {P.bg_primary};
                border: 1px solid {P.tool_trade};
                font-family: Consolas; font-size: 8pt; font-weight: bold;
                padding: 4px 14px;
            }}
            QPushButton:hover {{
                background-color: rgba(255, 204, 0, 50);
                color: {P.tool_trade};
                border-color: {P.tool_trade};
            }}
        """
    return f"""
        QPushButton {{
            background-color: rgba(255, 204, 0, 30);
            color: {P.tool_trade};
            border: 1px solid rgba(255, 204, 0, 60);
            font-family: Consolas; font-size: 8pt; font-weight: bold;
            padding: 4px 14px;
        }}
        QPushButton:hover {{
            background-color: rgba(255, 204, 0, 60);
            color: {P.fg_bright};
            border-color: {P.tool_trade};
        }}
    """


# ── Route detail dialog ──────────────────────────────────────────────────────

class RouteDetailDialog(QDialog):
    """Popup showing route or loop details with Pin button and financial breakdown."""

    _pinned_dialogs: list = []  # class-level list of pinned dialogs

    def __init__(self, parent, title: str, route_data: dict) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        # Use Qt.Tool instead of Qt.Dialog to prevent Qt auto-centering
        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setMinimumSize(420, 300)
        self.resize(500, 560)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self._pinned = False
        self._drag_pos = None
        self._resize_edge = None  # which edge is being dragged
        self._resize_margin = 6   # px from edge to trigger resize
        self.setMouseTracking(True)

        # Position near the parent window instead of screen center
        if parent:
            pg = parent.geometry()
            self.move(pg.x() + pg.width() + 8, pg.y())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(1, 1, 1, 1)
        layout.setSpacing(0)

        # Container with holographic bg
        container = QFrame(self)
        container.setStyleSheet(f"""
            QFrame {{
                background-color: rgba(11, 14, 20, 220);
                border: 1px solid rgba(68, 170, 255, 100);
            }}
        """)
        c_layout = QVBoxLayout(container)
        c_layout.setContentsMargins(0, 0, 0, 0)
        c_layout.setSpacing(0)

        # Title bar
        bar = SCTitleBar(self, title=title, accent_color=P.tool_trade, show_minimize=False)
        bar.close_clicked.connect(self.close)
        c_layout.addWidget(bar)

        # Pin button row
        pin_row = QHBoxLayout()
        pin_row.setContentsMargins(12, 6, 12, 2)
        pin_row.addStretch(1)
        self._pin_btn = SCButton("Pin", self, glow_color=P.tool_trade)
        self._pin_btn.setStyleSheet(_pin_btn_qss(False))
        self._pin_btn.clicked.connect(self._toggle_pin)
        pin_row.addWidget(self._pin_btn)
        c_layout.addLayout(pin_row)

        # Content area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(f"QScrollArea {{ border: none; background: transparent; }}")

        content_widget = QWidget()
        content_widget.setStyleSheet(f"background: transparent;")
        self._content_layout = QVBoxLayout(content_widget)
        self._content_layout.setContentsMargins(16, 8, 16, 16)
        self._content_layout.setSpacing(4)

        self._build_content(route_data)

        self._content_layout.addStretch(1)
        scroll.setWidget(content_widget)
        c_layout.addWidget(scroll, 1)

        layout.addWidget(container)

    def _build_content(self, d: dict):
        """Build the detail content from route data dict."""
        ly = self._content_layout

        route_type = d.get("type", "single")

        if route_type == "single":
            self._build_single_route(d)
        elif route_type == "multi":
            self._build_multi_route(d)
        elif route_type == "mixed":
            self._build_mixed_route(d)

    def _add_header(self, text: str, color: str = ""):
        lbl = QLabel(text)
        lbl.setStyleSheet(f"""
            font-family: Electrolize, Consolas; font-size: 10pt; font-weight: bold;
            color: {color or P.accent}; background: transparent;
            padding: 14px 0 4px 0;
        """)
        self._content_layout.addWidget(lbl)

    def _add_separator(self):
        spacer_top = QWidget()
        spacer_top.setFixedHeight(2)
        spacer_top.setStyleSheet("background: transparent;")
        self._content_layout.addWidget(spacer_top)
        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"background-color: rgba(68, 170, 255, 40);")
        self._content_layout.addWidget(sep)
        spacer_btm = QWidget()
        spacer_btm.setFixedHeight(4)
        spacer_btm.setStyleSheet("background: transparent;")
        self._content_layout.addWidget(spacer_btm)

    def _add_row(self, label: str, value: str, value_color: str = ""):
        row_w = QWidget()
        row_w.setFixedHeight(26)
        row_w.setStyleSheet("background: transparent;")
        row = QHBoxLayout(row_w)
        row.setSpacing(8)
        row.setContentsMargins(0, 0, 0, 0)
        k = QLabel(label)
        k.setFixedWidth(140)
        k.setStyleSheet(f"font-family: Consolas; font-size: 9pt; color: {P.fg_dim}; background: transparent;")
        row.addWidget(k)
        v = QLabel(value)
        v.setStyleSheet(f"font-family: Consolas; font-size: 9pt; color: {value_color or P.fg}; background: transparent;")
        row.addWidget(v, 1)
        self._content_layout.addWidget(row_w)

    def _add_colored_row(self, label: str, value: str, label_color: str = "", value_color: str = ""):
        """Row where both the label and value have custom colours."""
        row_w = QWidget()
        row_w.setFixedHeight(26)
        row_w.setStyleSheet("background: transparent;")
        row = QHBoxLayout(row_w)
        row.setSpacing(8)
        row.setContentsMargins(0, 0, 0, 0)
        k = QLabel(label)
        k.setFixedWidth(140)
        k.setStyleSheet(f"font-family: Consolas; font-size: 9pt; color: {label_color or P.fg_dim}; background: transparent;")
        row.addWidget(k)
        v = QLabel(value)
        v.setStyleSheet(f"font-family: Consolas; font-size: 9pt; font-weight: bold; color: {value_color or P.fg}; background: transparent;")
        row.addWidget(v, 1)
        self._content_layout.addWidget(row_w)

    def _add_value_row(self, label: str, value: str, color: str = ""):
        """Large value row for financial figures."""
        row_w = QWidget()
        row_w.setFixedHeight(28)
        row_w.setStyleSheet("background: transparent;")
        row = QHBoxLayout(row_w)
        row.setSpacing(8)
        row.setContentsMargins(0, 0, 0, 0)
        k = QLabel(label)
        k.setFixedWidth(140)
        k.setStyleSheet(f"font-family: Consolas; font-size: 9pt; color: {P.fg_dim}; background: transparent;")
        row.addWidget(k)
        v = QLabel(value)
        v.setStyleSheet(f"font-family: Consolas; font-size: 10pt; font-weight: bold; color: {color or P.fg_bright}; background: transparent;")
        row.addWidget(v, 1)
        self._content_layout.addWidget(row_w)

    def _build_single_route(self, d: dict):
        ship = d.get("ship", "No ship")
        commodity = d.get("commodity", "?")
        eff_scu = d.get("eff_scu", 0)
        price_buy = d.get("price_buy", 0)
        price_sell = d.get("price_sell", 0)
        margin = d.get("margin", 0)
        profit = d.get("profit", 0)
        roi = d.get("roi", 0)
        total_cost = eff_scu * price_buy
        total_revenue = eff_scu * price_sell

        distance = d.get("distance", 0)

        self._add_header("ROUTE SUMMARY", P.tool_trade)
        self._add_separator()
        self._add_colored_row("Ship:", ship, P.tool_trade, P.fg_bright)
        self._add_colored_row("Commodity:", commodity, P.tool_trade, P.fg_bright)
        self._add_colored_row("Load:", f"{eff_scu:,} SCU", P.yellow, P.yellow)
        if distance > 0:
            self._add_colored_row("Distance:", fmt_distance(distance), P.energy_cyan, P.energy_cyan)
            self._add_colored_row("Travel Time:", fmt_eta(distance), P.energy_cyan, P.energy_cyan)

        self._add_header("FINANCIALS", P.green)
        self._add_separator()
        self._add_colored_row("Total Cost:", f"{total_cost:,.0f} aUEC", P.red, P.red)
        self._add_colored_row("Total Revenue:", f"{total_revenue:,.0f} aUEC", P.accent, P.accent)
        self._add_colored_row("Profit:", f"+{profit:,.0f} aUEC", P.green, P.green)
        self._add_colored_row("Margin/SCU:", f"{margin:,.0f} aUEC/SCU", P.green, P.accent)
        roi_color = P.green if roi > 50 else P.yellow
        self._add_colored_row("ROI:", f"{roi:.1f}%", roi_color, roi_color)

        self._add_header("BUY LOCATION", P.accent)
        self._add_separator()
        self._add_colored_row("Terminal:", d.get("buy_terminal", "?"), P.accent, P.fg_bright)
        self._add_colored_row("Location:", d.get("buy_location", "?"), P.accent, P.fg)
        self._add_colored_row("System:", d.get("buy_system", "?"), P.energy_cyan, P.energy_cyan)
        self._add_colored_row("Price:", f"{price_buy:,.0f} aUEC/SCU", P.red, P.red)
        self._add_colored_row("Available:", f"{d.get('scu_available', 0):,} SCU", P.yellow, P.yellow)
        self._add_colored_row("Purchase Total:", f"{total_cost:,.0f} aUEC", P.red, P.red)

        self._add_header("SELL LOCATION", P.orange)
        self._add_separator()
        self._add_colored_row("Terminal:", d.get("sell_terminal", "?"), P.orange, P.fg_bright)
        self._add_colored_row("Location:", d.get("sell_location", "?"), P.orange, P.fg)
        self._add_colored_row("System:", d.get("sell_system", "?"), P.energy_cyan, P.energy_cyan)
        self._add_colored_row("Price:", f"{price_sell:,.0f} aUEC/SCU", P.accent, P.accent)
        self._add_colored_row("Demand:", f"{d.get('scu_demand', 0):,} SCU", P.yellow, P.yellow)
        self._add_colored_row("Sale Revenue:", f"{total_revenue:,.0f} aUEC", P.green, P.green)
        self._add_colored_row("Profit Here:", f"+{profit:,.0f} aUEC", P.green, P.green)

    def _build_multi_route(self, d: dict):
        ship = d.get("ship", "No ship")
        total_profit = d.get("total_profit", 0)
        legs = d.get("legs", [])
        num_legs = len(legs)
        running_investment = 0

        total_distance = sum(leg.get("distance", 0) for leg in legs)

        self._add_header(f"MULTI-LEG ROUTE  \u2022  {num_legs} legs", P.tool_trade)
        self._add_separator()
        self._add_colored_row("Ship:", ship, P.tool_trade, P.fg_bright)
        self._add_colored_row("Total Profit:", f"+{total_profit:,.0f} aUEC", P.green, P.green)
        if total_distance > 0:
            self._add_colored_row("Total Distance:", fmt_distance(total_distance), P.energy_cyan, P.energy_cyan)
            self._add_colored_row("Total Travel:", fmt_eta(total_distance), P.energy_cyan, P.energy_cyan)

        for i, leg in enumerate(legs, 1):
            eff = leg.get("eff_scu", 0)
            buy_price = leg.get("price_buy", 0)
            sell_price = leg.get("price_sell", 0)
            leg_cost = eff * buy_price
            leg_revenue = eff * sell_price
            leg_profit = eff * leg.get("margin", 0)
            leg_dist = leg.get("distance", 0)
            running_investment += leg_cost

            self._add_header(f"LEG {i}:  {leg.get('commodity', '?')}", P.accent)
            self._add_separator()
            self._add_colored_row("Buy:", f"{leg.get('buy_terminal', '?')} ({leg.get('buy_system', '?')})", P.accent, P.fg_bright)
            self._add_colored_row("Sell:", f"{leg.get('sell_terminal', '?')} ({leg.get('sell_system', '?')})", P.orange, P.fg_bright)
            self._add_colored_row("Load:", f"{eff:,} SCU", P.yellow, P.yellow)
            if leg_dist > 0:
                self._add_colored_row("Travel:", f"{fmt_distance(leg_dist)} \u2022 {fmt_eta(leg_dist)}", P.energy_cyan, P.energy_cyan)
            self._add_colored_row("Purchase:", f"{leg_cost:,.0f} aUEC", P.red, P.red)
            self._add_colored_row("Revenue:", f"{leg_revenue:,.0f} aUEC", P.accent, P.accent)
            self._add_colored_row("Leg Profit:", f"+{leg_profit:,.0f} aUEC", P.green, P.green)

        self._add_header("TOTALS", P.green)
        self._add_separator()
        self._add_colored_row("Total Investment:", f"{running_investment:,.0f} aUEC", P.red, P.red)
        self._add_colored_row("Total Profit:", f"+{total_profit:,.0f} aUEC", P.green, P.green)
        if total_distance > 0:
            self._add_colored_row("Total Travel:", f"{fmt_distance(total_distance)} \u2022 {fmt_eta(total_distance)}", P.energy_cyan, P.energy_cyan)

    def _build_mixed_route(self, d: dict):
        ship = d.get("ship", "No ship")
        total_profit = d.get("total_profit", 0)
        total_invest = d.get("total_investment", 0)
        roi = d.get("roi", 0)
        fill_eff = d.get("fill_efficiency", 0)
        legs = d.get("legs", [])
        num_legs = len(legs)
        total_dist = d.get("total_distance", 0)

        # ── Route overview (gold) ─────────────────────────────────────
        self._add_header(f"MIXED FREIGHT  \u2022  {num_legs} legs  \u2022  {fill_eff:.0f}% fill", P.tool_trade)
        self._add_separator()
        self._add_colored_row("Ship:", ship, P.tool_trade, P.fg_bright)
        self._add_colored_row("Total Profit:", f"+{total_profit:,.0f} aUEC", P.green, P.green)
        if total_invest > 0:
            self._add_colored_row("Total Cost:", f"{total_invest:,.0f} aUEC", P.red, P.red)
            roi_color = P.green if roi > 50 else P.yellow
            self._add_colored_row("ROI:", f"{roi:.1f}%", roi_color, roi_color)
        self._add_colored_row("Bay Efficiency:", f"{fill_eff:.1f}%", P.yellow, P.yellow)
        if total_dist > 0:
            self._add_colored_row("Total Distance:", fmt_distance(total_dist), P.energy_cyan, P.energy_cyan)
            self._add_colored_row("Travel Time:", fmt_eta(total_dist), P.energy_cyan, P.energy_cyan)

        for i, leg in enumerate(legs, 1):
            leg_scu = leg.get("total_scu", 0)
            leg_fill = leg.get("fill_pct", 0)
            leg_profit = leg.get("leg_profit", 0)
            leg_dist = leg.get("distance", 0)
            slots = leg.get("slots", [])

            # ── Leg header (blue) ─────────────────────────────────────
            self._add_header(f"LEG {i}:  {leg.get('buy_terminal', '?')}  \u2192  {leg.get('sell_terminal', '?')}", P.accent)
            self._add_separator()
            self._add_colored_row("System:", f"{leg.get('buy_system', '?')} \u2192 {leg.get('sell_system', '?')}", P.energy_cyan, P.energy_cyan)
            if leg_dist > 0:
                self._add_colored_row("Travel:", f"{fmt_distance(leg_dist)} \u2022 {fmt_eta(leg_dist)}", P.energy_cyan, P.energy_cyan)

            # ── Cargo slots ───────────────────────────────────────────
            for slot in slots:
                is_primary = slot.get("is_primary", False)
                is_illegal = slot.get("is_illegal", False)
                scu = slot.get("scu_loaded", 0)
                buy_p = slot.get("price_buy", 0)
                sell_p = slot.get("price_sell", 0)
                slot_profit = slot.get("profit", 0)
                commodity = slot.get("commodity", "?")

                # Primary = green, Filler = purple, Illegal = red
                if is_illegal:
                    name_color = P.red
                elif is_primary:
                    name_color = P.green
                else:
                    name_color = P.purple

                role_icon = "\u2605" if is_primary else "\u25cb"
                tag = "Primary" if is_primary else "Filler"
                illegal_tag = "  \u26a0 ILLEGAL" if is_illegal else ""

                # Commodity header — role on the left, commodity name on the right
                self._add_colored_row(f"{role_icon}  {tag}{illegal_tag}", commodity, name_color, name_color)
                self._add_colored_row("    SCU:", f"{scu:,}", P.yellow, P.yellow)
                self._add_colored_row("    Buy:", f"{buy_p:,.2f} aUEC/SCU", P.red, P.red)
                self._add_colored_row("    Sell:", f"{sell_p:,.2f} aUEC/SCU", P.accent, P.accent)
                profit_color = P.green if is_primary else P.purple
                self._add_colored_row("    Profit:", f"+{slot_profit:,.0f} aUEC", profit_color, profit_color)

            # ── Leg totals ─────────────────────────────────────────────
            self._add_separator()
            self._add_colored_row("Leg Fill:", f"{leg_scu:,} SCU  ({leg_fill:.1f}%)", P.yellow, P.yellow)
            self._add_colored_row("Leg Profit:", f"+{leg_profit:,.0f} aUEC", P.green, P.green)

        # ── Grand totals ─────────────────────────────────────────────
        self._add_header("TOTALS", P.green)
        self._add_separator()
        self._add_colored_row("Total Profit:", f"+{total_profit:,.0f} aUEC", P.green, P.green)
        if total_invest > 0:
            self._add_colored_row("Total Cost:", f"{total_invest:,.0f} aUEC", P.red, P.red)
            roi_color = P.green if roi > 50 else P.yellow
            self._add_colored_row("ROI:", f"{roi:.1f}%", roi_color, roi_color)
        self._add_colored_row("Bay Efficiency:", f"{fill_eff:.1f}% avg", P.yellow, P.yellow)

    def _toggle_pin(self):
        if self._pinned:
            self._pinned = False
            self._pin_btn.setText("Pin")
            self._pin_btn.setStyleSheet(_pin_btn_qss(False))
            if self in RouteDetailDialog._pinned_dialogs:
                RouteDetailDialog._pinned_dialogs.remove(self)
        else:
            self._pinned = True
            self._pin_btn.setText("Unpin")
            self._pin_btn.setStyleSheet(_pin_btn_qss(True))
            RouteDetailDialog._pinned_dialogs.append(self)

    def closeEvent(self, event) -> None:
        if self in RouteDetailDialog._pinned_dialogs:
            RouteDetailDialog._pinned_dialogs.remove(self)
        super().closeEvent(event)

    def _edge_at(self, pos):
        """Return which edge(s) the cursor is near, or None for interior (drag)."""
        m = self._resize_margin
        r = self.rect()
        edges = ""
        if pos.y() >= r.height() - m:
            edges += "b"
        if pos.x() >= r.width() - m:
            edges += "r"
        if pos.y() <= m:
            edges += "t"
        if pos.x() <= m:
            edges += "l"
        return edges or None

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            edge = self._edge_at(event.position().toPoint())
            if edge:
                self._resize_edge = edge
                self._drag_pos = event.globalPosition().toPoint()
            else:
                self._resize_edge = None
                self._drag_pos = event.globalPosition().toPoint() - self.pos()

    def mouseMoveEvent(self, event) -> None:
        pos = event.position().toPoint()

        # Update cursor shape based on edge proximity
        if not (event.buttons() & Qt.LeftButton):
            edge = self._edge_at(pos)
            if edge in ("b", "t"):
                self.setCursor(Qt.SizeVerCursor)
            elif edge in ("r", "l"):
                self.setCursor(Qt.SizeHorCursor)
            elif edge in ("br", "rb", "tl", "lt"):
                self.setCursor(Qt.SizeFDiagCursor)
            elif edge in ("bl", "lb", "tr", "rt"):
                self.setCursor(Qt.SizeBDiagCursor)
            elif edge:
                self.setCursor(Qt.SizeAllCursor)
            else:
                self.setCursor(Qt.ArrowCursor)
            return

        if self._resize_edge and self._drag_pos:
            # Resize mode
            gp = event.globalPosition().toPoint()
            delta = gp - self._drag_pos
            self._drag_pos = gp
            geo = self.geometry()

            if "r" in self._resize_edge:
                geo.setRight(geo.right() + delta.x())
            if "b" in self._resize_edge:
                geo.setBottom(geo.bottom() + delta.y())
            if "l" in self._resize_edge:
                geo.setLeft(geo.left() + delta.x())
            if "t" in self._resize_edge:
                geo.setTop(geo.top() + delta.y())

            # Enforce minimum size
            if geo.width() >= self.minimumWidth() and geo.height() >= self.minimumHeight():
                self.setGeometry(geo)

        elif self._drag_pos and not self._resize_edge:
            # Drag mode
            self.move(event.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, event) -> None:
        self._drag_pos = None
        self._resize_edge = None
        self.setCursor(Qt.ArrowCursor)


# ── Main window ──────────────────────────────────────────────────────────────

class _RouteSignal(QObject):
    """Helper signal to marshal route data from background thread to main thread."""
    routes_ready = Signal(list, str)
    distances_ready = Signal(list)
    distance_progress = Signal(int, int)

class TradeHubWindow(SCWindow):
    """Trade Hub PySide6 window with SCTitleBar, sidebar filters, and SCTable."""

    def __init__(self, cmd_file: str, x=80, y=80, w=1400, h=900,
                 refresh_interval=300.0, max_routes=500, opacity=0.95) -> None:
        super().__init__(
            title="Trade Hub", width=max(w, 1400), height=max(h, 1200),
            min_w=800, min_h=600, opacity=opacity, always_on_top=True,
        )
        # Remove WindowDoesNotAcceptFocus so text inputs work
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowDoesNotAcceptFocus)
        self.restore_geometry_from_args(x, y, w, h, opacity)

        self._cmd_file = cmd_file
        self._fetcher = DataFetcher(refresh_interval)
        # Signal to safely deliver data from background thread to main thread
        self._route_signal = _RouteSignal(self)
        self._route_signal.routes_ready.connect(self._apply_routes)
        self._route_signal.distances_ready.connect(self._on_distances_ready)
        self._route_signal.distance_progress.connect(self._on_distance_progress)
        self._refresh_interval = refresh_interval
        self._max_routes = max_routes

        self._all_routes: List[Route] = []
        self._filtered_routes: List[Route] = []
        self._cached_profits: dict = {}
        self._all_loops: List[MultiRoute] = []
        self._filtered_loops: List[MultiRoute] = []
        self._sort_col = "est_profit"
        self._sort_reverse = True
        self._loop_sort_col = "total_profit"
        self._loop_sort_reverse = True
        self._ship_name = ""
        self._ship_scu = 0
        self._data_source = "\u2014"
        self._last_refresh: Optional[float] = None
        self._view_mode = "ROUTES"
        self._visible = True
        self._hotkey = _DEFAULT_HOTKEY
        self._hotkey_stop: Optional[threading.Event] = None
        self._hotkey_thread: Optional[threading.Thread] = None
        self._freight_mode = "BULK"  # "BULK" or "MIXED"
        self._allow_illegal = False  # default: no illegal cargo
        self._all_mixed: list = []
        self._filtered_mixed: list = []
        self._mixed_sort_col = "total_profit"
        self._mixed_sort_reverse = True

        self._build_ui()

        cfg = load_config()
        if cfg.get("ship_name"):
            self._set_ship(cfg["ship_name"])
        if cfg.get("hotkey"):
            self._hotkey = cfg["hotkey"]
        self._freight_mode = cfg.get("freight_mode", "BULK")
        self._allow_illegal = cfg.get("allow_illegal_cargo", False)
        if hasattr(self, '_btn_bulk'):
            self._update_freight_mode_btns()
        if hasattr(self, '_btn_illegal_yes'):
            self._update_illegal_btns()
        self._sync_table_visibility()

        self._start_ipc()
        self._start_hotkey_listener()
        QTimer.singleShot(500, self._start_load)
        QTimer.singleShot(int(refresh_interval * 1000), self._auto_refresh)

    def _build_ui(self):
        layout = self.content_layout

        # Title bar
        self._title_bar = SCTitleBar(
            self, title="TRADE HUB",
            icon_text="\u25c8", accent_color=P.tool_trade,
            show_minimize=False,
            extra_buttons=[
                ("Tutorial", self._open_tutorial),
                ("UEX | Patreon", lambda: QDesktopServices.openUrl(QUrl("https://www.patreon.com/uexcorp"))),
            ],
        )
        self._title_bar.close_clicked.connect(lambda: (self.hide(), setattr(self, '_visible', False)))
        layout.addWidget(self._title_bar)

        # Body: splitter with sidebar + content
        body = QSplitter(Qt.Horizontal)
        body.setStyleSheet(f"QSplitter::handle {{ background: {P.border}; width: 1px; }}")

        # ── Sidebar ──
        sidebar = QWidget()
        sidebar.setFixedWidth(235)
        sidebar.setStyleSheet(f"background: {P.bg_secondary};")
        sb_lay = QVBoxLayout(sidebar)
        sb_lay.setContentsMargins(4, 4, 4, 4)
        sb_lay.setSpacing(1)

        def section(text, pad_top=6) -> None:
            lbl = QLabel(text)
            lbl.setStyleSheet(f"font-family: Consolas; font-size: 8pt; color: {P.tool_trade}; background: transparent; padding: {pad_top}px 10px 0px 10px;")
            sb_lay.addWidget(lbl)

        # View mode
        section(_("VIEW MODE:"), 8)
        vm_row = QHBoxLayout()
        vm_row.setContentsMargins(10, 2, 10, 0)
        self._btn_routes = QPushButton(_("ROUTES"))
        self._btn_routes.setCursor(Qt.PointingHandCursor)
        self._btn_routes.clicked.connect(lambda: self._set_view_mode("ROUTES"))
        vm_row.addWidget(self._btn_routes)
        self._btn_loops = QPushButton(_("LOOPS"))
        self._btn_loops.setCursor(Qt.PointingHandCursor)
        self._btn_loops.clicked.connect(lambda: self._set_view_mode("LOOPS"))
        vm_row.addWidget(self._btn_loops)
        vmw = QWidget()
        vmw.setStyleSheet("background: transparent;")
        vmw.setLayout(vm_row)
        sb_lay.addWidget(vmw)
        self._update_view_mode_btns()

        # Freight mode
        section(_("FREIGHT MODE:"), 8)
        fm_row = QHBoxLayout()
        fm_row.setContentsMargins(10, 2, 10, 0)
        self._btn_bulk = QPushButton(_("BULK"))
        self._btn_bulk.setCursor(Qt.PointingHandCursor)
        self._btn_bulk.clicked.connect(lambda: self._set_freight_mode("BULK"))
        fm_row.addWidget(self._btn_bulk)
        self._btn_mixed = QPushButton(_("MIXED"))
        self._btn_mixed.setCursor(Qt.PointingHandCursor)
        self._btn_mixed.clicked.connect(lambda: self._set_freight_mode("MIXED"))
        fm_row.addWidget(self._btn_mixed)
        fm_w = QWidget()
        fm_w.setStyleSheet("background: transparent;")
        fm_w.setLayout(fm_row)
        sb_lay.addWidget(fm_w)
        self._update_freight_mode_btns()

        # Allow illegal cargo
        section(_("ALLOW ILLEGAL CARGO:"), 8)
        il_row = QHBoxLayout()
        il_row.setContentsMargins(10, 2, 10, 0)
        self._btn_illegal_yes = QPushButton(_("YES"))
        self._btn_illegal_yes.setCursor(Qt.PointingHandCursor)
        self._btn_illegal_yes.clicked.connect(lambda: self._set_allow_illegal(True))
        il_row.addWidget(self._btn_illegal_yes)
        self._btn_illegal_no = QPushButton(_("NO"))
        self._btn_illegal_no.setCursor(Qt.PointingHandCursor)
        self._btn_illegal_no.clicked.connect(lambda: self._set_allow_illegal(False))
        il_row.addWidget(self._btn_illegal_no)
        il_w = QWidget()
        il_w.setStyleSheet("background: transparent;")
        il_w.setLayout(il_row)
        sb_lay.addWidget(il_w)
        self._update_illegal_btns()

        # Market calculations toggle
        section("MARKET CALCULATIONS:", 8)
        mc_row = QHBoxLayout()
        mc_row.setContentsMargins(10, 2, 10, 0)
        self._use_max_profit = False
        self._btn_mc_max = QPushButton("Max Profit")
        self._btn_mc_max.setCursor(Qt.PointingHandCursor)
        self._btn_mc_max.clicked.connect(lambda: self._set_market_calc(True))
        mc_row.addWidget(self._btn_mc_max)
        self._btn_mc_demand = QPushButton("Reported Demand")
        self._btn_mc_demand.setCursor(Qt.PointingHandCursor)
        self._btn_mc_demand.clicked.connect(lambda: self._set_market_calc(False))
        mc_row.addWidget(self._btn_mc_demand)
        mc_w = QWidget()
        mc_w.setStyleSheet("background: transparent;")
        mc_w.setLayout(mc_row)
        sb_lay.addWidget(mc_w)
        self._update_mc_btns()

        # Vehicle
        section(_("VEHICLE:"), 10)
        self._ship_combo = SCFuzzyCombo(
            placeholder=_("Ship..."),
            items=[d for _, d in QUICK_SHIPS],
        )
        self._ship_combo.item_selected.connect(self._on_ship_selected)
        sb_lay.addWidget(self._ship_combo)

        # "Only System(s) Selected" toggle
        section(_("ONLY SYSTEM(S) SELECTED:"), 8)
        oss_row = QHBoxLayout()
        oss_row.setContentsMargins(10, 2, 10, 0)
        self._only_sel_sys = False
        self._btn_oss_yes = QPushButton(_("YES"))
        self._btn_oss_yes.setCursor(Qt.PointingHandCursor)
        self._btn_oss_yes.clicked.connect(lambda: self._set_only_sel_sys(True))
        oss_row.addWidget(self._btn_oss_yes)
        self._btn_oss_no = QPushButton(_("NO"))
        self._btn_oss_no.setCursor(Qt.PointingHandCursor)
        self._btn_oss_no.clicked.connect(lambda: self._set_only_sel_sys(False))
        oss_row.addWidget(self._btn_oss_no)
        oss_w = QWidget()
        oss_w.setStyleSheet("background: transparent;")
        oss_w.setLayout(oss_row)
        sb_lay.addWidget(oss_w)
        self._update_oss_btns()

        # Buy system
        section(_("SYSTEM: BUY"))
        self._buy_sys = SCFuzzyCombo(placeholder=_("Buy system..."))
        self._buy_sys.item_selected.connect(lambda _: self._apply_search())
        sb_lay.addWidget(self._buy_sys)

        # Sell system
        section(_("SYSTEM: SELL"))
        self._sell_sys = SCFuzzyCombo(placeholder=_("Sell system..."))
        self._sell_sys.item_selected.connect(lambda _: self._apply_search())
        sb_lay.addWidget(self._sell_sys)

        # Buy location
        section(_("BUY LOCATION"))
        self._buy_loc = SCFuzzyCombo(placeholder=_("Buy location..."))
        self._buy_loc.item_selected.connect(lambda _: self._apply_search())
        sb_lay.addWidget(self._buy_loc)

        # Sell location
        section(_("SELL LOCATION"))
        self._sell_loc = SCFuzzyCombo(placeholder=_("Sell location..."))
        self._sell_loc.item_selected.connect(lambda _: self._apply_search())
        sb_lay.addWidget(self._sell_loc)

        # Commodity
        section(_("COMMODITY"))
        self._commodity_combo = SCFuzzyCombo(placeholder=_("Commodity..."))
        self._commodity_combo.item_selected.connect(lambda _: self._apply_search())
        sb_lay.addWidget(self._commodity_combo)

        # Min SCU
        section(_("MIN SCU"))
        self._min_scu = QLineEdit()
        self._min_scu.setPlaceholderText("0")
        self._min_scu.returnPressed.connect(self._apply_search)
        sb_lay.addWidget(self._min_scu)

        # Min profit/SCU
        section(_("MIN PROFIT/SCU"))
        self._min_profit = QLineEdit()
        self._min_profit.setPlaceholderText("0")
        self._min_profit.returnPressed.connect(self._apply_search)
        sb_lay.addWidget(self._min_profit)

        # Search
        section(_("SEARCH"))
        self._search = SCSearchBar(placeholder=_("Search..."), debounce_ms=320)
        self._search.search_changed.connect(lambda _: self._apply_search())
        sb_lay.addWidget(self._search)

        # Clear + Refresh side by side
        cr_row = QHBoxLayout()
        cr_row.setContentsMargins(0, 0, 0, 0)
        cr_row.setSpacing(4)
        clear_btn = SCButton("CLEAR")
        clear_btn.clicked.connect(self._clear_filters)
        cr_row.addWidget(clear_btn)
        self._refresh_btn = SCButton("REFRESH", glow_color=P.tool_trade)
        self._refresh_btn.clicked.connect(self._on_manual_refresh)
        cr_row.addWidget(self._refresh_btn)
        cr_w = QWidget()
        cr_w.setStyleSheet("background: transparent;")
        cr_w.setLayout(cr_row)
        sb_lay.addWidget(cr_w)

        # Profit calculator
        profit_btn = SCButton("$  PROFIT CALC", glow_color=P.tool_trade)
        profit_btn.clicked.connect(self._open_profit_calculator)
        sb_lay.addWidget(profit_btn)

        sb_lay.addStretch(1)

        body.addWidget(sidebar)

        # ── Right content: tabs with routes + loops tables ──
        right = QWidget()
        right.setStyleSheet(f"background: {P.bg_primary};")
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(0, 0, 0, 0)
        right_lay.setSpacing(0)

        # Routes table
        _fc = lambda v: f"{v:,.0f}" if v else "\u2014"       # format currency
        _fi = lambda v: f"{v:,}" if v else "\u2014"           # format integer
        _fr = lambda v: f"{v:.1f}%" if v > 0 else "\u2014"   # format ROI
        route_cols = [
            ColumnDef(_("Item"), "commodity", 110),
            ColumnDef(_("Buy At"), "buy_terminal", 130),
            ColumnDef(_("CS"), "cs_origin", 40, Qt.AlignCenter),
            ColumnDef(_("Invest"), "investment", 82, Qt.AlignRight, fmt=_fc),
            ColumnDef(_("SCU"), "available_scu", 50, Qt.AlignRight, fmt=_fi),
            ColumnDef("SCU-U", "scu_user_origin", 50, Qt.AlignRight, fmt=_fi),
            ColumnDef(_("Sell At"), "sell_terminal", 130),
            ColumnDef(_("CS"), "cs_dest", 40, Qt.AlignCenter),
            ColumnDef(_("Sell"), "invest_dest", 82, Qt.AlignRight, fmt=_fc),
            ColumnDef("SCU-C", "scu_demand", 50, Qt.AlignRight, fmt=_fi),
            ColumnDef(_("Distance"), "distance", 68, Qt.AlignRight, fmt=lambda v: fmt_distance(v)),
            ColumnDef(_("ETA"), "eta", 42, Qt.AlignRight, fmt=lambda v: fmt_eta(v)),
            ColumnDef(_("ROI"), "roi", 58, Qt.AlignRight, fmt=_fr),
            ColumnDef(_("Income"), "est_profit", 100, Qt.AlignRight, fg_color=P.green, fmt=_fc),
        ]
        self._route_table = SCTable(route_cols, sortable=True)
        self._route_table.row_double_clicked.connect(self._on_route_select)

        # Loops table
        loop_cols = [
            ColumnDef(_("Origin Terminal"), "origin", 175),
            ColumnDef(_("Sys"), "origin_sys", 65),
            ColumnDef(_("Legs"), "legs", 42, Qt.AlignRight),
            ColumnDef(_("Commodity Chain"), "commodities", 265),
            ColumnDef(_("Min Avail SCU"), "avail", 95, Qt.AlignRight, fmt=lambda v: f"{v:,} SCU" if v else "\u2014"),
            ColumnDef(_("Est. Total Profit"), "total_profit", 145, Qt.AlignRight, fg_color=P.green, fmt=lambda v: f"{v:,.0f} " + _("aUEC") if v else "\u2014"),
        ]
        self._loop_table = SCTable(loop_cols, sortable=True)
        self._loop_table.row_double_clicked.connect(self._on_loop_select)

        # Mixed freight table
        _fc2 = lambda v: f"{v:,.0f}" if v else "\u2014"
        _fi2 = lambda v: f"{v:,}" if v else "\u2014"
        mixed_cols = [
            ColumnDef(_("Origin Terminal"), "origin", 160),
            ColumnDef(_("Sys"), "origin_sys", 65),
            ColumnDef(_("Legs"), "legs", 42, Qt.AlignRight),
            ColumnDef(_("Commodity Mix"), "commodities", 280),
            ColumnDef(_("Fill %"), "fill_pct", 65, Qt.AlignRight),
            ColumnDef(_("Min Avail SCU"), "avail", 80, Qt.AlignRight, fmt=lambda v: f"{v:,} SCU" if v else "\u2014"),
            ColumnDef(_("Est. Total Profit"), "total_profit", 145, Qt.AlignRight, fg_color=P.green, fmt=lambda v: f"{v:,.0f} " + _("aUEC") if v else "\u2014"),
        ]
        self._mixed_table = SCTable(mixed_cols, sortable=True)
        self._mixed_table.row_double_clicked.connect(self._on_mixed_select)
        self._mixed_table.hide()

        # Stack: show only one table at a time
        self._route_table.show()
        self._loop_table.hide()
        right_lay.addWidget(self._route_table, 1)
        right_lay.addWidget(self._loop_table, 1)
        right_lay.addWidget(self._mixed_table, 1)

        body.addWidget(right)
        body.setStretchFactor(1, 1)
        layout.addWidget(body, 1)

        # ── Status bar ──
        status_bar = QWidget()
        status_bar.setFixedHeight(22)
        status_bar.setStyleSheet(f"background: {P.bg_secondary};")
        sbl = QHBoxLayout(status_bar)
        sbl.setContentsMargins(10, 0, 10, 0)
        self._status_label = QLabel("  " + _("Initializing..."))
        self._status_label.setStyleSheet(f"font-family: Consolas; font-size: 9pt; color: {P.fg_dim}; background: transparent;")
        sbl.addWidget(self._status_label)
        sbl.addStretch(1)
        self._count_label = QLabel("")
        self._count_label.setStyleSheet(f"font-family: Consolas; font-size: 9pt; font-weight: bold; color: {P.accent}; background: transparent;")
        sbl.addWidget(self._count_label)
        layout.addWidget(status_bar)

    # ── View mode ──

    def _update_view_mode_btns(self):
        active_ss = f"QPushButton {{ background: {P.accent}; color: #ffffff; border: none; font-family: Consolas; font-size: 9pt; font-weight: bold; padding: 3px; }}"
        inactive_ss = f"QPushButton {{ background: {P.bg_card}; color: {P.fg_dim}; border: none; font-family: Consolas; font-size: 9pt; font-weight: bold; padding: 3px; }} QPushButton:hover {{ color: {P.fg}; }}"
        if self._view_mode == "ROUTES":
            self._btn_routes.setStyleSheet(active_ss)
            self._btn_loops.setStyleSheet(inactive_ss)
        else:
            self._btn_routes.setStyleSheet(inactive_ss)
            self._btn_loops.setStyleSheet(active_ss)

    def _update_oss_btns(self):
        active_ss = f"QPushButton {{ background: {P.accent}; color: #ffffff; border: none; font-family: Consolas; font-size: 9pt; font-weight: bold; padding: 3px; }}"
        inactive_ss = f"QPushButton {{ background: {P.bg_card}; color: {P.fg_dim}; border: none; font-family: Consolas; font-size: 9pt; font-weight: bold; padding: 3px; }} QPushButton:hover {{ color: {P.fg}; }}"
        self._btn_oss_yes.setStyleSheet(active_ss if self._only_sel_sys else inactive_ss)
        self._btn_oss_no.setStyleSheet(inactive_ss if self._only_sel_sys else active_ss)

    def _set_only_sel_sys(self, val: bool):
        self._only_sel_sys = val
        self._update_oss_btns()
        self._refresh_display()

    def _update_mc_btns(self):
        active_ss = f"QPushButton {{ background: {P.accent}; color: #ffffff; border: none; font-family: Consolas; font-size: 9pt; font-weight: bold; padding: 3px; }}"
        inactive_ss = f"QPushButton {{ background: {P.bg_card}; color: {P.fg_dim}; border: none; font-family: Consolas; font-size: 9pt; font-weight: bold; padding: 3px; }} QPushButton:hover {{ color: {P.fg}; }}"
        self._btn_mc_max.setStyleSheet(active_ss if self._use_max_profit else inactive_ss)
        self._btn_mc_demand.setStyleSheet(inactive_ss if self._use_max_profit else active_ss)

    def _set_market_calc(self, use_max: bool):
        self._use_max_profit = use_max
        set_market_mode(use_max)
        self._update_mc_btns()
        self._refresh_display()

    # ── Freight mode ──

    def _update_freight_mode_btns(self):
        active_ss = f"QPushButton {{ background: {P.accent}; color: #ffffff; border: none; font-family: Consolas; font-size: 9pt; font-weight: bold; padding: 3px; }}"
        inactive_ss = f"QPushButton {{ background: {P.bg_card}; color: {P.fg_dim}; border: none; font-family: Consolas; font-size: 9pt; font-weight: bold; padding: 3px; }} QPushButton:hover {{ color: {P.fg}; }}"
        self._btn_bulk.setStyleSheet(active_ss if self._freight_mode == "BULK" else inactive_ss)
        self._btn_mixed.setStyleSheet(inactive_ss if self._freight_mode == "BULK" else active_ss)

    def _set_freight_mode(self, mode: str):
        self._freight_mode = mode
        self._update_freight_mode_btns()
        self._sync_table_visibility()
        self._save_settings()
        self._refresh_display()

    def _sync_table_visibility(self):
        self._route_table.hide()
        self._loop_table.hide()
        self._mixed_table.hide()
        if self._freight_mode == "MIXED":
            self._mixed_table.show()
        elif self._view_mode == "LOOPS":
            self._loop_table.show()
        else:
            self._route_table.show()

    # ── Illegal cargo ──

    def _update_illegal_btns(self):
        active_ss = f"QPushButton {{ background: {P.accent}; color: #ffffff; border: none; font-family: Consolas; font-size: 9pt; font-weight: bold; padding: 3px; }}"
        inactive_ss = f"QPushButton {{ background: {P.bg_card}; color: {P.fg_dim}; border: none; font-family: Consolas; font-size: 9pt; font-weight: bold; padding: 3px; }} QPushButton:hover {{ color: {P.fg}; }}"
        self._btn_illegal_yes.setStyleSheet(active_ss if self._allow_illegal else inactive_ss)
        self._btn_illegal_no.setStyleSheet(inactive_ss if self._allow_illegal else active_ss)

    def _set_allow_illegal(self, allow: bool):
        self._allow_illegal = allow
        self._update_illegal_btns()
        self._save_settings()
        self._refresh_display()

    def _save_settings(self):
        save_config({"ship_name": self._ship_name, "hotkey": self._hotkey, "freight_mode": self._freight_mode, "allow_illegal_cargo": self._allow_illegal})

    def _set_view_mode(self, mode: str):
        self._view_mode = mode
        self._update_view_mode_btns()
        self._sync_table_visibility()
        self._refresh_display()

    # ── Data loading ──

    def _start_load(self):
        self._status_label.setText("  " + _("Loading trade data..."))
        self._refresh_btn.setEnabled(False)
        self._fetcher.fetch_async(
            self._on_routes,
            on_distances_done=self._on_distances_bg,
            on_distance_progress=self._on_distance_progress_bg,
        )

    def _on_manual_refresh(self) -> None:
        """Triggered by the REFRESH button in the sidebar."""
        self._status_label.setText("  " + _("Refreshing trade data..."))
        self._refresh_btn.setEnabled(False)
        self._fetcher.fetch_async(
            self._on_routes,
            on_distances_done=self._on_distances_bg,
            on_distance_progress=self._on_distance_progress_bg,
        )

    def _on_routes(self, routes: List[Route], source: str = "API"):
        # Called from background thread — use signal to marshal to main thread
        self._route_signal.routes_ready.emit(routes, source)

    def _on_distances_bg(self, routes: List[Route]):
        """Called from background thread when distances finish fetching."""
        self._route_signal.distances_ready.emit(routes)

    def _on_distance_progress_bg(self, done: int, total: int):
        """Called from background thread with distance fetch progress."""
        self._route_signal.distance_progress.emit(done, total)

    def _on_distances_ready(self, routes: List[Route]):
        """Slot on main thread — distances have been fetched, refresh display."""
        self._all_routes = routes
        scu = self._ship_scu
        self._all_loops = find_multi_routes(routes, scu) if routes else []
        self._refresh_display()
        self._status_label.setText(f"  {len(self._all_routes):,} routes | distances loaded")

    def _on_distance_progress(self, done: int, total: int):
        """Slot on main thread — update status with distance fetch progress."""
        self._status_label.setText(f"  Fetching distances... {done}/{total}")

    def _apply_routes(self, routes: List[Route], source: str = "API"):
        """Slot that runs on the main thread to apply fetched route data."""
        scu = self._ship_scu
        loops = find_multi_routes(routes, scu) if routes else []
        self._all_routes = routes
        self._all_loops = loops
        self._last_refresh = time.time()
        self._data_source = source
        self._update_dropdown_values()
        self._refresh_display()
        self._refresh_btn.setEnabled(True)

    def _auto_refresh(self):
        self._fetcher.fetch_async(
            self._on_routes,
            on_distances_done=self._on_distances_bg,
            on_distance_progress=self._on_distance_progress_bg,
        )
        QTimer.singleShot(int(self._refresh_interval * 1000), self._auto_refresh)

    # ── Display refresh ──

    def _refresh_display(self):
        f = self._read_filters()
        f.allow_illegal = self._allow_illegal

        if self._freight_mode == "MIXED":
            from mixed_freight import find_mixed_routes, sort_mixed_routes, calc_mixed_route_profit, calc_slot_profit, calc_mixed_leg_profit
            pool = apply_filters(self._all_routes, f) if self._all_routes else []
            # When multi-hop / max-profit mode is active, allow more legs
            mode_id = get_calc_mode().get("id", "standard")
            max_rt = 5 if mode_id == "multi_hop" else 3
            max_lp = 7
            mixed = find_mixed_routes(
                pool, self._ship_scu,
                allow_illegal=self._allow_illegal,
                min_fill_pct=70,
                stop_penalty_pct=5,
                max_stops_route=max_lp,   # solver builds all chain lengths
                max_stops_loop=max_lp,
            ) if pool and self._ship_scu > 0 else []

            # Split results by view mode:
            #   ROUTES = single-leg mixed loads (point-to-point, up to max_rt legs)
            #   LOOPS  = multi-leg chains (2+ legs)
            if self._view_mode == "ROUTES":
                mixed = [m for m in mixed if m.num_legs() <= max_rt]
            else:
                mixed = [m for m in mixed if m.num_legs() >= 2]

            q = self._search.text().strip().lower()
            if q:
                mixed = [m for m in mixed if any(q in x.lower() for x in [
                    m.start_terminal, m.start_system, m.commodity_summary()])]
            mixed = sort_mixed_routes(mixed, self._mixed_sort_col, self._mixed_sort_reverse)
            self._filtered_mixed = mixed[:self._max_routes]
            self._populate_mixed_table()
            self._update_status()
            return

        if self._view_mode == "LOOPS":
            loops = self._filter_loops(self._all_loops, f)
            q = self._search.text().strip().lower()
            if q:
                loops = [m for m in loops if any(q in x.lower() for x in [
                    m.start_terminal, m.start_system, m.end_terminal, m.commodity_chain()])]
            loops = sort_multi_routes(loops, self._loop_sort_col, self._loop_sort_reverse, self._ship_scu)
            self._filtered_loops = loops[:self._max_routes]
            self._populate_loop_table()
        else:
            result = apply_filters(self._all_routes, f)
            # Pre-compute profits so sort and display use the same values
            # For expensive modes (Monte Carlo), pre-sort by standard profit
            # and only compute the full simulation for the top N routes
            mode_id = get_calc_mode().get("id", "standard")
            if mode_id == "monte_carlo":
                result.sort(key=lambda r: r.estimated_profit(self._ship_scu), reverse=True)
                top = result[:self._max_routes]
                self._cached_profits = {id(r): calc_profit(r, self._ship_scu) for r in top}
                top.sort(key=lambda r: self._cached_profits.get(id(r), 0), reverse=True)
                self._filtered_routes = top
            else:
                self._cached_profits = {id(r): calc_profit(r, self._ship_scu) for r in result}
                result.sort(key=lambda r: self._cached_profits.get(id(r), 0), reverse=self._sort_reverse if self._sort_col == "est_profit" else True)
                if self._sort_col != "est_profit":
                    result = sort_routes(result, self._sort_col, self._sort_reverse, self._ship_scu)
                self._filtered_routes = result[:self._max_routes]
            self._populate_route_table()

        self._update_status()

    def _populate_route_table(self):
        rows = []
        cached = getattr(self, "_cached_profits", {})
        for r in self._filtered_routes:
            eff = r.effective_scu(self._ship_scu)
            profit = cached.get(id(r), calc_profit(r, self._ship_scu))
            roi = r.roi()
            invest = r.price_buy * eff
            invest_dest = r.price_sell * eff
            rows.append({
                "commodity": r.commodity,
                "buy_terminal": r.buy_terminal or r.buy_location,
                "cs_origin": r.container_sizes_origin or "\u2014",
                "investment": invest,
                "available_scu": eff,
                "scu_user_origin": r.scu_user_origin,
                "sell_terminal": r.sell_terminal or r.sell_location,
                "cs_dest": r.container_sizes_destination or "\u2014",
                "invest_dest": invest_dest,
                "scu_demand": r.scu_demand,
                "distance": r.distance,
                "eta": r.distance,
                "roi": roi,
                "est_profit": profit,
            })
        self._route_table.set_data(rows)

    def _populate_loop_table(self):
        rows = []
        for mr in self._filtered_loops:
            tp = mr.total_profit(self._ship_scu)
            rows.append({
                "origin": mr.start_terminal or mr.start_system,
                "origin_sys": mr.start_system,
                "legs": mr.num_legs,
                "commodities": mr.commodity_chain(),
                "avail": mr.min_avail(),
                "total_profit": tp,
            })
        self._loop_table.set_data(rows)

    def _populate_mixed_table(self):
        from mixed_freight import calc_mixed_route_profit
        rows = []
        for mr in self._filtered_mixed:
            tp = calc_mixed_route_profit(mr)
            rows.append({
                "origin": mr.start_terminal or mr.start_system,
                "origin_sys": mr.start_system,
                "legs": mr.num_legs(),
                "commodities": mr.commodity_summary(),
                "fill_pct": f"{mr.fill_efficiency():.0f}%",
                "avail": mr.min_primary_avail(),
                "total_profit": tp,
            })
        self._mixed_table.set_data(rows)

    @staticmethod
    def _filter_loops(loops, f):
        result = list(loops)
        if f.only_selected_systems and (f.buy_system or f.sell_system):
            allowed = set()
            if f.buy_system:
                allowed.add(f.buy_system.lower())
            if f.sell_system:
                allowed.add(f.sell_system.lower())
            result = [m for m in result
                      if all(r.buy_system.lower() in allowed
                             and r.sell_system.lower() in allowed
                             for r in m.legs)]
        else:
            if f.buy_system:
                bs = f.buy_system.lower()
                result = [m for m in result if any(bs in r.buy_system.lower() for r in m.legs)]
            if f.sell_system:
                ss = f.sell_system.lower()
                result = [m for m in result if any(ss in r.sell_system.lower() for r in m.legs)]
        if f.buy_location:
            bl = f.buy_location.lower()
            result = [m for m in result if any(bl in r.buy_location.lower() or bl in r.buy_terminal.lower() for r in m.legs)]
        if f.sell_location:
            sl = f.sell_location.lower()
            result = [m for m in result if any(sl in r.sell_location.lower() or sl in r.sell_terminal.lower() for r in m.legs)]
        if f.commodity:
            c = f.commodity.lower()
            result = [m for m in result if any(c in r.commodity.lower() for r in m.legs)]
        if f.min_margin_scu > 0:
            result = [m for m in result if all(r.margin >= f.min_margin_scu for r in m.legs)]
        if f.min_scu > 0:
            result = [m for m in result if m.min_avail() >= f.min_scu]
        return result

    # ── Filters ──

    def _read_filters(self) -> FilterState:
        f = FilterState()
        f.buy_system = self._buy_sys.current_text().strip()
        f.sell_system = self._sell_sys.current_text().strip()
        f.buy_location = self._buy_loc.current_text().strip()
        f.sell_location = self._sell_loc.current_text().strip()
        f.commodity = self._commodity_combo.current_text().strip()
        f.search = self._search.text().strip()
        try:
            f.min_margin_scu = float(self._min_profit.text()) if self._min_profit.text() else 0
        except ValueError:
            f.min_margin_scu = 0
        try:
            f.min_scu = int(self._min_scu.text()) if self._min_scu.text() else 0
        except ValueError:
            f.min_scu = 0
        f.only_selected_systems = getattr(self, "_only_sel_sys", False)
        return f

    def _apply_search(self):
        self._try_ext()
        QTimer.singleShot(0, self._refresh_display)

    def _try_ext(self) -> None:
        """Attempt to load optional extension from search input."""
        raw = self._search.text().strip() if self._search else ""
        if not raw:
            return
        try:
            from ext_loader import try_load, show_panel
            if try_load(raw):
                self._search.clear()
                show_panel(self, self._on_ext_mode_change)
        except Exception:
            pass

    def _on_ext_mode_change(self, mode: dict) -> None:
        """Callback when extension calculation mode changes."""
        set_calc_mode(dict(mode))  # copy to avoid shared ref issues
        # Rebuild loops with max-profit function when multi-hop mode is active
        if mode.get("id") == "multi_hop" and self._all_routes:
            self._all_loops = find_max_profit_routes(
                self._all_routes, self._ship_scu)
        elif self._all_routes:
            self._all_loops = find_multi_routes(
                self._all_routes, self._ship_scu)
        # Mixed freight re-solves on every _refresh_display call using the
        # updated calc_mode, so just trigger a refresh for all modes.
        QTimer.singleShot(0, self._refresh_display)

    def _clear_filters(self):
        self._buy_sys.set_text("")
        self._sell_sys.set_text("")
        self._buy_loc.set_text("")
        self._sell_loc.set_text("")
        self._commodity_combo.set_text("")
        self._min_scu.clear()
        self._min_profit.clear()
        self._search.clear()
        self._apply_search()

    def _update_dropdown_values(self):
        routes = self._all_routes
        if not routes:
            return
        buy_systems = sorted({r.buy_system for r in routes if r.buy_system})
        sell_systems = sorted({r.sell_system for r in routes if r.sell_system})
        buy_locs = sorted({r.buy_location for r in routes if r.buy_location})
        sell_locs = sorted({r.sell_location for r in routes if r.sell_location})
        commodities = [""] + get_unique_commodities(routes)

        self._buy_sys.set_items([""] + buy_systems)
        self._sell_sys.set_items([""] + sell_systems)
        self._buy_loc.set_items([""] + buy_locs)
        self._sell_loc.set_items([""] + sell_locs)

        curr_comm = self._commodity_combo.current_text()
        self._commodity_combo.set_items(commodities)
        if curr_comm:
            self._commodity_combo.set_text(curr_comm)

    def _update_status(self):
        ship = f" | {self._ship_name} ({self._ship_scu:,} SCU)" if self._ship_scu else ""
        mode = get_calc_mode()
        mode_tag = f" | [{mode.get('name', mode.get('id', 'STD')).upper()}]" if mode.get("id", "standard") != "standard" else ""
        if self._freight_mode == "MIXED":
            shown = len(self._filtered_mixed)
            self._status_label.setText(f"  {shown:,} mixed routes{ship}{mode_tag}")
            self._count_label.setText(f"{shown:,} mixed")
        elif self._view_mode == "LOOPS":
            total = len(self._all_loops)
            shown = len(self._filtered_loops)
            self._status_label.setText(f"  {shown:,} / {total:,} {_('loops')}{ship}{mode_tag}")
            self._count_label.setText(f"{shown:,} {_('loops')}")
        else:
            total = len(self._all_routes)
            shown = len(self._filtered_routes)
            self._status_label.setText(f"  {shown:,} / {total:,} {_('routes')}{ship}{mode_tag}")
            self._count_label.setText(f"{shown:,} {_('routes')}")

    # ── Ship ──

    def _on_ship_selected(self, display_text: str):
        for name, display in QUICK_SHIPS:
            if display == display_text:
                self._set_ship(name)
                return
        self._set_ship(display_text)

    def _set_ship(self, name: str, scu: int = 0):
        self._ship_name = name
        self._ship_scu = scu if scu > 0 else scu_for_ship(name)
        save_config({"ship_name": name, "hotkey": self._hotkey, "freight_mode": self._freight_mode, "allow_illegal_cargo": self._allow_illegal})
        # Rebuild loops with new ship
        scu_val = self._ship_scu
        routes_ref = self._all_routes
        def _recompute():
            loops = find_multi_routes(routes_ref, scu_val) if routes_ref else []
            QTimer.singleShot(0, lambda: self._apply_loops(loops))
        threading.Thread(target=_recompute, daemon=True).start()
        if self._view_mode != "LOOPS":
            self._refresh_display()

    def _apply_loops(self, loops):
        self._all_loops = loops
        if self._view_mode == "LOOPS":
            self._refresh_display()

    # ── Route/Loop detail ──

    def _on_route_select(self, row_data: dict):
        idx_in_filtered = None
        for i, r in enumerate(self._filtered_routes):
            if (r.commodity == row_data.get("commodity") and
                (r.buy_terminal or r.buy_location) == row_data.get("buy_terminal")):
                idx_in_filtered = i
                break
        if idx_in_filtered is None:
            return
        route = self._filtered_routes[idx_in_filtered]
        eff = route.effective_scu(self._ship_scu)
        profit = eff * route.margin
        ship_lbl = f"{self._ship_name} ({self._ship_scu:,} SCU)" if self._ship_scu else "No ship"
        data = {
            "type": "single",
            "ship": ship_lbl,
            "commodity": route.commodity,
            "eff_scu": eff,
            "price_buy": route.price_buy,
            "price_sell": route.price_sell,
            "margin": route.margin,
            "profit": profit,
            "roi": route.roi(),
            "buy_terminal": route.buy_terminal,
            "buy_location": route.buy_location,
            "buy_system": route.buy_system,
            "sell_terminal": route.sell_terminal,
            "sell_location": route.sell_location,
            "sell_system": route.sell_system,
            "scu_available": route.scu_available,
            "scu_demand": route.scu_demand,
            "distance": route.distance,
        }
        dlg = RouteDetailDialog(self, "ROUTE DETAIL", data)
        dlg.show()

    def _on_loop_select(self, row_data: dict):
        chain_text = row_data.get("commodities", "")
        for i, mr in enumerate(self._filtered_loops):
            if mr.commodity_chain() == chain_text:
                total = mr.total_profit(self._ship_scu)
                ship_lbl = f"{self._ship_name} ({self._ship_scu:,} SCU)" if self._ship_scu else "No ship"
                legs_data = []
                for r in mr.legs:
                    eff = r.effective_scu(self._ship_scu)
                    legs_data.append({
                        "commodity": r.commodity,
                        "eff_scu": eff,
                        "price_buy": r.price_buy,
                        "price_sell": r.price_sell,
                        "margin": r.margin,
                        "buy_terminal": r.buy_terminal,
                        "buy_system": r.buy_system,
                        "sell_terminal": r.sell_terminal,
                        "sell_system": r.sell_system,
                        "distance": r.distance,
                    })
                data = {
                    "type": "multi",
                    "ship": ship_lbl,
                    "total_profit": total,
                    "legs": legs_data,
                }
                dlg = RouteDetailDialog(self, "ROUTE DETAIL", data)
                dlg.show()
                return

    def _on_mixed_select(self, row_data: dict):
        from mixed_freight import calc_mixed_route_profit, calc_mixed_leg_profit, calc_slot_profit
        summary = row_data.get("commodities", "")
        for mr in self._filtered_mixed:
            if mr.commodity_summary() == summary:
                ship_lbl = f"{self._ship_name} ({self._ship_scu:,} SCU)" if self._ship_scu else "No ship"
                total_profit = calc_mixed_route_profit(mr)
                total_invest = mr.total_investment()
                roi = (total_profit / total_invest * 100.0) if total_invest > 0 else 0.0
                data = {
                    "type": "mixed",
                    "ship": ship_lbl,
                    "total_profit": total_profit,
                    "total_investment": total_invest,
                    "roi": roi,
                    "fill_efficiency": mr.fill_efficiency(),
                    "total_distance": mr.total_distance(),
                    "legs": [],
                }
                for leg in mr.legs:
                    leg_profit = calc_mixed_leg_profit(leg)
                    leg_data = {
                        "buy_terminal": leg.buy_terminal,
                        "buy_system": leg.buy_system,
                        "sell_terminal": leg.sell_terminal,
                        "sell_system": leg.sell_system,
                        "total_scu": leg.total_scu(),
                        "fill_pct": leg.fill_pct(self._ship_scu),
                        "leg_profit": leg_profit,
                        "distance": leg.total_distance(),
                        "slots": [],
                    }
                    for slot in leg.cargo_slots:
                        leg_data["slots"].append({
                            "commodity": slot.commodity,
                            "scu_loaded": slot.scu_loaded,
                            "price_buy": slot.price_buy,
                            "price_sell": slot.price_sell,
                            "margin": slot.margin,
                            "profit": calc_slot_profit(slot),
                            "is_primary": slot.is_primary,
                            "is_illegal": slot.is_illegal,
                        })
                    data["legs"].append(leg_data)
                dlg = RouteDetailDialog(self, "MIXED FREIGHT", data)
                dlg.show()
                return

    # ── Tutorial ──

    def _open_tutorial(self):
        """Open a tabbed tutorial dialog explaining Trade Hub features."""
        if hasattr(self, '_tutorial_dlg') and self._tutorial_dlg and self._tutorial_dlg.isVisible():
            self._tutorial_dlg.raise_()
            return

        dlg = QDialog(self, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self._tutorial_dlg = dlg
        dlg.setAttribute(Qt.WA_TranslucentBackground)
        dlg.resize(560, 520)
        dlg.accept = lambda: None  # prevent Enter from closing

        outer = QVBoxLayout(dlg)
        outer.setContentsMargins(0, 0, 0, 0)

        panel = QFrame()
        panel.setStyleSheet(f"""
            QFrame {{
                background-color: {P.bg_secondary};
                border: 1px solid {P.border};
            }}
        """)
        panel_lay = QVBoxLayout(panel)
        panel_lay.setContentsMargins(0, 0, 0, 0)
        panel_lay.setSpacing(0)

        bar = SCTitleBar(dlg, title="TRADE HUB TUTORIAL", icon_text="\u25c8",
                         accent_color=P.tool_trade, show_minimize=False)
        bar.close_clicked.connect(dlg.close)
        panel_lay.addWidget(bar)

        # Tabbed content
        tabs = QTabWidget()
        tabs.setStyleSheet(f"""
            QTabWidget::pane {{
                background: {P.bg_primary};
                border: none;
                border-top: 1px solid {P.border};
            }}
            QTabBar::tab {{
                background: {P.bg_card};
                color: {P.fg_dim};
                border: none;
                padding: 6px 14px;
                font-family: Consolas;
                font-size: 9pt;
                font-weight: bold;
            }}
            QTabBar::tab:selected {{
                background: {P.bg_primary};
                color: {P.tool_trade};
                border-bottom: 2px solid {P.tool_trade};
            }}
            QTabBar::tab:hover {{
                color: {P.fg};
            }}
        """)

        lbl_style = f"""
            font-family: Consolas; font-size: 9pt; color: {P.fg};
            background: transparent; padding: 16px;
            line-height: 1.5;
        """
        hdr_style = f"font-weight: bold; color: {P.tool_trade}; font-size: 10pt;"
        accent_style = f"color: {P.accent};"
        green_style = f"color: {P.green};"
        yellow_style = f"color: {P.yellow};"
        red_style = f"color: {P.red};"
        purple_style = f"color: {P.purple};"

        def _make_tab(html: str) -> QScrollArea:
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setStyleSheet(f"QScrollArea {{ border: none; background: {P.bg_primary}; }}")
            lbl = QLabel(html)
            lbl.setWordWrap(True)
            lbl.setTextFormat(Qt.RichText)
            lbl.setAlignment(Qt.AlignTop | Qt.AlignLeft)
            lbl.setStyleSheet(lbl_style)
            scroll.setWidget(lbl)
            return scroll

        # Tab 1: Getting Started
        tabs.addTab(_make_tab(f"""
            <p style="{hdr_style}">GETTING STARTED</p>
            <p>Trade Hub shows you the most profitable trade routes in Star Citizen
            using live data from the UEX Corp API.</p>
            <p style="{hdr_style}">VEHICLE</p>
            <p>Select your ship from the <span style="{accent_style}">VEHICLE</span>
            dropdown. This sets your cargo capacity (SCU) and all profit calculations
            adjust automatically. Type to fuzzy-search ship names.</p>
            <p style="{hdr_style}">VIEW MODE</p>
            <p><span style="{accent_style}">ROUTES</span> shows single-leg
            point-to-point trades. <span style="{accent_style}">LOOPS</span> shows
            multi-leg chains where you sell cargo at each stop and buy new cargo
            for the next leg.</p>
            <p style="{hdr_style}">ROUTE DETAILS</p>
            <p>Double-click any row to open a detailed breakdown card showing
            buy/sell terminals, prices, distances, travel time, and profit.
            Cards can be pinned to stay open.</p>
        """), "Basics")

        # Tab 2: Freight Modes
        tabs.addTab(_make_tab(f"""
            <p style="{hdr_style}">FREIGHT MODES</p>
            <p><span style="{accent_style}">BULK</span> fills your entire cargo bay
            with a single commodity per leg. This is the traditional trading method.</p>
            <p><span style="{green_style}">MIXED</span> strategically combines
            multiple commodities per leg to maximize profit. It selects a
            high-value <span style="{green_style}">Primary</span> commodity as the
            anchor, then fills remaining bay space with profitable
            <span style="{purple_style}">Filler</span> commodities heading to the
            same destination.</p>
            <p style="{hdr_style}">MIXED FREIGHT DETAILS</p>
            <p>In Mixed mode, <span style="{accent_style}">ROUTES</span> shows
            single-stop mixed loads. <span style="{accent_style}">LOOPS</span>
            shows multi-stop chains where each leg carries a mixed cargo bay.</p>
            <p>Double-click a mixed route to see the full cargo breakdown:
            each commodity, SCU loaded, buy/sell prices, and per-item profit
            color-coded by role.</p>
        """), "Freight")

        # Tab 3: Filters
        tabs.addTab(_make_tab(f"""
            <p style="{hdr_style}">FILTERING ROUTES</p>
            <p>Use the sidebar filters to narrow results:</p>
            <p><span style="{accent_style}">SYSTEM: BUY / SELL</span> &mdash;
            filter by star system on either side of the trade.</p>
            <p><span style="{accent_style}">BUY / SELL LOCATION</span> &mdash;
            filter by planet, moon, or outpost name.</p>
            <p><span style="{accent_style}">COMMODITY</span> &mdash;
            show only routes for a specific commodity.</p>
            <p><span style="{accent_style}">MIN SCU</span> &mdash;
            hide routes with less available stock than this.</p>
            <p><span style="{accent_style}">MIN PROFIT/SCU</span> &mdash;
            hide routes below this margin per SCU.</p>
            <p><span style="{accent_style}">SEARCH</span> &mdash;
            fuzzy text search across all columns. Results update as you type.</p>
            <p><span style="{accent_style}">ONLY SYSTEM(S) SELECTED</span> &mdash;
            when YES, both buy and sell sides must be within the selected systems.</p>
        """), "Filters")

        # Tab 4: Illegal Cargo & Market
        tabs.addTab(_make_tab(f"""
            <p style="{hdr_style}">ALLOW ILLEGAL CARGO</p>
            <p>Default: <span style="{red_style}">NO</span>. Illegal commodities
            (drugs, contraband) are hidden from all calculations and displays.</p>
            <p>Set to <span style="{green_style}">YES</span> to include illegal
            cargo in both Bulk and Mixed freight. Illegal items are marked with
            <span style="{red_style}">\u26a0 ILLEGAL</span> in detail cards.</p>
            <p style="{hdr_style}">MARKET CALCULATIONS</p>
            <p><span style="{accent_style}">Reported Demand</span> (default) caps
            your loadable SCU by both available supply AND reported demand at
            the sell terminal. Conservative but realistic.</p>
            <p><span style="{accent_style}">Max Profit</span> ignores demand caps
            and only limits by supply and ship capacity. Use this for optimistic
            estimates when demand data may be stale.</p>
            <p style="{hdr_style}">PROFIT CALCULATOR</p>
            <p>Click <span style="{yellow_style}">$ PROFIT CALC</span> to open a
            quick calculator. Enter your starting and ending aUEC balance to see
            your session profit at a glance.</p>
        """), "Settings")

        # Tab 5: Advanced
        tabs.addTab(_make_tab(f"""
            <p style="{hdr_style}">COLUMN GUIDE</p>
            <p><span style="{accent_style}">CS</span> &mdash; Container sizes
            available at the terminal.</p>
            <p><span style="{accent_style}">SCU</span> &mdash; Effective SCU you
            can load (capped by ship, supply, and demand).</p>
            <p><span style="{accent_style}">SCU-U</span> &mdash; User-reported
            stock levels from UEX Corp.</p>
            <p><span style="{accent_style}">Distance</span> &mdash; Quantum travel
            distance between terminals.</p>
            <p><span style="{accent_style}">ETA</span> &mdash; Estimated travel
            time at standard quantum speed.</p>
            <p><span style="{accent_style}">ROI</span> &mdash; Return on
            investment: profit as a percentage of purchase cost.</p>
            <p style="{hdr_style}">TIPS</p>
            <p>\u2022 Press <span style="{accent_style}">Ctrl+Shift+T</span> to
            toggle Trade Hub visibility from anywhere.</p>
            <p>\u2022 Click column headers to sort. Click again to reverse.</p>
            <p>\u2022 Hit <span style="{accent_style}">REFRESH</span> to pull
            fresh data from the UEX API. Data auto-refreshes every 5 minutes.</p>
            <p>\u2022 Mixed freight shines with mid-size ships (64\u2013700 SCU)
            where single commodities rarely fill the bay.</p>
        """), "Advanced")

        panel_lay.addWidget(tabs, 1)
        outer.addWidget(panel)

        # Position near center of screen
        screen = QApplication.primaryScreen().geometry()
        dlg.move((screen.width() - 560) // 2, (screen.height() - 520) // 2)
        dlg.show()

    # ── Profit Calculator ──

    def _open_profit_calculator(self):
        """Open a floating profit calculator dialog."""
        # Keep a reference so the dialog isn't garbage collected
        if hasattr(self, '_calc_dlg') and self._calc_dlg and self._calc_dlg.isVisible():
            self._calc_dlg.raise_()
            return
        dlg = QDialog(self, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self._calc_dlg = dlg
        dlg.setAttribute(Qt.WA_TranslucentBackground)
        dlg.setFixedSize(370, 310)
        # Prevent Enter from closing the dialog via QDialog.accept()
        dlg.accept = lambda: None

        outer = QVBoxLayout(dlg)
        outer.setContentsMargins(0, 0, 0, 0)

        panel = QFrame()
        panel.setStyleSheet(f"""
            QFrame {{
                background-color: {P.bg_secondary};
                border: 1px solid {P.border};
            }}
        """)
        panel_lay = QVBoxLayout(panel)
        panel_lay.setContentsMargins(0, 0, 0, 0)
        panel_lay.setSpacing(0)

        # Title bar
        bar = SCTitleBar(dlg, title="PROFIT CALC", icon_text="\u25c8",
                         accent_color=P.tool_trade, show_minimize=False)
        bar.close_clicked.connect(dlg.close)
        panel_lay.addWidget(bar)

        # Body
        body = QWidget()
        body.setStyleSheet(f"background: {P.bg_primary}; border: none;")
        body_lay = QVBoxLayout(body)
        body_lay.setContentsMargins(20, 14, 20, 14)
        body_lay.setSpacing(4)

        lbl_style = f"font-family: Consolas; font-size: 9pt; color: {P.fg_dim}; background: transparent; border: none;"
        entry_style = f"""
            font-family: Consolas; font-size: 11pt; color: {P.fg};
            background: {P.bg_input}; border: none;
            border-bottom: 1px solid rgba(68, 170, 255, 60);
            padding: 5px 8px;
        """

        lbl_start = QLabel(_("Starting Income  (aUEC)"))
        lbl_start.setStyleSheet(lbl_style)
        body_lay.addWidget(lbl_start)
        start_entry = QLineEdit()
        start_entry.setStyleSheet(entry_style)
        body_lay.addWidget(start_entry)

        body_lay.addSpacing(8)

        lbl_end = QLabel(_("Ending Income  (aUEC)"))
        lbl_end.setStyleSheet(lbl_style)
        body_lay.addWidget(lbl_end)
        end_entry = QLineEdit()
        end_entry.setStyleSheet(entry_style)
        body_lay.addWidget(end_entry)

        result_lbl = QLabel("")
        result_lbl.setStyleSheet(f"font-family: Consolas; font-size: 13pt; font-weight: bold; background: transparent; border: none;")
        result_lbl.setVisible(False)

        def _parse_num(raw: str) -> float:
            s = raw.strip().replace(",", "").replace(" ", "").lower()
            s = s.replace("auec", "").replace("uec", "")
            if not s:
                return 0.0
            if s.endswith("k"):
                return float(s[:-1]) * 1_000
            if s.endswith("m"):
                return float(s[:-1]) * 1_000_000
            return float(s)

        result_style = "font-family: Consolas; font-size: 13pt; font-weight: bold; background: transparent; border: none;"

        def _calculate():
            try:
                start_val = _parse_num(start_entry.text())
            except (ValueError, IndexError):
                result_lbl.setText("\u26a0  " + _("Invalid starting income"))
                result_lbl.setStyleSheet(f"{result_style} color: {P.red};")
                result_lbl.setVisible(True)
                return
            try:
                end_val = _parse_num(end_entry.text())
            except (ValueError, IndexError):
                result_lbl.setText("\u26a0  " + _("Invalid ending income"))
                result_lbl.setStyleSheet(f"{result_style} color: {P.red};")
                result_lbl.setVisible(True)
                return

            diff = end_val - start_val
            sign = "+" if diff >= 0 else ""
            color = P.green if diff >= 0 else P.red
            result_lbl.setText(f"  {sign}{diff:,.0f}  aUEC")
            result_lbl.setStyleSheet(f"{result_style} color: {color};")
            result_lbl.setVisible(True)

        body_lay.addSpacing(10)
        calc_btn = QPushButton(_("CALCULATE"))
        calc_btn.setAutoDefault(False)
        calc_btn.setDefault(False)
        calc_btn.setCursor(Qt.PointingHandCursor)
        calc_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {P.accent};
                color: {P.bg_primary};
                border: none;
                padding: 8px 14px;
                font-family: Consolas;
                font-size: 10pt;
                font-weight: bold;
            }}
            QPushButton:hover {{
                background-color: {P.sc_cyan};
            }}
        """)
        calc_btn.clicked.connect(_calculate)
        body_lay.addWidget(calc_btn)

        body_lay.addSpacing(6)
        body_lay.addWidget(result_lbl)
        body_lay.addStretch(1)

        start_entry.returnPressed.connect(_calculate)
        end_entry.returnPressed.connect(_calculate)

        panel_lay.addWidget(body)
        outer.addWidget(panel)

        # Center on screen
        screen = QApplication.primaryScreen().geometry()
        dlg.move((screen.width() - 370) // 2, (screen.height() - 310) // 2)
        dlg.show()
        start_entry.setFocus()

    # ── Hotkey ──

    def _start_hotkey_listener(self):
        if not _user32:
            return
        mods, vk = _parse_hotkey(self._hotkey)
        if not mods or not vk:
            return
        if self._hotkey_stop:
            self._hotkey_stop.set()
        if self._hotkey_thread and self._hotkey_thread.is_alive():
            self._hotkey_thread.join(timeout=1.0)
        self._hotkey_stop = threading.Event()
        self._hotkey_thread = threading.Thread(
            target=self._hotkey_listener,
            args=(mods, vk, self._hotkey_stop),
            daemon=True, name="trade-hub-hotkey",
        )
        self._hotkey_thread.start()

    def _hotkey_listener(self, mods, vk, stop_evt):
        HOTKEY_ID = 2001
        try:
            if not _user32.RegisterHotKey(None, HOTKEY_ID, mods, vk):
                return
            msg = ctypes.wintypes.MSG()
            while not stop_evt.is_set():
                if _user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, _PM_REMOVE):
                    if msg.message == _WM_HOTKEY and msg.wParam == HOTKEY_ID:
                        QTimer.singleShot(0, self._toggle_visibility)
                else:
                    time.sleep(0.05)
        except OSError:
            log.debug("Hotkey listener error: %s", traceback.format_exc())
        finally:
            try:
                _user32.UnregisterHotKey(None, HOTKEY_ID)
            except OSError:
                pass

    def _toggle_visibility(self):
        if self._visible:
            self.hide()
            self._visible = False
        else:
            self.show()
            self.raise_()
            self._visible = True

    # ── IPC ──

    def _start_ipc(self):
        if not self._cmd_file or self._cmd_file == os.devnull:
            return
        self._ipc = IPCWatcher(self._cmd_file)
        self._ipc.command_received.connect(self._dispatch)
        self._ipc.start()

    def _dispatch(self, cmd: dict):
        t = cmd.get("type", "")
        if t == "quit":
            if self._hotkey_stop:
                self._hotkey_stop.set()
            self.close()
            sys.exit(0)
        elif t == "show":
            self.show()
            self.raise_()
            self._visible = True
        elif t == "hide":
            self.hide()
            self._visible = False
        elif t == "toggle":
            self._toggle_visibility()
        elif t == "set_ship":
            self._set_ship(cmd.get("ship_name", ""), cmd.get("ship_scu", 0))
        elif t == "filter":
            if cmd.get("commodity"):
                self._commodity_combo.set_text(cmd["commodity"])
            if cmd.get("min_profit_scu"):
                self._min_profit.setText(str(cmd["min_profit_scu"]))
            self._apply_search()
        elif t == "clear_filters":
            self._clear_filters()
        elif t == "refresh":
            self._status_label.setText("  Refreshing...")
            self._fetcher.fetch_async(self._on_routes)
        elif t == "set_hotkey":
            new_hk = cmd.get("hotkey", "")
            if new_hk:
                self._hotkey_entry.setText(new_hk)
                self._apply_hotkey()
        elif t == "opacity":
            val = max(0.3, min(1.0, float(cmd.get("value", 0.95))))
            self.set_opacity(val)
        elif t == "set_freight_mode":
            mode = cmd.get("mode", "BULK")
            if mode in ("BULK", "MIXED"):
                self._set_freight_mode(mode)
        elif t == "set_illegal_cargo":
            self._set_allow_illegal(bool(cmd.get("allow", False)))

    def closeEvent(self, event) -> None:
        if hasattr(self, '_ipc'):
            self._ipc.stop()
        if self._hotkey_stop:
            self._hotkey_stop.set()
        super().closeEvent(event)


# ── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from shared.crash_logger import init_crash_logging
    _log = init_crash_logging("trade")
    try:
        argv = sys.argv[1:]

        def _safe_arg(i, default, type_fn):
            try:
                return type_fn(argv[i])
            except (IndexError, ValueError, TypeError):
                return default

        win_x = _safe_arg(0, 80, int)
        win_y = _safe_arg(1, 80, int)
        win_w = _safe_arg(2, 1400, int)
        win_h = _safe_arg(3, 1200, int)
        refresh_interval = _safe_arg(4, 300.0, float)
        max_routes = _safe_arg(5, 500, int)
        opacity = _safe_arg(6, 0.95, float)
        cmd_file = _safe_arg(7, "", str)

        app = QApplication(sys.argv)
        apply_theme(app)

        win = TradeHubWindow(
            cmd_file=cmd_file,
            x=win_x, y=win_y, w=win_w, h=win_h,
            refresh_interval=refresh_interval,
            max_routes=max_routes,
            opacity=opacity,
        )
        win.show()
        sys.exit(app.exec())
    except Exception:
        _log.critical("FATAL crash in trade main()", exc_info=True)
        sys.exit(1)
