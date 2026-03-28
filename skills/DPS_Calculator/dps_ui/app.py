"""DPS Calculator main application — three-panel erkul.games layout (PySide6)."""
from __future__ import annotations

import logging
import os
import re
import sys
import threading
from typing import Optional

import requests
import webbrowser

from PySide6.QtCore import Qt, QTimer, Signal, Slot, QObject
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QSplitter, QScrollArea, QFrame, QSizePolicy, QTabWidget,
)

# Path setup — bootstrap from the parent DPS_Calculator dir (not dps_ui/)
_UI_DIR = os.path.dirname(os.path.abspath(__file__))
_APP_DIR = os.path.normpath(os.path.join(_UI_DIR, '..'))
sys.path.insert(0, os.path.normpath(os.path.join(_APP_DIR, '..', '..')))
from shared.app_bootstrap import bootstrap_skill  # noqa: E402
bootstrap_skill(os.path.join(_APP_DIR, '__init__.py'))

from shared.i18n import s_ as _
from shared.qt.theme import P, apply_theme
from shared.qt.base_window import SCWindow
from shared.qt.title_bar import SCTitleBar
from shared.qt.ipc_thread import IPCWatcher
from shared.qt.fuzzy_combo import SCFuzzyCombo
from shared.api_config import ERKUL_LOADOUT_TIMEOUT, ERKUL_VERSION_TIMEOUT

from dps_ui.constants import (
    BG, BG2, BG3, BG4, BORDER, FG, FG_DIM, FG_DIMMER, ACCENT, GREEN, YELLOW,
    RED, ORANGE, CYAN, PURPLE, PHYS_COL, ENERGY_COL, DIST_COL, THERM_COL,
    HEADER_BG, SECT_HDR_BG, CARD_EVEN, CARD_ODD, CARD_BORDER, ROW_EVEN, ROW_ODD,
    SIZE_COLORS, TYPE_STRIPE,
    API_BASE, API_HEADERS, CACHE_FILE,
    WEAPON_TABLE_COLS, MISSILE_TABLE_COLS, SHIELD_TABLE_COLS,
    COOLER_TABLE_COLS, RADAR_TABLE_COLS, PP_COLS, QD_COLS,
)
from dps_ui.helpers import _port_label, group_short, pct, _fy_slug, _fy_hp_group, fmt_sig
from dps_ui.widgets import ComponentTable, ComponentPickerPopup, _picker_btn
from dps_ui.power_widget import PowerAllocatorWidget as PowerAllocator
from data.repository import ComponentRepository
from services.slot_extractor import extract_slots_by_type
from services.stat_computation import (
    compute_shield_stats, compute_cooler_stats, compute_radar_stats,
    compute_powerplant_stats, compute_qdrive_stats, compute_thruster_stats,
    compute_powerplant_stats_erkul, compute_qdrive_stats_erkul,
)
from services.loadout_aggregator import compute_footer_totals, compute_raw_signatures

_log = logging.getLogger(__name__)


class _CallbackProxy(QObject):
    """Thread-safe signal relay: emit from any thread, slot runs on main thread."""
    _signal = Signal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._signal.connect(self._dispatch, Qt.QueuedConnection)

    @Slot(object)
    def _dispatch(self, pair):
        try:
            fn, args = pair
            _log.debug("_CallbackProxy dispatching %s", getattr(fn, '__name__', fn))
            fn(*args)
        except Exception:  # broad catch intentional: Qt signal proxy must not crash the main thread
            _log.critical("_CallbackProxy._dispatch CRASHED for %s",
                          getattr(fn, '__name__', fn), exc_info=True)

    def call_on_main(self, fn, *args) -> None:
        """Schedule *fn(*args)* on the Qt main thread.  Safe from any thread."""
        try:
            self._signal.emit((fn, args))
        except RuntimeError:
            pass  # proxy already deleted


class DpsCalcApp(SCWindow):
    """Three-panel DPS Calculator window using PySide6."""

    def __init__(self, x, y, w, h, opacity, cmd_file, *, preloaded_cache=None, needs_refresh=True) -> None:
        _log.info("DpsCalcApp.__init__ START (x=%s y=%s w=%s h=%s)", x, y, w, h)
        super().__init__(
            title="#DPS Calculator",
            width=w, height=h, min_w=800, min_h=400,
            opacity=opacity, always_on_top=True,
        )
        _log.info("  SCWindow.__init__ OK")

        self.cmd_file = cmd_file
        self._cb = _CallbackProxy(self)   # thread-safe main-thread relay
        self._data = ComponentRepository()
        self._preloaded_cache = preloaded_cache
        self._needs_refresh = needs_refresh
        self._ship_name: Optional[str] = None
        self._current_ship: Optional[dict] = None
        self._power_sim = True
        self._weapon_power_ratio = 1.0
        self._shield_power_ratio = 1.0
        self._flight_mode = "scm"
        self._ship_data: Optional[dict] = None
        self._pending_ship: Optional[str] = None
        self._ov_vars: dict = {}
        self._sig_vars: dict = {}

        self._sel: dict = {
            "weapons": {}, "missiles": {}, "defenses": {}, "components": {},
            "propulsion": {},
        }
        self._slot_tables: dict = {}
        self._rows: dict = {
            k: [] for k in ("weapons", "missiles", "defenses", "components", "propulsion")
        }
        self._cooler_rows = []
        self._radar_rows = []
        self._powerplant_rows = []
        self._qdrive_rows = []
        self._thruster_rows = []
        self._fy_groups: dict = {}

        self._ready = False
        _log.info("  Building UI...")
        self._build_ui()
        _log.info("  UI built OK")
        self.restore_geometry_from_args(x, y, w, h, opacity)
        _log.info("  Geometry restored")

        # IPC watcher
        if cmd_file:
            _log.info("  Starting IPC watcher for %s", cmd_file)
            self._ipc = IPCWatcher(cmd_file, poll_ms=200, parent=self)
            self._ipc.command_received.connect(self._dispatch)
            self._ipc.start()
            _log.info("  IPC watcher started")

        self._ready = True

        # Load data (after UI is fully built) — staged for progress & safe shutdown
        _log.info("  Starting data load (needs_refresh=%s)...", self._needs_refresh)
        self._data.load(
            on_done=lambda: self._cb.call_on_main(self._on_data_loaded),
            on_stage=lambda name, num, total: self._cb.call_on_main(
                self._on_load_stage, name, num, total),
            preloaded_cache=self._preloaded_cache,
            needs_refresh=self._needs_refresh,
        )
        self._preloaded_cache = None  # free memory

        # Pending ship check timer
        self._pending_timer = QTimer(self)
        self._pending_timer.timeout.connect(self._check_pending)
        self._pending_timer.start(500)
        _log.info("DpsCalcApp.__init__ DONE")

    # -- UI (three-panel layout) -------------------------------------------

    def _build_ui(self) -> None:
        _log.debug("_build_ui START")
        layout = self.content_layout

        # Title bar
        _log.debug("  Creating title bar...")
        title_bar = SCTitleBar(
            self, title=_("#DPS CALCULATOR"),
            icon_text="\u2694", accent_color=P.tool_dps,
            show_minimize=False,
            extra_buttons=[
                ("? Tutorial", self._show_tutorial),
                ("Erkul's Patreon", lambda: webbrowser.open("https://www.erkul.games/live/calculator")),
            ],
        )
        title_bar.close_clicked.connect(self.hide)
        layout.addWidget(title_bar)

        # Header bar
        hdr = QWidget(self)
        hdr.setFixedHeight(40)
        hdr.setStyleSheet(f"background-color: {HEADER_BG};")
        hdr_lay = QHBoxLayout(hdr)
        hdr_lay.setContentsMargins(10, 0, 10, 0)
        hdr_lay.setSpacing(8)

        # LIVE badge
        self._version_lbl = QLabel(_("LIVE"), hdr)
        self._version_lbl.setStyleSheet(f"""
            background-color: #1a3a2a; color: {GREEN};
            font-family: Consolas; font-size: 8pt; font-weight: bold;
            padding: 1px 4px;
        """)
        hdr_lay.addWidget(self._version_lbl)

        # Ship selector
        lbl_ship = QLabel(_("Ship"), hdr)
        lbl_ship.setStyleSheet(f"color: {FG_DIM}; font-family: Consolas; font-size: 9pt; background: transparent;")
        hdr_lay.addWidget(lbl_ship)

        self._ship_combo = SCFuzzyCombo(placeholder=_("Loading\u2026"), parent=hdr)
        self._ship_combo.setFixedWidth(240)
        self._ship_combo.item_selected.connect(self._on_ship_selected)
        hdr_lay.addWidget(self._ship_combo)

        hdr_lay.addStretch(1)

        # Status
        self._status_lbl = QLabel(_("Fetching data from erkul.games\u2026"), hdr)
        self._status_lbl.setStyleSheet(
            f"color: {FG_DIM}; font-family: Consolas; font-size: 8pt; background: transparent;"
        )
        hdr_lay.addWidget(self._status_lbl)

        # Refresh
        btn_refresh = QPushButton(_("\u27f3 Refresh"), hdr)
        btn_refresh.setCursor(Qt.PointingHandCursor)
        btn_refresh.setStyleSheet(f"""
            QPushButton {{
                background-color: {BG3}; color: {FG_DIM};
                font-family: Consolas; font-size: 8pt;
                border: none; padding: 3px 6px;
            }}
            QPushButton:hover {{ background-color: {BORDER}; color: {FG}; }}
        """)
        btn_refresh.clicked.connect(self._do_refresh)
        hdr_lay.addWidget(btn_refresh)

        layout.addWidget(hdr)

        _log.debug("  Creating header OK")
        # Three-panel splitter
        self._splitter = QSplitter(Qt.Horizontal, self)
        self._splitter.setHandleWidth(1)

        # Left panel
        _log.debug("  Creating left panel...")
        left_scroll = self._make_scroll_area()
        self._left_content = QWidget()
        self._left_layout = QVBoxLayout(self._left_content)
        self._left_layout.setContentsMargins(0, 0, 0, 0)
        self._left_layout.setSpacing(0)
        self._left_layout.addStretch(1)
        left_scroll.setWidget(self._left_content)
        self._splitter.addWidget(left_scroll)

        _log.debug("  Left panel OK")
        # Center panel with tab widget
        center_widget = QWidget()
        center_lay = QVBoxLayout(center_widget)
        center_lay.setContentsMargins(0, 0, 0, 0)
        center_lay.setSpacing(0)

        self._center_tabs = QTabWidget(center_widget)
        self._center_tabs.setStyleSheet(f"""
            QTabBar::tab {{
                background-color: {BG2}; color: {FG_DIM};
                border: none; border-bottom: 2px solid transparent;
                padding: 6px 12px;
                font-family: Consolas; font-size: 9pt; font-weight: bold;
            }}
            QTabBar::tab:hover {{
                color: {FG}; background-color: {BG3};
            }}
            QTabBar::tab:selected {{
                color: {ACCENT}; border-bottom-color: {ACCENT};
                background-color: {BG};
            }}
            QTabWidget::pane {{
                background-color: {BG}; border: none;
            }}
        """)

        # Tab 0: Defenses / Systems
        tab0_scroll = self._make_scroll_area()
        self._center_tab0_content = QWidget()
        self._center_tab0_layout = QVBoxLayout(self._center_tab0_content)
        self._center_tab0_layout.setContentsMargins(0, 0, 0, 0)
        self._center_tab0_layout.setSpacing(0)
        self._center_tab0_layout.addStretch(1)
        tab0_scroll.setWidget(self._center_tab0_content)
        self._center_tabs.addTab(tab0_scroll, "\u2299  " + _("Defenses / Systems"))

        # Tab 1: Power & Propulsion
        tab1_scroll = self._make_scroll_area()
        self._center_tab1_content = QWidget()
        self._center_tab1_layout = QVBoxLayout(self._center_tab1_content)
        self._center_tab1_layout.setContentsMargins(0, 0, 0, 0)
        self._center_tab1_layout.setSpacing(0)
        self._center_tab1_layout.addStretch(1)
        tab1_scroll.setWidget(self._center_tab1_content)
        self._center_tabs.addTab(tab1_scroll, "\u2699  " + _("Power & Propulsion"))

        center_lay.addWidget(self._center_tabs)
        self._splitter.addWidget(center_widget)

        _log.debug("  Center panel + tabs OK")
        # Right panel
        right_scroll = self._make_scroll_area()
        self._right_content = QWidget()
        self._right_layout = QVBoxLayout(self._right_content)
        self._right_layout.setContentsMargins(0, 0, 0, 0)
        self._right_layout.setSpacing(0)
        self._right_layout.addStretch(1)
        right_scroll.setWidget(self._right_content)
        self._splitter.addWidget(right_scroll)

        self._splitter.setSizes([420, 360, 380])
        layout.addWidget(self._splitter, 1)

        _log.debug("  Right panel OK")
        # Placeholder
        self._build_right_panel_placeholder()
        _log.debug("  Right panel placeholder OK")

        # Footer
        self._build_footer(layout)
        _log.debug("_build_ui DONE")

    def _make_scroll_area(self) -> QScrollArea:
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        sa.setStyleSheet(f"QScrollArea {{ background-color: {BG}; border: none; }}")
        return sa

    def _build_footer(self, parent_layout) -> None:
        footer = QWidget(self)
        footer.setFixedHeight(36)
        footer.setStyleSheet(f"background-color: {HEADER_BG};")
        foot_lay = QHBoxLayout(footer)
        foot_lay.setContentsMargins(10, 0, 10, 0)
        foot_lay.setSpacing(4)

        self._footer_labels: dict[str, QLabel] = {}
        for key, label_text, color in [
            ("dps_raw", _("DPS:"),       GREEN),
            ("dps_sus", _("Sustained:"), YELLOW),
            ("alpha",   _("Alpha:"),     ACCENT),
            ("shld_hp", _("Shield:"),    DIST_COL),
            ("hull_hp", _("Hull:"),      PHYS_COL),
            ("cooling", _("Cooling:"),   CYAN),
        ]:
            lbl = QLabel(label_text, footer)
            lbl.setStyleSheet(f"color: {FG_DIM}; font-family: Consolas; font-size: 8pt; background: transparent;")
            foot_lay.addWidget(lbl)
            val_lbl = QLabel("\u2014", footer)
            val_lbl.setStyleSheet(
                f"color: {color}; font-family: Consolas; font-size: 9pt; "
                f"font-weight: bold; background: transparent;"
            )
            foot_lay.addWidget(val_lbl)
            foot_lay.addSpacing(8)
            self._footer_labels[key] = val_lbl

        foot_lay.addStretch(1)
        parent_layout.addWidget(footer)

    # -- Right panel placeholder -----------------------------------------------

    def _build_right_panel_placeholder(self) -> None:
        self._clear_layout(self._right_layout)
        lbl = QLabel(_("Select a ship to view stats."), self._right_content)
        lbl.setStyleSheet(
            f"color: {FG_DIM}; font-family: Consolas; font-size: 10pt; "
            f"background: transparent; padding: 20px 10px;"
        )
        self._right_layout.insertWidget(0, lbl)

    # -- Right panel builder ---------------------------------------------------

    def _build_right_panel(self, ship: dict) -> None:
        self._clear_layout(self._right_layout)
        self._ov_vars = {}
        self._sig_vars = {}
        container = self._right_content

        def section(title, color=ACCENT):
            sh = QWidget(container)
            sh.setStyleSheet(f"background-color: {SECT_HDR_BG};")
            sh_lay = QHBoxLayout(sh)
            sh_lay.setContentsMargins(8, 4, 8, 4)
            lbl = QLabel(f"\u25a0 {title}", sh)
            lbl.setStyleSheet(
                f"color: {color}; font-family: Consolas; font-size: 9pt; "
                f"font-weight: bold; background: transparent;"
            )
            sh_lay.addWidget(lbl)
            sh_lay.addStretch(1)
            self._right_layout.insertWidget(self._right_layout.count() - 1, sh)

        def stat_row(label, key, color=FG, font_size=9, bold=False):
            fr = QWidget(container)
            fr_lay = QHBoxLayout(fr)
            fr_lay.setContentsMargins(8, 1, 8, 1)
            fr_lay.setSpacing(4)
            lbl = QLabel(label, fr)
            lbl.setFixedWidth(140)
            lbl.setStyleSheet(
                f"color: {FG_DIM}; font-family: Consolas; font-size: 8pt; background: transparent;"
            )
            fr_lay.addWidget(lbl)
            val_lbl = QLabel("\u2014", fr)
            weight = "bold" if bold else "normal"
            val_lbl.setStyleSheet(
                f"color: {color}; font-family: Consolas; font-size: {font_size}pt; "
                f"font-weight: {weight}; background: transparent;"
            )
            fr_lay.addWidget(val_lbl)
            fr_lay.addStretch(1)
            self._ov_vars[key] = val_lbl
            self._right_layout.insertWidget(self._right_layout.count() - 1, fr)

        def big_stat(label, key, color, size=16):
            fr = QWidget(container)
            fr_lay = QHBoxLayout(fr)
            fr_lay.setContentsMargins(8, 2, 8, 2)
            fr_lay.setSpacing(4)
            val_lbl = QLabel("\u2014", fr)
            val_lbl.setStyleSheet(
                f"color: {color}; font-family: Consolas; font-size: {size}pt; "
                f"font-weight: bold; background: transparent;"
            )
            fr_lay.addWidget(val_lbl)
            desc_lbl = QLabel(label, fr)
            desc_lbl.setStyleSheet(
                f"color: {FG_DIM}; font-family: Consolas; font-size: 9pt; background: transparent;"
            )
            fr_lay.addWidget(desc_lbl)
            fr_lay.addStretch(1)
            self._ov_vars[key] = val_lbl
            self._right_layout.insertWidget(self._right_layout.count() - 1, fr)

        # -- Signature bar (IR / EM / CS) --
        sig_bar = QWidget(container)
        sig_bar.setFixedHeight(32)
        sig_bar.setStyleSheet(f"background-color: {HEADER_BG};")
        sig_lay = QHBoxLayout(sig_bar)
        sig_lay.setContentsMargins(8, 0, 8, 0)
        sig_lay.setSpacing(4)
        sig_lay.addStretch(1)

        for sig_key, icon_text, icon_color, label in [
            ("ir",  "\u2af6",  THERM_COL,  "IR"),
            ("em",  "\u26a1", YELLOW,     "EM"),
            ("cs",  "\u25c6",  ORANGE,     "CS"),
        ]:
            ic = QLabel(icon_text, sig_bar)
            ic.setStyleSheet(
                f"color: {icon_color}; font-family: Consolas; font-size: 11pt; "
                f"font-weight: bold; background: transparent;"
            )
            sig_lay.addWidget(ic)
            val = QLabel("\u2014", sig_bar)
            val.setStyleSheet(
                f"color: {FG}; font-family: Consolas; font-size: 11pt; "
                f"font-weight: bold; background: transparent;"
            )
            sig_lay.addWidget(val)
            self._sig_vars[sig_key] = val
            if sig_key != "cs":
                sep = QFrame(sig_bar)
                sep.setFrameShape(QFrame.VLine)
                sep.setStyleSheet(f"color: {BORDER}; background: transparent;")
                sep.setFixedWidth(1)
                sep.setFixedHeight(18)
                sig_lay.addWidget(sep)

        sig_lay.addStretch(1)
        self._right_layout.insertWidget(self._right_layout.count() - 1, sig_bar)

        # Ship name header
        name = ship.get("name", "?")
        name_lbl = QLabel(name, container)
        name_lbl.setStyleSheet(
            f"color: {FG}; font-family: Consolas; font-size: 14pt; "
            f"font-weight: bold; padding: 6px 8px; background: transparent;"
        )
        self._right_layout.insertWidget(self._right_layout.count() - 1, name_lbl)

        # PowerAllocator widget
        def _item_lookup(local_name):
            return self._data.lookup_by_local_name(local_name)

        def _raw_lookup(identifier):
            return self._data.raw_lookup(identifier)

        def _on_power_change():
            pa = getattr(self, "_power_allocator", None)
            if pa:
                self._weapon_power_ratio = getattr(pa, "weapon_power_ratio", 1.0)
                self._shield_power_ratio = getattr(pa, "shield_power_ratio", 1.0)
            self._update_footer()

        self._power_allocator = PowerAllocator(
            container, item_lookup_fn=_item_lookup, raw_lookup_fn=_raw_lookup,
            on_change=_on_power_change)
        self._right_layout.insertWidget(self._right_layout.count() - 1, self._power_allocator)

        # Sections
        section(_("WEAPON DPS"), GREEN)
        big_stat(_("dps"), "dps_raw", GREEN, 16)
        big_stat(_("sustained"), "dps_sus", YELLOW, 12)
        big_stat(_("alpha"), "alpha", ACCENT, 12)
        big_stat(_("missile dmg"), "missile_dmg", RED, 12)
        stat_row(_("Weapon slots:"), "gun_slots")
        stat_row(_("Missile racks:"), "miss_slots")

        section(_("SHIELDS"), DIST_COL)
        big_stat(_("hp"), "shld_hp", DIST_COL, 14)
        stat_row(_("Regen/s:"), "shld_regen", GREEN)
        stat_row(_("Phys resist:"), "shld_phys", PHYS_COL)
        stat_row(_("Energy resist:"), "shld_enrg", ENERGY_COL)
        stat_row(_("Dist resist:"), "shld_dist", DIST_COL)

        section(_("HULL"), PHYS_COL)
        big_stat(_("hp"), "hull_hp", PHYS_COL, 14)
        stat_row(_("Armor type:"), "armor_type")
        stat_row(_("Phys dmg:"), "armor_phys", PHYS_COL)
        stat_row(_("Energy dmg:"), "armor_enrg", ENERGY_COL)
        stat_row(_("Dist dmg:"), "armor_dist", DIST_COL)

        section(_("SHIP SPECS"), FG)
        stat_row(_("Cargo (SCU):"), "cargo")
        stat_row(_("Crew:"), "crew")
        stat_row(_("SCM speed:"), "scm_speed", GREEN)
        stat_row(_("AB speed:"), "ab_speed", YELLOW)
        stat_row(_("QT speed:"), "qt_speed")
        stat_row(_("H2 fuel:"), "h2_fuel")
        stat_row(_("QT fuel:"), "qt_fuel")

        section(_("POWER"), ORANGE)
        stat_row(_("Power output:"), "pwr_output", ORANGE)
        stat_row(_("Power draw:"), "pwr_draw", ENERGY_COL)
        stat_row(_("Power margin:"), "pwr_margin", GREEN)

        section(_("COOLING"), CYAN)
        stat_row(_("Total cooling:"), "cooling", CYAN)

        section(_("SIGNATURES"), YELLOW)
        stat_row(_("EM signature:"), "sig_em", YELLOW)
        stat_row(_("IR signature:"), "sig_ir", THERM_COL)
        stat_row(_("CS signature:"), "sig_cs", ORANGE)

    # -- Section header helper -------------------------------------------------

    def _section_header(self, parent_layout, title, type_color=ACCENT, reset_fn=None) -> None:
        sh = QWidget()
        sh.setFixedHeight(28)
        sh.setStyleSheet(f"background-color: {SECT_HDR_BG};")
        sh_lay = QHBoxLayout(sh)
        sh_lay.setContentsMargins(8, 0, 8, 0)
        sh_lay.setSpacing(4)
        lbl = QLabel(f"\u25a0 {title}", sh)
        lbl.setStyleSheet(
            f"color: {type_color}; font-family: Consolas; font-size: 9pt; "
            f"font-weight: bold; background: transparent;"
        )
        sh_lay.addWidget(lbl)
        sh_lay.addStretch(1)
        if reset_fn:
            btn = QPushButton(_("RESET"), sh)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setStyleSheet(f"""
                QPushButton {{
                    color: {ACCENT}; background-color: {SECT_HDR_BG};
                    font-family: Consolas; font-size: 7pt; border: none; padding: 0 6px;
                }}
                QPushButton:hover {{ color: {FG}; }}
            """)
            btn.clicked.connect(reset_fn)
            sh_lay.addWidget(btn)
        # Insert before the stretch at the end of the parent layout
        parent_layout.insertWidget(parent_layout.count() - 1, sh)

    # -- Table-based section builders ------------------------------------------

    def _build_table_slot(self, parent_layout, section_key, slot, list_fn, find_fn,
                          table_cols, type_color) -> ComponentTable:
        sid = slot["id"]
        max_sz = slot["max_size"] or 1

        # Slot header
        sh = QWidget()
        sh_lay = QHBoxLayout(sh)
        sh_lay.setContentsMargins(4, 4, 4, 0)
        sh_lay.setSpacing(6)
        sz_bg = SIZE_COLORS.get(max_sz, SIZE_COLORS[1])
        sz_lbl = QLabel(f"S{max_sz}", sh)
        sz_lbl.setFixedWidth(24)
        sz_lbl.setAlignment(Qt.AlignCenter)
        sz_lbl.setStyleSheet(
            f"background-color: {sz_bg}; color: white; "
            f"font-family: Consolas; font-size: 8pt; font-weight: bold;"
        )
        sh_lay.addWidget(sz_lbl)
        slot_lbl = QLabel(slot["label"], sh)
        slot_lbl.setStyleSheet(
            f"color: {FG_DIM}; font-family: Consolas; font-size: 8pt; background: transparent;"
        )
        sh_lay.addWidget(slot_lbl)
        sh_lay.addStretch(1)
        parent_layout.insertWidget(parent_layout.count() - 1, sh)

        items = list_fn(max_sz)

        def _on_sel(item, _sid=sid, _key=section_key):
            name = item["name"] if item else ""
            self._sel[_key][_sid] = name
            self._update_footer()

        stock_ref = ""
        if slot.get("local_ref"):
            st = find_fn(slot["local_ref"], max_size=max_sz)
            if not st:
                st = find_fn(slot["local_ref"])
            if st:
                stock_ref = st.get("ref", "")
                self._sel[section_key][sid] = st["name"]
                if not any(i.get("ref") == stock_ref for i in items):
                    items = [st] + items

        # Create a parent widget for the ComponentTable
        tbl_container = QWidget()
        tbl_lay = QVBoxLayout(tbl_container)
        tbl_lay.setContentsMargins(0, 0, 0, 0)

        tbl = ComponentTable(tbl_container, table_cols, items, _on_sel,
                             current_ref=stock_ref, type_color=type_color,
                             max_rows=6)
        tbl_lay.addWidget(tbl)
        parent_layout.insertWidget(parent_layout.count() - 1, tbl_container)

        self._slot_tables.setdefault(section_key, []).append((slot, tbl, find_fn))
        return tbl

    # -- Data loaded -----------------------------------------------------------

    def _on_load_stage(self, name: str, num: int, total: int) -> None:
        self._status_lbl.setText(f"Loading\u2026 {name} ({num}/{total})")

    def _on_data_loaded(self) -> None:
        _log.info("_on_data_loaded called (error=%s, loaded=%s)",
                  self._data.error, self._data.loaded)
        try:
            if self._data.error:
                _log.error("Data load error: %s", self._data.error)
                self._status_lbl.setText(f"Error: {self._data.error}")
                return
            names = self._data.get_ship_names()
            _log.info("  %d ships loaded", len(names))
            self._ship_combo.set_items(names)

            nw = len(self._data.weapons_by_name)
            ns = len(self._data.shields_by_name)
            nc = len(self._data.coolers_by_name)
            nm = len(self._data.missiles_by_name)
            _log.info("  %d weapons, %d shields, %d coolers, %d missiles", nw, ns, nc, nm)
            self._status_lbl.setText(
                f"Ready \u2014 {len(names)} ships \u00b7 {nw} weapons \u00b7 {ns} shields "
                f"\u00b7 {nc} coolers \u00b7 {nm} missiles | erkul.games"
            )

            if self._pending_ship:
                _log.info("  Loading pending ship: %s", self._pending_ship)
                self._ship_combo.set_text(self._pending_ship)
                self._load_ship(self._pending_ship)
                self._pending_ship = None

            self._start_version_check()
            _log.info("_on_data_loaded DONE")
        except Exception as exc:
            _log.critical("_on_data_loaded CRASHED", exc_info=True)
            self._status_lbl.setText(f"Load error: {exc}")

    def _on_ship_selected(self, name: str) -> None:
        self._load_ship(name)

    # -- Ship loading ----------------------------------------------------------

    def _load_ship(self, name: str) -> None:
        _log.info("_load_ship('%s')", name)
        ship = self._data.get_ship_data(name)
        if not ship:
            _log.warning("  Ship '%s' not found in data", name)
            return
        self._ship_name = ship.get("name", name)
        self._ship_combo.set_text(self._ship_name)
        self._sel = {k: {} for k in self._sel}

        loadout = ship.get("loadout") or ship.get("_fetched_loadout")

        if loadout:
            self._apply_ship_loadout(ship, loadout)
        else:
            ref = ship.get("ref", "")
            if ref:
                self._status_lbl.setText(f"Loading {self._ship_name} loadout\u2026")
                def _fetch_loadout(s=ship, r=ref):
                    result = []
                    try:
                        resp = requests.get(
                            f"{API_BASE}/live/ships/{r}/loadout",
                            headers=API_HEADERS, timeout=ERKUL_LOADOUT_TIMEOUT,
                        )
                        try:
                            if resp.ok:
                                result = resp.json()
                                s["_fetched_loadout"] = result
                        finally:
                            resp.close()
                    except (requests.RequestException, ValueError):
                        pass
                    self._cb.call_on_main(self._apply_ship_loadout, s, result)
                threading.Thread(target=_fetch_loadout, daemon=True).start()
            else:
                self._apply_ship_loadout(ship, [])

    def _apply_ship_loadout(self, ship: dict, loadout: list) -> None:
        ship_name = ship.get("name", "?")
        _log.info("_apply_ship_loadout('%s', %d loadout items)", ship_name, len(loadout))
        try:
            self._apply_ship_loadout_inner(ship, loadout)
            _log.info("_apply_ship_loadout('%s') OK", ship_name)
        except Exception as exc:
            _log.critical("_apply_ship_loadout('%s') CRASHED", ship_name, exc_info=True)
            self._status_lbl.setText(f"Ship load error: {exc}")

    def _apply_ship_loadout_inner(self, ship: dict, loadout: list) -> None:
        self._ship_data = ship
        self._current_ship = ship
        self._slot_tables = {}

        # -- LEFT PANEL: weapons + missiles --
        self._clear_layout(self._left_layout)
        self._rows["weapons"] = []
        self._rows["missiles"] = []

        all_weapon_slots = extract_slots_by_type(loadout, {"WeaponGun", "Turret"})
        for s in all_weapon_slots:
            lr = s.get("local_ref", "")
            if lr:
                found = self._data.find_weapon(lr, max_size=s["max_size"])
                if not found:
                    found = self._data.find_weapon(lr)
                if not found:
                    s["local_ref"] = ""
        gun_slots = [s for s in all_weapon_slots if " / " not in s["label"]]
        turret_slots = [s for s in all_weapon_slots if " / " in s["label"]]
        self._rebuild_weapons_section(self._left_layout, gun_slots, turret_slots)

        missile_slots_raw = extract_slots_by_type(loadout, {"MissileLauncher"})
        gun_ids = {s["id"] for s in gun_slots + turret_slots}
        missile_slots = [s for s in missile_slots_raw if s["id"] not in gun_ids]
        for s in missile_slots:
            lr = s.get("local_ref", "")
            if lr:
                found = self._data.find_missile(lr, max_size=s["max_size"])
                if not found:
                    found = self._data.find_missile(lr)
                if not found:
                    s["local_ref"] = ""
        self._rebuild_missiles_section(self._left_layout, missile_slots)

        # -- CENTER TAB 0: Defenses / Systems --
        self._clear_layout(self._center_tab0_layout)
        self._rows["defenses"] = []
        self._rows["components"] = []
        self._cooler_rows = []
        self._radar_rows = []

        shield_slots = extract_slots_by_type(loadout, {"Shield"})
        self._rebuild_shields_section(self._center_tab0_layout, shield_slots)

        cooler_slots = extract_slots_by_type(loadout, {"Cooler"})
        self._rebuild_coolers_section(self._center_tab0_layout, cooler_slots)

        radar_slots = extract_slots_by_type(loadout, {"Radar"})
        self._rebuild_radars_section(self._center_tab0_layout, radar_slots)

        # -- CENTER TAB 1: Power & Propulsion --
        self._clear_layout(self._center_tab1_layout)
        self._powerplant_rows = []
        self._qdrive_rows = []
        self._rows["propulsion"] = []

        pp_slots = extract_slots_by_type(loadout, {"PowerPlant"})
        if pp_slots:
            self._section_header(self._center_tab1_layout, _("POWER PLANTS"), ORANGE,
                                 reset_fn=lambda: self._reset_section("powerplants"))
            for i, slot in enumerate(pp_slots):
                slot["id"] = f"pp_{i}"
                tbl = self._build_table_slot(
                    self._center_tab1_layout, "components", slot,
                    self._data.powerplants_for_size, self._data.find_powerplant,
                    PP_COLS, TYPE_STRIPE["PowerPlant"])
                self._slot_tables.setdefault("powerplants", []).append(
                    (slot, tbl, self._data.find_powerplant))

        qd_slots = extract_slots_by_type(loadout, {"QuantumDrive"})
        if qd_slots:
            self._section_header(self._center_tab1_layout, _("QUANTUM DRIVES"), ACCENT)
            for slot in qd_slots:
                self._build_table_slot(
                    self._center_tab1_layout, "propulsion", slot,
                    self._data.qdrives_for_size, self._data.find_qdrive,
                    QD_COLS, TYPE_STRIPE["QuantumDrive"])

        # Thruster placeholder
        self._thruster_container = QWidget()
        self._thruster_layout = QVBoxLayout(self._thruster_container)
        self._thruster_layout.setContentsMargins(0, 0, 0, 0)
        self._thruster_layout.setSpacing(0)
        self._center_tab1_layout.insertWidget(
            self._center_tab1_layout.count() - 1, self._thruster_container)

        # Switch to tab 0
        self._center_tabs.setCurrentIndex(0)

        # -- RIGHT PANEL --
        self._build_right_panel(ship)
        self._update_overview(ship)
        if hasattr(self, "_power_allocator"):
            self._power_allocator.load_ship(ship)
        self._compute_power_stats(ship)
        self._update_footer()

        ship_name = ship.get("name", "?")
        self._status_lbl.setText(f"Loaded: {ship_name} \u2014 fetching Fleetyards\u2026")
        self._fy_groups = {}

        def _fy_done(groups: dict):
            self._fy_groups = groups
            self._rebuild_thrusters_section(groups)
            self._status_lbl.setText(f"Loaded: {ship_name}")

        self._data.fetch_fy_hardpoints(
            ship_name,
            on_done=lambda g: self._cb.call_on_main(_fy_done, g),
        )

    # -- Section builders ------------------------------------------------------

    def _rebuild_weapons_section(self, parent_layout, gun_slots, turret_slots) -> None:
        key = "weapons"
        all_slots = gun_slots + turret_slots
        if not all_slots:
            lbl = QLabel("  " + _("No weapon slots."))
            lbl.setStyleSheet(
                f"color: {FG_DIM}; font-family: Consolas; font-size: 9pt; "
                f"background: transparent; padding: 8px;"
            )
            parent_layout.insertWidget(parent_layout.count() - 1, lbl)
            return

        if gun_slots:
            self._section_header(parent_layout, _("WEAPONS"), ENERGY_COL)
            for slot in gun_slots:
                self._build_table_slot(
                    parent_layout, key, slot,
                    self._data.weapons_for_size, self._data.find_weapon,
                    WEAPON_TABLE_COLS, TYPE_STRIPE["WeaponGun"])

        if turret_slots:
            self._section_header(parent_layout, _("TURRETS"), ENERGY_COL)
            for slot in turret_slots:
                self._build_table_slot(
                    parent_layout, key, slot,
                    self._data.weapons_for_size, self._data.find_weapon,
                    WEAPON_TABLE_COLS, TYPE_STRIPE["WeaponGun"])

    def _rebuild_missiles_section(self, parent_layout, slots) -> None:
        key = "missiles"
        if not slots:
            return
        self._section_header(parent_layout, _("MISSILE & BOMB RACKS"), RED)
        for slot in slots:
            self._build_table_slot(
                parent_layout, key, slot,
                self._data.missiles_for_size, self._data.find_missile,
                MISSILE_TABLE_COLS, TYPE_STRIPE["MissileLauncher"])

    def _rebuild_shields_section(self, parent_layout, slots) -> None:
        if not slots:
            return
        self._section_header(parent_layout, _("SHIELDS"), DIST_COL)
        for slot in slots:
            self._build_table_slot(
                parent_layout, "defenses", slot,
                self._data.shields_for_size, self._data.find_shield,
                SHIELD_TABLE_COLS, TYPE_STRIPE["Shield"])

    def _rebuild_coolers_section(self, parent_layout, slots) -> None:
        if not slots:
            return
        self._section_header(parent_layout, _("COOLERS"), CYAN,
                             reset_fn=lambda: self._reset_section("coolers"))
        for slot in slots:
            self._build_table_slot(
                parent_layout, "components", slot,
                self._data.coolers_for_size, self._data.find_cooler,
                COOLER_TABLE_COLS, TYPE_STRIPE["Cooler"])

    def _rebuild_radars_section(self, parent_layout, slots) -> None:
        if not slots:
            return
        self._section_header(parent_layout, _("RADARS"), FG_DIM,
                             reset_fn=lambda: self._reset_section("radars"))
        for slot in slots:
            self._build_table_slot(
                parent_layout, "components", slot,
                self._data.radars_for_size, self._data.find_radar,
                RADAR_TABLE_COLS, TYPE_STRIPE["Radar"])

    def _rebuild_thrusters_section(self, groups: dict) -> None:
        container = getattr(self, "_thruster_container", None)
        if not container:
            return
        # Clear
        layout = self._thruster_layout
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        self._thruster_rows = []

        for grp_key, title in [("main_thrusters", _("MAIN THRUSTERS")),
                                ("retro_thrusters", _("RETRO THRUSTERS")),
                                ("maneuvering_thrusters", _("MANEUVERING"))]:
            items = groups.get(grp_key, [])
            if not items:
                continue
            # Section header
            sh = QWidget()
            sh.setFixedHeight(28)
            sh.setStyleSheet(f"background-color: {SECT_HDR_BG};")
            sh_lay = QHBoxLayout(sh)
            sh_lay.setContentsMargins(8, 0, 8, 0)
            lbl = QLabel(f"\u25a0 {title}", sh)
            lbl.setStyleSheet(
                f"color: {YELLOW}; font-family: Consolas; font-size: 9pt; "
                f"font-weight: bold; background: transparent;"
            )
            sh_lay.addWidget(lbl)
            sh_lay.addStretch(1)
            layout.addWidget(sh)

            for i, hp in enumerate(items):
                st = compute_thruster_stats(hp)
                lbl_name = re.sub(r"hardpoint_", "", hp.get("name", f"T {i+1}"), flags=re.I)
                lbl_name = lbl_name.replace("_", " ").title()
                card = self._make_thruster_card(st, lbl_name)
                layout.addWidget(card)
                self._thruster_rows.append(st)

    def _make_thruster_card(self, st: dict, slot_label: str) -> QWidget:
        card = QWidget()
        card_lay = QHBoxLayout(card)
        card_lay.setContentsMargins(4, 2, 4, 2)
        card_lay.setSpacing(6)

        stripe = QWidget(card)
        stripe.setFixedWidth(3)
        stripe.setStyleSheet(f"background-color: {TYPE_STRIPE['Thruster']};")
        card_lay.addWidget(stripe)

        sz = st.get("size", 1)
        sz_bg = SIZE_COLORS.get(sz, SIZE_COLORS[1])
        sz_lbl = QLabel(f"S{sz}", card)
        sz_lbl.setFixedWidth(24)
        sz_lbl.setAlignment(Qt.AlignCenter)
        sz_lbl.setStyleSheet(
            f"background-color: {sz_bg}; color: white; "
            f"font-family: Consolas; font-size: 8pt; font-weight: bold;"
        )
        card_lay.addWidget(sz_lbl)

        info = QWidget(card)
        info_lay = QVBoxLayout(info)
        info_lay.setContentsMargins(0, 0, 0, 0)
        info_lay.setSpacing(0)
        l1 = QLabel(slot_label, info)
        l1.setStyleSheet(f"color: {FG_DIM}; font-family: Consolas; font-size: 8pt; background: transparent;")
        info_lay.addWidget(l1)
        l2_lay = QHBoxLayout()
        l2_lay.setSpacing(6)
        n = QLabel(st.get("name", ""), info)
        n.setStyleSheet(f"color: {FG}; font-family: Consolas; font-size: 8pt; background: transparent;")
        l2_lay.addWidget(n)
        m = QLabel(st.get("mfr", ""), info)
        m.setStyleSheet(f"color: {FG_DIM}; font-family: Consolas; font-size: 7pt; background: transparent;")
        l2_lay.addWidget(m)
        l2_lay.addStretch(1)
        info_lay.addLayout(l2_lay)
        card_lay.addWidget(info, 1)

        return card

    # -- Overview update -------------------------------------------------------

    def _update_overview(self, ship: dict) -> None:
        v = self._ov_vars
        hull = ship.get("hull", {})
        hull_hp = hull.get("totalHp", 0) if isinstance(hull, dict) else 0

        armor = ship.get("armor", {})
        if isinstance(armor, dict):
            armor_d = armor.get("data", armor)
        else:
            armor_d = {}
        a_health = armor_d.get("health", {}) or {}
        hull_hp = hull_hp or a_health.get("hp", 0)
        a_resist = a_health.get("damageResistanceMultiplier", {}) or {}
        a_phys = (1 - a_resist.get("physical", 1)) * 100
        a_enrg = (1 - a_resist.get("energy", 1)) * 100
        a_dist = (1 - a_resist.get("distortion", 1)) * 100
        a_type = armor_d.get("subType", "?")

        ifcs = ship.get("ifcs", {}) or {}
        scm = ifcs.get("scmSpeed", 0)
        ab = ifcs.get("maxAfterburnSpeed", 0)
        if isinstance(scm, dict): scm = 0
        if isinstance(ab, dict): ab = 0

        qt = ship.get("qtFuelCapacity", 0) or 0
        h2 = ship.get("fuelCapacity", 0) or 0
        cargo = ship.get("cargo", 0)
        if isinstance(cargo, (int, float)):
            cargo_scu = int(cargo)
        elif isinstance(cargo, dict):
            cargo_scu = int(cargo.get("capacity", 0) or 0)
        else:
            cargo_scu = 0

        vehicle = ship.get("vehicle", {}) or {}
        crew = vehicle.get("crewSize", "?")

        def _set(key, text):
            lbl = v.get(key)
            if lbl:
                lbl.setText(str(text))

        _set("hull_hp", f"{hull_hp:,.0f}")
        _set("armor_type", a_type)
        _set("armor_phys", f"{a_phys:+.0f}%")
        _set("armor_enrg", f"{a_enrg:+.0f}%")
        _set("armor_dist", f"{a_dist:+.0f}%")
        _set("cargo", str(cargo_scu))
        _set("crew", str(crew))
        _set("scm_speed", f"{scm:,.0f}" if scm else "?")
        _set("ab_speed", f"{ab:,.0f}" if ab else "?")
        _set("qt_speed", "?")
        _set("h2_fuel", f"{h2:,.0f}" if h2 else "?")
        _set("qt_fuel", f"{qt:,.0f}" if qt else "?")

        self._footer_labels["hull_hp"].setText(f"{hull_hp:,.0f}" if hull_hp else "\u2014")

        cs_raw = ship.get("crossSection", 0)
        if isinstance(cs_raw, dict):
            cs_x = float(cs_raw.get("x", 0) or 0)
            cs_y = float(cs_raw.get("y", 0) or 0)
            cs_z = float(cs_raw.get("z", 0) or 0)
            cs_sig = max(cs_x, cs_y, cs_z)
        elif isinstance(cs_raw, (int, float)):
            cs_sig = float(cs_raw)
        else:
            cs_sig = 0

        if "cs" in self._sig_vars:
            self._sig_vars["cs"].setText(fmt_sig(cs_sig))
        _set("sig_cs", fmt_sig(cs_sig))

    # -- Footer / totals -------------------------------------------------------

    def _update_footer(self) -> None:
        totals = compute_footer_totals(
            self._sel,
            find_weapon=self._data.find_weapon,
            find_missile=self._data.find_missile,
            find_shield=self._data.find_shield,
            find_cooler=self._data.find_cooler,
            find_radar=self._data.find_radar,
            find_powerplant=self._data.find_powerplant,
            power_sim=self._power_sim,
            weapon_power_ratio=self._weapon_power_ratio,
            shield_power_ratio=self._shield_power_ratio,
        )

        tot_raw = totals["dps_raw"]
        tot_sus = totals["dps_sus"]
        tot_alp = totals["alpha"]
        miss_dmg = totals["missile_dmg"]
        tot_hp = totals["shield_hp"]
        tot_regen = totals["shield_regen"]
        shld_res = totals["shield_res"]
        shld_count = totals["shield_count"]
        tot_cool = totals["cooling"]
        tot_pwr_out = totals["power_output"]
        tot_pwr_draw = totals["power_draw"]
        n_guns = totals["gun_count"]
        n_miss = totals["missile_count"]

        fl = self._footer_labels
        fl["dps_raw"].setText(f"{tot_raw:,.0f}" if tot_raw else "\u2014")
        fl["dps_sus"].setText(f"{tot_sus:,.0f}" if tot_sus else "\u2014")
        fl["alpha"].setText(f"{tot_alp:,.1f}" if tot_alp else "\u2014")
        fl["shld_hp"].setText(f"{tot_hp:,.0f}" if tot_hp else "\u2014")
        fl["cooling"].setText(f"{tot_cool/1000:,.0f}k" if tot_cool else "\u2014")

        v = self._ov_vars

        def _set(key, text):
            lbl = v.get(key)
            if lbl:
                lbl.setText(str(text))

        _set("dps_raw", f"{tot_raw:,.0f}" if tot_raw else "\u2014")
        _set("dps_sus", f"{tot_sus:,.0f}" if tot_sus else "\u2014")
        _set("alpha", f"{tot_alp:,.1f}" if tot_alp else "\u2014")
        _set("missile_dmg", f"{miss_dmg:,.0f}" if miss_dmg else "\u2014")
        _set("gun_slots", f"{n_guns} " + _("equipped"))
        _set("miss_slots", f"{n_miss} " + _("equipped"))
        _set("shld_hp", f"{tot_hp:,.0f}" if tot_hp else "\u2014")
        _set("shld_regen", f"{tot_regen:.1f}" if tot_regen else "\u2014")
        avg = lambda val: val / shld_count if shld_count else 0
        _set("shld_phys", pct(avg(shld_res["phys"])))
        _set("shld_enrg", pct(avg(shld_res["enrg"])))
        _set("shld_dist", pct(avg(shld_res["dist"])))
        _set("cooling", f"{tot_cool/1000:,.0f}k" if tot_cool else "\u2014")
        _set("pwr_output", f"{tot_pwr_out:,.0f}" if tot_pwr_out else "\u2014")
        _set("pwr_draw", f"{tot_pwr_draw:,.0f}" if tot_pwr_draw else "\u2014")
        margin = tot_pwr_out - tot_pwr_draw
        _set("pwr_margin", f"{margin:+,.0f}" if tot_pwr_out else "\u2014")

        self._update_signatures()

    def _update_signatures(self) -> None:
        em_sig = 0.0
        ir_sig = 0.0
        if hasattr(self, "_power_allocator"):
            pa = self._power_allocator
            em_sig = getattr(pa, "em_signature", 0)
            ir_sig = getattr(pa, "ir_signature", 0)

        if "ir" in self._sig_vars:
            self._sig_vars["ir"].setText(fmt_sig(ir_sig))
        if "em" in self._sig_vars:
            self._sig_vars["em"].setText(fmt_sig(em_sig))

        v = self._ov_vars
        lbl = v.get("sig_em")
        if lbl:
            lbl.setText(fmt_sig(em_sig))
        lbl = v.get("sig_ir")
        if lbl:
            lbl.setText(fmt_sig(ir_sig))

    # -- Power simulation ------------------------------------------------------

    def _compute_power_stats(self, ship: dict) -> None:
        if hasattr(self, "_power_allocator") and self._power_allocator._slots:
            pa = self._power_allocator
            self._weapon_power_ratio = getattr(pa, "weapon_power_ratio", 1.0)
            self._shield_power_ratio = getattr(pa, "shield_power_ratio", 1.0)
        else:
            self._weapon_power_ratio = 1.0
            self._shield_power_ratio = 1.0

    # -- Version check ---------------------------------------------------------

    def _start_version_check(self) -> None:
        def _check():
            version = ""
            for path in ("/live/gameVersion", "/live/version"):
                try:
                    r = requests.get(API_BASE + path, headers=API_HEADERS, timeout=ERKUL_VERSION_TIMEOUT)
                    try:
                        if r.ok:
                            obj = r.json()
                            version = (
                                obj.get("gameVersion") or
                                obj.get("version") or
                                obj.get("live") or ""
                            )
                            if version:
                                break
                    finally:
                        r.close()
                except (requests.RequestException, ValueError):
                    continue
            if not version:
                return
            self._cb.call_on_main(self._show_version_badge, version)
            cached_v = self._data.cached_game_version
            if cached_v == version:
                return
            self._data.cached_game_version = version
            if cached_v:
                self._cb.call_on_main(
                    self._status_lbl.setText,
                    f"Game updated to v{version} \u2014 refreshing data\u2026"
                )
                try:
                    if os.path.isfile(CACHE_FILE):
                        os.remove(CACHE_FILE)
                except OSError:
                    pass
                self._data.invalidate_and_reload(
                    on_done=lambda: self._cb.call_on_main(self._on_data_loaded)
                )
            else:
                self._data.save_cache_with_version(version)
        threading.Thread(target=_check, daemon=True).start()

    def _show_version_badge(self, version: str) -> None:
        self._version_lbl.setText(f"v{version}")

    # -- Reset / Refresh -------------------------------------------------------

    def _reset_section(self, section_name) -> None:
        if self._ship_name:
            self._load_ship(self._ship_name)

    def _show_tutorial(self) -> None:
        from dps_ui.tutorial_popup import TutorialPopup
        popup = TutorialPopup(self)
        popup.show_relative_to(self)

    def _do_refresh(self) -> None:
        self._status_lbl.setText(_("Refreshing from erkul.games\u2026"))
        try:
            if os.path.isfile(CACHE_FILE):
                os.remove(CACHE_FILE)
        except OSError:
            pass
        self._data.invalidate_and_reload(
            on_done=lambda: self._cb.call_on_main(self._on_data_loaded)
        )

    def _reset_all(self) -> None:
        if self._ship_name:
            self._load_ship(self._ship_name)
        else:
            self._sel = {k: {} for k in self._sel}

    # -- Voice helpers (unified for ComponentTable) ----------------------------

    def _voice_set_slot(self, section_key, slot_query, comp_name, find_fn=None) -> None:
        tables = self._slot_tables.get(section_key, [])
        if not tables:
            return
        q = slot_query.lower().strip()

        if q == "all":
            targets = tables
        elif q.isdigit():
            idx = int(q) - 1
            targets = [tables[idx]] if 0 <= idx < len(tables) else []
        else:
            targets = [t for t in tables if q in t[0]["label"].lower()]
            if not targets:
                targets = tables

        for slot_dict, tbl, slot_find_fn in targets:
            fn = find_fn or slot_find_fn
            max_sz = slot_dict.get("max_size")
            stats = fn(comp_name, max_size=max_sz)
            if stats:
                sid = slot_dict["id"]
                sel_key = section_key
                if section_key in ("coolers", "radars", "powerplants"):
                    sel_key = "components"
                elif section_key in ("qdrives",):
                    sel_key = "propulsion"
                self._sel.setdefault(sel_key, {})[sid] = stats["name"]
                tbl.set_selected(stats.get("ref", ""))
        self._update_footer()

    def _set_by_slot(self, tab_key: str, slot_query: str, comp_name: str) -> None:
        find_map = {
            "weapons": self._data.find_weapon,
            "missiles": self._data.find_missile,
            "defenses": self._data.find_shield,
        }
        self._voice_set_slot(tab_key, slot_query, comp_name,
                             find_fn=find_map.get(tab_key))

    def _set_component_slot(self, comp_type: str, slot_query: str, name: str) -> None:
        find_map = {"cooler": self._data.find_cooler, "radar": self._data.find_radar}
        tables = self._slot_tables.get("components", [])
        if not tables:
            return
        q = slot_query.lower().strip()
        fn = find_map.get(comp_type, self._data.find_cooler)

        filtered = [(s, t, sf) for s, t, sf in tables if sf == fn]

        if q == "all":
            targets = filtered
        elif q.isdigit():
            idx = int(q) - 1
            targets = [filtered[idx]] if 0 <= idx < len(filtered) else []
        else:
            targets = [t for t in filtered if q in t[0]["label"].lower()]

        for slot_dict, tbl, slot_fn in targets:
            max_sz = slot_dict.get("max_size")
            stats = fn(name, max_size=max_sz)
            if stats:
                self._sel["components"][slot_dict["id"]] = stats["name"]
                tbl.set_selected(stats.get("ref", ""))
        self._update_footer()

    def _set_powerplant_slot(self, slot_query: str, name: str) -> None:
        self._voice_set_slot("powerplants", slot_query, name,
                             find_fn=self._data.find_powerplant)

    def _set_qdrive_slot(self, slot_query: str, name: str) -> None:
        self._voice_set_slot("qdrives", slot_query, name,
                             find_fn=self._data.find_qdrive)

    # -- IPC dispatch ----------------------------------------------------------

    @Slot(dict)
    def _stop_threads(self) -> None:
        """Stop all background threads/timers.  Safe to call multiple times."""
        if hasattr(self, '_ipc'):
            self._ipc.stop()
        if hasattr(self, '_pending_timer'):
            self._pending_timer.stop()
        if hasattr(self, '_data'):
            self._data.cancel_load()

    def _shutdown(self) -> None:
        """Gracefully stop threads, then quit the application."""
        _log.info("_shutdown: stopping threads before quit")
        self._stop_threads()
        QApplication.instance().quit()

    def closeEvent(self, event) -> None:
        """Stop background threads before Qt destroys child QObjects."""
        _log.info("closeEvent: stopping background threads")
        self._stop_threads()
        super().closeEvent(event)

    def _dispatch(self, cmd: dict) -> None:
        t = cmd.get("type", "")
        if t == "quit":
            self._shutdown()
            return
        elif t == "show":
            self.show()
            self.raise_()
            self.activateWindow()
        elif t == "hide":
            self.hide()
        elif t == "set_ship":
            ship = cmd.get("ship", "")
            if ship:
                if self._data.loaded:
                    self._ship_combo.set_text(ship)
                    self._load_ship(ship)
                else:
                    self._pending_ship = ship
        elif t == "set_weapon":
            slot = str(cmd.get("slot", "1"))
            name = cmd.get("name", "")
            tab = cmd.get("tab", "weapons")
            if name and self._data.loaded:
                self._set_by_slot(tab, slot, name)
        elif t == "set_component":
            comp_type = cmd.get("component_type", "cooler")
            slot = str(cmd.get("slot", "1"))
            name = cmd.get("name", "")
            if name and self._data.loaded:
                self._set_component_slot(comp_type, slot, name)
        elif t == "set_powerplant":
            slot = str(cmd.get("slot", "1"))
            name = cmd.get("name", "")
            if name and self._data.loaded:
                self._set_powerplant_slot(slot, name)
        elif t == "set_quantumdrive":
            slot = str(cmd.get("slot", "1"))
            name = cmd.get("name", "")
            if name and self._data.loaded:
                self._set_qdrive_slot(slot, name)
        elif t == "reset":
            self._reset_all()
        elif t == "refresh":
            self._do_refresh()

    def _check_pending(self) -> None:
        if self._pending_ship and self._data.loaded:
            self._ship_combo.set_text(self._pending_ship)
            self._load_ship(self._pending_ship)
            self._pending_ship = None

    # -- Helpers ---------------------------------------------------------------

    def _clear_layout(self, layout) -> None:
        """Remove all widgets from a layout, keeping the trailing stretch."""
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        layout.addStretch(1)

    def run(self) -> None:
        self.show()
