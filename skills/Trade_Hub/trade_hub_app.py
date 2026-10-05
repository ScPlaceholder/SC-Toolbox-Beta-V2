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
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, List, Optional

# Bootstrap project root and skill directory
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..')))
from shared.app_bootstrap import bootstrap_skill  # noqa: E402
bootstrap_skill(__file__)

from PySide6.QtCore import Qt, QTimer, QUrl, Signal, QObject
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QSplitter, QFrame, QTabWidget, QLineEdit, QDialog, QScrollArea,
    QMessageBox, QInputDialog,
    QCheckBox, QRadioButton, QButtonGroup, QSpinBox, QDialogButtonBox,
    QCheckBox, QRadioButton, QButtonGroup, QSpinBox, QDialogButtonBox, QSlider,
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
    _dist_cache,
)
from basket_view import BasketView
from starmap.panel import StarMapPanel
from heatmap_view import HeatmapView
from commodities_view import CommoditiesView
from career import Career
from career_view import CareerView

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


def _career_btn_qss(color: str) -> str:
    """Stylesheet for the Complete / Failed / Favorite buttons on a route popup."""
    return (
        f"QPushButton {{ background-color: rgba(255,255,255,18); color: {color}; "
        f"border: 1px solid {color}; font-family: Consolas; font-size: 8pt; font-weight: bold; "
        f"padding: 4px 12px; }} "
        f"QPushButton:hover {{ background-color: {color}; color: #0b0e14; }} "
        f"QPushButton:disabled {{ color: {P.fg_disabled}; border-color: {P.border}; "
        f"background-color: transparent; }}"
    )


class RouteFailDialog(QDialog):
    """Logs a failed run: which failure reasons applied (checkboxes) + whether it
    was a full or partial loss (with the UEC amount for a partial loss)."""

    REASONS = ("Pirate Attack", "Unable to Sell", "Ship Loss", "Price Change")

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Route Failed")
        self.kind = "full"
        self.loss = None
        self.reasons: list = []
        self.setStyleSheet(
            f"QDialog{{background:{P.bg_primary};}} "
            f"QLabel{{color:{P.fg}; font-family:Consolas; font-size:9pt;}} "
            f"QCheckBox,QRadioButton{{color:{P.fg}; font-family:Consolas; font-size:10pt; "
            f"spacing:8px; padding:3px;}} "
            f"QCheckBox::indicator,QRadioButton::indicator{{width:16px; height:16px;}} "
            f"QRadioButton::indicator:unchecked{{border:2px solid {P.fg_dim}; border-radius:9px; "
            f"background:{P.bg_input};}} "
            f"QRadioButton::indicator:checked{{border:2px solid {P.energy_cyan}; border-radius:9px; "
            f"background:{P.energy_cyan};}} "
            f"QCheckBox::indicator:unchecked{{border:2px solid {P.fg_dim}; border-radius:3px; "
            f"background:{P.bg_input};}} "
            f"QCheckBox::indicator:checked{{border:2px solid {P.energy_cyan}; border-radius:3px; "
            f"background:{P.energy_cyan};}} "
            f"QSpinBox{{background:{P.bg_input}; color:{P.fg}; border:1px solid {P.border}; "
            f"border-radius:4px; padding:3px 6px;}} "
            f"QPushButton{{background:{P.bg_card}; color:{P.fg}; border:1px solid {P.border}; "
            f"border-radius:4px; padding:5px 14px; font-family:Consolas; font-size:9pt;}} "
            f"QPushButton:hover{{border-color:{P.energy_cyan}; color:{P.fg_bright};}}")
        v = QVBoxLayout(self)
        v.setContentsMargins(18, 16, 18, 14)
        v.setSpacing(8)

        hdr = QLabel("What went wrong?  (check all that apply)")
        hdr.setStyleSheet(f"color:{P.tool_trade}; font-weight:bold; font-size:10pt;")
        v.addWidget(hdr)
        self._checks = []
        for name in self.REASONS:
            cb = QCheckBox(name)
            cb.setCursor(Qt.PointingHandCursor)
            self._checks.append(cb)
            v.addWidget(cb)

        line = QLabel("How much did you lose?")
        line.setStyleSheet(f"color:{P.tool_trade}; font-weight:bold; font-size:10pt; padding-top:6px;")
        v.addWidget(line)
        self._grp = QButtonGroup(self)
        self._full = QRadioButton("Full loss  (cargo + investment)")
        self._full.setChecked(True)
        self._full.setCursor(Qt.PointingHandCursor)
        self._partial = QRadioButton("Partial loss:")
        self._partial.setCursor(Qt.PointingHandCursor)
        self._grp.addButton(self._full)
        self._grp.addButton(self._partial)
        v.addWidget(self._full)
        prow = QHBoxLayout()
        prow.addWidget(self._partial)
        self._loss = QSpinBox()
        self._loss.setRange(0, 2_000_000_000)
        self._loss.setSingleStep(1000)
        self._loss.setGroupSeparatorShown(True)
        self._loss.setSuffix(" aUEC")
        self._loss.setButtonSymbols(QSpinBox.UpDownArrows)
        prow.addWidget(self._loss, 1)
        v.addLayout(prow)
        # The amount field is always editable; changing it selects "Partial loss"
        # (so you don't have to pick the radio first — that was confusing).
        self._loss.valueChanged.connect(lambda *_: self._partial.setChecked(True))

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Log Failure")
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def _accept(self) -> None:
        self.reasons = [cb.text() for cb in self._checks if cb.isChecked()]
        if self._partial.isChecked():
            self.kind = "partial"
            self.loss = self._loss.value()
        else:
            self.kind = "full"
            self.loss = None
        self.accept()


# ── Route detail dialog ──────────────────────────────────────────────────────

class RouteDetailDialog(QDialog):
    """Popup showing route or loop details with Pin button and financial breakdown."""

    _pinned_dialogs: list = []  # class-level list of pinned dialogs

    def __init__(self, parent, title: str, route_data: dict) -> None:
        super().__init__(parent)
        self._th = parent
        self._route_data = route_data
        self.setWindowTitle(title)
        # Use Qt.Tool instead of Qt.Dialog to prevent Qt auto-centering
        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setMinimumSize(420, 300)
        self.resize(500, 560)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self._pinned = False

        # Adjustable cargo amounts ("Override default"): keyed per route leg /
        # cargo slot / basket pick. Financial rows registered in _fin_labels
        # are re-rendered live whenever an amount changes.
        self._amounts: Dict[str, int] = {}
        self._amount_defaults: Dict[str, int] = {}
        self._fin_labels: list = []
        self._updating = False
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

        # Action / pin button row
        pin_row = QHBoxLayout()
        pin_row.setContentsMargins(12, 6, 12, 2)
        self._btn_show_route = SCButton("Show Route", self, glow_color=P.energy_cyan)
        self._btn_show_route.setStyleSheet(_pin_btn_qss(False))
        self._btn_show_route.clicked.connect(self._show_on_map)
        pin_row.addWidget(self._btn_show_route)
        self._btn_show_hub = SCButton("Show Trade Hub", self, glow_color=P.accent)
        self._btn_show_hub.setStyleSheet(_pin_btn_qss(False))
        self._btn_show_hub.clicked.connect(self._show_trade_hub)
        pin_row.addWidget(self._btn_show_hub)
        pin_row.addStretch(1)
        self._pin_btn = SCButton("Pin", self, glow_color=P.tool_trade)
        self._pin_btn.setStyleSheet(_pin_btn_qss(False))
        self._pin_btn.clicked.connect(self._toggle_pin)
        pin_row.addWidget(self._pin_btn)
        c_layout.addLayout(pin_row)

        # Career action row: log the run to My Career or save it as a favorite.
        act_row = QHBoxLayout()
        act_row.setContentsMargins(12, 0, 12, 4)
        self._btn_complete = QPushButton("✓ Complete Route")
        self._btn_complete.setCursor(Qt.PointingHandCursor)
        self._btn_complete.setStyleSheet(_career_btn_qss(P.green))
        self._btn_complete.clicked.connect(self._complete_route)
        act_row.addWidget(self._btn_complete)
        self._btn_failed = QPushButton("✕ Route Failed")
        self._btn_failed.setCursor(Qt.PointingHandCursor)
        self._btn_failed.setStyleSheet(_career_btn_qss(P.red))
        self._btn_failed.clicked.connect(self._route_failed)
        act_row.addWidget(self._btn_failed)
        self._btn_fav = QPushButton("★ Add Favorite")
        self._btn_fav.setCursor(Qt.PointingHandCursor)
        self._btn_fav.setStyleSheet(_career_btn_qss(P.tool_trade))
        self._btn_fav.clicked.connect(self._add_favorite)
        act_row.addWidget(self._btn_fav)
        c_layout.addLayout(act_row)

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

    def _show_on_map(self):
        """Draw this route on the star map and switch the Trade Hub to it."""
        th = self._th
        if th is None:
            return
        try:
            if hasattr(th, "_starmap_panel"):
                wps = self._route_waypoints(self._route_data)
                if wps:
                    th._starmap_panel.show_route(wps)
            if hasattr(th, "_set_view_mode"):
                th._set_view_mode("STARMAP")
        except Exception:
            pass

    def _show_trade_hub(self):
        """Switch the Trade Hub back to the routes table."""
        th = self._th
        if th is not None and hasattr(th, "_set_view_mode"):
            try:
                th._set_view_mode("ROUTES")
            except Exception:
                pass

    # ── career actions ──
    def _complete_route(self):
        th = self._th
        if th is not None and hasattr(th, "_career_complete"):
            try:
                th._career_complete(self._route_data)
            except Exception:
                import logging
                logging.getLogger("TradeHub").exception(
                    "career complete FAILED — totals not logged (route_data keys: %s)",
                    list(self._route_data.keys()) if isinstance(self._route_data, dict) else type(self._route_data))
        QTimer.singleShot(0, self.close)   # the bubble exits on completion

    def _route_failed(self):
        th = self._th
        if th is None or not hasattr(th, "_career_fail"):
            return
        dlg = RouteFailDialog(self)
        if dlg.exec() != QDialog.Accepted:
            return
        th._career_fail(self._route_data, dlg.kind, dlg.loss, dlg.reasons)
        QTimer.singleShot(0, self.close)

    def _add_favorite(self):
        th = self._th
        ok = False
        if th is not None and hasattr(th, "_career_favorite"):
            try:
                ok = th._career_favorite(self._route_data)
            except Exception:
                ok = False
        self._btn_fav.setText("★ Favorited" if ok else "Already a favorite")
        self._btn_fav.setEnabled(False)

    @staticmethod
    def _route_waypoints(d: dict) -> list:
        if d.get("type", "single") == "single":
            return [(d.get("buy_location", ""), d.get("buy_system", ""), "buy"),
                    (d.get("sell_location", ""), d.get("sell_system", ""), "sell")]
        wps = []
        legs = d.get("legs", []) or []
        for i, leg in enumerate(legs):
            bl = leg.get("buy_location") or leg.get("origin") or ""
            bs = leg.get("buy_system") or leg.get("origin_system") or ""
            sl = leg.get("sell_location") or leg.get("destination") or ""
            ss = leg.get("sell_system") or leg.get("destination_system") or ""
            if i == 0 and bl:
                wps.append((bl, bs, "buy"))
            if sl:
                wps.append((sl, ss, "sell" if i == len(legs) - 1 else "stop"))
        return wps
    # ── adjustable cargo amounts (Override default) ──────────────────────────
    def _amt(self, key: str) -> int:
        return int(self._amounts.get(key, self._amount_defaults.get(key, 0)))

    def _refresh_financials(self) -> None:
        for lbl, fn in self._fin_labels:
            try:
                lbl.setText(fn())
            except Exception:
                pass

    def _write_back_amount(self, key: str, val: int) -> None:
        """Sync route_data so Complete Route / favorites log the ACTUAL cargo
        amounts the user ran with, not the theoretical default."""
        d = self._route_data
        try:
            t = d.get("type", "single")
            if t == "single" and key == "main":
                d["eff_scu"] = val
                d["profit"] = val * d.get("margin", 0)
            elif t == "multi" and key.startswith("leg") and "_slot" not in key:
                leg = d["legs"][int(key[3:])]
                leg["eff_scu"] = val
                leg["profit"] = val * leg.get("margin", 0)
                d["total_profit"] = sum(
                    l.get("eff_scu", 0) * l.get("margin", 0)
                    for l in d.get("legs", []))
            elif t == "mixed" and "_slot" in key:
                li, si = key.split("_slot")
                leg = d["legs"][int(li[3:])]
                slot = leg["slots"][int(si)]
                slot["scu_loaded"] = val
                slot["profit"] = val * (slot.get("price_sell", 0) - slot.get("price_buy", 0))
                leg["leg_profit"] = sum(
                    s.get("profit", 0) for s in leg.get("slots", []))
                d["total_profit"] = sum(
                    l.get("leg_profit", 0) for l in d.get("legs", []))
                d["total_investment"] = sum(
                    s.get("scu_loaded", 0) * s.get("price_buy", 0)
                    for l in d.get("legs", []) for s in l.get("slots", []))
            elif t == "basket" and key.startswith("stop"):
                si, pi = key[len("stop"):].split("_pick")
                pick = d["stops"][int(si)]["picks"][int(pi)]
                pick["scu"] = val
        except Exception:
            pass


    def _add_amount_editor(self, key: str, default_scu: int, max_scu: int,
                           label: str = "Load:") -> None:
        """Slider + text box for one adjustable cargo amount, gated behind an
        'Override default' toggle. Either control live-updates every registered
        financial row and writes the amount back into route_data."""
        default_scu = max(int(default_scu or 0), 0)
        max_scu = max(int(max_scu or 0), default_scu * 10, default_scu + 100, 1000)
        self._amount_defaults[key] = default_scu
        self._amounts[key] = default_scu

        row_w = QWidget()
        row_w.setStyleSheet("background: transparent;")
        row = QHBoxLayout(row_w)
        row.setSpacing(8)
        row.setContentsMargins(0, 0, 0, 0)
        k = QLabel(label)
        k.setFixedWidth(140)
        k.setStyleSheet(f"font-family: Consolas; font-size: 9pt; color: {P.fg_dim}; background: transparent;")
        row.addWidget(k)

        slider = QSlider(Qt.Horizontal)
        slider.setRange(0, max_scu)
        slider.setValue(default_scu)
        slider.setFixedHeight(18)
        slider.setStyleSheet(
            f"QSlider::groove:horizontal {{ height: 4px; background: {P.bg_input}; border-radius: 2px; }}"
            f"QSlider::handle:horizontal {{ width: 12px; margin: -5px 0; border-radius: 6px; background: {P.yellow}; }}"
        )
        row.addWidget(slider, 1)

        spin = QSpinBox()
        spin.setRange(0, max_scu)
        spin.setValue(default_scu)
        spin.setSuffix(" SCU")
        spin.setButtonSymbols(QSpinBox.NoButtons)
        spin.setFixedWidth(96)
        spin.setStyleSheet(
            f"background: {P.bg_input}; color: {P.fg}; border: 1px solid {P.border}; "
            f"border-radius: 4px; padding: 2px 6px; font-family: Consolas; font-size: 9pt;"
        )
        row.addWidget(spin)

        btn = QPushButton("Override default")
        btn.setCheckable(True)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setStyleSheet(_pin_btn_qss(False))
        row.addWidget(btn)

        def _apply(val: int) -> None:
            if self._updating:
                return
            self._updating = True
            try:
                slider.setValue(val)
                spin.setValue(val)
            finally:
                self._updating = False
            self._amounts[key] = val
            self._write_back_amount(key, val)
            self._refresh_financials()

        def _toggle(on: bool) -> None:
            slider.setEnabled(on)
            spin.setEnabled(on)
            btn.setText("Override: ON" if on else "Override default")
            btn.setStyleSheet(_pin_btn_qss(on))
            if not on:
                _apply(self._amount_defaults[key])

        slider.valueChanged.connect(lambda v: _apply(int(v)))
        spin.valueChanged.connect(lambda v: _apply(int(v)))
        btn.toggled.connect(_toggle)
        slider.setEnabled(False)
        spin.setEnabled(False)

        self._content_layout.addWidget(row_w)

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
        elif route_type == "basket":
            self._build_basket_route(d)

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

    def _add_colored_row(self, label: str, value: str, label_color: str = "", value_color: str = "",
                         dyn=None):
        """Row where both the label and value have custom colours.
        Pass dyn=callable for an amount-dependent value — the label text is
        re-rendered whenever an override slider/text box changes."""
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
        if dyn is not None:
            self._fin_labels.append((v, dyn))
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
        self._add_amount_editor(
            "main", eff_scu,
            max(d.get("scu_available", 0), d.get("scu_demand", 0), eff_scu, 1),
        )
        if distance > 0:
            self._add_colored_row("Distance:", fmt_distance(distance), P.energy_cyan, P.energy_cyan)
            self._add_colored_row("Travel Time:", fmt_eta(distance), P.energy_cyan, P.energy_cyan)

        self._add_header("FINANCIALS", P.green)
        self._add_separator()
        self._add_colored_row(
            "Total Cost:", f"{total_cost:,.0f} aUEC", P.red, P.red,
            dyn=lambda: f"{self._amt('main') * price_buy:,.0f} aUEC",
        )
        self._add_colored_row(
            "Total Revenue:", f"{total_revenue:,.0f} aUEC", P.accent, P.accent,
            dyn=lambda: f"{self._amt('main') * price_sell:,.0f} aUEC",
        )
        self._add_colored_row(
            "Profit:", f"+{profit:,.0f} aUEC", P.green, P.green,
            dyn=lambda: f"+{self._amt('main') * margin:,.0f} aUEC",
        )
        self._add_colored_row("Margin/SCU:", f"{margin:,.0f} aUEC/SCU", P.green, P.accent)
        roi_color = P.green if roi > 50 else P.yellow
        self._add_colored_row(
            "ROI:", f"{roi:.1f}%", roi_color, roi_color,
            dyn=lambda: (
                f"{(self._amt('main') * margin) / (self._amt('main') * price_buy) * 100:,.1f}%"
                if self._amt('main') * price_buy else "0.0%"
            ),
        )

        self._add_header("BUY LOCATION", P.accent)
        self._add_separator()
        self._add_colored_row("Terminal:", d.get("buy_terminal", "?"), P.accent, P.fg_bright)
        self._add_colored_row("Location:", d.get("buy_location", "?"), P.accent, P.fg)
        self._add_colored_row("System:", d.get("buy_system", "?"), P.energy_cyan, P.energy_cyan)
        self._add_colored_row("Price:", f"{price_buy:,.0f} aUEC/SCU", P.red, P.red)
        self._add_colored_row("Available:", f"{d.get('scu_available', 0):,} SCU", P.yellow, P.yellow)
        self._add_colored_row(
            "Purchase Total:", f"{total_cost:,.0f} aUEC", P.red, P.red,
            dyn=lambda: f"{self._amt('main') * price_buy:,.0f} aUEC",
        )

        self._add_header("SELL LOCATION", P.orange)
        self._add_separator()
        self._add_colored_row("Terminal:", d.get("sell_terminal", "?"), P.orange, P.fg_bright)
        self._add_colored_row("Location:", d.get("sell_location", "?"), P.orange, P.fg)
        self._add_colored_row("System:", d.get("sell_system", "?"), P.energy_cyan, P.energy_cyan)
        self._add_colored_row("Price:", f"{price_sell:,.0f} aUEC/SCU", P.accent, P.accent)
        self._add_colored_row("Demand:", f"{d.get('scu_demand', 0):,} SCU", P.yellow, P.yellow)
        self._add_colored_row(
            "Sale Revenue:", f"{total_revenue:,.0f} aUEC", P.green, P.green,
            dyn=lambda: f"{self._amt('main') * price_sell:,.0f} aUEC",
        )
        self._add_colored_row(
            "Profit Here:", f"+{profit:,.0f} aUEC", P.green, P.green,
            dyn=lambda: f"+{self._amt('main') * margin:,.0f} aUEC",
        )
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
        self._add_colored_row(
            "Total Profit:", f"+{total_profit:,.0f} aUEC", P.green, P.green,
            dyn=lambda legs=legs: (
                f"+{sum(self._amt(f'leg{j}') * legs[j].get('margin', 0) for j in range(len(legs))):,.0f} aUEC"
            ),
        )
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
            leg_key = f"leg{i - 1}"

            self._add_header(f"LEG {i}:  {leg.get('commodity', '?')}", P.accent)
            self._add_separator()
            self._add_colored_row("Buy:", f"{leg.get('buy_terminal', '?')} ({leg.get('buy_system', '?')})", P.accent, P.fg_bright)
            self._add_colored_row("Sell:", f"{leg.get('sell_terminal', '?')} ({leg.get('sell_system', '?')})", P.orange, P.fg_bright)
            self._add_amount_editor(
                leg_key, eff,
                max(leg.get("scu_available", 0), leg.get("scu_demand", 0), eff, 1),
            )
            if leg_dist > 0:
                self._add_colored_row("Travel:", f"{fmt_distance(leg_dist)} \u2022 {fmt_eta(leg_dist)}", P.energy_cyan, P.energy_cyan)
            self._add_colored_row(
                "Purchase:", f"{leg_cost:,.0f} aUEC", P.red, P.red,
                dyn=lambda k=leg_key, b=buy_price: f"{self._amt(k) * b:,.0f} aUEC",
            )
            self._add_colored_row(
                "Revenue:", f"{leg_revenue:,.0f} aUEC", P.accent, P.accent,
                dyn=lambda k=leg_key, s=sell_price: f"{self._amt(k) * s:,.0f} aUEC",
            )
            self._add_colored_row(
                "Leg Profit:", f"+{leg_profit:,.0f} aUEC", P.green, P.green,
                dyn=lambda k=leg_key, m=leg.get("margin", 0): f"+{self._amt(k) * m:,.0f} aUEC",
            )

        self._add_header("TOTALS", P.green)
        self._add_separator()
        self._add_colored_row(
            "Total Investment:", f"{running_investment:,.0f} aUEC", P.red, P.red,
            dyn=lambda legs=legs: (
                f"{sum(self._amt(f'leg{j}') * legs[j].get('price_buy', 0) for j in range(len(legs))):,.0f} aUEC"
            ),
        )
        self._add_colored_row(
            "Total Profit:", f"+{total_profit:,.0f} aUEC", P.green, P.green,
            dyn=lambda legs=legs: (
                f"+{sum(self._amt(f'leg{j}') * legs[j].get('margin', 0) for j in range(len(legs))):,.0f} aUEC"
            ),
        )
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

        def _mixed_totals():
            cost = 0.0
            prof = 0.0
            for li, leg in enumerate(legs):
                for jj, sl in enumerate(leg.get("slots", []) or []):
                    a = self._amt(f"leg{li}_slot{jj}")
                    cost += a * sl.get("price_buy", 0)
                    prof += a * (sl.get("price_sell", 0) - sl.get("price_buy", 0))
            return cost, prof

        # -- Route overview (gold) --
        self._add_header(f"MIXED FREIGHT  \u2022  {num_legs} legs  \u2022  {fill_eff:.0f}% fill", P.tool_trade)
        self._add_separator()
        self._add_colored_row("Ship:", ship, P.tool_trade, P.fg_bright)
        self._add_colored_row(
            "Total Profit:", f"+{total_profit:,.0f} aUEC", P.green, P.green,
            dyn=lambda: f"+{_mixed_totals()[1]:,.0f} aUEC",
        )
        if total_invest > 0:
            self._add_colored_row(
                "Total Cost:", f"{total_invest:,.0f} aUEC", P.red, P.red,
                dyn=lambda: f"{_mixed_totals()[0]:,.0f} aUEC",
            )
            roi_color = P.green if roi > 50 else P.yellow
            self._add_colored_row(
                "ROI:", f"{roi:.1f}%", roi_color, roi_color,
                dyn=lambda: (
                    f"{_mixed_totals()[1] / _mixed_totals()[0] * 100:,.1f}%"
                    if _mixed_totals()[0] else "0.0%"
                ),
            )
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

            # -- Leg header (blue) --
            self._add_header(f"LEG {i}:  {leg.get('buy_terminal', '?')}  \u2192  {leg.get('sell_terminal', '?')}", P.accent)
            self._add_separator()
            self._add_colored_row("System:", f"{leg.get('buy_system', '?')} \u2192 {leg.get('sell_system', '?')}", P.energy_cyan, P.energy_cyan)
            if leg_dist > 0:
                self._add_colored_row("Travel:", f"{fmt_distance(leg_dist)} \u2022 {fmt_eta(leg_dist)}", P.energy_cyan, P.energy_cyan)

            # -- Cargo slots --
            for j, slot in enumerate(slots):
                is_primary = slot.get("is_primary", False)
                is_illegal = slot.get("is_illegal", False)
                scu = slot.get("scu_loaded", 0)
                buy_p = slot.get("price_buy", 0)
                sell_p = slot.get("price_sell", 0)
                slot_profit = slot.get("profit", 0)
                commodity = slot.get("commodity", "?")
                slot_key = f"leg{i - 1}_slot{j}"
                slot_margin = sell_p - buy_p

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

                self._add_colored_row(f"{role_icon}  {tag}{illegal_tag}", commodity, name_color, name_color)
                self._add_amount_editor(
                    slot_key, scu,
                    max(scu, slot.get("scu_available", 0), 1),
                    label="    SCU:",
                )
                self._add_colored_row("    Buy:", f"{buy_p:,.2f} aUEC/SCU", P.red, P.red)
                self._add_colored_row("    Sell:", f"{sell_p:,.2f} aUEC/SCU", P.accent, P.accent)
                profit_color = P.green if is_primary else P.purple
                self._add_colored_row(
                    "    Profit:", f"+{slot_profit:,.0f} aUEC", profit_color, profit_color,
                    dyn=lambda k=slot_key, m=slot_margin: f"+{self._amt(k) * m:,.0f} aUEC",
                )

            # -- Leg totals --
            self._add_separator()
            self._add_colored_row(
                "Leg Fill:", f"{leg_scu:,} SCU  ({leg_fill:.1f}%)", P.yellow, P.yellow,
                dyn=lambda li=i - 1, lf=leg_fill, ns=len(slots): (
                    f"{sum(self._amt(f'leg{li}_slot{jj}') for jj in range(ns)):,} SCU  ({lf:.1f}%)"
                ),
            )
            self._add_colored_row(
                "Leg Profit:", f"+{leg_profit:,.0f} aUEC", P.green, P.green,
                dyn=lambda li=i - 1, slots=slots: (
                    f"+{sum(self._amt(f'leg{li}_slot{jj}') * (slots[jj].get('price_sell', 0) - slots[jj].get('price_buy', 0)) for jj in range(len(slots))):,.0f} aUEC"
                ),
            )

        # -- Grand totals --
        self._add_header("TOTALS", P.green)
        self._add_separator()
        self._add_colored_row(
            "Total Profit:", f"+{total_profit:,.0f} aUEC", P.green, P.green,
            dyn=lambda: f"+{_mixed_totals()[1]:,.0f} aUEC",
        )
        if total_invest > 0:
            self._add_colored_row(
                "Total Cost:", f"{total_invest:,.0f} aUEC", P.red, P.red,
                dyn=lambda: f"{_mixed_totals()[0]:,.0f} aUEC",
            )
            roi_color = P.green if roi > 50 else P.yellow
            self._add_colored_row(
                "ROI:", f"{roi:.1f}%", roi_color, roi_color,
                dyn=lambda: (
                    f"{_mixed_totals()[1] / _mixed_totals()[0] * 100:,.1f}%"
                    if _mixed_totals()[0] else "0.0%"
                ),
            )
        self._add_colored_row("Bay Efficiency:", f"{fill_eff:.1f}% avg", P.yellow, P.yellow)
    def _build_basket_route(self, d: dict):
        mode = d.get("mode", "buy")  # "buy" | "sell"
        sell = mode == "sell"
        default_title = "BASKET SALE" if sell else "BASKET ROUTE"
        label = d.get("label", "") or default_title
        start_name = d.get("start", "?")
        stops = d.get("stops", [])
        total_dist = d.get("total_distance", 0) or 0
        unresolved = d.get("unresolved", []) or []
        num_stops = len(stops)

        self._add_header(f"{default_title}  \u2022  {num_stops} stop(s)  \u2022  {label}", P.tool_trade)
        self._add_separator()
        self._add_colored_row("Start:", start_name, P.tool_trade, P.fg_bright)
        if total_dist > 0:
            self._add_colored_row("Total Distance:", fmt_distance(total_dist), P.energy_cyan, P.energy_cyan)
            self._add_colored_row("Travel Time:", fmt_eta(total_dist), P.energy_cyan, P.energy_cyan)
        if unresolved:
            self._add_colored_row("Unresolved:", ", ".join(unresolved), P.red, P.red)

        price_label = "Sell:" if sell else "Buy:"
        price_color = P.green if sell else P.red
        totals_label = "Est. Revenue (demand):" if sell else "Est. Spend (stocked):"
        totals_color = P.green if sell else P.red

        money = 0.0
        for i, stop in enumerate(stops, 1):
            stop_header = f"STOP {i}:  {stop.get('terminal', '?')}"
            self._add_header(stop_header, P.accent)
            self._add_separator()
            loc = stop.get("location", "")
            sys_ = stop.get("system", "")
            if loc:
                self._add_colored_row("Location:", loc, P.accent, P.fg)
            if sys_:
                self._add_colored_row("System:", sys_, P.energy_cyan, P.energy_cyan)
            leg_dist = stop.get("distance_from_prev", 0) or 0
            if leg_dist > 0:
                self._add_colored_row(
                    "Travel:",
                    f"{fmt_distance(leg_dist)} \u2022 {fmt_eta(leg_dist)}",
                    P.energy_cyan, P.energy_cyan,
                )
            for j, pick in enumerate(stop.get("picks", []) or []):
                cm = pick.get("commodity", "?")
                scu = pick.get("scu", 0) or 0
                price = pick.get("price", 0) or 0
                self._add_amount_editor(
                    f"stop{i - 1}_pick{j}", scu,
                    max(scu * 2, scu + 32, 1),
                    label=f"  \u2605 {cm}",
                )
                self._add_colored_row(
                    f"    {price_label}",
                    f"{price:,.2f} aUEC/SCU",
                    price_color, price_color,
                )
                money += scu * price

        self._add_header("TOTALS", P.green)
        self._add_separator()
        self._add_colored_row("Stops:", f"{num_stops}", P.tool_trade, P.fg_bright)
        if total_dist > 0:
            self._add_colored_row("Total Distance:", fmt_distance(total_dist), P.energy_cyan, P.energy_cyan)
            self._add_colored_row("Travel Time:", fmt_eta(total_dist), P.energy_cyan, P.energy_cyan)
        if money > 0:
            self._add_colored_row(
                totals_label, f"{money:,.0f} aUEC", totals_color, totals_color,
                dyn=lambda stops=stops: (
                    f"{sum(self._amt(f'stop{si}_pick{pj}') * (stops[si].get('picks', [])[pj].get('price', 0) or 0)
                          for si in range(len(stops)) for pj in range(len(stops[si].get('picks', []) or []))):,.0f} aUEC"
                ),
            )
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
        self._freight_mode = cfg.get("freight_mode", "BULK")
        self._allow_illegal = cfg.get("allow_illegal_cargo", False)
        if hasattr(self, '_btn_bulk'):
            self._update_freight_mode_btns()
        if hasattr(self, '_btn_illegal_yes'):
            self._update_illegal_btns()
        self._sync_table_visibility()

        self._start_ipc()
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
        self._btn_basket = QPushButton(_("BASKET"))
        self._btn_basket.setCursor(Qt.PointingHandCursor)
        self._btn_basket.clicked.connect(lambda: self._set_view_mode("BASKET"))
        vm_row.addWidget(self._btn_basket)
        vmw = QWidget()
        vmw.setStyleSheet("background: transparent;")
        vmw.setLayout(vm_row)
        sb_lay.addWidget(vmw)
        # Star Map gets its own full-width row — a 4th button overflows vm_row.
        self._btn_starmap = QPushButton(_("STAR MAP"))
        self._btn_starmap.setCursor(Qt.PointingHandCursor)
        self._btn_starmap.clicked.connect(lambda: self._set_view_mode("STARMAP"))
        sb_lay.addWidget(self._btn_starmap)
        self._btn_heatmap = QPushButton(_("HEATMAP"))
        self._btn_heatmap.setCursor(Qt.PointingHandCursor)
        self._btn_heatmap.clicked.connect(lambda: self._set_view_mode("HEATMAP"))
        sb_lay.addWidget(self._btn_heatmap)
        self._btn_commodities = QPushButton(_("COMMODITIES"))
        self._btn_commodities.setCursor(Qt.PointingHandCursor)
        self._btn_commodities.clicked.connect(lambda: self._set_view_mode("COMMODITIES"))
        sb_lay.addWidget(self._btn_commodities)
        self._btn_career = QPushButton(_("MY CAREER"))
        self._btn_career.setCursor(Qt.PointingHandCursor)
        self._btn_career.clicked.connect(lambda: self._set_view_mode("CAREER"))
        sb_lay.addWidget(self._btn_career)
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

        # Starting investment (max aUEC available for first-leg buy)
        section(_("STARTING INVESTMENT (aUEC)"))
        self._max_investment = QLineEdit()
        self._max_investment.setPlaceholderText(_("e.g. 2000000"))
        self._max_investment.setToolTip(_(
            "Maximum aUEC you have to start a trade. Routes whose "
            "first-leg buy cost (price × ship capacity or "
            "available stock, whichever is smaller) exceeds this "
            "amount are hidden. Leave blank or 0 to show all routes."
        ))
        self._max_investment.returnPressed.connect(self._apply_search)
        sb_lay.addWidget(self._max_investment)

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

        # Basket view — multi-commodity pickup planner
        self._basket_view = BasketView(
            routes_getter=lambda: self._all_routes,
            dist_cache=_dist_cache,
        )
        self._basket_view.plan_clicked.connect(self._on_basket_plan_select)
        self._basket_view.hide()
        right_lay.addWidget(self._basket_view, 1)

        self._career = Career()   # lifetime trade ledger (persisted)
        # Star Map — interactive galaxy view (pop-out capable)
        self._starmap_panel = StarMapPanel(self)
        self._starmap_panel.set_trade_hub(self)
        self._starmap_panel.hide()
        right_lay.addWidget(self._starmap_panel, 1)
        self._heatmap_view = HeatmapView(self)
        self._heatmap_view.hide()
        right_lay.addWidget(self._heatmap_view, 1)
        self._commodities_view = CommoditiesView(self)
        self._commodities_view.hide()
        right_lay.addWidget(self._commodities_view, 1)
        self._career_view = CareerView(self)
        self._career_view.hide()
        right_lay.addWidget(self._career_view, 1)
        self._launch_snapshot_done = False   # local price-history: force one snapshot at launch

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
        self._btn_routes.setStyleSheet(active_ss if self._view_mode == "ROUTES" else inactive_ss)
        self._btn_loops.setStyleSheet(active_ss if self._view_mode == "LOOPS" else inactive_ss)
        if hasattr(self, "_btn_basket"):
            self._btn_basket.setStyleSheet(active_ss if self._view_mode == "BASKET" else inactive_ss)
        if hasattr(self, "_btn_starmap"):
            self._btn_starmap.setStyleSheet(active_ss if self._view_mode == "STARMAP" else inactive_ss)
        if hasattr(self, "_btn_heatmap"):
            self._btn_heatmap.setStyleSheet(active_ss if self._view_mode == "HEATMAP" else inactive_ss)
        if hasattr(self, "_btn_commodities"):
            self._btn_commodities.setStyleSheet(active_ss if self._view_mode == "COMMODITIES" else inactive_ss)
        if hasattr(self, "_btn_career"):
            self._btn_career.setStyleSheet(active_ss if self._view_mode == "CAREER" else inactive_ss)

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
        if hasattr(self, "_basket_view"):
            self._basket_view.hide()
        if hasattr(self, "_starmap_panel"):
            self._starmap_panel.hide()
        if hasattr(self, "_heatmap_view"):
            self._heatmap_view.hide()
        if hasattr(self, "_commodities_view"):
            self._commodities_view.hide()
        if hasattr(self, "_career_view"):
            self._career_view.hide()
        if self._view_mode == "STARMAP" and hasattr(self, "_starmap_panel"):
            self._starmap_panel.show()
        elif self._view_mode == "HEATMAP" and hasattr(self, "_heatmap_view"):
            self._heatmap_view.show()
        elif self._view_mode == "COMMODITIES" and hasattr(self, "_commodities_view"):
            self._commodities_view.show()
        elif self._view_mode == "CAREER" and hasattr(self, "_career_view"):
            self._career_view.refresh()
            self._career_view.show()
        elif self._view_mode == "BASKET" and hasattr(self, "_basket_view"):
            self._basket_view.show()
        elif self._freight_mode == "MIXED":
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
        save_config({"ship_name": self._ship_name, "freight_mode": self._freight_mode, "allow_illegal_cargo": self._allow_illegal})

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
        if hasattr(self, "_basket_view"):
            self._basket_view.refresh_data()
        if hasattr(self, "_starmap_panel"):
            self._starmap_panel.on_routes_loaded()
        self._snapshot_prices()
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
        if hasattr(self, "_basket_view"):
            self._basket_view.refresh_data()
        if hasattr(self, "_starmap_panel"):
            self._starmap_panel.on_routes_loaded()
        self._snapshot_prices()
        self._refresh_display()
        self._refresh_btn.setEnabled(True)

    def _snapshot_prices(self) -> None:
        """Local price-history snapshot — force one at launch, then once per day
        (maybe_snapshot only writes when today has no row yet). Runs off-thread.
        Called from both route-apply slots so it fires on every data load."""
        try:
            from starmap import price_log
            price_log.maybe_snapshot(force=not self._launch_snapshot_done)
            self._launch_snapshot_done = True
        except Exception:
            log.exception("snapshot hook failed")

    def _auto_refresh(self):
        self._fetcher.fetch_async(
            self._on_routes,
            on_distances_done=self._on_distances_bg,
            on_distance_progress=self._on_distance_progress_bg,
        )
        QTimer.singleShot(int(self._refresh_interval * 1000), self._auto_refresh)

    # ── Display refresh ──

    def _refresh_display(self):
        if self._view_mode in ("STARMAP", "COMMODITIES", "CAREER"):
            return
        if self._view_mode == "HEATMAP":
            if hasattr(self, "_heatmap_view"):
                self._heatmap_view.refresh(self._all_routes)
            return
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

            # STARTING INVESTMENT cap (MIXED / BASKET path):
            # the first leg's total buy cost across all cargo slots
            # must fit the user's budget.  Subsequent legs are paid
            # for with proceeds from earlier sales so only the first
            # leg matters for the "money on hand" check.
            if f.max_investment > 0:
                mixed = [
                    m for m in mixed
                    if m.legs
                    and m.legs[0].total_investment() <= f.max_investment
                ]

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
            # STARTING INVESTMENT cap (LOOPS path): first leg's
            # (buy_price * effective_scu) must fit the user's budget.
            if f.max_investment > 0:
                _ship = self._ship_scu
                loops = [
                    m for m in loops
                    if m.legs
                    and m.legs[0].price_buy * m.legs[0].effective_scu(_ship)
                        <= f.max_investment
                ]
            q = self._search.text().strip().lower()
            if q:
                loops = [m for m in loops if any(q in x.lower() for x in [
                    m.start_terminal, m.start_system, m.end_terminal, m.commodity_chain()])]
            loops = sort_multi_routes(loops, self._loop_sort_col, self._loop_sort_reverse, self._ship_scu)
            self._filtered_loops = loops[:self._max_routes]
            self._populate_loop_table()
        else:
            result = apply_filters(self._all_routes, f)
            # STARTING INVESTMENT cap (single-route ROUTES path):
            # this trip's buy cost (price * effective_scu) must fit
            # the user's budget.
            if f.max_investment > 0:
                _ship = self._ship_scu
                result = [
                    r for r in result
                    if r.price_buy * r.effective_scu(_ship)
                        <= f.max_investment
                ]
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
        try:
            # Accept "2,000,000" or "2000000" or "2.5e6".  Strip commas
            # / whitespace before float() so common keyboard habits work.
            raw_inv = self._max_investment.text().strip().replace(",", "") if self._max_investment else ""
            f.max_investment = float(raw_inv) if raw_inv else 0.0
        except (ValueError, AttributeError):
            f.max_investment = 0.0
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
        if hasattr(self, "_max_investment") and self._max_investment is not None:
            self._max_investment.clear()
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
        save_config({"ship_name": name, "freight_mode": self._freight_mode, "allow_illegal_cargo": self._allow_illegal})
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
        self._open_route_detail(route, show_on_map=True)

    def _open_route_detail(self, route, show_on_map: bool = False):
        """Open the standard Route Detail popup (with its Pin button) for a Route.
        Shared by the main route table and the Heatmap view."""
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
        if show_on_map and hasattr(self, "_starmap_panel"):
            self._starmap_panel.show_route([
                (route.buy_location, route.buy_system, "buy"),
                (route.sell_location, route.sell_system, "sell"),
            ])
        return dlg

    def _open_route_data(self, data: dict):
        """Open a Route Detail popup directly from a stored data dict (favorites)."""
        dlg = RouteDetailDialog(self, "ROUTE DETAIL", data)
        dlg.show()
        return dlg

    # ── career ledger (from the route popup buttons) ──────────────────────────
    def _career_entry(self, data: dict) -> dict:
        # Built from the CURRENT (possibly Override-adjusted) amounts in
        # route_data, per route type, so My Career logs what was actually run.
        # Multi-leg / mixed / basket routes don't carry the flat single-route
        # keys — previously they logged blank routes and stale/zero totals.
        t = data.get("type", "single")
        entry = {
            "commodity": data.get("commodity", ""),
            "ship": self._ship_name or "—",
            "scu": 0, "price_buy": 0.0, "price_sell": 0.0,
            "profit": 0.0, "investment": 0.0,
            "buy_system": data.get("buy_system", ""),
            "sell_system": data.get("sell_system", ""),
            "buy_location": data.get("buy_location", ""),
            "sell_location": data.get("sell_location", ""),
        }
        if t == "multi":
            legs = data.get("legs") or []
            if legs:
                entry["buy_location"] = entry["buy_location"] or legs[0].get("buy_terminal", "")
                entry["buy_system"] = entry["buy_system"] or legs[0].get("buy_system", "")
                entry["sell_location"] = entry["sell_location"] or legs[-1].get("sell_terminal", "")
                entry["sell_system"] = entry["sell_system"] or legs[-1].get("sell_system", "")
                entry["commodity"] = entry["commodity"] or " → ".join(
                    dict.fromkeys(l.get("commodity", "?") for l in legs))
                entry["price_buy"] = float(legs[0].get("price_buy") or 0)
                entry["price_sell"] = float(legs[-1].get("price_sell") or 0)
            entry["scu"] = max((int(l.get("eff_scu") or 0) for l in legs), default=0)
            entry["profit"] = float(sum(
                (l.get("eff_scu") or 0) * l.get("margin", 0) for l in legs) or 0)
            entry["investment"] = float(sum(
                (l.get("eff_scu") or 0) * l.get("price_buy", 0) for l in legs) or 0)
        elif t == "mixed":
            legs = data.get("legs") or []
            slots = [s for l in legs for s in (l.get("slots") or [])]
            if legs:
                entry["buy_location"] = entry["buy_location"] or legs[0].get("buy_terminal", "")
                entry["buy_system"] = entry["buy_system"] or legs[0].get("buy_system", "")
                entry["sell_location"] = entry["sell_location"] or legs[-1].get("sell_terminal", "")
                entry["sell_system"] = entry["sell_system"] or legs[-1].get("sell_system", "")
            names = list(dict.fromkeys(s.get("commodity", "?") for s in slots))
            entry["commodity"] = entry["commodity"] or (
                "Mixed: " + ", ".join(names[:3]) + ("…" if len(names) > 3 else ""))
            entry["scu"] = sum(int(s.get("scu_loaded") or 0) for s in slots)
            entry["investment"] = float(sum(
                (s.get("scu_loaded") or 0) * s.get("price_buy", 0) for s in slots) or 0)
            entry["profit"] = float(sum(
                (s.get("scu_loaded") or 0)
                * (s.get("price_sell", 0) - s.get("price_buy", 0)) for s in slots) or 0)
        elif t == "basket":
            stops = data.get("stops") or []
            picks = [p for s in stops for p in (s.get("picks") or [])]
            if stops:
                entry["buy_location"] = entry["buy_location"] or data.get("start", "")
                entry["buy_system"] = entry["buy_system"] or stops[0].get("system", "")
                entry["sell_location"] = entry["sell_location"] or stops[-1].get("terminal", "")
                entry["sell_system"] = entry["sell_system"] or stops[-1].get("system", "")
            names = list(dict.fromkeys(p.get("commodity", "?") for p in picks))
            entry["commodity"] = entry["commodity"] or (
                "Basket: " + ", ".join(names[:3]) + ("…" if len(names) > 3 else ""))
            entry["scu"] = sum(int(p.get("scu") or 0) for p in picks)
            entry["investment"] = float(sum(
                (p.get("scu") or 0) * p.get("price", 0) for p in picks) or 0)
            entry["profit"] = float(data.get("profit") or 0)  # sell side unknown at plan time
        else:  # single route
            scu = int(data.get("eff_scu") or 0)
            pb = float(data.get("price_buy") or 0)
            ps = float(data.get("price_sell") or 0)
            entry["scu"] = scu
            entry["price_buy"] = pb
            entry["price_sell"] = ps
            entry["profit"] = float(data.get("profit") or scu * (ps - pb) or 0)
            entry["investment"] = pb * scu
        return entry


    def _career_complete(self, data: dict) -> None:
        self._career.complete(self._career_entry(data))
        self._refresh_career()

    def _career_fail(self, data: dict, kind: str, loss, reasons=None) -> None:
        e = self._career_entry(data)
        e["loss"] = e.get("investment", 0) if kind == "full" else float(loss or 0)
        e["kind"] = kind
        e["reasons"] = list(reasons or [])
        # keep 'profit' on the entry so the run can be toggled back to a success
        self._career.fail(e)
        self._refresh_career()

    def _career_reclassify_fail(self, entry: dict) -> bool:
        """My Career ⇄ toggle on a SUCCESS: pop the failure dialog (reasons + loss)
        and move that already-logged run into the failures list."""
        dlg = RouteFailDialog(getattr(self, "_career_view", None) or self)
        if dlg.exec() != QDialog.Accepted:
            return False
        self._career.set_failed(entry, dlg.kind, dlg.loss, dlg.reasons)
        self._refresh_career()
        return True

    def _career_favorite(self, data: dict) -> bool:
        fav = dict(data)
        fav["ship"] = self._ship_name or "—"
        # Fill the flat display keys for non-single routes so the My Career
        # favorites table shows the route instead of blank Buy@/Sell@ cells
        # (and so favorite dedupe keys are distinct per route).
        e = self._career_entry(data)
        for k in ("commodity", "buy_location", "buy_system",
                  "sell_location", "sell_system"):
            if not fav.get(k):
                fav[k] = e.get(k, "")
        ok = self._career.add_favorite(fav)
        self._refresh_career()
        return ok

    def _refresh_career(self) -> None:
        if hasattr(self, "_career_view"):
            try:
                self._career_view.refresh()
            except Exception:
                pass
        if hasattr(self, "_starmap_panel"):      # refresh the 'My Runs' map heat
            try:
                self._starmap_panel._rebuild_overlay()
            except Exception:
                pass

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

    def _on_basket_plan_select(self, plan):
        """Open a RouteDetailDialog showing the full basket plan."""
        stops_data = []
        start_name = ""
        for i, stop in enumerate(plan.stops):
            if i == 0 and (stop.distance_from_prev_gm or 0) <= 0:
                start_name = stop.terminal.terminal_name or ""
            stops_data.append({
                "terminal": stop.terminal.terminal_name,
                "location": stop.terminal.location,
                "system": stop.terminal.system,
                "distance_from_prev": stop.distance_from_prev_gm or 0,
                "picks": [
                    {
                        "commodity": o.commodity,
                        "scu": o.scu_available,
                        "price": o.price_buy,
                    }
                    for o in stop.picks
                ],
            })
        if not start_name and plan.stops:
            start_name = plan.stops[0].terminal.terminal_name or "?"
        data = {
            "type": "basket",
            "mode": getattr(plan, "mode", "buy"),
            "label": plan.label,
            "start": start_name or "?",
            "stops": stops_data,
            "total_distance": plan.total_distance_gm,
            "unresolved": plan.unresolved,
        }
        title = "BASKET SALE" if data["mode"] == "sell" else "BASKET ROUTE"
        dlg = RouteDetailDialog(self, title, data)
        dlg.show()

    # ── Tutorial ──

    @staticmethod
    def _tutorial_pages() -> list:
        """(tab title, html) for the tutorial, in the order shown.

        A name the user can read in the tool (a button, a heading, a column) is in <b>, and
        tests/test_tutorial.py checks that each one is still a string somewhere else in the source."""
        hdr_style = f"font-weight: bold; color: {P.tool_trade}; font-size: 10pt;"
        acc = f"color: {P.accent};"
        grn = f"color: {P.green};"
        red = f"color: {P.red};"
        pur = f"color: {P.purple};"
        dim = f"color: {P.fg_dim};"
        try:
            from shared.hotkey_label import hotkey_label
            hotkey = hotkey_label("hotkey_trade", "<shift>+6")
        except Exception:                   # noqa: BLE001 - a tutorial is not worth a failed open
            hotkey = "Shift+6"

        basics = f"""
            <p style="{hdr_style}">WHAT IT IS FOR</p>
            <p>Use Trade Hub to find something to haul: what to buy, where, and
            where to sell it for the most profit. Prices come from UEX.</p>
            <p style="{hdr_style}">START HERE</p>
            <p>1. Under <b style="{acc}">VEHICLE:</b>, choose your ship (type to
            search). Its cargo size limits how much each route can carry.
            <b style="{acc}">-- No Ship Cap --</b> removes the limit.</p>
            <p>2. Read the table. Each row is one trade, best first. Click a
            column heading to sort by it and click again to reverse.</p>
            <p>3. Double-click a row to open that route's card, with the
            terminals, prices, distance and profit.</p>
            <p style="{hdr_style}">FRESH PRICES</p>
            <p><b style="{acc}">REFRESH</b> fetches the prices again. The tool
            also does this by itself every few minutes.</p>
            <p style="{hdr_style}">OPENING IT</p>
            <p>Trade Hub is the <b style="{acc}">TRADE HUB</b> tab of the
            Everything Finder. Its own hotkey is {hotkey}, which opens it in a
            window of its own.</p>
        """

        views = f"""
            <p style="{hdr_style}">VIEW MODE</p>
            <p>The buttons under <b style="{acc}">VIEW MODE:</b> change what the
            main area shows.</p>
            <p><b style="{acc}">ROUTES</b> &mdash; single trades: buy here, sell
            there.</p>
            <p><b style="{acc}">LOOPS</b> &mdash; chains of trades, selling at
            each stop and buying for the next leg.</p>
            <p><b style="{acc}">BASKET</b> &mdash; you say what you want.
            Choose <b>BUY</b> or <b>SELL</b> beside <b>MODE:</b>, pick a
            <b>START LOCATION:</b>, tick the commodities, and press
            <b>PLAN ROUTE</b> (or <b>PLAN SALE</b>). It offers up to five
            plans, such as <b>MIN STOPS</b>, <b>SHORTEST TRIP</b> and
            <b>BEST PRICE</b>.</p>
            <p><b style="{acc}">STAR MAP</b> &mdash; Trade Hub's own map, where a
            route can be drawn.</p>
            <p><b style="{acc}">HEATMAP</b> &mdash; three short lists:
            <b>TOP ROUTES</b>, <b>TRADE FLOWS</b> and <b>ACTIVITY</b>.
            Double-click a row for its card. The filters at the side do not
            apply here.</p>
            <p><b style="{acc}">COMMODITIES</b> &mdash; a card for every
            commodity with its average buy and sell price.</p>
            <p><b style="{acc}">MY CAREER</b> &mdash; the runs you have logged,
            the ones that failed, and your favourite routes. A run is logged
            only when you press a button on a route's card (see
            <b>Cards</b>).</p>
        """

        freight = f"""
            <p style="{hdr_style}">FREIGHT MODE</p>
            <p><b style="{acc}">BULK</b> fills your cargo bay with one commodity
            for each leg.</p>
            <p><b style="{grn}">MIXED</b> combines several commodities in one
            load. It picks a high-value <span style="{grn}">Primary</span>
            commodity first, then fills the space that is left with
            <span style="{pur}">Filler</span> commodities going to the same
            place.</p>
            <p>MIXED needs a ship: with <b style="{acc}">-- No Ship Cap --</b>
            chosen it shows nothing.</p>
            <p style="{hdr_style}">A MIXED CARD</p>
            <p>Double-click a mixed route to see the whole load: each
            commodity, the SCU of it, the buy and sell prices and the profit,
            coloured by its role. An illegal commodity is marked
            <b style="{red}">ILLEGAL</b>.</p>
        """

        filters = f"""
            <p style="{hdr_style}">NARROWING THE LIST</p>
            <p><b style="{acc}">SYSTEM: BUY</b> and
            <b style="{acc}">SYSTEM: SELL</b> &mdash; the star system at each
            end of the trade.</p>
            <p><b style="{acc}">ONLY SYSTEM(S) SELECTED:</b> &mdash; on
            <b>YES</b>, both ends must be in the systems you chose.</p>
            <p><b style="{acc}">BUY LOCATION</b> and
            <b style="{acc}">SELL LOCATION</b> &mdash; a place or terminal at
            each end.</p>
            <p><b style="{acc}">COMMODITY</b> &mdash; routes for one commodity
            only.</p>
            <p><b style="{acc}">MIN SCU</b> &mdash; hide routes with less stock
            than this.</p>
            <p><b style="{acc}">MIN PROFIT/SCU</b> &mdash; hide routes that earn
            less than this for each SCU.</p>
            <p><b style="{acc}">STARTING INVESTMENT (aUEC)</b> &mdash; what you
            have to spend. Routes whose first purchase costs more are
            hidden.</p>
            <p style="{dim}">Press Enter after typing a number in the last
            three.</p>
            <p><b style="{acc}">SEARCH</b> &mdash; type any text; the list
            narrows as you type.</p>
            <p><b style="{acc}">CLEAR</b> empties every filter.</p>
            <p style="{hdr_style}">ILLEGAL CARGO</p>
            <p><b style="{acc}">ALLOW ILLEGAL CARGO:</b> starts on
            <b style="{red}">NO</b>, which takes illegal commodities out of
            <b>ROUTES</b> and out of <b>MIXED</b> loads. The other views can
            still list them.</p>
            <p style="{hdr_style}">HOW MUCH YOU CAN SELL</p>
            <p>Under <b style="{acc}">MARKET CALCULATIONS:</b>,
            <b>Reported Demand</b> (the default) limits a load to what the
            buying terminal is reported to want, as well as to what is in
            stock. <b>Max Profit</b> ignores demand and limits a load only by
            stock and by your ship.</p>
        """

        cards = f"""
            <p style="{hdr_style}">A ROUTE'S CARD</p>
            <p>Double-click a row to open its card. A card stays open until you
            close it, so you can keep several side by side.</p>
            <p><b style="{acc}">Show Route</b> &mdash; draws the route on the
            <b>STAR MAP</b> view and switches to it.</p>
            <p><b style="{acc}">Show Trade Hub</b> &mdash; goes back to the
            <b>ROUTES</b> table.</p>
            <p><b style="{acc}">Complete Route</b> &mdash; you flew it. The run
            is logged to <b>MY CAREER</b> and the card closes.</p>
            <p><b style="{acc}">Route Failed</b> &mdash; it went wrong. Tick
            what happened (<b>Pirate Attack</b>, <b>Unable to Sell</b>,
            <b>Ship Loss</b> or <b>Price Change</b>), say whether you lost
            everything or part of it, and press <b>Log Failure</b>.</p>
            <p><b style="{acc}">Add Favorite</b> &mdash; keeps the route in
            <b>MY CAREER</b>.</p>
            <p style="{hdr_style}">PROFIT CALCULATOR</p>
            <p><b style="{acc}">PROFIT CALC</b> opens a small calculator. Enter
            what you had under <b>Starting Income (aUEC)</b> and what you have
            now under <b>Ending Income (aUEC)</b>, then press
            <b>CALCULATE</b>.</p>
        """

        columns = f"""
            <p style="{hdr_style}">COLUMNS OF THE ROUTES TABLE</p>
            <p><b style="{acc}">Item</b> &mdash; the commodity.
            <b style="{acc}">Buy At</b> and <b style="{acc}">Sell At</b>
            &mdash; the two terminals.</p>
            <p><b style="{acc}">CS</b> &mdash; the container sizes the terminal
            handles.</p>
            <p><b style="{acc}">Invest</b> &mdash; what the load costs to
            buy.</p>
            <p><b style="{acc}">SCU</b> &mdash; how much you can load: limited
            by your ship and by the stock on sale, and by demand as well when
            <b>MARKET CALCULATIONS:</b> is on <b>Reported Demand</b>.</p>
            <p><b style="{acc}">Distance</b> &mdash; how far apart the two
            terminals are.</p>
            <p><b style="{acc}">ETA</b> &mdash; a rough travel time. It is the
            same whichever ship you chose.</p>
            <p><b style="{acc}">ROI</b> &mdash; profit as a percentage of what
            the load cost.</p>
            <p><b style="{acc}">Income</b> &mdash; the profit you can expect
            from the load.</p>
            <p style="{hdr_style}">TIPS</p>
            <p>• The table shows only the best few hundred rows. Use the
            filters to bring the ones you want into it.</p>
            <p>• Your ship, the freight mode and the illegal cargo choice
            are remembered. The view mode and the filters start fresh each
            time.</p>
        """

        return [("Basics", basics), ("Views", views), ("Freight", freight),
                ("Filters", filters), ("Cards", cards), ("Columns", columns)]

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

        for title, html in self._tutorial_pages():
            tabs.addTab(_make_tab(html), title)

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
            if cmd.get("max_investment") and hasattr(self, "_max_investment"):
                self._max_investment.setText(str(cmd["max_investment"]))
            self._apply_search()
        elif t == "clear_filters":
            self._clear_filters()
        elif t == "refresh":
            self._status_label.setText("  Refreshing...")
            self._fetcher.fetch_async(self._on_routes)
        elif t == "opacity":
            val = max(0.3, min(1.0, float(cmd.get("value", 0.95))))
            self.set_opacity(val)
        elif t == "set_freight_mode":
            mode = cmd.get("mode", "BULK")
            if mode in ("BULK", "MIXED"):
                self._set_freight_mode(mode)
        elif t == "set_illegal_cargo":
            self._set_allow_illegal(bool(cmd.get("allow", False)))
        elif t == "route_detail":
            # AI Assistant: open a Route Detail popup from a data dict,
            # optionally pinned and drawn on the star map.
            data = cmd.get("data")
            if isinstance(data, dict):
                self.show()
                self.raise_()
                self._visible = True
                dlg = self._open_route_data(data)
                if cmd.get("pin") and not dlg._pinned:
                    dlg._toggle_pin()
                if cmd.get("show_on_map"):
                    QTimer.singleShot(100, dlg._show_on_map)

    def closeEvent(self, event) -> None:
        if hasattr(self, '_starmap_panel'):
            self._starmap_panel.shutdown()
        if hasattr(self, '_ipc'):
            self._ipc.stop()
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
