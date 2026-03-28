"""Mining Loadout main window — PySide6 GUI."""
import ctypes
import json
import logging
import queue
import sys
import os
import threading
import time
import traceback
from typing import Any, Dict, List, Optional, Tuple

import shared.path_setup  # noqa: E402  # centralised path config
from shared.i18n import s_ as _

from PySide6.QtCore import Qt, QTimer, Signal, QObject
from PySide6.QtGui import QGuiApplication, QClipboard
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame,
)


class _DataSignals(QObject):
    """Thread-safe signals for background→main-thread data delivery."""
    data_ready = Signal(list, list, list)   # lasers, modules, gadgets
    fetch_error = Signal()

from shared.qt.theme import P, apply_theme
from shared.qt.base_window import SCWindow
from shared.qt.dropdown import SCComboBox

from models.items import (
    GadgetItem, LaserItem, ModuleItem,
    NONE_GADGET, NONE_LASER, NONE_MODULE,
    SHIPS,
)
from services.calc_service import calc_stats, calc_loadout_price
from services.config_service import load_config, save_config
from services.api_client import fetch_mining_data
from ui.constants import P as _P, STAT_LABEL_MAP
from ui.components.title_bar import build_title_bar
from ui.components.sidebar import build_sidebar
from ui.components.stats_panel import build_stats_panel
from ui.components.turret_panel import build_turret_panel
from ui.components.detail_card import DetailCardManager
from ui.components.tutorial_bubble import TutorialBubble

log = logging.getLogger("MiningLoadout")

# ── Win32 constants ───────────────────────────────────────────────────────────
if sys.platform == "win32":
    _user32 = ctypes.windll.user32
    _kernel32 = ctypes.windll.kernel32
else:
    _user32 = None
    _kernel32 = None

_HWND_TOPMOST = -1
_SWP_NOSIZE = 0x0001
_SWP_NOMOVE = 0x0002
_SWP_NOACTIVATE = 0x0010
_SW_RESTORE = 9


class MiningLoadoutWindow(SCWindow):
    """Main mining loadout GUI window."""

    def __init__(
        self,
        cmd_queue: queue.Queue,
        win_x: int = 80,
        win_y: int = 80,
        win_w: int = 1200,
        win_h: int = 720,
        refresh_interval: float = 86400.0,
        opacity: float = 0.95,
    ) -> None:
        super().__init__(
            title="Mining Loadout",
            width=win_w,
            height=win_h,
            min_w=800,
            min_h=500,
            opacity=opacity,
            always_on_top=True,
        )
        self.move(win_x, win_y)

        self.cmd_queue = cmd_queue
        self.refresh_interval = refresh_interval

        # Data
        self.all_lasers: List[LaserItem] = []
        self.all_modules: List[ModuleItem] = []
        self.all_gadgets: List[GadgetItem] = []
        self._data_loaded = False
        self._fetching = False
        self._last_fetch_ts: Optional[float] = None

        # Loadout state
        self.ship_name = "MOLE"
        self._turret_laser_selections: List[str] = []
        self._turret_module_selections: List[List[str]] = []

        # UI references (set during _build_ui)
        self._turret_area: Optional[QHBoxLayout] = None
        self._stat_labels: Dict[str, QLabel] = {}
        self._stat_directions: Dict[str, int] = {}
        self._price_detail_label: Optional[QLabel] = None
        self._src_detail_label: Optional[QLabel] = None
        self._price_bar_label: Optional[QLabel] = None
        self._status_label: Optional[QLabel] = None
        self._upd_label: Optional[QLabel] = None
        self._src_label: Optional[QLabel] = None
        self._gadget_combo: Optional[SCComboBox] = None
        self._laser_combos: List[SCComboBox] = []
        self._module_combos: List[List[SCComboBox]] = []
        self._ship_btns: Dict[str, Any] = {}
        self._card_manager: Optional[DetailCardManager] = None
        self._tutorial_bubble: Optional[TutorialBubble] = None

        # Thread-safe signals for data loading
        self._data_signals = _DataSignals(self)
        self._data_signals.data_ready.connect(self._on_data_loaded)
        self._data_signals.fetch_error.connect(self._on_fetch_error)

        self._card_manager = DetailCardManager(self, opacity)
        self._build_ui()
        self._load_config()

    # ── Entry ──────────────────────────────────────────────────────────────────

    def run(self) -> None:
        """Show window, start timers. Called after QApplication is created."""
        try:
            self.show()
            self.raise_()
            self._force_show()
            self._start_poll_queue()
            self._start_keepalive()
            QTimer.singleShot(400, self._start_load)
            log.info("Window shown")
        except Exception:  # broad catch intentional: top-level UI entry point
            tb = traceback.format_exc()
            log.critical("FATAL:\n%s", tb)

    # ── Win32 helpers ─────────────────────────────────────────────────────────

    def _get_hwnd(self) -> Optional[int]:
        try:
            return int(self.winId())
        except (RuntimeError, OverflowError, ValueError):
            return None

    def _apply_topmost(self) -> None:
        hwnd = self._get_hwnd()
        if hwnd and _user32:
            try:
                _user32.SetWindowPos(hwnd, _HWND_TOPMOST, 0, 0, 0, 0,
                                     _SWP_NOSIZE | _SWP_NOMOVE | _SWP_NOACTIVATE)
            except OSError:
                log.debug("SetWindowPos topmost failed: %s", traceback.format_exc())

    def _force_show(self) -> None:
        hwnd = self._get_hwnd()
        if not hwnd or not _user32:
            return
        try:
            _user32.ShowWindow(hwnd, _SW_RESTORE)
        except OSError:
            log.debug("_force_show Win32 call failed: %s", traceback.format_exc())
        self._apply_topmost()

    def _start_keepalive(self) -> None:
        timer = QTimer(self)
        timer.timeout.connect(self._keepalive_tick)
        timer.start(2000)

    def _keepalive_tick(self) -> None:
        if self.isVisible():
            self._apply_topmost()

    # ── Build UI ──────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        layout = self.content_layout

        # Title bar
        bar_refs = build_title_bar(
            window=self,
            on_close=lambda: self.hide(),
            on_refresh=self._do_refresh,
            on_tutorial=self._show_tutorial,
        )
        layout.addWidget(bar_refs["title_bar"])
        self._upd_label = bar_refs["upd_label"]
        self._src_label = bar_refs["src_label"]

        # Accent line under title
        accent_line = QFrame()
        accent_line.setFixedHeight(1)
        accent_line.setStyleSheet(f"background-color: {P.tool_mining};")
        layout.addWidget(accent_line)

        # Body: sidebar | center | stats
        body = QWidget()
        body.setStyleSheet(f"background-color: {P.bg_primary};")
        body_lay = QHBoxLayout(body)
        body_lay.setContentsMargins(0, 0, 0, 0)
        body_lay.setSpacing(0)

        # Sidebar
        sb_refs = build_sidebar(
            on_ship_changed=self._on_ship_changed,
            on_reset=self._reset_loadout,
            on_copy_stats=self._copy_stats,
        )
        body_lay.addWidget(sb_refs["widget"])
        self._ship_btns = sb_refs["ship_btns"]
        self._status_label = sb_refs["status_label"]

        # Center area
        center = QWidget()
        center.setStyleSheet(f"background-color: {P.bg_primary};")
        center_lay = QVBoxLayout(center)
        center_lay.setContentsMargins(6, 6, 6, 6)
        center_lay.setSpacing(6)

        # Turret area
        turret_container = QWidget()
        turret_container.setStyleSheet(f"background-color: {P.bg_primary};")
        self._turret_area = QHBoxLayout(turret_container)
        self._turret_area.setContentsMargins(0, 0, 0, 0)
        self._turret_area.setSpacing(4)
        center_lay.addWidget(turret_container, 1)

        # Gadget strip
        inv_frame = QWidget()
        inv_frame.setStyleSheet(f"background-color: {P.bg_header};")
        inv_lay = QHBoxLayout(inv_frame)
        inv_lay.setContentsMargins(8, 6, 8, 6)
        inv_lay.setSpacing(4)

        gadget_lbl = QLabel("  " + _("INVENTORY \u2014 GADGET"))
        gadget_lbl.setStyleSheet(f"""
            font-family: Electrolize, Consolas;
            font-size: 9pt;
            font-weight: bold;
            color: {P.tool_mining};
            background: transparent;
        """)
        inv_lay.addWidget(gadget_lbl)

        self._gadget_combo = SCComboBox()
        self._gadget_combo.addItem(NONE_GADGET)
        self._gadget_combo.currentIndexChanged.connect(lambda _: self._on_loadout_changed())
        inv_lay.addWidget(self._gadget_combo)

        ginfo = QLabel(" \u24d8 ")
        ginfo.setStyleSheet(f"""
            font-family: Consolas;
            font-size: 11pt;
            color: {P.accent};
            background: transparent;
        """)
        ginfo.setCursor(Qt.PointingHandCursor)
        ginfo.mousePressEvent = lambda _: self._pin_item("gadget")
        inv_lay.addWidget(ginfo)
        inv_lay.addStretch(1)

        center_lay.addWidget(inv_frame)

        body_lay.addWidget(center, 1)

        # Stats panel (right side)
        stats_separator = QFrame()
        stats_separator.setFixedWidth(1)
        stats_separator.setStyleSheet(f"background-color: {P.tool_mining};")
        body_lay.addWidget(stats_separator)

        stats_widget = QWidget()
        stats_widget.setFixedWidth(268)
        stats_widget.setStyleSheet(f"background-color: {P.bg_card};")
        sp_refs = build_stats_panel(stats_widget)
        self._stat_labels = sp_refs["stat_labels"]
        self._stat_directions = sp_refs["stat_directions"]
        self._price_detail_label = sp_refs["price_detail_label"]
        self._src_detail_label = sp_refs["src_detail_label"]
        body_lay.addWidget(stats_widget)

        layout.addWidget(body, 1)

        # Bottom status bar
        bot_accent = QFrame()
        bot_accent.setFixedHeight(1)
        bot_accent.setStyleSheet(f"background-color: {P.tool_mining};")
        layout.addWidget(bot_accent)

        status_bar = QWidget()
        status_bar.setFixedHeight(22)
        status_bar.setStyleSheet(f"background-color: {P.bg_header};")
        sbar_lay = QHBoxLayout(status_bar)
        sbar_lay.setContentsMargins(8, 0, 8, 0)
        sbar_lay.setSpacing(4)

        self._price_bar_label = QLabel("  " + _("Loadout Price:") + "  \u2014 " + _("aUEC"))
        self._price_bar_label.setStyleSheet(f"""
            font-family: Consolas;
            font-size: 9pt;
            color: {P.green};
            background: transparent;
        """)
        sbar_lay.addWidget(self._price_bar_label)
        sbar_lay.addStretch(1)

        layout.addWidget(status_bar)

        self._update_ship_btn_styles()
        self._rebuild_turret_panels(reset_to_stock=False)

    # ── Turret panels ────────────────────────────────────────────────────────

    def _rebuild_turret_panels(self, reset_to_stock: bool = True) -> None:
        if not self._turret_area:
            return

        # Clear existing turret widgets
        while self._turret_area.count():
            item = self._turret_area.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        self._laser_combos.clear()
        self._module_combos.clear()

        cfg = SHIPS[self.ship_name]
        n = cfg.turrets
        stock = cfg.stock_laser

        # Ensure selections exist
        while len(self._turret_laser_selections) < n:
            self._turret_laser_selections.append(stock or NONE_LASER)
        while len(self._turret_module_selections) < n:
            self._turret_module_selections.append([NONE_MODULE, NONE_MODULE])

        # Trim to current ship
        self._turret_laser_selections = self._turret_laser_selections[:n]
        self._turret_module_selections = self._turret_module_selections[:n]

        # Only reset to stock on explicit ship change / reset
        if reset_to_stock:
            for i in range(n):
                self._turret_laser_selections[i] = stock or NONE_LASER
                for j in range(2):
                    self._turret_module_selections[i][j] = NONE_MODULE

        for i in range(n):
            refs = build_turret_panel(
                turret_name=cfg.turret_names[i],
                laser_size=cfg.laser_size,
                turret_index=i,
                on_changed=self._on_loadout_changed,
                on_laser_info=lambda ti: self._pin_item("laser", ti),
                on_module_info=lambda ti, sl: self._pin_item("module", ti, sl),
            )
            self._turret_area.addWidget(refs["widget"])
            self._laser_combos.append(refs["laser_combo"])
            self._module_combos.append(refs["module_combos"])

        if self._data_loaded:
            self._populate_dropdowns()
        self._on_loadout_changed()

    def _populate_dropdowns(self) -> None:
        cfg = SHIPS[self.ship_name]
        lsize = cfg.laser_size
        n = cfg.turrets
        stock = cfg.stock_laser

        laser_names = [NONE_LASER] + sorted(
            l.name for l in self.all_lasers if l.size == 0 or l.size == lsize
        )
        module_names = [NONE_MODULE] + sorted(m.name for m in self.all_modules)
        gadget_names = [NONE_GADGET] + sorted(g.name for g in self.all_gadgets)

        for i in range(n):
            if i < len(self._laser_combos):
                combo = self._laser_combos[i]
                combo.blockSignals(True)
                combo.clear()
                combo.addItems(laser_names)
                cur = self._turret_laser_selections[i]
                idx = combo.findText(cur)
                if idx >= 0:
                    combo.setCurrentIndex(idx)
                else:
                    stock_idx = combo.findText(stock) if stock else 0
                    combo.setCurrentIndex(max(0, stock_idx))
                combo.blockSignals(False)

            if i < len(self._module_combos):
                for j, mc in enumerate(self._module_combos[i]):
                    mc.blockSignals(True)
                    mc.clear()
                    mc.addItems(module_names)
                    cur = self._turret_module_selections[i][j]
                    idx = mc.findText(cur)
                    if idx >= 0:
                        mc.setCurrentIndex(idx)
                    else:
                        mc.setCurrentIndex(0)
                    mc.blockSignals(False)

        if self._gadget_combo is not None:
            self._gadget_combo.blockSignals(True)
            self._gadget_combo.clear()
            self._gadget_combo.addItems(gadget_names)
            self._gadget_combo.setCurrentIndex(0)
            self._gadget_combo.blockSignals(False)

    # ── Stat calculation & display ────────────────────────────────────────────

    def _on_loadout_changed(self, _=None) -> None:
        self._sync_selections_from_combos()
        self._update_module_slot_states()
        self._update_stats()
        self._save_config()

    def _sync_selections_from_combos(self) -> None:
        """Sync internal selection lists from combo box state."""
        cfg = SHIPS[self.ship_name]
        for i in range(cfg.turrets):
            if i < len(self._laser_combos):
                self._turret_laser_selections[i] = self._laser_combos[i].currentText()
            if i < len(self._module_combos):
                for j, mc in enumerate(self._module_combos[i]):
                    if i < len(self._turret_module_selections) and j < len(self._turret_module_selections[i]):
                        self._turret_module_selections[i][j] = mc.currentText()

    def _update_module_slot_states(self) -> None:
        cfg = SHIPS[self.ship_name]
        for i in range(cfg.turrets):
            lname = self._turret_laser_selections[i] if i < len(self._turret_laser_selections) else ""
            laser = self._get_laser(lname)
            slots = laser.module_slots if laser else 2
            if i < len(self._module_combos):
                for j, mc in enumerate(self._module_combos[i]):
                    if j < slots:
                        mc.setEnabled(True)
                    else:
                        mc.setEnabled(False)
                        if i < len(self._turret_module_selections) and j < len(self._turret_module_selections[i]):
                            self._turret_module_selections[i][j] = NONE_MODULE
                            mc.blockSignals(True)
                            mc.setCurrentIndex(0)
                            mc.blockSignals(False)

    def _get_laser(self, name: str) -> Optional[LaserItem]:
        return next((l for l in self.all_lasers if l.name == name), None)

    def _get_module(self, name: str) -> Optional[ModuleItem]:
        return next((m for m in self.all_modules if m.name == name), None)

    def _get_gadget(self, name: str) -> Optional[GadgetItem]:
        return next((g for g in self.all_gadgets if g.name == name), None)

    def _update_stats(self) -> None:
        cfg = SHIPS[self.ship_name]
        n = cfg.turrets
        stock = cfg.stock_laser

        laser_items: List[Optional[LaserItem]] = []
        module_items: List[List[Optional[ModuleItem]]] = []

        for i in range(n):
            lname = self._turret_laser_selections[i] if i < len(self._turret_laser_selections) else ""
            laser_items.append(self._get_laser(lname))
            mods: List[Optional[ModuleItem]] = []
            for j in range(2):
                mname = self._turret_module_selections[i][j] if i < len(self._turret_module_selections) else ""
                mods.append(self._get_module(mname))
            module_items.append(mods)

        gname = self._gadget_combo.currentText() if self._gadget_combo else ""
        gadget = self._get_gadget(gname)

        stats = calc_stats(self.ship_name, laser_items, module_items, gadget)
        price = calc_loadout_price(self.ship_name, laser_items, module_items, gadget)

        for key, lbl in self._stat_labels.items():
            v = stats.get(key, 0)
            if key in ("min_power", "max_power", "ext_power"):
                lbl.setText(self._fmt_pwr(v))
            elif key in ("opt_range", "max_range"):
                lbl.setText(f"{v:.0f} m" if v else "\u2014")
            else:
                lbl.setText(self._fmt_pct(v))

            direction = self._stat_directions.get(key, 0)
            if direction == 0 or v == 0:
                color = P.fg
            elif direction == 1:
                color = P.green if v > 0 else (P.red if v < 0 else P.fg)
            else:
                color = P.green if v < 0 else (P.red if v > 0 else P.fg)
            lbl.setStyleSheet(f"""
                font-family: Consolas;
                font-size: 9pt;
                font-weight: bold;
                color: {color};
                background: transparent;
            """)

        if self._price_detail_label:
            self._price_detail_label.setText(f"{price:,.0f} " + _("aUEC"))
        if self._price_bar_label:
            self._price_bar_label.setText(f"  {_('Loadout Price:')}  {price:,.0f} " + _("aUEC"))
        if self._src_detail_label:
            self._src_detail_label.setText(f"Stock laser: {stock} (free)\nData: UEX Corp API")

    @staticmethod
    def _fmt_pct(v: float) -> str:
        if v == 0:
            return "0%"
        return f"{v:+.0f}%"

    @staticmethod
    def _fmt_pwr(v: float) -> str:
        if v is None:
            return "\u2014"
        if v < 1000:
            return f"{int(v + 0.5):,}"
        return f"{v:,.1f}"

    # ── Detail cards ──────────────────────────────────────────────────────────

    def _pin_item(self, kind: str, turret_idx: int = 0, slot: int = 0) -> None:
        item = None
        if kind == "laser":
            lname = self._turret_laser_selections[turret_idx] if turret_idx < len(self._turret_laser_selections) else ""
            item = self._get_laser(lname)
        elif kind == "module":
            mname = self._turret_module_selections[turret_idx][slot] if turret_idx < len(self._turret_module_selections) else ""
            item = self._get_module(mname)
        elif kind == "gadget":
            gname = self._gadget_combo.currentText() if self._gadget_combo else ""
            item = self._get_gadget(gname)
        if item and self._card_manager:
            self._card_manager.pin_item(kind, item)

    # ── Ship management ───────────────────────────────────────────────────────

    def _update_ship_btn_styles(self) -> None:
        for ship, btn in self._ship_btns.items():
            active = (ship == self.ship_name)
            if active:
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background-color: {P.tool_mining};
                        color: #000000;
                        border: 1px solid {P.tool_mining};
                        padding: 4px;
                        font-family: Consolas;
                        font-size: 9pt;
                        font-weight: bold;
                    }}
                """)
            else:
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background-color: {P.bg_card};
                        color: {P.fg_dim};
                        border: 1px solid {P.border};
                        padding: 4px;
                        font-family: Consolas;
                        font-size: 9pt;
                        font-weight: bold;
                    }}
                    QPushButton:hover {{
                        background-color: {P.bg_input};
                        border-color: {P.tool_mining};
                        color: {P.fg_bright};
                    }}
                """)

    def _on_ship_changed(self, ship: str) -> None:
        if ship == self.ship_name:
            return
        self.ship_name = ship
        self._update_ship_btn_styles()
        self._rebuild_turret_panels()
        log.info("Ship changed to %s", ship)

    def _reset_loadout(self) -> None:
        cfg = SHIPS[self.ship_name]
        stock = cfg.stock_laser
        for i in range(cfg.turrets):
            if i < len(self._turret_laser_selections):
                self._turret_laser_selections[i] = stock or NONE_LASER
            if i < len(self._turret_module_selections):
                for j in range(2):
                    self._turret_module_selections[i][j] = NONE_MODULE
        if self._gadget_combo:
            self._gadget_combo.blockSignals(True)
            self._gadget_combo.setCurrentIndex(0)
            self._gadget_combo.blockSignals(False)
        # Re-sync combos
        self._populate_dropdowns()
        self._on_loadout_changed()
        log.info("Loadout reset for %s", self.ship_name)

    def _copy_stats(self) -> None:
        cfg = SHIPS[self.ship_name]
        n = cfg.turrets
        lines = [f"Mining Loadout \u2014 {self.ship_name}", ""]
        for i in range(n):
            lname = self._turret_laser_selections[i] if i < len(self._turret_laser_selections) else NONE_LASER
            m1 = self._turret_module_selections[i][0] if i < len(self._turret_module_selections) else NONE_MODULE
            m2 = self._turret_module_selections[i][1] if i < len(self._turret_module_selections) else NONE_MODULE
            lines.append(f"{cfg.turret_names[i]}: {lname}  |  {m1}  |  {m2}")
        gname = self._gadget_combo.currentText() if self._gadget_combo else NONE_GADGET
        lines.append(f"Gadget: {gname}")
        lines.append("")
        for key, lbl in self._stat_labels.items():
            lines.append(f"{STAT_LABEL_MAP.get(key, key)}: {lbl.text()}")
        if self._price_detail_label:
            lines.append(f"Price: {self._price_detail_label.text()}")
        try:
            clipboard = QGuiApplication.clipboard()
            clipboard.setText("\n".join(lines))
        except RuntimeError:
            log.debug("Clipboard copy failed: %s", traceback.format_exc())

    # ── Tutorial ──────────────────────────────────────────────────────────────

    def _show_tutorial(self) -> None:
        if self._tutorial_bubble is not None:
            try:
                if self._tutorial_bubble.isVisible():
                    self._tutorial_bubble.raise_()
                    self._tutorial_bubble.activateWindow()
                    return
            except RuntimeError:
                self._tutorial_bubble = None
        self._tutorial_bubble = TutorialBubble(self, self.windowOpacity())
        self._tutorial_bubble.show()

    # ── Data loading ──────────────────────────────────────────────────────────

    def _start_load(self, use_cache: bool = True) -> None:
        if self._fetching:
            return
        self._fetching = True
        if self._status_label:
            self._status_label.setText("  " + _("Fetching UEX data\u2026"))
        threading.Thread(
            target=self._fetch_worker, args=(use_cache,),
            daemon=True, name="MiningFetch",
        ).start()

    def _fetch_worker(self, use_cache: bool = True) -> None:
        try:
            lasers, modules, gadgets = fetch_mining_data(use_cache=use_cache)
            self._data_signals.data_ready.emit(lasers, modules, gadgets)
        except Exception:  # broad catch intentional: background fetch thread
            log.error("Fetch failed:\n%s", traceback.format_exc())
            self._data_signals.fetch_error.emit()
        finally:
            self._fetching = False

    def _on_fetch_error(self) -> None:
        if self._status_label:
            self._status_label.setText("  " + _("API fetch failed \u2014 check internet"))

    def _on_data_loaded(
        self,
        lasers: List[LaserItem],
        modules: List[ModuleItem],
        gadgets: List[GadgetItem],
    ) -> None:
        self.all_lasers = lasers
        self.all_modules = modules
        self.all_gadgets = gadgets
        self._data_loaded = True
        self._last_fetch_ts = time.time()
        self._populate_dropdowns()
        self._on_loadout_changed()
        ts = time.strftime("%H:%M:%S")
        if self._status_label:
            self._status_label.setText(f"  {len(lasers)} lasers \u00b7 {len(modules)} modules \u00b7 {len(gadgets)} gadgets")
        if self._upd_label:
            self._upd_label.setText(f"Updated {ts}")
        if self._src_label:
            self._src_label.setText("[UEX API]")
        log.info("Data loaded: %d lasers, %d modules, %d gadgets", len(lasers), len(modules), len(gadgets))
        QTimer.singleShot(3_600_000, self._auto_refresh_loop)

    def _do_refresh(self) -> None:
        if self._fetching:
            return
        if self._status_label:
            self._status_label.setText("  " + _("Refreshing data\u2026"))
        self._start_load(use_cache=False)

    def _auto_refresh_loop(self) -> None:
        if self._last_fetch_ts and (time.time() - self._last_fetch_ts) >= self.refresh_interval:
            self._do_refresh()
        QTimer.singleShot(3_600_000, self._auto_refresh_loop)

    # ── IPC polling ───────────────────────────────────────────────────────────

    def _start_poll_queue(self) -> None:
        timer = QTimer(self)
        timer.timeout.connect(self._poll_queue)
        timer.start(150)

    def _poll_queue(self) -> None:
        try:
            while True:
                cmd = self.cmd_queue.get_nowait()
                log.debug("IPC: %s", cmd)
                self._dispatch(cmd)
        except queue.Empty:
            pass

    def _dispatch(self, cmd: dict) -> None:
        t = cmd.get("type", "")
        if t == "show":
            self.show()
            self.raise_()
            self._force_show()
        elif t == "hide":
            self.hide()
        elif t == "quit":
            QApplication.instance().quit()
        elif t == "refresh":
            self._do_refresh()
        elif t == "reset":
            self._reset_loadout()
        elif t == "set_ship":
            ship = cmd.get("ship", "")
            if ship in SHIPS:
                self._on_ship_changed(ship)
        elif t == "set_laser":
            ti = int(cmd.get("turret", 0))
            lname = cmd.get("name", cmd.get("laser_name", ""))
            if lname and ti < len(self._turret_laser_selections):
                match = next((l.name for l in self.all_lasers if lname.lower() in l.name.lower()), None)
                if match:
                    self._turret_laser_selections[ti] = match
                    if ti < len(self._laser_combos):
                        combo = self._laser_combos[ti]
                        idx = combo.findText(match)
                        if idx >= 0:
                            combo.setCurrentIndex(idx)
                    self._on_loadout_changed()
        elif t == "set_module":
            ti = int(cmd.get("turret", 0))
            slot = int(cmd.get("slot", 0))
            mname = cmd.get("name", cmd.get("module_name", ""))
            if mname and ti < len(self._turret_module_selections) and slot < 2:
                match = next((m.name for m in self.all_modules if mname.lower() in m.name.lower()), None)
                if match:
                    self._turret_module_selections[ti][slot] = match
                    if ti < len(self._module_combos) and slot < len(self._module_combos[ti]):
                        mc = self._module_combos[ti][slot]
                        idx = mc.findText(match)
                        if idx >= 0:
                            mc.setCurrentIndex(idx)
                    self._on_loadout_changed()
        elif t == "set_gadget":
            gname = cmd.get("name", cmd.get("gadget_name", ""))
            if gname and self._gadget_combo:
                match = next((g.name for g in self.all_gadgets if gname.lower() in g.name.lower()), None)
                if match:
                    idx = self._gadget_combo.findText(match)
                    if idx >= 0:
                        self._gadget_combo.setCurrentIndex(idx)
                    self._on_loadout_changed()

    # ── Config persistence ────────────────────────────────────────────────────

    def _load_config(self) -> None:
        cfg = load_config()
        saved_ship = cfg.get("ship", "MOLE")
        if saved_ship in SHIPS:
            self.ship_name = saved_ship
            self._update_ship_btn_styles()
        loadout = cfg.get("loadout", {})
        ship_cfg = SHIPS.get(self.ship_name)
        n = ship_cfg.turrets if ship_cfg else 1
        while len(self._turret_laser_selections) < n:
            self._turret_laser_selections.append(NONE_LASER)
        while len(self._turret_module_selections) < n:
            self._turret_module_selections.append([NONE_MODULE, NONE_MODULE])
        for i in range(n):
            key = f"turret_{i}"
            if key in loadout:
                td = loadout[key]
                self._turret_laser_selections[i] = td.get("laser", NONE_LASER)
                mods = td.get("modules", [NONE_MODULE, NONE_MODULE])
                for j in range(2):
                    self._turret_module_selections[i][j] = mods[j] if j < len(mods) else NONE_MODULE

    def _save_config(self) -> None:
        cfg = SHIPS.get(self.ship_name)
        n = cfg.turrets if cfg else 1
        turret_lasers = []
        turret_modules = []
        for i in range(n):
            lname = self._turret_laser_selections[i] if i < len(self._turret_laser_selections) else NONE_LASER
            turret_lasers.append(lname)
            mods = [
                self._turret_module_selections[i][j]
                if i < len(self._turret_module_selections) and j < len(self._turret_module_selections[i])
                else NONE_MODULE
                for j in range(2)
            ]
            turret_modules.append(mods)
        gname = self._gadget_combo.currentText() if self._gadget_combo else NONE_GADGET
        save_config(self.ship_name, "", turret_lasers, turret_modules, gname)

