"""Main application window for Mining Signals."""

from __future__ import annotations

import json
import logging
import os
import sys
import threading

from PySide6.QtCore import Qt, QTimer, Signal, QObject, Slot, QMetaObject, Q_ARG, Qt as QtConst
from PySide6.QtGui import QColor, QBrush, QPalette
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QFrame, QPushButton, QLineEdit, QHeaderView, QStyledItemDelegate,
    QTabWidget, QFileDialog, QDialog, QSpinBox, QCheckBox,
    QScrollArea,
)

from shared.qt.theme import P, apply_theme
from shared.qt.base_window import SCWindow
from shared.qt.title_bar import SCTitleBar
from shared.qt.data_table import SCTable, ColumnDef, SCTableModel
from shared.qt.ipc_thread import IPCWatcher
from shared.platform_utils import set_dpi_awareness
from shared.data_utils import parse_cli_args

from services.sheet_fetcher import SheetFetcher
from services.signal_matcher import SignalMatcher, SignalMatch
from services.loadout_loader import (
    load_loadout_file, describe_snapshot, LoadoutSnapshot,
    snapshot_to_laser_configs, get_gadget_list,
)
from services.salvage_loader import (
    load_salvage_file, describe_salvage_snapshot, SalvageSnapshot,
)
from services.breakability import (
    LaserConfig, GadgetInfo, BreakResult, FleetBreakResult,
    compute_with_gadgets, fleet_breakability, default_player_count,
)
from ocr.screen_reader import is_ocr_available, scan_region, tesseract_status
from ocr.onnx_hud_reader import scan_hud_onnx

from .scan_bubble import ScanBubble
from .break_bubble import BreakBubble
from .region_selector import RegionSelector
from .display_placer import DisplayPlacer
from .tutorial_popup import TutorialPopup
from .resource_popup import ResourcePopup
from .mining_ledger import MiningLedgerTab
from .mining_chart import MiningChartTab
from .refinery_locations_tab import RefineryLocationsTab
from .refinery_yields_tab import RefineryYieldsTab
from .break_panel import BreakPanel
from . import chart_bubble

log = logging.getLogger(__name__)

# Turret name lookup — avoids importing Mining_Loadout models in the UI
_TURRET_NAMES: dict[str, list[str]] = {
    "Prospector": ["Main Turret"],
    "MOLE": ["Front Turret", "Port Turret", "Starboard Turret"],
    "Golem": ["Main Turret"],
}


def _ml_turret_name(ship: str, index: int) -> str:
    names = _TURRET_NAMES.get(ship, [])
    if 0 <= index < len(names):
        return names[index]
    return f"Turret {index + 1}"


ACCENT = "#33dd88"

# Standard close button style matching the main title bar's X button
_CLOSE_BTN_STYLE = """
    QPushButton {
        background: rgba(255, 60, 60, 0.15);
        color: #cc6666;
        border: none;
        border-radius: 3px;
        font-family: Consolas;
        font-size: 13pt;
        font-weight: bold;
        padding: 0px;
    }
    QPushButton:hover {
        background-color: rgba(220, 50, 50, 0.85);
        color: #ffffff;
    }
"""
_CONFIG_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "mining_signals_config.json",
)

# Ship slots available in the Mining Ships tab. Keys are the internal
# ids stored in config; values are display labels.
SHIP_SLOTS: list[tuple[str, str]] = [
    ("golem", "Golem"),
    ("prospector", "Prospector"),
    ("mole", "Mole"),
]

# Rarity tier colours
RARITY_FG: dict[str, str] = {
    "Common": "#8cc63f",
    "Uncommon": "#00bcd4",
    "Rare": "#ffc107",
    "Epic": "#aa66ff",
    "Legendary": "#ff9800",
    "ROC": "#33ccdd",
    "FPS": "#44aaff",
    "Salvage": "#66ccff",
}

# Rarity sort order (ascending) — used by the table's Rarity column sort.
# Unknown rarities get a high value so they sort last.
RARITY_SORT_ORDER: dict[str, int] = {
    "FPS":       1,
    "ROC":       2,
    "Salvage":   3,
    "Uncommon":  4,
    "Common":    5,
    "Rare":      6,
    "Epic":      7,
    "Legendary": 8,
}
# Reverse lookup: sort index -> rarity name (for display formatter)
RARITY_BY_KEY: dict[int, str] = {v: k for k, v in RARITY_SORT_ORDER.items()}


def _rarity_key(rarity: str) -> int:
    """Return the custom sort index for a rarity name."""
    return RARITY_SORT_ORDER.get(rarity, 999)


class _RarityRowDelegate(QStyledItemDelegate):
    """Item delegate that paints each row's text in its rarity color.

    Bypasses QSS text color by painting the cell text directly.
    Preserves alternating row backgrounds and selection highlights.
    """

    def __init__(self, source_model: SCTableModel, parent=None):
        super().__init__(parent)
        self._source_model = source_model
        self._even_bg = QColor(P.bg_primary)
        self._odd_bg = QColor(P.bg_card)
        self._selection_bg = QColor(P.selection)

    def _resolve_row(self, index):
        """Map a possibly-proxied index back to a source row number."""
        model = index.model()
        src_idx = index
        if hasattr(model, "mapToSource"):
            try:
                src_idx = model.mapToSource(index)
            except Exception:
                pass
        return src_idx.row()

    def _row_color(self, index) -> QColor:
        """Return the text color for the given index based on row rarity."""
        row_num = self._resolve_row(index)
        row = self._source_model.row_data(row_num)
        if not row:
            return QColor(P.fg)
        rarity_name = row.get("_rarity_name", "")
        if not rarity_name:
            raw_rarity = row.get("rarity", "")
            if isinstance(raw_rarity, tuple) and len(raw_rarity) >= 2:
                rarity_name = str(raw_rarity[1])
            else:
                rarity_name = str(raw_rarity)
        color_hex = RARITY_FG.get(rarity_name)
        return QColor(color_hex) if color_hex else QColor(P.fg)

    def paint(self, painter, option, index):
        # Draw background (alternating or selection highlight)
        painter.save()
        if option.state & option.state.__class__.State_Selected:
            painter.fillRect(option.rect, self._selection_bg)
        elif index.row() % 2 == 0:
            painter.fillRect(option.rect, self._even_bg)
        else:
            painter.fillRect(option.rect, self._odd_bg)

        # Resolve text + alignment from the model
        text = index.data(Qt.DisplayRole)
        if text is None:
            text = ""
        else:
            text = str(text)

        align = index.data(Qt.TextAlignmentRole)
        if align is None:
            align = int(Qt.AlignLeft | Qt.AlignVCenter)

        # Draw the text in the row's rarity color
        color = self._row_color(index)
        painter.setPen(color)
        # Standard Qt cell padding: 8px horizontal, matches QSS
        rect = option.rect.adjusted(8, 0, -8, 0)
        painter.drawText(rect, int(align), text)
        painter.restore()


def _load_config() -> dict:
    cfg: dict = {
        "refresh_interval_minutes": 60,
        "scan_interval_seconds": 3,
        "ocr_region": None,
        "hud_region": None,
        "ship_loadouts": {k: None for k, _ in SHIP_SLOTS},
        "active_ship": None,
        "gadget_quantities": {},
        "always_use_best_gadget": False,
        "fleet_loadouts": [],
        "fleet_player_counts": {},  # path -> int (override default crew)
        "module_uses_remaining": {},  # ship_id -> [remaining_per_turret]
        "game_dir": r"C:\Star Citizen\StarCitizen\LIVE",
        "refinery_picked_up": [],
        "refinery_deleted": [],
        "refinery_ocr_region": None,
        "refinery_orders": [],
        "refinery_auto_scan": False,
        "calc_mode": "fleet",  # "fleet" | "team"
        "salvage_loadouts": [],  # list of DPS Calculator loadout paths
        "ledger_file": os.path.join(
            os.path.expanduser("~"), "Documents", "SC Loadouts", "mining_roster.json",
        ),
    }
    try:
        if os.path.isfile(_CONFIG_FILE):
            with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                cfg.update(data)
                # Normalize nested ship_loadouts so missing keys exist
                if not isinstance(cfg.get("ship_loadouts"), dict):
                    cfg["ship_loadouts"] = {}
                for k, _ in SHIP_SLOTS:
                    cfg["ship_loadouts"].setdefault(k, None)
                # Migrate ledger_file from in-folder to Documents
                lf = cfg.get("ledger_file", "")
                if lf and os.path.dirname(os.path.normpath(lf)) == os.path.normpath(
                    os.path.dirname(_CONFIG_FILE)
                ):
                    cfg["ledger_file"] = os.path.join(
                        os.path.expanduser("~"), "Documents", "SC Loadouts",
                        "mining_roster.json",
                    )
    except (OSError, json.JSONDecodeError):
        pass
    return cfg


def _save_config(cfg: dict) -> None:
    try:
        tmp = _CONFIG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, _CONFIG_FILE)
    except OSError as exc:
        log.warning("Failed to save config: %s", exc)


class _DataLoader(QObject):
    """Loads sheet data in a background thread."""

    data_ready = Signal(list)   # list[dict] of rows
    error = Signal(str)

    def __init__(self, fetcher: SheetFetcher, parent=None) -> None:
        super().__init__(parent)
        self._fetcher = fetcher

    def load(self, force: bool = False) -> None:
        def _run():
            result = self._fetcher.load(force_refresh=force)
            if result.ok:
                self.data_ready.emit(result.data)
            else:
                self.error.emit(result.error or "Unknown error")
        threading.Thread(target=_run, daemon=True).start()


class MiningSignalsApp(SCWindow):
    """Mining Signals tool — reference table + OCR scanner."""

    _scan_value_ready = Signal(int)   # emitted from bg thread, handled on main thread
    _hud_data_ready = Signal()        # emitted when HUD data updates (break bubble)

    def __init__(
        self,
        x: int = 100, y: int = 100,
        w: int = 980, h: int = 960,
        opacity: float = 0.95,
        cmd_file: str | None = None,
    ) -> None:
        # The Scanner tab needs to fit both the signal table (~420 px
        # of column content) and the break calculator side panel
        # (~240 px minimum), so min_w is set just above the sum of
        # those two plus window chrome and the default launch width
        # leaves a comfortable margin for the panel's detail text.
        super().__init__(
            title="Mining Signals",
            width=w, height=h,
            min_w=720, min_h=320,
            opacity=opacity,
            always_on_top=True,
            accent=ACCENT,
        )
        # Clamp position to the visible area of the primary screen
        # so the window never ends up on a disconnected monitor
        self.restore_geometry_from_args(x, y, w, h, opacity)

        self._config = _load_config()
        self._cmd_file = cmd_file
        self._rows: list[dict] = []
        self._all_table_data: list[dict] = []
        self._matcher = SignalMatcher([])
        self._scan_timer: QTimer | None = None
        self._scan_bubble = ScanBubble()
        self._break_bubble = BreakBubble()

        # Loaded loadout snapshots per ship slot (parsed from disk).
        self._ship_snapshots: dict[str, LoadoutSnapshot | None] = {
            k: None for k, _ in SHIP_SLOTS
        }
        self._ship_slot_labels: dict[str, QLabel] = {}

        # Fleet: multiple ships in one slot
        self._fleet_snapshots: list[LoadoutSnapshot] = []

        # Salvage ships loaded from DPS Calculator (display only — no breakability)
        self._salvage_snapshots: list[SalvageSnapshot] = []

        # Gadget tab: spinbox references for refresh
        self._gadget_spinboxes: dict[str, object] = {}  # name -> QSpinBox

        # Consecutive-match consensus: require 2 agreeing reads before showing
        self._last_ocr_value: int | None = None
        self._confirmed_value: int | None = None
        # Guard against scan pileup on slower machines
        self._scan_in_progress: bool = False

        # Refinery order store
        from services.refinery_orders import RefineryOrderStore
        self._refinery_order_store = RefineryOrderStore(self._config)
        self._refinery_scan_timer: QTimer | None = None
        self._refinery_scan_in_progress: bool = False
        self._refinery_countdown_timer: QTimer | None = None

        # Services
        self._fetcher = SheetFetcher(
            ttl=self._config.get("refresh_interval_minutes", 60) * 60,
        )
        self._loader = _DataLoader(self._fetcher, self)
        self._loader.data_ready.connect(self._on_data_loaded)
        self._loader.error.connect(self._on_data_error)

        self._scan_value_ready.connect(self._on_scan_result)
        self._hud_data_ready.connect(self._update_break_bubble)

        # HUD consensus: rolling-window majority vote on recent mass
        # reads. Prevents flickering between single-digit drift misreads
        # (e.g. a static 6805 being read as 6805/6815/6845/6855 on
        # consecutive scans because the subpixel wiggle animation
        # drifts position-2 digit by 1-5 across scans). The rolling
        # window commits the MOST-FREQUENT value across the last N
        # reads, so transient misreads are outvoted by the true value.
        from collections import deque
        self._last_hud_mass: float | None = None          # confirmed (displayed)
        self._last_hud_resistance: float | None = None    # confirmed (displayed)
        # Raw recent reads, newest at the right. `maxlen=7` means a
        # stable value needs roughly 4 correct scans out of the last
        # 7 to become the displayed value — robust against per-scan
        # single-digit drift, but still responsive (4 seconds of lag
        # at 1 Hz before a new rock commits).
        self._hud_mass_window: deque = deque(maxlen=7)
        self._hud_resistance_window: deque = deque(maxlen=7)
        # Kept for back-compat with any code that reads these; the
        # real decision happens in `_commit_hud_from_window`.
        self._prev_hud_mass: float | None = None
        self._prev_hud_resistance: float | None = None
        # Instability: latest raw read. No consensus smoothing — it's
        # used as an on/off IMPOSSIBLE flag, not a displayed number.
        self._last_hud_instability: float | None = None

        self._build_ui()
        self._setup_ipc()

        # Initial data load
        self._loader.load()

        # Pre-warm the UEX item database in a background thread so the
        # first breakability calculation doesn't freeze the UI.
        def _warm_db():
            try:
                from services.loadout_loader import _load_item_db
                _load_item_db()
            except Exception:
                pass
        threading.Thread(target=_warm_db, daemon=True).start()

        # Auto-refresh timer
        refresh_ms = self._config.get("refresh_interval_minutes", 60) * 60 * 1000
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(lambda: self._loader.load(force=True))
        self._refresh_timer.start(refresh_ms)

    def _build_ui(self) -> None:
        layout = self.content_layout

        # ── Title bar ──
        self._title_bar = SCTitleBar(
            self,
            title="Mining Signals",
            icon_text="",
            accent_color=ACCENT,
            hotkey_text="Shift+9",
            extra_buttons=[("Tutorial", self._show_tutorial)],
        )
        self._title_bar.minimize_clicked.connect(self.showMinimized)
        self._title_bar.close_clicked.connect(self.user_close)
        layout.addWidget(self._title_bar)

        # ── Tab bar: Scanner (main) + Mining Ships ──
        # The scanner page holds all the existing scanner widgets. The
        # Mining Ships page holds the per-ship loadout slots. During
        # active scanning the tab bar hides so the collapsed view
        # stays minimal (see _on_scan_toggle).
        self._tabs = QTabWidget(self)
        self._tabs.setStyleSheet(f"""
            QTabWidget::pane {{
                border: none;
                background: transparent;
            }}
            QTabBar::tab {{
                background: {P.bg_card};
                color: {P.fg_dim};
                border: none;
                padding: 5px 14px;
                font-family: Consolas, monospace;
                font-size: 9pt;
                font-weight: bold;
            }}
            QTabBar::tab:selected {{
                background: transparent;
                color: {ACCENT};
                border-bottom: 2px solid {ACCENT};
            }}
            QTabBar::tab:hover:!selected {{
                color: {P.fg};
            }}
        """)
        # Region status shown at the right edge of the tab bar
        self._ocr_status = QLabel("", self._tabs)
        self._ocr_status.setStyleSheet(
            f"font-size: 8pt; color: {P.fg_dim}; background: transparent; "
            f"padding: 4px 8px;"
        )
        self._tabs.setCornerWidget(self._ocr_status, Qt.TopRightCorner)

        layout.addWidget(self._tabs, 1)

        # ── Mining Chart tab (live SCMDB data — Regolith-style) ──
        # Added at the top so it sits before Scanner in the tab order.
        self._chart_tab = MiningChartTab(
            parent=self._tabs,
            popout_handler=self._show_chart_popout,
        )
        self._tabs.addTab(self._chart_tab, "Mining Chart")

        # ── Scanner page (wraps the existing scanner UI) ──
        self._scanner_page = QWidget(self._tabs)
        scanner_layout = QVBoxLayout(self._scanner_page)
        scanner_layout.setContentsMargins(0, 0, 0, 0)
        scanner_layout.setSpacing(0)
        self._tabs.addTab(self._scanner_page, "Scanner")

        # The rest of the scanner widgets are parented to
        # self._scanner_page and appended to `layout` below. We keep
        # using the name `layout` for minimal diff against the
        # original code — from here on `layout` refers to the
        # scanner page's layout.
        layout = scanner_layout

        # ── Search bar: value + name (filters table) ──
        self._search_row = QWidget(self._scanner_page)
        search_layout = QHBoxLayout(self._search_row)
        search_layout.setContentsMargins(8, 4, 8, 2)
        search_layout.setSpacing(6)

        value_icon = QLabel("#", self._search_row)
        value_icon.setStyleSheet(f"""
            font-family: Consolas, monospace;
            font-size: 10pt; font-weight: bold;
            color: {P.fg_dim}; background: transparent;
        """)
        search_layout.addWidget(value_icon)

        self._search_input = QLineEdit(self._search_row)
        self._search_input.setPlaceholderText("Signal value...")
        self._search_input.textChanged.connect(self._on_search)
        self._search_input.setFixedWidth(130)
        search_layout.addWidget(self._search_input)

        name_icon = QLabel("\U0001f50d", self._search_row)
        name_icon.setStyleSheet(f"font-size: 9pt; color: {P.fg_dim}; background: transparent;")
        search_layout.addWidget(name_icon)

        self._name_input = QLineEdit(self._search_row)
        self._name_input.setPlaceholderText("Resource name...")
        self._name_input.textChanged.connect(self._on_name_search)
        search_layout.addWidget(self._name_input, 1)

        self._search_result = QLabel("", self._search_row)
        self._search_result.setStyleSheet(f"""
            font-family: Electrolize, Consolas, monospace;
            font-size: 9pt; font-weight: bold;
            color: {P.fg_dim}; background: transparent;
        """)
        search_layout.addWidget(self._search_result)

        layout.addWidget(self._search_row)

        # ── OCR controls row 1: scan buttons ──
        self._ocr_row = QWidget(self)
        ocr_layout = QHBoxLayout(self._ocr_row)
        ocr_layout.setContentsMargins(8, 2, 8, 2)
        ocr_layout.setSpacing(6)

        _btn_style = f"""
            QPushButton {{
                font-family: Consolas, monospace;
                font-size: 8pt; font-weight: bold;
                color: {ACCENT}; background: transparent;
                border: 1px solid {ACCENT}; border-radius: 3px;
                padding: 3px 8px;
            }}
            QPushButton:hover {{ background: rgba(51, 221, 136, 0.15); }}
        """

        self._btn_set_region = QPushButton("Set Scanning Region", self._ocr_row)
        self._btn_set_region.setCursor(Qt.PointingHandCursor)
        self._btn_set_region.setToolTip("Select screen area where the mining scanner number appears")
        self._btn_set_region.clicked.connect(self._on_set_region)
        self._btn_set_region.setStyleSheet(_btn_style)
        ocr_layout.addWidget(self._btn_set_region)

        # Second region button for the mining HUD (mass / resistance readout)
        self._btn_set_hud_region = QPushButton("Set Mining HUD Region", self._ocr_row)
        self._btn_set_hud_region.setCursor(Qt.PointingHandCursor)
        self._btn_set_hud_region.setToolTip(
            "Select screen area where rock mass / resistance appear on the mining HUD"
        )
        self._btn_set_hud_region.clicked.connect(self._on_set_hud_region)
        self._btn_set_hud_region.setStyleSheet(_btn_style)
        ocr_layout.addWidget(self._btn_set_hud_region)

        self._btn_scan_toggle = QPushButton("Start Scan", self._ocr_row)
        self._btn_scan_toggle.setCursor(Qt.PointingHandCursor)
        self._btn_scan_toggle.setCheckable(True)
        self._btn_scan_toggle.clicked.connect(self._on_scan_toggle)
        self._btn_scan_toggle.setStyleSheet(f"""
            QPushButton {{
                font-family: Consolas, monospace;
                font-size: 8pt; font-weight: bold;
                color: {P.fg}; background: transparent;
                border: 1px solid {P.border}; border-radius: 3px;
                padding: 3px 8px;
            }}
            QPushButton:hover {{ background: rgba(51, 221, 136, 0.15); border-color: {ACCENT}; }}
            QPushButton:checked {{
                color: {P.bg_primary}; background: {ACCENT};
                border-color: {ACCENT};
            }}
        """)
        ocr_layout.addWidget(self._btn_scan_toggle)

        # Inline scan result — primary display, always visible
        self._inline_result = QLabel("", self._ocr_row)
        self._inline_result.setStyleSheet(f"""
            font-family: Electrolize, Consolas, monospace;
            font-size: 11pt; font-weight: bold;
            color: {ACCENT}; background: transparent;
            padding: 0 6px;
        """)
        ocr_layout.addWidget(self._inline_result)

        self._hotkey_hint = QLabel("Shift+9 to hide", self._ocr_row)
        self._hotkey_hint.setStyleSheet(f"""
            font-family: Consolas, monospace;
            font-size: 7pt; color: {P.fg_dim};
            background: transparent;
        """)
        ocr_layout.addWidget(self._hotkey_hint)

        ocr_layout.addStretch(1)
        layout.addWidget(self._ocr_row)

        # ── OCR controls row 2: display location + ship selector ──
        self._display_row = QWidget(self)
        display_layout = QHBoxLayout(self._display_row)
        display_layout.setContentsMargins(8, 0, 8, 4)
        display_layout.setSpacing(6)

        self._btn_set_display = QPushButton("Set Mining Output Display Location", self._display_row)
        self._btn_set_display.setCursor(Qt.PointingHandCursor)
        self._btn_set_display.setToolTip("Choose where the result bubble appears on screen")
        self._btn_set_display.clicked.connect(self._on_set_display)
        self._btn_set_display.setStyleSheet(_btn_style)
        display_layout.addWidget(self._btn_set_display)

        self._btn_set_break_display = QPushButton("Set Break Bubble Location", self._display_row)
        self._btn_set_break_display.setCursor(Qt.PointingHandCursor)
        self._btn_set_break_display.setToolTip("Choose where the breakability panel appears on screen")
        self._btn_set_break_display.clicked.connect(self._on_set_break_display)
        self._btn_set_break_display.setStyleSheet(_btn_style)
        display_layout.addWidget(self._btn_set_break_display)

        self._btn_choose_ship = QPushButton("Choose Mining Ship", self._display_row)
        self._btn_choose_ship.setCursor(Qt.PointingHandCursor)
        self._btn_choose_ship.setToolTip(
            "Pick which loaded ship loadout to use for breakability calculations"
        )
        self._btn_choose_ship.clicked.connect(self._on_choose_mining_ship)
        self._btn_choose_ship.setStyleSheet(_btn_style)
        display_layout.addWidget(self._btn_choose_ship)

        self._btn_calc_mode = QPushButton("Calc: Fleet", self._display_row)
        self._btn_calc_mode.setCursor(Qt.PointingHandCursor)
        self._btn_calc_mode.setCheckable(True)
        is_team = self._config.get("calc_mode") == "team"
        self._btn_calc_mode.setChecked(is_team)
        if is_team:
            self._btn_calc_mode.setText("Calc: Team")
        self._btn_calc_mode.setToolTip(
            "Fleet = all ships combined. Team = only your assigned team."
        )
        self._btn_calc_mode.clicked.connect(self._on_toggle_calc_mode)
        self._btn_calc_mode.setStyleSheet(_btn_style)
        display_layout.addWidget(self._btn_calc_mode)

        display_layout.addStretch(1)
        layout.addWidget(self._display_row)

        # ── Breakability row: mass + resistance inputs + live result ──
        self._break_row = QWidget(self)
        break_layout = QHBoxLayout(self._break_row)
        break_layout.setContentsMargins(8, 0, 8, 4)
        break_layout.setSpacing(6)

        _input_style = f"""
            QLineEdit {{
                font-family: Consolas, monospace;
                font-size: 9pt; color: {P.fg};
                background: {P.bg_card}; border: 1px solid {P.border};
                border-radius: 3px; padding: 2px 6px;
            }}
            QLineEdit:focus {{ border-color: {ACCENT}; }}
        """

        mass_lbl = QLabel("Mass:", self._break_row)
        mass_lbl.setStyleSheet(
            f"font-family: Consolas, monospace; font-size: 8pt; "
            f"color: {P.fg_dim}; background: transparent;"
        )
        break_layout.addWidget(mass_lbl)

        self._mass_input = QLineEdit(self._break_row)
        self._mass_input.setPlaceholderText("0")
        self._mass_input.setFixedWidth(80)
        self._mass_input.setStyleSheet(_input_style)
        self._mass_input.textChanged.connect(self._on_break_inputs_changed)
        break_layout.addWidget(self._mass_input)

        res_lbl = QLabel("Resistance %:", self._break_row)
        res_lbl.setStyleSheet(
            f"font-family: Consolas, monospace; font-size: 8pt; "
            f"color: {P.fg_dim}; background: transparent;"
        )
        break_layout.addWidget(res_lbl)

        self._resistance_input = QLineEdit(self._break_row)
        self._resistance_input.setPlaceholderText("0")
        self._resistance_input.setFixedWidth(60)
        self._resistance_input.setStyleSheet(_input_style)
        self._resistance_input.textChanged.connect(self._on_break_inputs_changed)
        break_layout.addWidget(self._resistance_input)

        self._break_result = QLabel("", self._break_row)
        self._break_result.setStyleSheet(
            f"font-family: Consolas, monospace; font-size: 9pt; "
            f"font-weight: bold; color: {P.fg_dim}; background: transparent; "
            f"padding: 0 8px;"
        )
        break_layout.addWidget(self._break_result, 1)

        # Substitute button — shown when manual input rock can't be broken
        self._btn_substitute = QPushButton("Substitute", self._break_row)
        self._btn_substitute.setCursor(Qt.PointingHandCursor)
        self._btn_substitute.setStyleSheet(
            f"QPushButton {{ font-family: Consolas; font-size: 8pt; font-weight: bold; "
            f"color: #ff4444; background: transparent; border: 1px solid #ff4444; "
            f"border-radius: 3px; padding: 3px 8px; }}"
            f"QPushButton:hover {{ background: rgba(255,68,68,0.15); }}"
        )
        self._btn_substitute.clicked.connect(self._on_show_substitute)
        self._btn_substitute.setVisible(False)
        break_layout.addWidget(self._btn_substitute)

        layout.addWidget(self._break_row)

        # ── Scan hint (shown during scanning) ──
        self._scan_hint = QLabel(
            "Results can take several seconds to scan. Please stay on target and await results.",
            self,
        )
        self._scan_hint.setStyleSheet(f"""
            font-family: Consolas, monospace;
            font-size: 8pt; font-weight: bold;
            color: {P.fg_bright}; background: transparent;
            padding: 2px 8px;
        """)
        self._scan_hint.setWordWrap(True)
        self._scan_hint.setVisible(False)
        layout.addWidget(self._scan_hint)

        # ── Status bar ──
        self._status_row = QWidget(self)
        status_layout = QHBoxLayout(self._status_row)
        status_layout.setContentsMargins(8, 0, 8, 2)
        self._status_label = QLabel("Loading...", self._status_row)
        self._status_label.setStyleSheet(f"font-size: 8pt; color: {P.fg_dim}; background: transparent;")
        status_layout.addStretch(1)
        status_layout.addWidget(self._status_label)
        layout.addWidget(self._status_row)

        # ── Separator ──
        self._separator = QFrame(self)
        self._separator.setFrameShape(QFrame.HLine)
        self._separator.setFixedHeight(1)
        self._separator.setStyleSheet(f"background-color: {P.border};")
        layout.addWidget(self._separator)

        # ── Signal table ──
        # The Rarity column stores the sort index (int) so Qt's native
        # comparison sorts by our custom order. The fmt maps it back
        # to the display name.
        def _fmt_rarity(raw):
            if isinstance(raw, int):
                return RARITY_BY_KEY.get(raw, "")
            if isinstance(raw, tuple) and len(raw) >= 2:
                return str(raw[1])
            return str(raw) if raw is not None else ""

        self._table = SCTable(
            columns=[
                ColumnDef("Resource", "name", width=95),
                ColumnDef("Rarity", "rarity", width=70, fmt=_fmt_rarity),
                ColumnDef("1", "1", width=52, alignment=Qt.AlignRight),
                ColumnDef("2", "2", width=52, alignment=Qt.AlignRight),
                ColumnDef("3", "3", width=52, alignment=Qt.AlignRight),
                ColumnDef("4", "4", width=52, alignment=Qt.AlignRight),
                ColumnDef("5", "5", width=52, alignment=Qt.AlignRight),
                ColumnDef("6", "6", width=52, alignment=Qt.AlignRight),
            ],
            parent=self,
            sortable=True,
        )
        # Replace the default row delegate with one that colors each
        # row's text by rarity. Access the internal source model to
        # read the row's rarity at paint time.
        self._table.setItemDelegate(
            _RarityRowDelegate(self._table._source_model, self._table)
        )
        # Column sizing: every column hugs its content and nothing
        # stretches, so there are no gaps between Resource / Rarity
        # and the six signal-value columns regardless of how wide the
        # window is.  Any leftover horizontal space on the right is
        # just empty scroll-area background — not a column gap.
        header = self._table.horizontalHeader()
        header.setStretchLastSection(False)
        for i in range(8):  # Resource, Rarity, 1..6
            header.setSectionResizeMode(i, QHeaderView.ResizeToContents)
        # Double-click a row to open a detail popup with pin/close
        self._table.row_double_clicked.connect(self._open_resource_popup)

        # Wrap the table in a horizontal row so a permanent break
        # calculator side panel can live to its right — that's what
        # fills the empty whitespace that used to sit past column 6.
        self._break_panel = BreakPanel(self._scanner_page)
        table_row = QHBoxLayout()
        table_row.setContentsMargins(0, 0, 0, 0)
        table_row.setSpacing(0)
        table_row.addWidget(self._table, 0)
        table_row.addWidget(self._break_panel, 1)
        layout.addLayout(table_row, 1)

        # Widgets to hide when scan is active
        # (keep Set Region, scan toggle, and hotkey hint visible)
        self._expanded_widgets = [
            self._search_row, self._status_row,
            self._separator, self._table, self._break_panel,
            self._display_row, self._break_row,
        ]

        # ── Mining Ships tab page ──
        self._ships_page = self._build_ships_tab()
        self._tabs.addTab(self._ships_page, "Mining Ships")

        # ── Gadgets tab ──
        self._gadgets_page = self._build_gadgets_tab()
        self._tabs.addTab(self._gadgets_page, "Gadgets")

        # ── Refinery tab ──
        self._refinery_page = self._build_refinery_tab()
        self._tabs.addTab(self._refinery_page, "Refinery")

        # ── Mining Ledger tab ──
        self._ledger_tab = MiningLedgerTab(
            config=self._config,
            save_config_fn=_save_config,
            fleet_snapshots=self._fleet_snapshots,
            ship_snapshots=self._ship_snapshots,
            salvage_snapshots=self._salvage_snapshots,
            parent=self._tabs,
        )
        self._tabs.addTab(self._ledger_tab, "Mining Roster")

        # Default to the Scanner tab on startup; Mining Chart sits at the
        # top of the bar but shouldn't hijack the initial view.
        self._tabs.setCurrentWidget(self._scanner_page)

        # Load any previously-selected loadout files from config
        self._restore_ship_loadouts()
        self._restore_fleet_loadouts()
        self._restore_salvage_loadouts()

        # Refresh ledger fleet panel now that snapshots are loaded
        self._ledger_tab.refresh_fleet_panel()

        # Update OCR status
        self._update_ocr_status()
        self._update_ship_button_label()
        self._update_consumables_display()
        # Seed the break panel with the current (possibly empty) state.
        self._refresh_break_panel()

    # ── Mining Ships tab ──

    def _build_ships_tab(self) -> QWidget:
        """Construct the Mining Ships tab page.

        Contains two sub-tabs:
          - Mining: one row per mining ship (Golem / Prospector / Mole)
            plus Mining Ops Fleet section.
          - Salvage: loadable DPS Calculator salvage ship loadouts.
        """
        container = QWidget(self._tabs)
        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(0, 0, 0, 0)
        container_layout.setSpacing(0)

        sub_tabs = QTabWidget(container)
        sub_tabs.setDocumentMode(True)
        container_layout.addWidget(sub_tabs)

        # ── Mining sub-tab ──
        page = QWidget(sub_tabs)
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(16, 16, 16, 16)
        page_layout.setSpacing(10)

        header = QLabel(
            "Load a saved Mining Loadout file for each ship. The active "
            "selection (via 'Choose Mining Ship' on the Scanner tab) feeds "
            "the breakability calculator.",
            page,
        )
        header.setWordWrap(True)
        header.setStyleSheet(
            f"font-family: Consolas, monospace; font-size: 8pt; "
            f"color: {P.fg_dim}; background: transparent;"
        )
        page_layout.addWidget(header)

        # Shared button style for the load/clear buttons
        btn_style = f"""
            QPushButton {{
                font-family: Consolas, monospace;
                font-size: 8pt; font-weight: bold;
                color: {ACCENT}; background: transparent;
                border: 1px solid {ACCENT}; border-radius: 3px;
                padding: 4px 10px;
            }}
            QPushButton:hover {{ background: rgba(51, 221, 136, 0.15); }}
            QPushButton:disabled {{
                color: {P.fg_dim}; border-color: {P.border};
            }}
        """

        for slot_id, ship_label in SHIP_SLOTS:
            # Outer container for this ship's block
            block = QWidget(page)
            block_layout = QVBoxLayout(block)
            block_layout.setContentsMargins(0, 0, 0, 0)
            block_layout.setSpacing(2)

            # Header row: ship name + buttons
            header_row = QWidget(block)
            header_layout = QHBoxLayout(header_row)
            header_layout.setContentsMargins(0, 0, 0, 0)
            header_layout.setSpacing(8)

            name_lbl = QLabel(f"{ship_label}:", header_row)
            name_lbl.setStyleSheet(
                f"font-family: Consolas, monospace; font-size: 10pt; "
                f"font-weight: bold; color: {P.fg}; background: transparent;"
            )
            header_layout.addWidget(name_lbl)
            header_layout.addStretch(1)

            load_btn = QPushButton("\U0001F4C2 Load", header_row)
            load_btn.setCursor(Qt.PointingHandCursor)
            load_btn.setToolTip(f"Load a Mining Loadout JSON file for the {ship_label}")
            load_btn.setStyleSheet(btn_style)
            load_btn.clicked.connect(
                lambda _=False, sid=slot_id, lbl=ship_label: self._on_load_ship_loadout(sid, lbl)
            )
            header_layout.addWidget(load_btn)

            clear_btn = QPushButton("Clear", header_row)
            clear_btn.setCursor(Qt.PointingHandCursor)
            clear_btn.setToolTip(f"Unload the {ship_label}'s loadout")
            clear_btn.setStyleSheet(btn_style)
            clear_btn.clicked.connect(
                lambda _=False, sid=slot_id: self._on_clear_ship_loadout(sid)
            )
            header_layout.addWidget(clear_btn)

            block_layout.addWidget(header_row)

            # Hierarchy detail label (multi-line, shows turrets + modules)
            detail_lbl = QLabel("", block)
            detail_lbl.setWordWrap(True)
            detail_lbl.setTextFormat(Qt.RichText)
            detail_lbl.setStyleSheet(
                f"font-family: Consolas, monospace; font-size: 8pt; "
                f"color: {P.fg_dim}; background: transparent; "
                f"padding-left: 16px;"
            )
            block_layout.addWidget(detail_lbl)
            self._ship_slot_labels[slot_id] = detail_lbl

            page_layout.addWidget(block)

        # ── Mining Ops Fleet section ──
        fleet_sep = QFrame(page)
        fleet_sep.setFrameShape(QFrame.HLine)
        fleet_sep.setFixedHeight(1)
        fleet_sep.setStyleSheet(f"background-color: {P.border};")
        page_layout.addWidget(fleet_sep)

        fleet_header = QWidget(page)
        fh_layout = QHBoxLayout(fleet_header)
        fh_layout.setContentsMargins(0, 6, 0, 0)
        fh_layout.setSpacing(8)

        fleet_lbl = QLabel("Mining Ops Fleet:", fleet_header)
        fleet_lbl.setStyleSheet(
            f"font-family: Consolas, monospace; font-size: 10pt; "
            f"font-weight: bold; color: {P.fg}; background: transparent;"
        )
        fh_layout.addWidget(fleet_lbl)
        fh_layout.addStretch(1)

        add_ship_btn = QPushButton("Add Ship", fleet_header)
        add_ship_btn.setCursor(Qt.PointingHandCursor)
        add_ship_btn.setStyleSheet(btn_style)
        add_ship_btn.clicked.connect(self._on_fleet_add_ship)
        fh_layout.addWidget(add_ship_btn)

        expand_btn = QPushButton("Expand Fleet", fleet_header)
        expand_btn.setCursor(Qt.PointingHandCursor)
        expand_btn.setStyleSheet(btn_style)
        expand_btn.clicked.connect(self._on_fleet_expand)
        fh_layout.addWidget(expand_btn)

        clear_fleet_btn = QPushButton("Clear Fleet", fleet_header)
        clear_fleet_btn.setCursor(Qt.PointingHandCursor)
        clear_fleet_btn.setStyleSheet(btn_style)
        clear_fleet_btn.clicked.connect(self._on_fleet_clear)
        fh_layout.addWidget(clear_fleet_btn)

        page_layout.addWidget(fleet_header)

        # Fleet ship name labels (first 5)
        self._fleet_names_label = QLabel("", page)
        self._fleet_names_label.setWordWrap(True)
        self._fleet_names_label.setStyleSheet(
            f"font-family: Consolas, monospace; font-size: 8pt; "
            f"color: {P.fg_dim}; background: transparent; padding-left: 16px;"
        )
        page_layout.addWidget(self._fleet_names_label)

        page_layout.addStretch(1)
        sub_tabs.addTab(page, "Mining")

        # ── Salvage sub-tab ──
        salvage_page = self._build_salvage_sub_tab(sub_tabs, btn_style)
        sub_tabs.addTab(salvage_page, "Salvage")

        return container

    def _build_salvage_sub_tab(self, parent: QWidget, btn_style: str) -> QWidget:
        """Salvage ships sub-tab — load DPS Calculator loadouts for display
        in the Mining Roster. Salvage ships don't contribute to breakability
        calculations, so they're display-only here.
        """
        page = QWidget(parent)
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(16, 16, 16, 16)
        page_layout.setSpacing(10)

        header = QLabel(
            "Load DPS Calculator loadout files (.json) for salvage ships. "
            "Salvage ships loaded here appear in the Mining Roster fleet "
            "panel so you can drag them into teams and strike groups.",
            page,
        )
        header.setWordWrap(True)
        header.setStyleSheet(
            f"font-family: Consolas, monospace; font-size: 8pt; "
            f"color: {P.fg_dim}; background: transparent;"
        )
        page_layout.addWidget(header)

        # Header row: title + Add/Clear buttons
        header_row = QWidget(page)
        hr_layout = QHBoxLayout(header_row)
        hr_layout.setContentsMargins(0, 4, 0, 0)
        hr_layout.setSpacing(8)

        title_lbl = QLabel("Salvage Fleet:", header_row)
        title_lbl.setStyleSheet(
            f"font-family: Consolas, monospace; font-size: 10pt; "
            f"font-weight: bold; color: {P.fg}; background: transparent;"
        )
        hr_layout.addWidget(title_lbl)
        hr_layout.addStretch(1)

        add_btn = QPushButton("\U0001F4C2 Add Salvage Ship", header_row)
        add_btn.setCursor(Qt.PointingHandCursor)
        add_btn.setStyleSheet(btn_style)
        add_btn.setToolTip("Load a DPS Calculator loadout file")
        add_btn.clicked.connect(self._on_salvage_add_ship)
        hr_layout.addWidget(add_btn)

        clear_btn = QPushButton("Clear All", header_row)
        clear_btn.setCursor(Qt.PointingHandCursor)
        clear_btn.setStyleSheet(btn_style)
        clear_btn.clicked.connect(self._on_salvage_clear)
        hr_layout.addWidget(clear_btn)

        page_layout.addWidget(header_row)

        # Scrollable list of loaded salvage ships
        self._salvage_names_label = QLabel("(no salvage ships loaded)", page)
        self._salvage_names_label.setWordWrap(True)
        self._salvage_names_label.setTextFormat(Qt.RichText)
        self._salvage_names_label.setStyleSheet(
            f"font-family: Consolas, monospace; font-size: 8pt; "
            f"color: {P.fg_dim}; background: transparent; padding-left: 16px;"
        )
        page_layout.addWidget(self._salvage_names_label)

        page_layout.addStretch(1)
        return page

    # ── Gadgets tab ──

    def _build_gadgets_tab(self) -> QWidget:
        """Build the Gadgets tab with quantity selectors + always-use toggle."""
        page = QWidget(self._tabs)
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(16, 16, 16, 16)
        page_layout.setSpacing(8)

        header = QLabel(
            "Set your available gadgets. Gadgets are only recommended when "
            "a ship cannot break a rock without one, unless 'Always use best "
            "gadget' is enabled.",
            page,
        )
        header.setWordWrap(True)
        header.setStyleSheet(
            f"font-family: Consolas, monospace; font-size: 8pt; "
            f"color: {P.fg_dim}; background: transparent;"
        )
        page_layout.addWidget(header)

        # Always-use toggle
        self._always_best_gadget = QCheckBox("Always use best gadget", page)
        self._always_best_gadget.setChecked(
            self._config.get("always_use_best_gadget", False)
        )
        self._always_best_gadget.setStyleSheet(
            f"font-family: Consolas, monospace; font-size: 9pt; "
            f"color: {ACCENT}; background: transparent;"
        )
        self._always_best_gadget.stateChanged.connect(self._on_always_best_changed)
        page_layout.addWidget(self._always_best_gadget)

        # Gadget rows
        gadgets_db = get_gadget_list()
        quantities = self._config.get("gadget_quantities", {})

        if not gadgets_db:
            no_data = QLabel(
                "Gadget data unavailable — ensure Mining Loadout tool is installed.",
                page,
            )
            no_data.setStyleSheet(
                f"font-family: Consolas, monospace; font-size: 8pt; "
                f"color: {P.fg_dim}; background: transparent;"
            )
            page_layout.addWidget(no_data)
        else:
            _spin_style = f"""
                QSpinBox {{
                    font-family: Consolas, monospace; font-size: 9pt;
                    color: {P.fg}; background: {P.bg_card};
                    border: 1px solid {P.border}; border-radius: 3px;
                    padding: 2px 4px;
                }}
                QSpinBox::up-button, QSpinBox::down-button {{
                    width: 16px; border: none;
                    background: {P.bg_secondary};
                }}
                QSpinBox::up-button:hover, QSpinBox::down-button:hover {{
                    background: rgba(51, 221, 136, 0.25);
                }}
                QSpinBox::up-arrow {{
                    image: none; border-left: 4px solid transparent;
                    border-right: 4px solid transparent;
                    border-bottom: 5px solid {ACCENT};
                    width: 0; height: 0;
                }}
                QSpinBox::down-arrow {{
                    image: none; border-left: 4px solid transparent;
                    border-right: 4px solid transparent;
                    border-top: 5px solid {ACCENT};
                    width: 0; height: 0;
                }}
            """

            def _trait_text(val, label):
                if val is None:
                    return None
                color = ACCENT if val < 0 else "#ff4444" if val > 0 else P.fg_dim
                return f'<span style="color:{color};">{label}: {val:+.0f}%</span>'

            for name in sorted(gadgets_db.keys()):
                g = gadgets_db[name]
                block = QWidget(page)
                block_layout = QVBoxLayout(block)
                block_layout.setContentsMargins(0, 0, 0, 4)
                block_layout.setSpacing(1)

                # Top row: name + spinbox
                top_row = QWidget(block)
                top_layout = QHBoxLayout(top_row)
                top_layout.setContentsMargins(0, 0, 0, 0)
                top_layout.setSpacing(8)

                name_lbl = QLabel(name, top_row)
                name_lbl.setFixedWidth(120)
                name_lbl.setStyleSheet(
                    f"font-family: Consolas, monospace; font-size: 9pt; "
                    f"font-weight: bold; color: {P.fg}; background: transparent;"
                )
                top_layout.addWidget(name_lbl)

                # All traits as colored tags
                traits = []
                for val, label in [
                    (g.resistance, "Resist"),
                    (g.instability, "Instab"),
                    (g.charge_window, "ChgWnd"),
                    (g.charge_rate, "ChgRate"),
                    (g.cluster, "Cluster"),
                ]:
                    t = _trait_text(val, label)
                    if t:
                        traits.append(t)

                traits_lbl = QLabel("  ".join(traits) if traits else "—", top_row)
                traits_lbl.setTextFormat(Qt.RichText)
                traits_lbl.setStyleSheet(
                    f"font-family: Consolas, monospace; font-size: 7pt; "
                    f"background: transparent;"
                )
                top_layout.addWidget(traits_lbl, 1)

                spin = QSpinBox(top_row)
                spin.setRange(0, 99)
                spin.setValue(quantities.get(name, 0))
                spin.setFixedWidth(70)
                spin.setStyleSheet(_spin_style)
                spin.valueChanged.connect(
                    lambda val, n=name: self._on_gadget_qty_changed(n, val)
                )
                top_layout.addWidget(spin)
                self._gadget_spinboxes[name] = spin

                block_layout.addWidget(top_row)
                page_layout.addWidget(block)

        # ── Mining Foreman Console button ──
        admiral_sep = QFrame(page)
        admiral_sep.setFrameShape(QFrame.HLine)
        admiral_sep.setFixedHeight(1)
        admiral_sep.setStyleSheet(f"background-color: {P.border};")
        page_layout.addWidget(admiral_sep)

        admiral_btn = QPushButton("Mining Foreman Console", page)
        admiral_btn.setCursor(Qt.PointingHandCursor)
        admiral_btn.setStyleSheet(
            f"QPushButton {{ font-family: Consolas, monospace; font-size: 9pt; "
            f"font-weight: bold; color: {ACCENT}; background: transparent; "
            f"border: 1px solid {ACCENT}; border-radius: 3px; padding: 6px 14px; }}"
            f"QPushButton:hover {{ background: rgba(51,221,136,0.15); }}"
        )
        admiral_btn.clicked.connect(self._on_fleet_admiral_view)
        page_layout.addWidget(admiral_btn)

        page_layout.addStretch(1)
        return page

    # ── Refinery tab ──

    def _build_refinery_tab(self) -> QWidget:
        """Build the Refinery tab with sub-tabs: In Process / Complete."""
        page = QWidget(self._tabs)
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(16, 8, 16, 8)
        page_layout.setSpacing(6)

        # Live monitor instance
        self._refinery_monitor = None
        # Raw log results for legacy complete-only entries
        self._refinery_raw_results: list[dict] = []
        self._refinery_picked_up: set[str] = set(
            self._config.get("refinery_picked_up", [])
        )
        self._refinery_deleted: set[str] = set(
            self._config.get("refinery_deleted", [])
        )

        _btn = (
            f"QPushButton {{ font-family: Consolas, monospace; font-size: 8pt; "
            f"color: {P.fg}; background: {P.bg_card}; "
            f"border: 1px solid {P.border}; border-radius: 3px; padding: 3px 8px; }}"
            f"QPushButton:hover {{ background: rgba(51,221,136,0.15); }}"
        )
        _btn_accent = (
            f"QPushButton {{ font-family: Consolas, monospace; font-size: 8pt; "
            f"font-weight: bold; color: {ACCENT}; background: transparent; "
            f"border: 1px solid {ACCENT}; border-radius: 3px; padding: 3px 8px; }}"
            f"QPushButton:hover {{ background: rgba(51,221,136,0.15); }}"
        )
        _btn_red = (
            f"QPushButton {{ font-family: Consolas, monospace; font-size: 8pt; "
            f"color: {P.fg}; background: {P.bg_card}; "
            f"border: 1px solid {P.border}; border-radius: 3px; padding: 3px 10px; }}"
            f"QPushButton:hover {{ background: rgba(255,60,60,0.15); "
            f"border-color: #cc6666; color: #cc6666; }}"
        )
        _lbl = (
            f"font-family: Consolas, monospace; font-size: 8pt; "
            f"color: {P.fg_dim}; background: transparent;"
        )

        # ── Toolbar row ──
        toolbar = QHBoxLayout()
        toolbar.setSpacing(6)

        btn_set_region = QPushButton("Set Refinery Region", page)
        btn_set_region.setStyleSheet(_btn)
        btn_set_region.clicked.connect(self._on_set_refinery_region)
        toolbar.addWidget(btn_set_region)

        btn_scan = QPushButton("Scan Now", page)
        btn_scan.setStyleSheet(_btn_accent)
        btn_scan.clicked.connect(self._do_refinery_scan)
        self._refinery_scan_btn = btn_scan
        toolbar.addWidget(btn_scan)

        self._refinery_auto_cb = QCheckBox("Auto-Scan", page)
        self._refinery_auto_cb.setChecked(
            self._config.get("refinery_auto_scan", False)
        )
        self._refinery_auto_cb.setStyleSheet(
            f"font-family: Consolas, monospace; font-size: 8pt; "
            f"color: {P.fg}; background: transparent;"
        )
        self._refinery_auto_cb.stateChanged.connect(self._on_refinery_auto_toggle)
        toolbar.addWidget(self._refinery_auto_cb)

        toolbar.addStretch(1)

        btn_log_path = QPushButton("Set Log Path", page)
        btn_log_path.setStyleSheet(_btn)
        btn_log_path.clicked.connect(self._on_refinery_set_dir)
        toolbar.addWidget(btn_log_path)

        page_layout.addLayout(toolbar)

        # Status labels
        region_text = "Region set" if self._config.get("refinery_ocr_region") else "No region set"
        self._refinery_region_label = QLabel(region_text, page)
        self._refinery_region_label.setStyleSheet(_lbl)
        page_layout.addWidget(self._refinery_region_label)

        # Summary
        self._refinery_summary = QLabel("", page)
        self._refinery_summary.setStyleSheet(
            f"font-family: Consolas, monospace; font-size: 9pt; "
            f"color: {ACCENT}; background: transparent;"
        )
        page_layout.addWidget(self._refinery_summary)

        # ── Sub-tabs ──
        sub_tabs = QTabWidget(page)
        sub_tabs.setStyleSheet(f"""
            QTabBar::tab {{
                font-family: Consolas, monospace; font-size: 9pt;
                color: {P.fg_dim}; background: transparent;
                padding: 6px 12px; border: none;
                border-bottom: 2px solid transparent;
            }}
            QTabBar::tab:selected {{
                color: {ACCENT}; border-bottom-color: {ACCENT};
            }}
            QTabBar::tab:hover:!selected {{ color: {P.fg}; }}
        """)

        # ── In Process sub-tab ──
        in_process_page = QWidget(sub_tabs)
        ip_layout = QVBoxLayout(in_process_page)
        ip_layout.setContentsMargins(0, 8, 0, 0)
        ip_layout.setSpacing(4)

        self._refinery_ip_table = SCTable(
            columns=[
                ColumnDef("Name", "name", width=160),
                ColumnDef("Station", "station", width=130),
                ColumnDef("Method", "method", width=130),
                ColumnDef("Cost", "cost", width=80, alignment=Qt.AlignRight,
                          fmt=lambda v: f"{v:,.0f}" if v else "—"),
                ColumnDef("Time Left", "time_left", width=100, fg_color=ACCENT),
                ColumnDef("Commodities", "commodities_str", width=200),
            ],
            parent=in_process_page,
            sortable=True,
        )
        self._refinery_ip_table.row_double_clicked.connect(
            self._on_refinery_order_clicked
        )
        ip_layout.addWidget(self._refinery_ip_table, 1)

        # IP action buttons
        ip_btns = QHBoxLayout()
        ip_btns.setSpacing(6)
        btn_rename = QPushButton("Rename", in_process_page)
        btn_rename.setStyleSheet(_btn)
        btn_rename.clicked.connect(self._on_refinery_rename)
        ip_btns.addWidget(btn_rename)
        btn_del_ip = QPushButton("Delete", in_process_page)
        btn_del_ip.setStyleSheet(_btn_red)
        btn_del_ip.clicked.connect(self._on_refinery_delete_ip)
        ip_btns.addWidget(btn_del_ip)
        ip_btns.addStretch(1)
        ip_layout.addLayout(ip_btns)

        sub_tabs.addTab(in_process_page, "Orders In Process")

        # ── Complete sub-tab ──
        complete_page = QWidget(sub_tabs)
        cp_layout = QVBoxLayout(complete_page)
        cp_layout.setContentsMargins(0, 8, 0, 0)
        cp_layout.setSpacing(4)

        self._refinery_cp_table = SCTable(
            columns=[
                ColumnDef("Name", "name", width=160),
                ColumnDef("Station", "station", width=130),
                ColumnDef("Method", "method", width=130),
                ColumnDef("Cost", "cost", width=80, alignment=Qt.AlignRight,
                          fmt=lambda v: f"{v:,.0f}" if v else "—"),
                ColumnDef("Completed", "completed_at", width=140),
                ColumnDef("Commodities", "commodities_str", width=200),
            ],
            parent=complete_page,
            sortable=True,
        )
        self._refinery_cp_table.row_double_clicked.connect(
            self._on_refinery_order_clicked
        )
        cp_layout.addWidget(self._refinery_cp_table, 1)

        # Complete action buttons
        cp_btns = QHBoxLayout()
        cp_btns.setSpacing(6)
        btn_pickup = QPushButton("Mark Picked Up", complete_page)
        btn_pickup.setStyleSheet(_btn_accent)
        btn_pickup.setToolTip("Move selected order to Picked Up tab")
        btn_pickup.clicked.connect(self._on_refinery_mark_picked_up)
        cp_btns.addWidget(btn_pickup)
        btn_del_cp = QPushButton("Delete", complete_page)
        btn_del_cp.setStyleSheet(_btn_red)
        btn_del_cp.clicked.connect(self._on_refinery_delete_cp)
        cp_btns.addWidget(btn_del_cp)
        btn_clear = QPushButton("Clear All Completed", complete_page)
        btn_clear.setStyleSheet(_btn_red)
        btn_clear.clicked.connect(self._on_refinery_clear_complete)
        cp_btns.addWidget(btn_clear)
        cp_btns.addStretch(1)
        cp_layout.addLayout(cp_btns)

        sub_tabs.addTab(complete_page, "Orders Complete")

        # ── Picked Up sub-tab ──
        pickup_page = QWidget(sub_tabs)
        pu_layout = QVBoxLayout(pickup_page)
        pu_layout.setContentsMargins(0, 8, 0, 0)
        pu_layout.setSpacing(4)

        self._refinery_pu_table = SCTable(
            columns=[
                ColumnDef("Name", "name", width=160),
                ColumnDef("Station", "station", width=130),
                ColumnDef("Method", "method", width=130),
                ColumnDef("Cost", "cost", width=80, alignment=Qt.AlignRight,
                          fmt=lambda v: f"{v:,.0f}" if v else "—"),
                ColumnDef("Picked Up", "picked_up_at", width=140),
                ColumnDef("Commodities", "commodities_str", width=200),
            ],
            parent=pickup_page,
            sortable=True,
        )
        self._refinery_pu_table.row_double_clicked.connect(
            self._on_refinery_order_clicked
        )
        pu_layout.addWidget(self._refinery_pu_table, 1)

        pu_btns = QHBoxLayout()
        pu_btns.setSpacing(6)
        btn_del_pu = QPushButton("Delete", pickup_page)
        btn_del_pu.setStyleSheet(_btn_red)
        btn_del_pu.clicked.connect(self._on_refinery_delete_pu)
        pu_btns.addWidget(btn_del_pu)
        btn_clear_pu = QPushButton("Clear All Picked Up", pickup_page)
        btn_clear_pu.setStyleSheet(_btn_red)
        btn_clear_pu.clicked.connect(self._on_refinery_clear_picked_up)
        pu_btns.addWidget(btn_clear_pu)
        pu_btns.addStretch(1)
        pu_layout.addLayout(pu_btns)

        sub_tabs.addTab(pickup_page, "Picked Up")

        # ── Locations sub-tab (refinery directory + near-me search) ──
        self._refinery_locations_tab = RefineryLocationsTab(
            parent=sub_tabs,
            player_location_provider=self._get_player_location,
            status_label=None,   # wire the shared label after it exists
        )
        sub_tabs.addTab(self._refinery_locations_tab, "Locations")

        # ── Yields sub-tab (refinery mineral yield comparison table) ──
        self._refinery_yields_tab = RefineryYieldsTab(parent=sub_tabs)
        # When yield data loads, share it with the Locations tab so its
        # detail popup can show per-mineral bonuses.
        self._refinery_yields_tab._loader.loaded.connect(
            lambda data: self._refinery_locations_tab.set_yield_data(data)
        )
        sub_tabs.addTab(self._refinery_yields_tab, "Yields")

        page_layout.addWidget(sub_tabs, 1)

        # Status label
        self._refinery_status = QLabel("Starting...", page)
        self._refinery_status.setStyleSheet(_lbl)
        page_layout.addWidget(self._refinery_status)

        # Share the status label with the locations sub-tab so row
        # clicks flash "Copied '<name>' to clipboard" in the same place
        # as the other refinery messages.
        self._refinery_locations_tab._shared_status = self._refinery_status

        # Start log monitor + countdown timer
        self._start_refinery_monitor()
        self._refinery_countdown_timer = QTimer(self)
        self._refinery_countdown_timer.timeout.connect(self._refresh_refinery_countdowns)
        self._refinery_countdown_timer.start(1000)

        # Start auto-scan if enabled
        if self._config.get("refinery_auto_scan", False):
            self._start_refinery_auto_scan()

        # Initial table refresh
        self._refresh_refinery_tables()

        return page

    # ── Refinery helpers ──

    def _persist_refinery_orders(self) -> None:
        """Save order store to config."""
        self._config["refinery_orders"] = self._refinery_order_store.to_config_list()
        _save_config(self._config)

    def _refresh_refinery_tables(self) -> None:
        """Rebuild both In Process and Complete tables from the order store."""
        # In Process table
        ip_orders = self._refinery_order_store.get_in_process()
        ip_data = []
        for o in ip_orders:
            ip_data.append({
                "id": o.id,
                "name": o.name,
                "station": o.station,
                "method": o.method,
                "cost": o.cost,
                "time_left": o.time_remaining_str(),
                "commodities_str": o.commodities_summary(),
            })
        self._refinery_ip_table.set_data(ip_data)

        # Complete table
        cp_orders = self._refinery_order_store.get_complete()
        cp_data = []
        for o in cp_orders:
            completed = ""
            if o.completed_at:
                completed = o.completed_at.replace("T", " ")[:16]
            cp_data.append({
                "id": o.id,
                "name": o.name,
                "station": o.station,
                "method": o.method,
                "cost": o.cost,
                "completed_at": completed,
                "commodities_str": o.commodities_summary(),
            })
        self._refinery_cp_table.set_data(cp_data)

        # Picked Up table
        pu_orders = self._refinery_order_store.get_picked_up()
        pu_data = []
        for o in pu_orders:
            picked = ""
            if o.picked_up_at:
                picked = o.picked_up_at.replace("T", " ")[:16]
            pu_data.append({
                "id": o.id,
                "name": o.name,
                "station": o.station,
                "method": o.method,
                "cost": o.cost,
                "picked_up_at": picked,
                "commodities_str": o.commodities_summary(),
            })
        self._refinery_pu_table.set_data(pu_data)

        # Summary
        n_ip = len(ip_orders)
        n_cp = len(cp_orders)
        n_pu = len(pu_orders)
        total_cost = sum(o.cost for o in ip_orders)
        self._refinery_summary.setText(
            f"{n_ip} in process  ·  {n_cp} complete  ·  "
            f"{n_pu} picked up  ·  {total_cost:,.0f} aUEC pending"
        )

    def _refresh_refinery_countdowns(self) -> None:
        """Update only the Time Left column for in-process orders (called every 1s)."""
        ip_orders = self._refinery_order_store.get_in_process()
        if not ip_orders:
            return

        # Preserve current selection across the data refresh
        selected = self._refinery_ip_table.get_selected_row()
        selected_id = selected.get("id") if selected else None

        ip_data = []
        for o in ip_orders:
            ip_data.append({
                "id": o.id,
                "name": o.name,
                "station": o.station,
                "method": o.method,
                "cost": o.cost,
                "time_left": o.time_remaining_str(),
                "commodities_str": o.commodities_summary(),
            })
        self._refinery_ip_table.set_data(ip_data)

        # Restore selection by matching order ID
        if selected_id:
            model = self._refinery_ip_table.model()
            src = self._refinery_ip_table._source_model
            for row in range(src.rowCount()):
                row_data = src.row_data(row)
                if row_data and row_data.get("id") == selected_id:
                    if self._refinery_ip_table._proxy:
                        src_idx = src.index(row, 0)
                        proxy_idx = self._refinery_ip_table._proxy.mapFromSource(src_idx)
                        self._refinery_ip_table.selectRow(proxy_idx.row())
                    else:
                        self._refinery_ip_table.selectRow(row)
                    break

    # ── Refinery OCR scanning ──

    def _on_set_refinery_region(self) -> None:
        """Open region selector for the refinery kiosk area."""
        selector = RegionSelector(self)
        selector.region_selected.connect(self._on_refinery_region_selected)
        selector.show()

    def _on_refinery_region_selected(self, region: dict) -> None:
        self._config["refinery_ocr_region"] = region
        _save_config(self._config)
        self._refinery_region_label.setText(
            f"Region: {region['w']}×{region['h']} at ({region['x']}, {region['y']})"
        )

    _last_refinery_hash: int = 0

    def _do_refinery_scan(self) -> None:
        """One-shot refinery OCR scan in background thread.

        Skips OCR if the captured image hasn't changed since last scan.
        """
        region = self._config.get("refinery_ocr_region")
        if not region:
            self._refinery_status.setText("Set a refinery region first.")
            return
        if self._refinery_scan_in_progress:
            return
        if self._scan_timer is not None:
            self._refinery_status.setText("Mining scanner active — skipping refinery scan.")
            return

        self._refinery_scan_in_progress = True
        self._refinery_scan_btn.setEnabled(False)
        self._refinery_status.setText("Scanning refinery panel...")

        station = ""
        if self._refinery_monitor and self._refinery_monitor.current_location:
            station = self._refinery_monitor.current_location

        prev_hash = MiningSignalsApp._last_refinery_hash

        def _run():
            try:
                # Quick change detection — hash a sample of the image
                from ocr.screen_reader import capture_region
                img = capture_region(region)
                if img is not None:
                    img_hash = hash(img.tobytes()[:4096])
                    if img_hash == prev_hash and prev_hash != 0:
                        # Panel unchanged — skip full OCR
                        QMetaObject.invokeMethod(
                            self, "_on_refinery_ocr_skipped",
                            Qt.QueuedConnection,
                        )
                        return
                    MiningSignalsApp._last_refinery_hash = img_hash

                from ocr.refinery_reader import scan_refinery
                result = scan_refinery(region, station=station)
            except Exception as exc:
                log.exception("Refinery OCR failed: %s", exc)
                result = None
            QMetaObject.invokeMethod(
                self, "_on_refinery_ocr_result",
                Qt.QueuedConnection,
                Q_ARG("QVariant", result),
            )

        threading.Thread(target=_run, daemon=True).start()

    @Slot("QVariant")
    @Slot()
    def _on_refinery_ocr_skipped(self) -> None:
        """Called when auto-scan detects no change — skip OCR."""
        self._refinery_scan_in_progress = False
        self._refinery_scan_btn.setEnabled(True)

    @Slot("QVariant")
    def _on_refinery_ocr_result(self, result) -> None:
        """Handle OCR scan result on main thread."""
        self._refinery_scan_in_progress = False
        self._refinery_scan_btn.setEnabled(True)

        if result is None:
            self._refinery_status.setText("Refinery panel not detected.")
            return
        if not result:
            self._refinery_status.setText("Panel detected but no orders parsed.")
            return

        added = 0
        for order_data in result:
            order = self._refinery_order_store.add_order(
                station=order_data.get("station", ""),
                commodities=order_data.get("commodities", []),
                method=order_data.get("method", ""),
                cost=order_data.get("cost", 0),
                processing_seconds=order_data.get("processing_seconds", 0),
            )
            if order:
                added += 1

        self._persist_refinery_orders()
        self._refresh_refinery_tables()
        self._refinery_status.setText(
            f"Scanned {len(result)} order(s), {added} added."
        )

    def _on_refinery_auto_toggle(self, state: int) -> None:
        """Toggle auto-scan on/off."""
        enabled = state != 0
        self._config["refinery_auto_scan"] = enabled
        _save_config(self._config)
        if enabled:
            self._start_refinery_auto_scan()
        else:
            self._stop_refinery_auto_scan()

    def _start_refinery_auto_scan(self) -> None:
        if self._refinery_scan_timer is not None:
            return
        self._refinery_scan_timer = QTimer(self)
        self._refinery_scan_timer.timeout.connect(self._do_refinery_scan)
        self._refinery_scan_timer.start(3000)

    def _stop_refinery_auto_scan(self) -> None:
        if self._refinery_scan_timer is not None:
            self._refinery_scan_timer.stop()
            self._refinery_scan_timer = None

    # ── Refinery log monitor ──

    def _get_player_location(self) -> str:
        """Return the most recent player location reported by the log
        scanner (empty string if nothing has been observed yet).

        Used by :class:`RefineryLocationsTab` to rank refineries by
        proximity.  Kept as a small method so the tab doesn't need a
        direct reference to the ``RefineryMonitor``.
        """
        mon = self._refinery_monitor
        if mon is None:
            return ""
        return mon.current_location or ""

    def _start_refinery_monitor(self) -> None:
        """Start (or restart) the live refinery log monitor."""
        if self._refinery_monitor is not None:
            self._refinery_monitor.stop()
            self._refinery_monitor = None

        game_dir = self._config.get("game_dir", "")
        if not game_dir:
            self._refinery_status.setText("No game directory set — click 'Set Log Path'.")
            return

        from services.log_scanner import RefineryMonitor

        self._refinery_monitor = RefineryMonitor(game_dir)
        self._refinery_monitor.subscribe(self._on_refinery_monitor_update)
        self._refinery_monitor.start()

    def _on_refinery_monitor_update(self, results: list[dict]) -> None:
        """Called from monitor bg thread — push to main thread."""
        QMetaObject.invokeMethod(
            self, "_on_refinery_log_results",
            Qt.QueuedConnection,
            Q_ARG("QVariant", results),
        )

    @Slot("QVariant")
    def _on_refinery_log_results(self, results: list) -> None:
        """Handle log completion events — match to in-process orders."""
        from services.refinery_orders import match_log_completion

        # The monitor updates ``current_location`` on every log line it
        # sees (even non-refinery ones), so every dispatch from its
        # worker is a good cue to refresh the Locations tab when
        # "Near me" is active.
        loc_tab = getattr(self, "_refinery_locations_tab", None)
        if loc_tab is not None:
            loc_tab.notify_player_location_changed()

        self._refinery_raw_results = results
        changed = False

        for event in results:
            eid = event.get("id", "")
            if eid in self._refinery_deleted:
                continue
            # Try to match to an in-process OCR order
            matched_ids = match_log_completion(self._refinery_order_store, event)
            for oid in matched_ids:
                self._refinery_order_store.complete_order(
                    oid, event["timestamp"], eid
                )
                changed = True

            # If no match, create a standalone complete entry (if not already tracked)
            if not matched_ids and not self._refinery_order_store.get_order(eid):
                self._refinery_order_store.add_log_only_completion(event)
                changed = True

        if changed:
            self._persist_refinery_orders()
            self._refresh_refinery_tables()

    def _on_refinery_set_dir(self) -> None:
        """Let the user pick the Star Citizen LIVE directory."""
        current = self._config.get("game_dir", "")
        path = QFileDialog.getExistingDirectory(
            self, "Select Star Citizen LIVE Directory", current,
        )
        if path:
            self._config["game_dir"] = path
            _save_config(self._config)
            self._start_refinery_monitor()

    # ── Refinery order actions ──

    def _on_refinery_order_clicked(self, row_data: dict) -> None:
        """Open detail popup for clicked order."""
        oid = row_data.get("id")
        if not oid:
            return
        order = self._refinery_order_store.get_order(oid)
        if not order:
            return
        from ui.refinery_popup import RefineryOrderPopup
        popup = RefineryOrderPopup(order, self._refinery_order_store, self)
        popup.order_changed.connect(self._on_refinery_order_changed)
        popup.show()

    def _on_refinery_order_changed(self) -> None:
        """Called when popup modifies an order (rename etc)."""
        self._persist_refinery_orders()
        self._refresh_refinery_tables()

    def _on_refinery_rename(self) -> None:
        """Rename the selected in-process order via inline dialog."""
        row = self._refinery_ip_table.get_selected_row()
        if not row:
            return
        oid = row.get("id")
        order = self._refinery_order_store.get_order(oid) if oid else None
        if not order:
            return
        from PySide6.QtWidgets import QInputDialog
        new_name, ok = QInputDialog.getText(
            self, "Rename Order", "New name:", text=order.name,
        )
        if ok and new_name.strip():
            self._refinery_order_store.rename_order(oid, new_name.strip())
            self._persist_refinery_orders()
            self._refresh_refinery_tables()

    def _on_refinery_delete_ip(self) -> None:
        row = self._refinery_ip_table.get_selected_row()
        if row and row.get("id"):
            self._refinery_order_store.delete_order(row["id"])
            self._persist_refinery_orders()
            self._refresh_refinery_tables()

    def _on_refinery_delete_cp(self) -> None:
        row = self._refinery_cp_table.get_selected_row()
        if row and row.get("id"):
            self._refinery_order_store.delete_order(row["id"])
            self._persist_refinery_orders()
            self._refresh_refinery_tables()

    def _on_refinery_clear_complete(self) -> None:
        for order in self._refinery_order_store.get_complete():
            self._refinery_order_store.delete_order(order.id)
        self._persist_refinery_orders()
        self._refresh_refinery_tables()

    def _on_refinery_mark_picked_up(self) -> None:
        """Move selected complete order to Picked Up."""
        row = self._refinery_cp_table.get_selected_row()
        if row and row.get("id"):
            self._refinery_order_store.pickup_order(row["id"])
            self._persist_refinery_orders()
            self._refresh_refinery_tables()

    def _on_refinery_delete_pu(self) -> None:
        row = self._refinery_pu_table.get_selected_row()
        if row and row.get("id"):
            self._refinery_order_store.delete_order(row["id"])
            self._persist_refinery_orders()
            self._refresh_refinery_tables()

    def _on_refinery_clear_picked_up(self) -> None:
        for order in self._refinery_order_store.get_picked_up():
            self._refinery_order_store.delete_order(order.id)
        self._persist_refinery_orders()
        self._refresh_refinery_tables()

    def _on_fleet_admiral_view(self) -> None:
        """Open the Mining Foreman Console — full consumable/gadget management popup."""
        if hasattr(self, "_admiral_popup") and self._admiral_popup:
            try:
                self._admiral_popup.close()
            except RuntimeError:
                pass

        popup = QWidget(None, Qt.WindowStaysOnTopHint | Qt.Tool | Qt.FramelessWindowHint)
        popup.setAttribute(Qt.WA_DeleteOnClose)
        popup.destroyed.connect(lambda: setattr(self, "_admiral_popup", None))
        self._admiral_popup = popup

        popup._drag_pos = None

        def _mp(event):
            if event.button() == Qt.LeftButton:
                popup._drag_pos = event.globalPosition().toPoint() - popup.frameGeometry().topLeft()

        def _mm(event):
            if popup._drag_pos and event.buttons() & Qt.LeftButton:
                popup.move(event.globalPosition().toPoint() - popup._drag_pos)

        popup.mousePressEvent = _mp
        popup.mouseMoveEvent = _mm

        popup.setFixedWidth(400)
        outer = QVBoxLayout(popup)
        outer.setContentsMargins(0, 0, 0, 0)

        frame = QFrame(popup)
        frame.setObjectName("admiral_frame")
        frame.setStyleSheet(
            f"QFrame#admiral_frame {{ background: {P.bg_card}; "
            f"border: 1px solid {ACCENT}; border-radius: 4px; }}"
        )
        fl = QVBoxLayout(frame)
        fl.setContentsMargins(12, 12, 12, 12)
        fl.setSpacing(6)

        _ns = f"background: transparent; border: none;"
        _spin_style = (
            f"QSpinBox {{ font-family: Consolas; font-size: 8pt; color: {P.fg}; "
            f"background: {P.bg_card}; border: 1px solid {P.border}; border-radius: 3px; }}"
            f"QSpinBox::up-button, QSpinBox::down-button {{ width: 14px; border: none; "
            f"background: {P.bg_secondary}; }}"
            f"QSpinBox::up-arrow {{ border-left: 3px solid transparent; "
            f"border-right: 3px solid transparent; border-bottom: 4px solid {ACCENT}; }}"
            f"QSpinBox::down-arrow {{ border-left: 3px solid transparent; "
            f"border-right: 3px solid transparent; border-top: 4px solid {ACCENT}; }}"
        )

        # Header + close
        hdr = QWidget(frame)
        hl = QHBoxLayout(hdr)
        hl.setContentsMargins(0, 0, 0, 0)
        title = QLabel("Mining Foreman Console", hdr)
        title.setStyleSheet(
            f"font-family: Electrolize, Consolas; font-size: 11pt; "
            f"font-weight: bold; color: {ACCENT}; {_ns}"
        )
        hl.addWidget(title)
        hl.addStretch(1)
        close_btn = QPushButton("\u2716", hdr)
        close_btn.setFixedSize(32, 28)
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setStyleSheet(_CLOSE_BTN_STYLE)
        close_btn.clicked.connect(popup.close)
        hl.addWidget(close_btn)
        fl.addWidget(hdr)

        # Refresh All buttons
        btn_row = QWidget(frame)
        br_layout = QHBoxLayout(btn_row)
        br_layout.setContentsMargins(0, 4, 0, 4)
        br_layout.setSpacing(6)

        ref_mods = QPushButton("Refresh All Modules", btn_row)
        ref_mods.setCursor(Qt.PointingHandCursor)
        ref_mods.setStyleSheet(
            f"QPushButton {{ font-family: Consolas; font-size: 8pt; font-weight: bold; "
            f"color: {ACCENT}; {_ns} border: 1px solid {ACCENT}; border-radius: 3px; "
            f"padding: 3px 8px; }}"
        )
        def _refresh_mods():
            pos = popup.pos()
            self._replenish_all_modules()
            popup.close()
            self._on_fleet_admiral_view()
            # Restore position of the new popup
            if hasattr(self, '_fleet_admiral_popup') and self._fleet_admiral_popup:
                self._fleet_admiral_popup.move(pos)

        ref_mods.clicked.connect(_refresh_mods)
        br_layout.addWidget(ref_mods)

        ref_gad = QPushButton("Refresh All Gadgets", btn_row)
        ref_gad.setCursor(Qt.PointingHandCursor)
        ref_gad.setStyleSheet(
            f"QPushButton {{ font-family: Consolas; font-size: 8pt; font-weight: bold; "
            f"color: #ffc107; {_ns} border: 1px solid #ffc107; border-radius: 3px; "
            f"padding: 3px 8px; }}"
        )

        def _refresh_gads():
            pos = popup.pos()
            self._replenish_all_gadgets()
            popup.close()
            self._on_fleet_admiral_view()
            if hasattr(self, '_fleet_admiral_popup') and self._fleet_admiral_popup:
                self._fleet_admiral_popup.move(pos)

        ref_gad.clicked.connect(_refresh_gads)
        br_layout.addWidget(ref_gad)
        br_layout.addStretch(1)
        fl.addWidget(btn_row)

        # ── Gadgets section (yellow) ──
        g_hdr = QLabel("Gadgets", frame)
        g_hdr.setStyleSheet(
            f"font-family: Consolas; font-size: 9pt; font-weight: bold; "
            f"color: #ffc107; {_ns} padding-top: 4px;"
        )
        fl.addWidget(g_hdr)

        quantities = self._config.get("gadget_quantities", {})
        gadgets_db = get_gadget_list()
        for name in sorted(gadgets_db.keys()):
            row = QWidget(frame)
            rl = QHBoxLayout(row)
            rl.setContentsMargins(8, 0, 0, 0)
            rl.setSpacing(6)

            lbl = QLabel(name, row)
            lbl.setFixedWidth(100)
            lbl.setStyleSheet(f"font-family: Consolas; font-size: 8pt; color: #ffc107; {_ns}")
            rl.addWidget(lbl)

            spin = QSpinBox(row)
            spin.setRange(0, 99)
            spin.setValue(quantities.get(name, 0))
            spin.setFixedWidth(60)
            spin.setStyleSheet(_spin_style)
            spin.valueChanged.connect(
                lambda val, n=name: self._on_gadget_qty_changed(n, val)
            )
            rl.addWidget(spin)
            rl.addStretch(1)
            fl.addWidget(row)

        # ── Modules section (green) per ship ──
        configs = self.active_laser_configs()
        ships_seen: set[str] = set()
        has_modules = False
        for c in configs:
            if not c.ship_id or c.active_module_uses == 0:
                continue
            has_modules = True
            if c.ship_id not in ships_seen:
                ships_seen.add(c.ship_id)
                s_hdr = QLabel(c.ship_display, frame)
                s_hdr.setStyleSheet(
                    f"font-family: Consolas; font-size: 9pt; font-weight: bold; "
                    f"color: {ACCENT}; {_ns} padding-top: 6px;"
                )
                fl.addWidget(s_hdr)

            row = QWidget(frame)
            rl = QHBoxLayout(row)
            rl.setContentsMargins(8, 0, 0, 0)
            rl.setSpacing(6)

            color = ACCENT if c.active_uses_remaining > 0 else "#ff4444"
            turret_text = f"T{c.turret_index+1}"
            if c.active_module_names:
                turret_text += f": {c.active_module_names}"
            lbl = QLabel(turret_text, row)
            lbl.setFixedWidth(200)
            lbl.setStyleSheet(f"font-family: Consolas; font-size: 8pt; color: {color}; {_ns}")
            rl.addWidget(lbl)

            spin = QSpinBox(row)
            spin.setRange(0, c.active_module_uses)
            spin.setValue(c.active_uses_remaining)
            spin.setFixedWidth(60)
            spin.setStyleSheet(_spin_style)
            spin.valueChanged.connect(
                lambda val, sid=c.ship_id, tidx=c.turret_index: self._set_module_uses(sid, tidx, val)
            )
            rl.addWidget(spin)

            max_lbl = QLabel(f"/ {c.active_module_uses}", row)
            max_lbl.setStyleSheet(f"font-family: Consolas; font-size: 8pt; color: {P.fg_dim}; {_ns}")
            rl.addWidget(max_lbl)
            rl.addStretch(1)
            fl.addWidget(row)

        if not has_modules:
            no_mods = QLabel("No active modules in fleet", frame)
            no_mods.setStyleSheet(f"font-family: Consolas; font-size: 8pt; color: {P.fg_dim}; {_ns}")
            fl.addWidget(no_mods)

        outer.addWidget(frame)
        popup.adjustSize()
        popup.move(self.mapToGlobal(self.rect().center()) - popup.rect().center())
        self._fleet_admiral_popup = popup
        popup.show()

    def _on_always_best_changed(self, state: int) -> None:
        self._config["always_use_best_gadget"] = bool(state)
        _save_config(self._config)
        # Refresh the inline breakability result immediately
        self._on_break_inputs_changed()

    def _on_gadget_qty_changed(self, name: str, value: int) -> None:
        self._config.setdefault("gadget_quantities", {})[name] = value
        _save_config(self._config)
        self._update_consumables_display()

    def _refresh_gadget_spinboxes(self) -> None:
        """Sync spinbox values from config (e.g. after auto-decrement)."""
        quantities = self._config.get("gadget_quantities", {})
        for name, spin in self._gadget_spinboxes.items():
            try:
                spin.blockSignals(True)
                spin.setValue(quantities.get(name, 0))
                spin.blockSignals(False)
            except RuntimeError:
                pass

    # ── Fleet handlers ──

    def _on_fleet_add_ship(self) -> None:
        """Open file picker to add one or more ships to the fleet."""
        default_dir = self._guess_mining_loadout_dir()
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Add Ships to Fleet", default_dir,
            "Mining Loadout (*.json);;All files (*.*)",
        )
        if not paths:
            return
        added = 0
        existing_paths = {os.path.normpath(s.source_path) for s in self._fleet_snapshots}
        for path in paths:
            norm = os.path.normpath(path)
            if norm in existing_paths:
                log.info("Fleet: skipping duplicate %s", os.path.basename(path))
                continue
            snap = load_loadout_file(path)
            if snap is not None:
                self._fleet_snapshots.append(snap)
                self._config.setdefault("fleet_loadouts", []).append(path)
                existing_paths.add(norm)
                added += 1
        if added:
            _save_config(self._config)
            self._update_fleet_label()
            self._ledger_tab.refresh_fleet_panel()
            log.info("Added %d ship(s) to fleet", added)

    def _on_fleet_clear(self) -> None:
        """Remove all ships from the fleet."""
        self._fleet_snapshots.clear()
        self._config["fleet_loadouts"] = []
        if self._config.get("active_ship") == "fleet":
            self._config["active_ship"] = None
            self._update_ship_button_label()
        _save_config(self._config)
        self._update_fleet_label()
        self._ledger_tab.refresh_fleet_panel()

    def _on_fleet_expand(self) -> None:
        """Show a scrollable popup with the full fleet details."""
        if not self._fleet_snapshots:
            return

        dialog = QWidget(self, Qt.Popup | Qt.FramelessWindowHint)
        dialog.setAttribute(Qt.WA_TranslucentBackground)
        dialog.setAttribute(Qt.WA_DeleteOnClose)

        outer = QVBoxLayout(dialog)
        outer.setContentsMargins(1, 1, 1, 1)

        frame = QFrame(dialog)
        frame.setStyleSheet(
            f"QFrame {{ background: {P.bg_card}; border: 1px solid {ACCENT}; "
            f"border-radius: 4px; }}"
        )
        frame_layout = QVBoxLayout(frame)
        frame_layout.setContentsMargins(10, 10, 10, 10)
        frame_layout.setSpacing(6)

        # Header row
        hdr = QWidget(frame)
        hdr_layout = QHBoxLayout(hdr)
        hdr_layout.setContentsMargins(0, 0, 0, 0)
        title = QLabel(f"Mining Ops Fleet ({len(self._fleet_snapshots)} ships)", hdr)
        title.setStyleSheet(
            f"font-family: Electrolize, Consolas, monospace; font-size: 10pt; "
            f"font-weight: bold; color: {ACCENT}; background: transparent;"
        )
        hdr_layout.addWidget(title)
        hdr_layout.addStretch(1)

        edit_btn = QPushButton("Add Ship", hdr)
        edit_btn.setCursor(Qt.PointingHandCursor)
        edit_btn.setStyleSheet(
            f"QPushButton {{ font-family: Consolas; font-size: 8pt; font-weight: bold; "
            f"color: {ACCENT}; background: transparent; border: 1px solid {ACCENT}; "
            f"border-radius: 3px; padding: 3px 8px; }}"
        )
        edit_btn.clicked.connect(lambda: (self._on_fleet_add_ship(), dialog.close()))
        hdr_layout.addWidget(edit_btn)

        frame_layout.addWidget(hdr)

        # Scrollable ship list
        scroll = QScrollArea(frame)
        scroll.setWidgetResizable(True)
        scroll.setMaximumHeight(400)
        scroll.setStyleSheet(
            f"QScrollArea {{ border: none; background: transparent; }}"
        )

        content = QWidget(scroll)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(8)

        for i, snap in enumerate(self._fleet_snapshots):
            ship_block = QWidget(content)
            sb_layout = QHBoxLayout(ship_block)
            sb_layout.setContentsMargins(0, 0, 0, 0)
            sb_layout.setSpacing(8)

            # Ship loadout details
            detail = QLabel("", ship_block)
            detail.setWordWrap(True)
            detail.setTextFormat(Qt.RichText)
            lines = self._format_snap_hierarchy(snap)
            detail.setText(lines)
            detail.setStyleSheet(
                f"font-family: Consolas, monospace; font-size: 8pt; "
                f"color: {P.fg_dim}; background: transparent;"
            )
            sb_layout.addWidget(detail, 1)

            # Delete button
            del_btn = QPushButton("x", ship_block)
            del_btn.setCursor(Qt.PointingHandCursor)
            del_btn.setFixedSize(32, 28)
            del_btn.setStyleSheet(_CLOSE_BTN_STYLE)
            del_btn.clicked.connect(
                lambda _=False, idx=i, dlg=dialog: self._on_fleet_delete_ship(idx, dlg)
            )
            sb_layout.addWidget(del_btn)

            content_layout.addWidget(ship_block)

        content_layout.addStretch(1)
        scroll.setWidget(content)
        frame_layout.addWidget(scroll, 1)
        outer.addWidget(frame)

        dialog.setFixedWidth(400)
        dialog.adjustSize()
        dialog.move(self.mapToGlobal(self.rect().center()))
        dialog.show()

    def _on_fleet_crew_changed(self, ship_path: str, crew: int) -> None:
        """Update the player count for a fleet ship."""
        self._config.setdefault("fleet_player_counts", {})[ship_path] = crew
        _save_config(self._config)
        log.info("Fleet crew for %s set to %d", os.path.basename(ship_path), crew)

    def _on_fleet_delete_ship(self, index: int, dialog: QWidget) -> None:
        """Remove a ship from the fleet by index."""
        if 0 <= index < len(self._fleet_snapshots):
            self._fleet_snapshots.pop(index)
            paths = self._config.get("fleet_loadouts", [])
            if 0 <= index < len(paths):
                paths.pop(index)
            _save_config(self._config)
            self._update_fleet_label()
            self._ledger_tab.refresh_fleet_panel()
        dialog.close()

    def _restore_fleet_loadouts(self) -> None:
        """Reload fleet ships from config at startup (deduplicates)."""
        paths = self._config.get("fleet_loadouts", [])
        self._fleet_snapshots.clear()
        valid_paths: list[str] = []
        seen: set[str] = set()
        for path in paths:
            norm = os.path.normpath(path)
            if norm in seen:
                continue
            snap = load_loadout_file(path)
            if snap:
                self._fleet_snapshots.append(snap)
                valid_paths.append(path)
                seen.add(norm)
        self._config["fleet_loadouts"] = valid_paths
        _save_config(self._config)
        self._update_fleet_label()

    def _update_fleet_label(self) -> None:
        """Update the compact fleet names display (first 5)."""
        if not self._fleet_snapshots:
            self._fleet_names_label.setText(
                f'<span style="color:{P.fg_dim};">(no ships in fleet)</span>'
            )
            return
        display_names: list[str] = []
        for i, s in enumerate(self._fleet_snapshots[:5]):
            name = self._fleet_display_name(s)
            if i == 0:
                name = f"[YOU] {name}"
            display_names.append(name)
        remaining = len(self._fleet_snapshots) - 5
        text = ", ".join(display_names)
        if remaining > 0:
            text += f" +{remaining} more"
        self._fleet_names_label.setText(
            f'<span style="color:{ACCENT};">{text}</span>'
        )

    @staticmethod
    def _fleet_display_name(snap: LoadoutSnapshot) -> str:
        """Format a fleet ship as 'filename (SHIP_TYPE)'."""
        fname = os.path.splitext(os.path.basename(snap.source_path))[0]
        return f"{fname} ({snap.ship})"

    # ── Salvage ships ──

    def _on_salvage_add_ship(self) -> None:
        """Open file picker to load a DPS Calculator loadout as a salvage ship."""
        default_dir = self._guess_mining_loadout_dir()
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Add Salvage Ship (DPS Calculator JSON)", default_dir,
            "DPS Calculator Loadout (*.json);;All files (*.*)",
        )
        if not paths:
            return
        added = 0
        existing_paths = {
            os.path.normpath(s.source_path) for s in self._salvage_snapshots
        }
        for path in paths:
            norm = os.path.normpath(path)
            if norm in existing_paths:
                log.info("Salvage: skipping duplicate %s", os.path.basename(path))
                continue
            snap = load_salvage_file(path)
            if snap is not None:
                self._salvage_snapshots.append(snap)
                self._config.setdefault("salvage_loadouts", []).append(path)
                existing_paths.add(norm)
                added += 1
        if added:
            _save_config(self._config)
            self._update_salvage_label()
            if hasattr(self, "_ledger_tab"):
                self._ledger_tab.refresh_fleet_panel()
            log.info("Added %d salvage ship(s)", added)

    def _on_salvage_clear(self) -> None:
        """Remove all salvage ships."""
        self._salvage_snapshots.clear()
        self._config["salvage_loadouts"] = []
        _save_config(self._config)
        self._update_salvage_label()
        if hasattr(self, "_ledger_tab"):
            self._ledger_tab.refresh_fleet_panel()

    def _restore_salvage_loadouts(self) -> None:
        """Reload salvage ships from config at startup (deduplicates)."""
        paths = self._config.get("salvage_loadouts", [])
        self._salvage_snapshots.clear()
        valid_paths: list[str] = []
        seen: set[str] = set()
        for path in paths:
            norm = os.path.normpath(path)
            if norm in seen:
                continue
            snap = load_salvage_file(path)
            if snap is not None:
                self._salvage_snapshots.append(snap)
                valid_paths.append(path)
                seen.add(norm)
        self._config["salvage_loadouts"] = valid_paths
        _save_config(self._config)
        self._update_salvage_label()

    def _update_salvage_label(self) -> None:
        """Update the compact salvage names display."""
        if not hasattr(self, "_salvage_names_label"):
            return
        if not self._salvage_snapshots:
            self._salvage_names_label.setText(
                f'<span style="color:{P.fg_dim};">(no salvage ships loaded)</span>'
            )
            return
        display_names: list[str] = []
        for s in self._salvage_snapshots[:8]:
            fname = os.path.splitext(os.path.basename(s.source_path))[0]
            display_names.append(f"{fname} ({s.ship})")
        remaining = len(self._salvage_snapshots) - 8
        text = ", ".join(display_names)
        if remaining > 0:
            text += f" +{remaining} more"
        self._salvage_names_label.setText(
            f'<span style="color:{ACCENT};">{text}</span>'
        )

    @staticmethod
    def _format_snap_hierarchy(snap: LoadoutSnapshot) -> str:
        """Format a snapshot's turret hierarchy as HTML."""
        PLACEHOLDER_NAMES = {
            "\u2014 No Laser \u2014", "\u2014 No Module \u2014", "\u2014 No Gadget \u2014",
            "— No Laser —", "— No Module —", "— No Gadget —",
        }
        fname = os.path.splitext(os.path.basename(snap.source_path))[0]
        lines: list[str] = [f'<b>{fname} ({snap.ship})</b>']
        for idx, turret in enumerate(snap.turrets):
            laser = turret.laser if turret.laser not in PLACEHOLDER_NAMES else "(empty)"
            lines.append(f'&nbsp;&nbsp;{laser}')
            for mod in turret.modules:
                if mod not in PLACEHOLDER_NAMES:
                    lines.append(f'&nbsp;&nbsp;&nbsp;&nbsp;{mod}')
        return "<br>".join(lines)

    def _restore_ship_loadouts(self) -> None:
        """Reload persisted loadout files at startup."""
        stored = self._config.get("ship_loadouts") or {}
        for slot_id, _ in SHIP_SLOTS:
            path = stored.get(slot_id)
            if not path:
                self._update_ship_slot_label(slot_id, None)
                continue
            snap = load_loadout_file(path)
            self._ship_snapshots[slot_id] = snap
            self._update_ship_slot_label(slot_id, snap)
            if snap is None:
                # File went missing — drop the stale reference
                self._config.setdefault("ship_loadouts", {})[slot_id] = None
        _save_config(self._config)

    # Placeholder module names from Mining_Loadout (skip in display)
    _PLACEHOLDER_NAMES = {
        "\u2014 No Laser \u2014", "\u2014 No Module \u2014", "\u2014 No Gadget \u2014",
        "— No Laser —", "— No Module —", "— No Gadget —",
    }

    def _update_ship_slot_label(self, slot_id: str, snap: LoadoutSnapshot | None) -> None:
        """Refresh the detail label for a ship slot.

        Renders a turret/module hierarchy using rich text:
            Laser Name
              Module 1
              Module 2
        """
        lbl = self._ship_slot_labels.get(slot_id)
        if lbl is None:
            return

        if snap is None:
            lbl.setText(
                f'<span style="color:{P.fg_dim};">(no loadout loaded)</span>'
            )
            return

        lines: list[str] = []
        for idx, turret in enumerate(snap.turrets):
            laser = turret.laser
            if laser in self._PLACEHOLDER_NAMES:
                laser = "(empty)"
            turret_label = _ml_turret_name(snap.ship, idx)
            lines.append(
                f'<span style="color:{ACCENT}; font-weight:bold;">'
                f'{turret_label}: {laser}</span>'
            )
            for mod in turret.modules:
                if mod in self._PLACEHOLDER_NAMES:
                    continue
                lines.append(
                    f'<span style="color:{P.fg_dim}; margin-left:16px;">'
                    f'&nbsp;&nbsp;&nbsp;&nbsp;{mod}</span>'
                )

        if snap.gadget and snap.gadget not in self._PLACEHOLDER_NAMES:
            lines.append(
                f'<span style="color:{ACCENT};">Gadget: {snap.gadget}</span>'
            )

        lbl.setText("<br>".join(lines) if lines else "(empty loadout)")

    def _update_ship_button_label(self) -> None:
        """Update the 'Choose Mining Ship' button to reflect active selection."""
        active = self._config.get("active_ship")
        if active == "fleet":
            self._btn_choose_ship.setText(f"Ship: Fleet ({len(self._fleet_snapshots)})")
        elif active:
            display = dict(SHIP_SLOTS).get(active, active.title())
            self._btn_choose_ship.setText(f"Ship: {display}")
        else:
            self._btn_choose_ship.setText("Choose Mining Ship")

    # ── Ship slot handlers ──

    def _on_load_ship_loadout(self, slot_id: str, ship_label: str) -> None:
        """Open a file picker and load a Mining Loadout JSON for one slot."""
        # Default to the Mining_Loadout tool's config location if it exists
        default_dir = self._guess_mining_loadout_dir()
        path, _ = QFileDialog.getOpenFileName(
            self,
            f"Load {ship_label} Loadout",
            default_dir,
            "Mining Loadout (*.json);;All files (*.*)",
        )
        if not path:
            return

        snap = load_loadout_file(path)
        if snap is None:
            self._ocr_status.setText(f"Failed to load loadout: {os.path.basename(path)}")
            return

        self._ship_snapshots[slot_id] = snap
        self._config.setdefault("ship_loadouts", {})[slot_id] = snap.source_path
        _save_config(self._config)
        self._update_ship_slot_label(slot_id, snap)
        self._ledger_tab.refresh_fleet_panel()
        log.info("Loaded %s loadout: %s", ship_label, snap.source_path)

    def _on_clear_ship_loadout(self, slot_id: str) -> None:
        """Unload a ship slot."""
        self._ship_snapshots[slot_id] = None
        self._config.setdefault("ship_loadouts", {})[slot_id] = None
        # If the cleared slot was the active selection, drop that too
        if self._config.get("active_ship") == slot_id:
            self._config["active_ship"] = None
            self._update_ship_button_label()
        _save_config(self._config)
        self._update_ship_slot_label(slot_id, None)
        self._ledger_tab.refresh_fleet_panel()

    @staticmethod
    def _guess_mining_loadout_dir() -> str:
        """Return the directory where Mining_Loadout exports loadout files.

        Mining_Loadout saves/loads exports to ~/Documents/SC Loadouts/.
        Falls back to the user's Documents folder if it doesn't exist yet.
        """
        sc_loadouts = os.path.join(os.path.expanduser("~"), "Documents", "SC Loadouts")
        if os.path.isdir(sc_loadouts):
            return sc_loadouts
        return os.path.join(os.path.expanduser("~"), "Documents")

    # ── Choose Mining Ship popup ──

    def _on_toggle_calc_mode(self, checked: bool) -> None:
        mode = "team" if checked else "fleet"
        self._config["calc_mode"] = mode
        self._btn_calc_mode.setText(f"Calc: {'Team' if checked else 'Fleet'}")
        _save_config(self._config)
        self._update_break_bubble()
        self._refresh_break_panel()

    def team_laser_configs(self, team_node) -> list:
        """Resolve LaserConfigs for all mining ships in a specific team."""
        configs = []
        if team_node is None:
            return configs
        ships = self._ledger_tab._scene.ships_in_team(team_node)
        player_counts = self._config.get("fleet_player_counts", {})
        module_uses = self._config.get("module_uses_remaining", {})
        team_name = getattr(team_node, "team_name", "") or ""
        cluster = getattr(team_node, "cluster", "") or ""

        for ship_node in ships:
            if not ship_node.loadout_path:
                continue
            snap = load_loadout_file(ship_node.loadout_path)
            if snap is None:
                continue
            ship_configs = snapshot_to_laser_configs(snap)
            display_name = ship_node.ship_name
            crew = player_counts.get(snap.source_path, default_player_count(snap.ship))
            ship_uses = module_uses.get(snap.source_path)
            ship_crew = list(getattr(ship_node, "crew", []) or [])

            for idx, c in enumerate(ship_configs):
                c.name = f"{display_name} > {c.name}"
                c.ship_id = snap.source_path
                c.ship_display = display_name
                c.ship_type = snap.ship
                c.player_count = crew
                c.turret_index = idx
                c.team_name = team_name
                c.cluster = cluster
                c.player_names = list(ship_crew)
                if ship_uses and idx < len(ship_uses):
                    c.active_uses_remaining = ship_uses[idx]
                else:
                    c.active_uses_remaining = c.active_module_uses
            configs.extend(ship_configs)
        return configs

    def _on_choose_mining_ship(self) -> None:
        """Show a compact popup with the three ship options.

        Uses a plain QWidget with the Popup flag instead of QDialog.exec()
        so that clicking outside the popup simply closes it (no error).
        """
        # Close any existing popup first (guard against dangling C++ pointer
        # left behind by WA_DeleteOnClose)
        try:
            if hasattr(self, "_ship_popup") and self._ship_popup is not None:
                self._ship_popup.close()
        except RuntimeError:
            pass
        self._ship_popup = None

        popup = QWidget(self, Qt.Popup | Qt.FramelessWindowHint)
        popup.setAttribute(Qt.WA_TranslucentBackground)
        popup.setAttribute(Qt.WA_DeleteOnClose)
        popup.destroyed.connect(lambda: setattr(self, "_ship_popup", None))
        self._ship_popup = popup

        outer = QVBoxLayout(popup)
        outer.setContentsMargins(1, 1, 1, 1)

        frame = QFrame(popup)
        frame.setStyleSheet(
            f"QFrame {{ background: {P.bg_card}; border: 1px solid {ACCENT}; "
            f"border-radius: 4px; }}"
        )
        frame_layout = QVBoxLayout(frame)
        frame_layout.setContentsMargins(10, 10, 10, 10)
        frame_layout.setSpacing(6)

        title = QLabel("Choose Mining Ship", frame)
        title.setStyleSheet(
            f"font-family: Electrolize, Consolas, monospace; font-size: 10pt; "
            f"font-weight: bold; color: {ACCENT}; background: transparent; "
            f"padding-bottom: 4px;"
        )
        frame_layout.addWidget(title)

        btn_style_enabled = f"""
            QPushButton {{
                font-family: Consolas, monospace; font-size: 9pt;
                font-weight: bold; color: {ACCENT}; background: transparent;
                border: 1px solid {ACCENT}; border-radius: 3px;
                padding: 6px 14px; text-align: left;
            }}
            QPushButton:hover {{ background: rgba(51, 221, 136, 0.18); }}
        """
        btn_style_disabled = f"""
            QPushButton {{
                font-family: Consolas, monospace; font-size: 9pt;
                color: {P.fg_dim}; background: transparent;
                border: 1px solid {P.border}; border-radius: 3px;
                padding: 6px 14px; text-align: left;
            }}
        """

        for slot_id, ship_label in SHIP_SLOTS:
            snap = self._ship_snapshots.get(slot_id)
            has_loadout = snap is not None
            if has_loadout:
                text = f"{ship_label}  \u2014  {os.path.basename(snap.source_path)}"
            else:
                text = f"{ship_label}  (no loadout \u2014 load one first)"

            btn = QPushButton(text, frame)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setEnabled(has_loadout)
            btn.setStyleSheet(btn_style_enabled if has_loadout else btn_style_disabled)
            btn.clicked.connect(
                lambda _=False, sid=slot_id, pw=popup: self._on_ship_picked(sid, pw)
            )
            frame_layout.addWidget(btn)

        # Fleet option
        has_fleet = len(self._fleet_snapshots) > 0
        fleet_text = f"Mining Ops Fleet  ({len(self._fleet_snapshots)} ships)" if has_fleet else "Mining Ops Fleet  (empty)"
        fleet_btn = QPushButton(fleet_text, frame)
        fleet_btn.setCursor(Qt.PointingHandCursor)
        fleet_btn.setEnabled(has_fleet)
        fleet_btn.setStyleSheet(btn_style_enabled if has_fleet else btn_style_disabled)
        fleet_btn.clicked.connect(
            lambda _=False, pw=popup: self._on_ship_picked("fleet", pw)
        )
        frame_layout.addWidget(fleet_btn)

        outer.addWidget(frame)

        # Position the popup directly under the Choose Mining Ship button
        btn_pos = self._btn_choose_ship.mapToGlobal(self._btn_choose_ship.rect().bottomLeft())
        popup.adjustSize()
        popup.move(btn_pos)
        popup.show()

    def _on_ship_picked(self, slot_id: str, popup: QWidget) -> None:
        """Record the selected ship and close the popup."""
        self._config["active_ship"] = slot_id
        _save_config(self._config)
        self._update_ship_button_label()
        self._refresh_break_panel()
        log.info("Active mining ship set to: %s", slot_id)
        popup.close()

    def _active_loadout_snapshot(self) -> LoadoutSnapshot | None:
        """Return the currently active ship's parsed loadout, or None."""
        active = self._config.get("active_ship")
        if not active:
            return None
        return self._ship_snapshots.get(active)

    def active_laser_configs(self) -> list[LaserConfig]:
        """Return resolved LaserConfig objects for the currently active ship/fleet.

        When fleet mode is active, concatenates configs from all fleet
        ships with ship-name prefixes on each turret for identification.
        """
        active = self._config.get("active_ship")

        if active == "fleet":
            configs: list[LaserConfig] = []
            player_counts = self._config.get("fleet_player_counts", {})
            module_uses = self._config.get("module_uses_remaining", {})
            for snap in self._fleet_snapshots:
                ship_configs = snapshot_to_laser_configs(snap)
                display_name = self._fleet_display_name(snap)
                crew = player_counts.get(
                    snap.source_path,
                    default_player_count(snap.ship),
                )
                # Get or initialize remaining module uses for this ship
                ship_uses = module_uses.get(snap.source_path)
                for idx, c in enumerate(ship_configs):
                    c.name = f"{display_name} > {c.name}"
                    c.ship_id = snap.source_path
                    c.ship_display = display_name
                    c.ship_type = snap.ship
                    c.player_count = crew
                    c.turret_index = idx
                    # Set remaining uses from config, or initialize from UEX max
                    if ship_uses and idx < len(ship_uses):
                        c.active_uses_remaining = ship_uses[idx]
                    else:
                        c.active_uses_remaining = c.active_module_uses
                configs.extend(ship_configs)
            return configs

        snap = self._active_loadout_snapshot()
        if snap is None:
            return []
        ship_configs = snapshot_to_laser_configs(snap)
        # Single ship mode: also populate uses remaining
        module_uses = self._config.get("module_uses_remaining", {})
        ship_uses = module_uses.get(snap.source_path) if snap else None
        for idx, c in enumerate(ship_configs):
            c.turret_index = idx
            c.ship_id = snap.source_path
            if ship_uses and idx < len(ship_uses):
                c.active_uses_remaining = ship_uses[idx]
            else:
                c.active_uses_remaining = c.active_module_uses
        return ship_configs

    def _setup_ipc(self) -> None:
        """Set up IPC polling for launcher commands."""
        if self._cmd_file:
            self._ipc = IPCWatcher(self._cmd_file, parent=self)
            self._ipc.command_received.connect(self._on_ipc_command)
            self._ipc.start()

    def _on_ipc_command(self, cmd: dict) -> None:
        cmd_type = cmd.get("type", "")
        log.debug("IPC command received: %s", cmd_type)
        if cmd_type in ("show", "activate", "raise"):
            # Ensure the window is on a visible screen before showing
            geom = self.geometry()
            self.restore_geometry_from_args(
                geom.x(), geom.y(), geom.width(), geom.height(),
                self.windowOpacity(),
            )
            if self.isMinimized():
                self.showNormal()
            else:
                self.show()
            self.raise_()
            self.activateWindow()
        elif cmd_type == "hide":
            self.hide()
        elif cmd_type == "toggle":
            self.toggle_visibility()
        elif cmd_type == "quit":
            QApplication.instance().quit()

    # ── Data loading ──

    def _on_data_loaded(self, rows: list[dict]) -> None:
        self._rows = rows
        self._matcher.update(rows)

        # Build table data with rarity-aware formatting
        table_data: list[dict] = []
        for row in rows:
            entry = dict(row)
            # Store numeric values for sorting
            for col in ("1", "2", "3", "4", "5", "6"):
                entry[col] = int(entry.get(col, 0))
            # Rarity becomes its sort index (int) so Qt sorts by custom order.
            # The display formatter maps the int back to the name.
            rarity_str = entry.get("rarity", "")
            entry["rarity"] = _rarity_key(rarity_str)
            # Keep the plain string under a separate key for filter logic
            # and the row-color delegate
            entry["_rarity_name"] = rarity_str
            table_data.append(entry)

        # Keep the full dataset so filters can be reapplied
        self._all_table_data = table_data
        self._apply_filters()
        self._status_label.setText(f"{len(rows)} resources loaded")
        log.info("UI: loaded %d resources", len(rows))

    def _on_data_error(self, msg: str) -> None:
        self._status_label.setText(f"Error: {msg}")
        log.warning("UI: data load error: %s", msg)

    # ── Search / filter ──

    @staticmethod
    def _fuzzy_score(query: str, target: str) -> float:
        """Return a fuzzy match score in [0.0, 1.0] for *query* against *target*.

        Scoring rules:
          - 1.0 for exact match
          - 0.9 if target starts with query
          - 0.7 if query is a substring of target
          - 0.5 * ratio if all query chars appear in target in order
          - 0.0 otherwise
        """
        if not query:
            return 1.0
        q = query.lower()
        t = target.lower()
        if q == t:
            return 1.0
        if t.startswith(q):
            return 0.9
        if q in t:
            return 0.7
        # Subsequence match: all query chars appear in order within target
        ti = 0
        matched = 0
        for ch in q:
            found = t.find(ch, ti)
            if found == -1:
                return 0.0
            ti = found + 1
            matched += 1
        # Reward tight matches (fewer skipped chars = higher score)
        ratio = matched / max(ti, 1)
        return 0.5 * ratio

    @staticmethod
    def _value_fuzzy_score(query: str, row: dict) -> float:
        """Return the best fuzzy score for *query* against *row*'s signal values.

        Scoring checks every rock column (1-15+) and returns the highest match:
          - 1.0 if any column exactly equals the query
          - 0.95 if any column's digits start with the query
          - 0.85 if the query is a contiguous substring of any column
          - 0.70 if the query is close numerically (within 5%) to any column
          - 0.0 otherwise
        """
        if not query.isdigit():
            return 0.0
        q = query
        try:
            q_int = int(query)
        except ValueError:
            return 0.0

        best = 0.0
        for key, raw in row.items():
            if key in ("name", "rarity"):
                continue
            try:
                col_val = int(raw)
            except (TypeError, ValueError):
                continue
            if col_val <= 0:
                continue

            s = str(col_val)
            # Exact match
            if s == q:
                return 1.0
            # Prefix match
            if s.startswith(q):
                best = max(best, 0.95)
                continue
            # Substring match
            if q in s:
                best = max(best, 0.85)
                continue
            # Numerical proximity (within 5% of the column value)
            tolerance = max(50, int(col_val * 0.05))
            if abs(col_val - q_int) <= tolerance:
                best = max(best, 0.70)

        return best

    def _apply_filters(self) -> None:
        """Filter the table based on the current value and name search inputs."""
        if not self._all_table_data:
            return

        value_text = self._search_input.text().strip()
        name_text = self._name_input.text().strip()

        filtered = self._all_table_data

        # Value filter: fuzzy digit matching across all rock columns
        if value_text:
            scored_vals = []
            for row in filtered:
                score = self._value_fuzzy_score(value_text, row)
                if score > 0.0:
                    scored_vals.append((score, row))
            scored_vals.sort(key=lambda sr: (-sr[0], sr[1]["name"]))
            filtered = [row for _, row in scored_vals]

        # Name filter: fuzzy matching
        if name_text:
            scored = []
            for row in filtered:
                score = self._fuzzy_score(name_text, row["name"])
                if score > 0.0:
                    scored.append((score, row))
            # Sort by score descending, then alphabetically
            scored.sort(key=lambda sr: (-sr[0], sr[1]["name"]))
            filtered = [row for _, row in scored]

        self._table.set_data(filtered)
        self._sync_table_min_width()

    def _on_search(self, text: str) -> None:
        """Handle value search input changes — updates result label and filters table."""
        text = text.strip()

        if not text:
            self._search_result.setText("")
            self._apply_filters()
            return

        try:
            value = int(text)
        except ValueError:
            self._search_result.setText("Enter a number")
            self._search_result.setStyleSheet(f"""
                font-family: Electrolize, Consolas, monospace;
                font-size: 9pt; font-weight: bold;
                color: {P.red}; background: transparent;
            """)
            return

        matches = self._matcher.match_all(value, tolerance=10)
        if matches:
            parts = []
            for m in matches:
                rock_word = "R" if m.rock_count == 1 else "R"
                parts.append(f"{m.name} ({m.rock_count}{rock_word})")
            color = RARITY_FG.get(matches[0].rarity, P.fg)
            self._search_result.setText(" | ".join(parts))
            self._search_result.setStyleSheet(f"""
                font-family: Electrolize, Consolas, monospace;
                font-size: 9pt; font-weight: bold;
                color: {color}; background: transparent;
            """)
        else:
            self._search_result.setText("No match")
            self._search_result.setStyleSheet(f"""
                font-family: Electrolize, Consolas, monospace;
                font-size: 9pt; font-weight: bold;
                color: {P.red}; background: transparent;
            """)

        self._apply_filters()

    def _on_name_search(self, text: str) -> None:
        """Handle name search input changes — filters table by fuzzy name match."""
        self._apply_filters()

    # ── OCR scanning ──

    def _update_ocr_status(self) -> None:
        region = self._config.get("ocr_region")
        status = tesseract_status()
        if status != "Ready":
            self._ocr_status.setText(status)
            # Still allow scan toggle — Tesseract will auto-download on first scan
            self._btn_scan_toggle.setEnabled(region is not None)
        elif not region:
            self._ocr_status.setText("No scan region set")
            self._btn_scan_toggle.setEnabled(False)
        else:
            self._ocr_status.setText(
                f"Region: {region['x']},{region['y']} "
                f"{region['w']}x{region['h']}"
            )
            self._btn_scan_toggle.setEnabled(True)

    def _show_tutorial(self) -> None:
        self._tutorial = TutorialPopup(self)
        self._tutorial.show()

    def _show_chart_popout(self, data) -> None:
        """Open the Mining Chart in a floating singleton window.

        Called by :class:`MiningChartTab` when the user clicks the
        "Pop-out Chart" button. ``data`` is the already-loaded
        ``MiningChartData`` (or ``None`` if the chart is still loading).
        """
        chart_bubble.show_singleton(self, data)

    def _open_resource_popup(self, row: dict) -> None:
        """Open a detail popup for the clicked resource row."""
        if row:
            ResourcePopup(row, parent=self)

    def _on_set_region(self) -> None:
        self._region_selector = RegionSelector()
        self._region_selector.region_selected.connect(self._on_region_selected)
        self._region_selector.show()

    def _on_region_selected(self, region: dict) -> None:
        self._config["ocr_region"] = region
        _save_config(self._config)
        self._update_ocr_status()
        log.info("Scanning region set: %s", region)

    def _on_set_hud_region(self) -> None:
        """Open the region selector for the mining HUD (mass / resistance)."""
        self._hud_region_selector = RegionSelector()
        self._hud_region_selector.region_selected.connect(self._on_hud_region_selected)
        self._hud_region_selector.show()

    def _on_hud_region_selected(self, region: dict) -> None:
        self._config["hud_region"] = region
        _save_config(self._config)
        log.info("Mining HUD region set: %s", region)

    def _on_set_display(self) -> None:
        self._display_placer = DisplayPlacer()
        self._display_placer.position_selected.connect(self._on_display_selected)
        self._display_placer.show()

    def _on_display_selected(self, pos: dict) -> None:
        self._config["bubble_position"] = pos
        _save_config(self._config)
        log.info("Bubble display position set: (%d, %d)", pos["x"], pos["y"])

    def _on_set_break_display(self) -> None:
        from .display_placer import BreakBubblePlacer
        self._break_placer = BreakBubblePlacer()
        self._break_placer.position_selected.connect(self._on_break_display_selected)
        self._break_placer.show()

    def _on_break_display_selected(self, pos: dict) -> None:
        self._config["break_bubble_position"] = pos
        _save_config(self._config)
        log.info("Break bubble position set: (%d, %d)", pos["x"], pos["y"])

    def _on_scan_toggle(self, checked: bool) -> None:
        if checked:
            # Save expanded size before collapsing
            self._expanded_size = (self.width(), self.height())
            self._btn_scan_toggle.setText("Stop Scan")

            # Force the Scanner tab and hide the tab bar so the
            # collapsed view matches its pre-tabs appearance.
            self._tabs.setCurrentWidget(self._scanner_page)
            self._tabs.tabBar().setVisible(False)

            # Hide everything except title bar and the scan toggle row
            for w in self._expanded_widgets:
                w.setVisible(False)

            # Shrink window to just title bar + scan controls + inline result + hint
            self.setMinimumHeight(110)
            self.resize(self.width(), 110)

            # Reset consensus state
            self._last_ocr_value = None
            self._confirmed_value = None
            self._inline_result.setText("")
            self._scan_hint.setVisible(True)

            # Show "Scanning — Please Wait" bubble immediately
            bubble_pos = self._config.get("bubble_position")
            if bubble_pos:
                self._scan_bubble.show_scanning(bubble_pos["x"], bubble_pos["y"])
            else:
                region = self._config.get("ocr_region", {})
                self._scan_bubble.show_scanning(
                    region.get("x", 500) + region.get("w", 200) + 10,
                    region.get("y", 400),
                )

            # Start scanning
            interval = self._config.get("scan_interval_seconds", 1) * 1000
            self._scan_timer = QTimer(self)
            self._scan_timer.timeout.connect(self._do_scan)
            self._scan_timer.start(interval)
            self._do_scan()  # immediate first scan
        else:
            self._btn_scan_toggle.setText("Start Scan")
            if self._scan_timer:
                self._scan_timer.stop()
                self._scan_timer = None

            self._scan_hint.setVisible(False)

            # Restore expanded view
            for w in self._expanded_widgets:
                w.setVisible(True)
            # Restore the tab bar so the user can switch tabs again
            self._tabs.tabBar().setVisible(True)

            self.setMinimumHeight(300)
            if hasattr(self, "_expanded_size"):
                self.resize(*self._expanded_size)

    # ── Rolling-window consensus for HUD reads ─────────────
    # Each per-scan raw value is pushed into a deque. The
    # displayed value is the rounded majority (most-frequent
    # integer) across the window. This defeats the per-scan
    # single-digit drift caused by the HUD wiggle animation:
    # static mass 6805 read as 6805/6815/6845/6855 across
    # scans → the window picks the mode (most frequent = 6805).

    def _push_hud_read(
        self,
        mass: float | None,
        resistance: float | None,
        instability: float | None,
    ) -> None:
        """Push raw HUD reads into rolling windows and commit majority."""
        # Mass ────
        if mass is not None:
            # Round to int for cleaner majority matching —
            # mass is always displayed without decimals.
            self._hud_mass_window.append(round(mass))
        else:
            self._hud_mass_window.append(None)
        self._prev_hud_mass = mass  # back-compat

        # Commit: pick the most common non-None value in the window.
        # Need at least 2 reads before committing anything, and the
        # winner must appear ≥ 2× to be trusted.
        mass_counts: dict[int, int] = {}
        none_count = 0
        for v in self._hud_mass_window:
            if v is None:
                none_count += 1
            else:
                mass_counts[v] = mass_counts.get(v, 0) + 1

        if mass_counts:
            best_val = max(mass_counts, key=mass_counts.get)
            best_n = mass_counts[best_val]
            if best_n >= 2:
                self._last_hud_mass = float(best_val)
            # If no value has 2+ appearances yet, keep showing
            # whatever was previously committed (or None).
        elif none_count >= 2:
            # All recent reads are None → panel unreadable, clear.
            self._last_hud_mass = None

        # Resistance ────
        if resistance is not None:
            self._hud_resistance_window.append(round(resistance))
        else:
            self._hud_resistance_window.append(None)
        self._prev_hud_resistance = resistance

        res_counts: dict[int, int] = {}
        res_nones = 0
        for v in self._hud_resistance_window:
            if v is None:
                res_nones += 1
            else:
                res_counts[v] = res_counts.get(v, 0) + 1

        if res_counts:
            best_val = max(res_counts, key=res_counts.get)
            best_n = res_counts[best_val]
            if best_n >= 2:
                self._last_hud_resistance = float(best_val)
        elif res_nones >= 2:
            self._last_hud_resistance = None

        # Instability: no window — a single valid read is enough
        # to flag an IMPOSSIBLE rock. A None read clears the cached
        # value so the flag drops when we move to a new rock.
        self._last_hud_instability = instability

    def _do_scan(self) -> None:
        region = self._config.get("ocr_region")
        if not region:
            return

        # Skip if the previous scan is still running (prevents pileup
        # on slower machines where OCR takes longer than the interval)
        if self._scan_in_progress:
            return

        self._scan_in_progress = True
        hud_region = self._config.get("hud_region")

        def _run():
            try:
                from concurrent.futures import ThreadPoolExecutor

                with ThreadPoolExecutor(max_workers=2) as pool:
                    sig_future = pool.submit(scan_region, region)

                    hud_future = None
                    if hud_region:
                        hud_future = pool.submit(scan_hud_onnx, hud_region)

                    signal_value = sig_future.result(timeout=15)

                    if hud_future is not None:
                        try:
                            # Light-background scans route through the
                            # PaddleOCR sidecar which takes ~9 s warm
                            # on CPU. The old 5 s timeout here caused
                            # every light scan to time out, producing
                            # garbage dark-pipeline fallback reads.
                            # 14 s covers a warm Paddle call (12 s
                            # inner timeout + overhead). Dark-path
                            # scans typically finish in <5 s anyway
                            # so the higher budget only costs time on
                            # actual light panels.
                            hud_result = hud_future.result(timeout=14)
                            hud_mass = hud_result.get("mass")
                            hud_res = hud_result.get("resistance")
                            hud_inst = hud_result.get("instability")
                            panel_visible = hud_result.get("panel_visible", False)

                            # If the scan panel isn't visible at all (the
                            # tall mineral-name row wasn't found), the
                            # player is looking away from a rock. Clear
                            # all cached values so the break bubble hides
                            # and stale data doesn't persist.
                            if not panel_visible:
                                self._last_hud_mass = None
                                self._last_hud_resistance = None
                                self._last_hud_instability = None
                                self._prev_hud_mass = None
                                self._prev_hud_resistance = None
                                self._hud_mass_window.clear()
                                self._hud_resistance_window.clear()
                                # Clear stale signal match so scanning can reappear
                                self._scan_bubble._matches = []
                                # Hide both bubbles + re-show "Scanning"
                                QMetaObject.invokeMethod(
                                    self._break_bubble, "hide",
                                    Qt.QueuedConnection,
                                )
                                QMetaObject.invokeMethod(
                                    self._scan_bubble, "hide",
                                    Qt.QueuedConnection,
                                )
                                QMetaObject.invokeMethod(
                                    self, "_maybe_show_scanning",
                                    Qt.QueuedConnection,
                                )
                            else:
                                # Panel is visible. Push raw reads into
                                # the rolling consensus windows and let
                                # `_commit_hud_from_window` decide what
                                # to display. A None read clears the
                                # displayed value only after it dominates
                                # the window, preventing transient OCR
                                # misses from hiding the bubble.
                                self._push_hud_read(
                                    hud_mass, hud_res, hud_inst
                                )
                        except Exception as exc:
                            log.debug("HUD ONNX scan failed: %s", exc)

                    if signal_value is not None:
                        QMetaObject.invokeMethod(
                            self, "_on_scan_result",
                            Qt.QueuedConnection,
                            Q_ARG(int, signal_value),
                        )

                    # Always update the break bubble on the main thread,
                    # even when signal scan returns None (e.g. fracture mode).
                    if self._last_hud_mass is not None or self._last_hud_resistance is not None:
                        QMetaObject.invokeMethod(
                            self, "_update_break_bubble",
                            Qt.QueuedConnection,
                        )
                    elif signal_value is None:
                        # No signal AND no HUD data — re-show "Scanning"
                        QMetaObject.invokeMethod(
                            self, "_maybe_show_scanning",
                            Qt.QueuedConnection,
                        )
            finally:
                self._scan_in_progress = False

        threading.Thread(target=_run, daemon=True).start()

    @Slot(int)
    def _on_scan_result(self, value: int) -> None:
        # Mirror live HUD OCR values into the manual input fields so
        # the user always sees what the pipeline is actually using.
        # HUD OCR has priority over manual input in
        # ``_get_mass_resistance``, so updating the text boxes here
        # is purely cosmetic — the breakability calc already uses
        # the live values regardless of what's typed.
        if self._last_hud_mass is not None:
            new_val = f"{self._last_hud_mass:.0f}"
            if self._mass_input.text().strip() != new_val:
                self._mass_input.setText(new_val)
                self._auto_mass = new_val
        if self._last_hud_resistance is not None:
            new_val = f"{self._last_hud_resistance:.0f}"
            if self._resistance_input.text().strip() != new_val:
                self._resistance_input.setText(new_val)
                self._auto_resistance = new_val

        # Always try to match and show. Consensus logic only adjusts
        # the displayed value toward the average of two agreeing reads.
        effective_value = value
        if self._last_ocr_value is not None:
            diff = abs(value - self._last_ocr_value)
            threshold = max(50, int(self._last_ocr_value * 0.05))
            if diff <= threshold:
                # Two reads agree — average them for a stable value
                effective_value = (value + self._last_ocr_value) // 2
                if effective_value != self._confirmed_value:
                    self._confirmed_value = effective_value
                    log.info("Confirmed: %d", effective_value)
                    # Consensus-confirmed — highest quality training label
                    try:
                        from ocr.screen_reader import get_last_capture
                        from ocr.training_collector import collect_training_sample
                        cap = get_last_capture()
                        if cap is not None:
                            collect_training_sample(cap, effective_value, confidence="consensus")
                    except Exception:
                        pass
        self._last_ocr_value = value

        self._search_input.setText(str(effective_value))
        matches = self._matcher.match_all(effective_value, tolerance=10)
        # Keep every match tied for the smallest delta — this handles
        # resources that share the exact same signal value (e.g. 6000
        # = FPS Mineables 2R AND Salvage 3R) while still discarding
        # nearby-but-not-equal candidates caused by OCR drift.
        if matches:
            best_delta = min(m.delta for m in matches)
            matches = [m for m in matches if m.delta == best_delta]
            value = effective_value
            log.info("Matched %d result(s) for %d", len(matches), value)
            self._last_matched_resource = matches[0].name if matches else ""

            # Update inline result label (always visible)
            parts = []
            for m in matches:
                rock_word = "R" if m.rock_count == 1 else "R"
                parts.append(f"{m.name} ({m.rock_count}{rock_word})")
            color = RARITY_FG.get(matches[0].rarity, ACCENT)
            inline_text = " | ".join(parts)
            self._inline_result.setText(inline_text)
            self._inline_result.setStyleSheet(f"""
                font-family: Electrolize, Consolas, monospace;
                font-size: 9pt; font-weight: bold;
                color: {color}; background: transparent;
            """)

            # Show the quick-glance scan bubble at the user's chosen display
            # location. (The larger ResourcePopup is only opened on manual
            # double-click in the table, not during active scanning.)
            bubble_pos = self._config.get("bubble_position")
            if bubble_pos:
                anchor_x = bubble_pos["x"]
                anchor_y = bubble_pos["y"]
            else:
                region = self._config.get("ocr_region", {})
                anchor_x = region.get("x", 500) + region.get("w", 200) + 10
                anchor_y = region.get("y", 400)
            try:
                self._scan_bubble.show_matches(
                    matches, anchor_x, anchor_y,
                    scanned_value=effective_value,
                )
            except Exception as exc:
                log.error("Bubble show_matches failed: %s", exc, exc_info=True)
        else:
            self._inline_result.setText("")
            self._scan_bubble._matches = []  # clear stale match
            self._scan_bubble.hide()
            log.debug("No match for confirmed value %d", value)
            self._maybe_show_scanning()

    def _build_gadget_infos(self) -> tuple[list[GadgetInfo], bool]:
        """Build the available gadget list from config quantities.

        Returns (gadget_infos, always_use_best).
        """
        quantities = self._config.get("gadget_quantities", {})
        always_best = self._config.get("always_use_best_gadget", False)
        infos: list[GadgetInfo] = []
        for name, qty in quantities.items():
            if qty > 0:
                # Look up resistance value from the UEX gadget database
                gadgets_db = get_gadget_list()
                g = gadgets_db.get(name)
                if g and g.resistance is not None:
                    infos.append(GadgetInfo(name=name, resistance=g.resistance))
        return infos, always_best

    def _run_breakability(
        self, mass: float, resistance: float, configs: list[LaserConfig],
    ) -> BreakResult:
        """Run the full breakability calculation with gadgets + active modules."""
        gadgets, always_best = self._build_gadget_infos()
        return compute_with_gadgets(
            mass, resistance, configs, gadgets, always_use_best=always_best,
        )

    def _build_home_team_breakdown(
        self, team_configs: list, used_laser_names: list[str],
    ) -> list[dict]:
        """Group the user's own team's used lasers by ship for display.

        Mirrors the shape of the substitutes dict list so the same
        break-bubble helper can render both home team and substitutes
        with a consistent cluster → team → ship → laser hierarchy.
        """
        used_set = set(used_laser_names)
        by_ship: dict[str, dict] = {}
        for c in team_configs:
            if c.name not in used_set:
                continue
            sid = c.ship_id or c.ship_display
            if sid not in by_ship:
                by_ship[sid] = {
                    "ship_display": c.ship_display,
                    "team_name": c.team_name,
                    "cluster": c.cluster,
                    "player_names": list(c.player_names),
                    "used_turrets": [],
                }
            by_ship[sid]["used_turrets"].append(c.name)
        return list(by_ship.values())

    def _show_team_break_result(
        self,
        result,
        mass: float,
        resistance: float,
        bx: int,
        by: int,
        team_configs: list | None = None,
    ) -> None:
        """Map TeamBreakResult to break_bubble.show_team_breakability."""
        instability = self._last_hud_instability
        team_configs = team_configs or []

        def _home(used):
            return self._build_home_team_breakdown(team_configs, used)

        if result.user_can_solo and result.solo_result:
            r = result.solo_result
            # User can break solo — no need to show team breakdown,
            # just display the player's own ship info.
            self._break_bubble.show_team_breakability(
                bx, by, mass=mass, resistance=resistance,
                instability=instability,
                search_scope="solo", can_break=True,
                power_percentage=r.percentage,
                used_lasers=r.used_lasers,
                active_modules_needed=r.active_modules_needed,
                gadget_recommendation=r.gadget_used or "",
            )
        elif result.team_can_break and result.team_result:
            r = result.team_result
            self._break_bubble.show_team_breakability(
                bx, by, mass=mass, resistance=resistance,
                instability=instability,
                search_scope="team", can_break=True,
                power_percentage=r.percentage,
                used_lasers=r.used_lasers,
                active_modules_needed=r.active_modules_needed,
                gadget_recommendation=r.gadget_used or "",
                home_team=_home(r.used_lasers),
            )
        elif result.substitute_result and not result.substitute_result.insufficient:
            r = result.substitute_result
            subs = [
                {
                    "ship_display": s.ship_display,
                    "team_name": s.team_name,
                    "cluster": s.cluster,
                    "player_names": list(s.player_names),
                    "used_turrets": list(s.used_turrets),
                }
                for s in result.substitutes
            ]
            self._break_bubble.show_team_breakability(
                bx, by, mass=mass, resistance=resistance,
                instability=instability,
                search_scope=result.search_scope, can_break=True,
                power_percentage=r.percentage,
                used_lasers=r.used_lasers,
                active_modules_needed=r.active_modules_needed,
                gadget_recommendation=r.gadget_used or "",
                substitutes=subs,
                home_team=_home(r.used_lasers),
            )
        else:
            self._break_bubble.show_team_breakability(
                bx, by, mass=mass, resistance=resistance,
                instability=instability,
                search_scope="", can_break=False,
            )

    @Slot()
    def _update_break_bubble(self) -> None:
        """Show/update the breakability HUD bubble from current data."""
        mass, resistance = self._get_mass_resistance()
        if mass is None or resistance is None:
            # No HUD data — re-show "Scanning" if no signal match either
            self._maybe_show_scanning()
            return

        # HUD data found — dismiss the "Scanning" placeholder
        self._dismiss_scanning()

        configs = self.active_laser_configs()
        if not configs:
            return

        active = self._config.get("active_ship")

        # Position: prefer dedicated break_bubble_position, else fall
        # back to signal bubble position offset below.
        break_pos = self._config.get("break_bubble_position")
        if break_pos:
            bx = break_pos["x"]
            by = break_pos["y"]
        else:
            bubble_pos = self._config.get("bubble_position")
            if bubble_pos:
                bx = bubble_pos["x"]
                by = bubble_pos["y"] + 80
            else:
                region = self._config.get("ocr_region", {})
                bx = region.get("x", 500) + region.get("w", 200) + 10
                by = region.get("y", 400) + 80

        # NOTE: No hard short-circuit on instability. The game's
        # IMPOSSIBLE flag is relative to the CURRENT loadout's applied
        # power — a single overcharging laser can trigger it, while
        # distributing the load across multiple weaker lasers (team /
        # cluster / fleet search below) may produce a viable charge
        # profile. Let the escalating search decide; the regular
        # breakability math will still report CANNOT BREAK honestly
        # when no combination has enough power vs the resistance.

        # Team mode: use team_breakability for team-scoped analysis
        if (active == "fleet" and self._config.get("calc_mode") == "team"
                and hasattr(self, "_ledger_tab")):
            scene = self._ledger_tab._scene
            assigned_user = self._ledger_tab._data.assigned_user
            if assigned_user:
                user_team = scene.find_team_for_player(assigned_user)
                user_ship = scene.find_ship_for_player(assigned_user)
                user_ship_id = user_ship.loadout_path if user_ship else ""

                team_configs = self.team_laser_configs(user_team) if user_team else []
                user_cluster = scene.cluster_for_team(user_team) if hasattr(user_team, "cluster") else ""

                cluster_configs = []
                if user_cluster:
                    for t_node in scene.teams_in_cluster(user_cluster):
                        if t_node is user_team:
                            continue
                        cfgs = self.team_laser_configs(t_node)
                        if cfgs:
                            cluster_configs.append((t_node.team_name, user_cluster, cfgs))

                fleet_cfgs = []
                for cl in scene.all_clusters():
                    if cl == user_cluster:
                        continue
                    for t_node in scene.teams_in_cluster(cl):
                        cfgs = self.team_laser_configs(t_node)
                        if cfgs:
                            fleet_cfgs.append((t_node.team_name, cl, cfgs))
                for t_node in scene._teams:
                    if not t_node.cluster and t_node is not user_team:
                        cfgs = self.team_laser_configs(t_node)
                        if cfgs:
                            fleet_cfgs.append((t_node.team_name, "", cfgs))

                from services.breakability import team_breakability as _team_break
                gadgets, always_best = self._build_gadget_infos()
                t_result = _team_break(
                    mass, resistance, user_ship_id,
                    team_configs, cluster_configs, fleet_cfgs,
                    available_gadgets=gadgets,
                    always_use_best_gadget=always_best,
                )
                self._show_team_break_result(
                    t_result, mass, resistance, bx, by,
                    team_configs=team_configs,
                )
                return

        # Fleet mode: use fleet_breakability for substitution analysis
        if active == "fleet" and self._fleet_snapshots:
            user_ship_id = self._fleet_snapshots[0].source_path
            gadgets, always_best = self._build_gadget_infos()
            fleet_result = fleet_breakability(
                mass, resistance, configs, user_ship_id,
                available_gadgets=gadgets,
                always_use_best_gadget=always_best,
            )

            if fleet_result.user_can_solo:
                # User's ship can handle it — show normal bubble
                result = fleet_result.solo_result
                resource_name = getattr(self, "_last_matched_resource", "")
                try:
                    cp = result.charge_profile
                    self._break_bubble.show_breakability(
                        bx, by,
                        resource_name=resource_name,
                        mass=mass, resistance=resistance,
                        instability=self._last_hud_instability,
                        power_percentage=result.percentage if not result.insufficient else None,
                        can_break=True,
                        used_lasers=result.used_lasers,
                        active_modules_needed=result.active_modules_needed,
                        gadget_recommendation=result.gadget_used or "",
                        min_throttle=cp.min_throttle_pct if cp else None,
                        est_crack_time=cp.est_total_time_sec if cp else None,
                    )
                except Exception as exc:
                    log.error("Break bubble (fleet solo) failed: %s", exc, exc_info=True)
            else:
                # User can't solo — show substitution tabs
                solo = fleet_result.solo_result
                try:
                    lp_gadget = fleet_result.least_players.gadget_used if fleet_result.least_players else None
                    ls_gadget = fleet_result.least_ships.gadget_used if fleet_result.least_ships else None
                    self._break_bubble.show_fleet_substitution(
                        bx, by,
                        mass=mass,
                        resistance=resistance,
                        instability=self._last_hud_instability,
                        solo_missing_power=solo.missing_power if solo else 0.0,
                        lp_power_pct=fleet_result.least_players.percentage if fleet_result.least_players else 0,
                        lp_players=fleet_result.least_players_count,
                        lp_ships=fleet_result.least_players_ships,
                        lp_stability=fleet_result.least_players_stability,
                        lp_gadget=lp_gadget or "",
                        ls_power_pct=fleet_result.least_ships.percentage if fleet_result.least_ships else 0,
                        ls_ship_count=fleet_result.least_ships_count,
                        ls_ships=fleet_result.least_ships_names,
                        ls_stability=fleet_result.least_ships_stability,
                        ls_gadget=ls_gadget or "",
                    )
                except Exception as exc:
                    log.error("Break bubble (fleet sub) failed: %s", exc, exc_info=True)
            return

        # Single ship mode
        result = self._run_breakability(mass, resistance, configs)

        # Auto-decrement consumables (once per rock, deduped)
        rock_key = (round(mass), round(resistance))
        if not hasattr(self, "_consumable_used_rocks"):
            self._consumable_used_rocks: set = set()

        if rock_key not in self._consumable_used_rocks:
            changed = False

            # Gadget auto-decrement
            if result.gadget_used:
                quantities = self._config.get("gadget_quantities", {})
                if quantities.get(result.gadget_used, 0) > 0:
                    quantities[result.gadget_used] -= 1
                    changed = True
                    self._refresh_gadget_spinboxes()

            # Active module auto-decrement (per turret that was activated)
            if result.turrets_activated:
                module_uses = self._config.setdefault("module_uses_remaining", {})
                for turret_name in result.turrets_activated:
                    # Find the matching laser config to get ship_id + turret_index
                    for c in configs:
                        if c.name == turret_name and c.ship_id:
                            ship_uses = module_uses.setdefault(
                                c.ship_id,
                                [c.active_module_uses] * 10,  # init from max
                            )
                            if c.turret_index >= 0 and c.turret_index < len(ship_uses):
                                if ship_uses[c.turret_index] > 0:
                                    ship_uses[c.turret_index] -= 1
                                    changed = True
                            break

            if changed:
                self._consumable_used_rocks.add(rock_key)
                _save_config(self._config)
                self._update_consumables_display()

        resource_name = getattr(self, "_last_matched_resource", "")

        try:
            # Extract charge simulation data if available
            cp = result.charge_profile
            self._break_bubble.show_breakability(
                bx, by,
                resource_name=resource_name,
                mass=mass,
                resistance=resistance,
                instability=self._last_hud_instability,
                power_required=result.missing_power if result.insufficient else None,
                power_percentage=result.percentage if not result.insufficient else None,
                can_break=not result.insufficient,
                unbreakable=result.unbreakable,
                missing_power=result.missing_power,
                used_lasers=result.used_lasers,
                active_modules_needed=result.active_modules_needed,
                gadget_recommendation=result.gadget_used or "",
                min_throttle=cp.min_throttle_pct if cp else None,
                est_crack_time=cp.est_total_time_sec if cp else None,
            )
        except Exception as exc:
            log.error("Break bubble failed: %s", exc, exc_info=True)

    # ── Consumable tracking UI ──

    def _update_consumables_display(self) -> None:
        """No-op — consumables are now managed via Mining Foreman Console on the Gadgets tab."""
        pass

    def _on_replenish_modules(self) -> None:
        """Open a popup to replenish active module uses."""
        popup = QWidget(self, Qt.Popup | Qt.FramelessWindowHint)
        popup.setAttribute(Qt.WA_TranslucentBackground)
        popup.setAttribute(Qt.WA_DeleteOnClose)

        outer = QVBoxLayout(popup)
        outer.setContentsMargins(1, 1, 1, 1)

        frame = QFrame(popup)
        frame.setStyleSheet(
            f"QFrame {{ background: {P.bg_card}; border: 1px solid {ACCENT}; "
            f"border-radius: 4px; }}"
        )
        fl = QVBoxLayout(frame)
        fl.setContentsMargins(10, 10, 10, 10)
        fl.setSpacing(6)

        # Header + Refresh All
        hdr = QWidget(frame)
        hdr_l = QHBoxLayout(hdr)
        hdr_l.setContentsMargins(0, 0, 0, 0)
        title = QLabel("Replenish Modules", hdr)
        title.setStyleSheet(
            f"font-family: Electrolize, Consolas; font-size: 10pt; "
            f"font-weight: bold; color: {ACCENT}; background: transparent;"
        )
        hdr_l.addWidget(title)
        hdr_l.addStretch(1)
        refresh_btn = QPushButton("Refresh All", hdr)
        refresh_btn.setCursor(Qt.PointingHandCursor)
        refresh_btn.setStyleSheet(
            f"QPushButton {{ font-family: Consolas; font-size: 8pt; font-weight: bold; "
            f"color: {ACCENT}; background: transparent; border: 1px solid {ACCENT}; "
            f"border-radius: 3px; padding: 3px 8px; }}"
        )
        refresh_btn.clicked.connect(lambda: self._replenish_all_modules(popup))
        hdr_l.addWidget(refresh_btn)
        fl.addWidget(hdr)

        # Per ship / turret rows
        module_uses = self._config.get("module_uses_remaining", {})
        configs = self.active_laser_configs()
        ships_seen: set[str] = set()

        _spin_style = (
            f"QSpinBox {{ font-family: Consolas; font-size: 8pt; color: {P.fg}; "
            f"background: {P.bg_card}; border: 1px solid {P.border}; border-radius: 3px; }}"
            f"QSpinBox::up-button, QSpinBox::down-button {{ width: 14px; border: none; "
            f"background: {P.bg_secondary}; }}"
            f"QSpinBox::up-arrow {{ border-left: 3px solid transparent; "
            f"border-right: 3px solid transparent; border-bottom: 4px solid {ACCENT}; }}"
            f"QSpinBox::down-arrow {{ border-left: 3px solid transparent; "
            f"border-right: 3px solid transparent; border-top: 4px solid {ACCENT}; }}"
        )

        for c in configs:
            if not c.ship_id or c.active_module_uses == 0:
                continue
            if c.ship_id not in ships_seen:
                ships_seen.add(c.ship_id)
                ship_lbl = QLabel(c.ship_display, frame)
                ship_lbl.setStyleSheet(
                    f"font-family: Consolas; font-size: 8pt; font-weight: bold; "
                    f"color: {P.fg}; background: transparent; padding-top: 4px;"
                )
                fl.addWidget(ship_lbl)

            row = QWidget(frame)
            rl = QHBoxLayout(row)
            rl.setContentsMargins(16, 0, 0, 0)
            rl.setSpacing(6)

            mod_label = f"T{c.turret_index+1}"
            if c.active_module_names:
                mod_label += f": {c.active_module_names}"
            mod_label += f" ({c.active_uses_remaining}/{c.active_module_uses})"
            turret_lbl = QLabel(mod_label, row)
            turret_lbl.setStyleSheet(
                f"font-family: Consolas; font-size: 8pt; color: {P.fg_dim}; "
                f"background: transparent;"
            )
            rl.addWidget(turret_lbl, 1)

            spin = QSpinBox(row)
            spin.setRange(0, c.active_module_uses)
            spin.setValue(c.active_uses_remaining)
            spin.setFixedWidth(60)
            spin.setStyleSheet(_spin_style)
            spin.valueChanged.connect(
                lambda val, sid=c.ship_id, tidx=c.turret_index: (
                    self._set_module_uses(sid, tidx, val)
                )
            )
            rl.addWidget(spin)
            fl.addWidget(row)

        outer.addWidget(frame)
        pos = self._btn_replenish_mods.mapToGlobal(
            self._btn_replenish_mods.rect().bottomLeft()
        )
        popup.adjustSize()
        popup.move(pos)
        popup.show()

    def _replenish_all_modules(self, popup: QWidget | None = None) -> None:
        """Reset all module uses to their max values."""
        configs = self.active_laser_configs()
        module_uses = self._config.setdefault("module_uses_remaining", {})
        for c in configs:
            if c.ship_id and c.active_module_uses > 0 and c.turret_index >= 0:
                ship_uses = module_uses.setdefault(
                    c.ship_id, [0] * max(c.turret_index + 1, 3)
                )
                while len(ship_uses) <= c.turret_index:
                    ship_uses.append(0)
                ship_uses[c.turret_index] = c.active_module_uses
        _save_config(self._config)
        self._update_consumables_display()
        if popup:
            popup.close()

    def _set_module_uses(self, ship_id: str, turret_index: int, value: int) -> None:
        """Set the remaining module uses for a specific turret."""
        module_uses = self._config.setdefault("module_uses_remaining", {})
        ship_uses = module_uses.setdefault(ship_id, [0] * max(turret_index + 1, 3))
        while len(ship_uses) <= turret_index:
            ship_uses.append(0)
        ship_uses[turret_index] = value
        _save_config(self._config)
        self._update_consumables_display()

    def _on_replenish_gadgets(self) -> None:
        """Open a popup to replenish gadget quantities."""
        popup = QWidget(self, Qt.Popup | Qt.FramelessWindowHint)
        popup.setAttribute(Qt.WA_TranslucentBackground)
        popup.setAttribute(Qt.WA_DeleteOnClose)

        outer = QVBoxLayout(popup)
        outer.setContentsMargins(1, 1, 1, 1)

        frame = QFrame(popup)
        frame.setStyleSheet(
            f"QFrame {{ background: {P.bg_card}; border: 1px solid {ACCENT}; "
            f"border-radius: 4px; }}"
        )
        fl = QVBoxLayout(frame)
        fl.setContentsMargins(10, 10, 10, 10)
        fl.setSpacing(6)

        hdr = QWidget(frame)
        hdr_l = QHBoxLayout(hdr)
        hdr_l.setContentsMargins(0, 0, 0, 0)
        title = QLabel("Replenish Gadgets", hdr)
        title.setStyleSheet(
            f"font-family: Electrolize, Consolas; font-size: 10pt; "
            f"font-weight: bold; color: {ACCENT}; background: transparent;"
        )
        hdr_l.addWidget(title)
        hdr_l.addStretch(1)
        refresh_btn = QPushButton("Refresh All", hdr)
        refresh_btn.setCursor(Qt.PointingHandCursor)
        refresh_btn.setStyleSheet(
            f"QPushButton {{ font-family: Consolas; font-size: 8pt; font-weight: bold; "
            f"color: {ACCENT}; background: transparent; border: 1px solid {ACCENT}; "
            f"border-radius: 3px; padding: 3px 8px; }}"
        )
        refresh_btn.clicked.connect(lambda: self._replenish_all_gadgets(popup))
        hdr_l.addWidget(refresh_btn)
        fl.addWidget(hdr)

        _spin_style = (
            f"QSpinBox {{ font-family: Consolas; font-size: 8pt; color: {P.fg}; "
            f"background: {P.bg_card}; border: 1px solid {P.border}; border-radius: 3px; }}"
            f"QSpinBox::up-button, QSpinBox::down-button {{ width: 14px; border: none; "
            f"background: {P.bg_secondary}; }}"
            f"QSpinBox::up-arrow {{ border-left: 3px solid transparent; "
            f"border-right: 3px solid transparent; border-bottom: 4px solid {ACCENT}; }}"
            f"QSpinBox::down-arrow {{ border-left: 3px solid transparent; "
            f"border-right: 3px solid transparent; border-top: 4px solid {ACCENT}; }}"
        )

        quantities = self._config.get("gadget_quantities", {})
        gadgets_db = get_gadget_list()

        for name in sorted(gadgets_db.keys()):
            row = QWidget(frame)
            rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 0, 0, 0)
            rl.setSpacing(6)

            lbl = QLabel(name, row)
            lbl.setFixedWidth(100)
            lbl.setStyleSheet(
                f"font-family: Consolas; font-size: 8pt; color: {P.fg}; "
                f"background: transparent;"
            )
            rl.addWidget(lbl)

            spin = QSpinBox(row)
            spin.setRange(0, 99)
            spin.setValue(quantities.get(name, 0))
            spin.setFixedWidth(60)
            spin.setStyleSheet(_spin_style)
            spin.valueChanged.connect(
                lambda val, n=name: self._on_gadget_qty_changed(n, val)
            )
            rl.addWidget(spin)
            rl.addStretch(1)
            fl.addWidget(row)

        outer.addWidget(frame)
        pos = self._btn_replenish_gadgets.mapToGlobal(
            self._btn_replenish_gadgets.rect().bottomLeft()
        )
        popup.adjustSize()
        popup.move(pos)
        popup.show()

    def _replenish_all_gadgets(self, popup: QWidget | None = None) -> None:
        """Reset all gadget quantities to their max (99)."""
        gadgets_db = get_gadget_list()
        quantities = self._config.setdefault("gadget_quantities", {})
        for name in gadgets_db:
            quantities[name] = max(quantities.get(name, 0), 10)  # default refill to 10
        _save_config(self._config)
        self._refresh_gadget_spinboxes()
        self._update_consumables_display()
        if popup:
            popup.close()

    def _on_show_substitute(self) -> None:
        """Open a draggable popup showing which fleet ships can substitute."""
        mass, resistance = self._get_mass_resistance()
        if mass is None or resistance is None:
            return

        configs = self.active_laser_configs()
        if not configs or not self._fleet_snapshots:
            return

        user_ship_id = self._fleet_snapshots[0].source_path
        gadgets, always_best = self._build_gadget_infos()
        fleet_result = fleet_breakability(
            mass, resistance, configs, user_ship_id,
            available_gadgets=gadgets, always_use_best_gadget=always_best,
        )

        if fleet_result.user_can_solo:
            return  # no substitution needed

        # Build a draggable popup
        popup = QWidget(None, Qt.WindowStaysOnTopHint | Qt.Tool | Qt.FramelessWindowHint)
        popup.setAttribute(Qt.WA_DeleteOnClose)
        popup._drag_pos = None

        def _mp(event):
            if event.button() == Qt.LeftButton:
                popup._drag_pos = event.globalPosition().toPoint() - popup.frameGeometry().topLeft()

        def _mm(event):
            if popup._drag_pos and event.buttons() & Qt.LeftButton:
                popup.move(event.globalPosition().toPoint() - popup._drag_pos)

        popup.mousePressEvent = _mp
        popup.mouseMoveEvent = _mm

        popup.setFixedWidth(360)
        outer = QVBoxLayout(popup)
        outer.setContentsMargins(0, 0, 0, 0)

        frame = QFrame(popup)
        frame.setObjectName("sub_frame")
        frame.setStyleSheet(
            f"QFrame#sub_frame {{ background: {P.bg_card}; "
            f"border: 1px solid #ff4444; border-radius: 4px; }}"
        )
        fl = QVBoxLayout(frame)
        fl.setContentsMargins(12, 12, 12, 12)
        fl.setSpacing(6)

        _ns = f"background: transparent; border: none;"

        # Header + close
        hdr = QWidget(frame)
        hl = QHBoxLayout(hdr)
        hl.setContentsMargins(0, 0, 0, 0)
        title = QLabel("Substitute Ships Needed", hdr)
        title.setStyleSheet(
            f"font-family: Electrolize, Consolas; font-size: 10pt; "
            f"font-weight: bold; color: #ff4444; {_ns}"
        )
        hl.addWidget(title)
        hl.addStretch(1)
        close_btn = QPushButton("\u2716", hdr)
        close_btn.setFixedSize(32, 28)
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setStyleSheet(_CLOSE_BTN_STYLE)
        close_btn.clicked.connect(popup.close)
        hl.addWidget(close_btn)
        fl.addWidget(hdr)

        # Rock info
        rock_lbl = QLabel(f"Mass: {mass:,.0f} kg  |  Resistance: {resistance:.0f}%", frame)
        rock_lbl.setStyleSheet(f"font-family: Consolas; font-size: 8pt; color: {P.fg}; {_ns}")
        fl.addWidget(rock_lbl)

        deficit = fleet_result.solo_result.missing_power if fleet_result.solo_result else 0
        def_lbl = QLabel(f"Your ship: +{deficit:,.0f} MW short", frame)
        def_lbl.setStyleSheet(f"font-family: Consolas; font-size: 8pt; color: #ff4444; {_ns}")
        fl.addWidget(def_lbl)

        # Least Players option
        if fleet_result.least_players:
            sep1 = QLabel(f"--- Least Players ({fleet_result.least_players_count}) ---", frame)
            sep1.setStyleSheet(f"font-family: Consolas; font-size: 8pt; font-weight: bold; color: {ACCENT}; {_ns} padding-top: 6px;")
            fl.addWidget(sep1)
            for name in fleet_result.least_players_ships:
                lbl = QLabel(f"  {name}", frame)
                lbl.setStyleSheet(f"font-family: Consolas; font-size: 8pt; color: {P.fg_dim}; {_ns}")
                fl.addWidget(lbl)
            pct_lbl = QLabel(f"  Power: {fleet_result.least_players.percentage:.0f}%", frame)
            pct_lbl.setStyleSheet(f"font-family: Consolas; font-size: 8pt; color: {ACCENT}; {_ns}")
            fl.addWidget(pct_lbl)

        # Least Ships option
        if fleet_result.least_ships:
            sep2 = QLabel(f"--- Least Ships ({fleet_result.least_ships_count}) ---", frame)
            sep2.setStyleSheet(f"font-family: Consolas; font-size: 8pt; font-weight: bold; color: {ACCENT}; {_ns} padding-top: 6px;")
            fl.addWidget(sep2)
            for name in fleet_result.least_ships_names:
                lbl = QLabel(f"  {name}", frame)
                lbl.setStyleSheet(f"font-family: Consolas; font-size: 8pt; color: {P.fg_dim}; {_ns}")
                fl.addWidget(lbl)
            pct_lbl = QLabel(f"  Power: {fleet_result.least_ships.percentage:.0f}%", frame)
            pct_lbl.setStyleSheet(f"font-family: Consolas; font-size: 8pt; color: {ACCENT}; {_ns}")
            fl.addWidget(pct_lbl)

        outer.addWidget(frame)
        popup.adjustSize()
        popup.move(self.mapToGlobal(self.rect().center()) - popup.rect().center())
        popup.show()

    def _on_show_consumables_detail(self) -> None:
        """Open a persistent, draggable popup with full consumable breakdown."""
        # Close existing if open
        if hasattr(self, "_consumables_popup") and self._consumables_popup:
            try:
                self._consumables_popup.close()
            except RuntimeError:
                pass

        popup = QWidget(None, Qt.WindowStaysOnTopHint | Qt.Tool | Qt.FramelessWindowHint)
        popup.setAttribute(Qt.WA_DeleteOnClose)
        popup.destroyed.connect(lambda: setattr(self, "_consumables_popup", None))
        self._consumables_popup = popup

        # Make draggable
        popup._drag_pos = None

        def _mouse_press(event):
            if event.button() == Qt.LeftButton:
                popup._drag_pos = event.globalPosition().toPoint() - popup.frameGeometry().topLeft()

        def _mouse_move(event):
            if popup._drag_pos and event.buttons() & Qt.LeftButton:
                popup.move(event.globalPosition().toPoint() - popup._drag_pos)

        popup.mousePressEvent = _mouse_press
        popup.mouseMoveEvent = _mouse_move

        popup.setFixedWidth(320)

        # Use a QFrame as the visual container so the border only applies
        # to the outer frame, not every child widget.
        outer_layout = QVBoxLayout(popup)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        frame = QFrame(popup)
        frame.setStyleSheet(
            f"QFrame#consumables_frame {{ background: {P.bg_card}; "
            f"border: 1px solid {ACCENT}; border-radius: 4px; }}"
        )
        frame.setObjectName("consumables_frame")
        outer_layout.addWidget(frame)

        main_layout = QVBoxLayout(frame)
        main_layout.setContentsMargins(10, 10, 10, 10)
        main_layout.setSpacing(4)

        # Header + close
        hdr = QWidget(frame)
        hdr_l = QHBoxLayout(hdr)
        hdr_l.setContentsMargins(0, 0, 0, 0)
        title = QLabel("Fleet Consumables", hdr)
        title.setStyleSheet(
            f"font-family: Electrolize, Consolas; font-size: 10pt; "
            f"font-weight: bold; color: {ACCENT}; background: transparent; border: none;"
        )
        hdr_l.addWidget(title)
        hdr_l.addStretch(1)
        close_btn = QPushButton("\u2716", hdr)
        close_btn.setFixedSize(32, 28)
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setStyleSheet(_CLOSE_BTN_STYLE)
        close_btn.clicked.connect(popup.close)
        hdr_l.addWidget(close_btn)
        main_layout.addWidget(hdr)

        _item_style = (
            f"font-family: Consolas; font-size: 8pt; background: transparent; border: none;"
        )

        # Gadgets section (yellow/orange color to distinguish from modules)
        quantities = self._config.get("gadget_quantities", {})
        has_gadgets = any(v > 0 for v in quantities.values())
        if has_gadgets:
            g_hdr = QLabel("Gadgets", frame)
            g_hdr.setStyleSheet(
                f"font-family: Consolas; font-size: 9pt; font-weight: bold; "
                f"color: #ffc107; background: transparent; border: none; padding-top: 4px;"
            )
            main_layout.addWidget(g_hdr)
            for name, qty in sorted(quantities.items()):
                if qty > 0:
                    lbl = QLabel(f"  {name}: {qty}", frame)
                    lbl.setStyleSheet(f"{_item_style} color: #ffc107;")
                    main_layout.addWidget(lbl)

        # Modules section per ship
        module_uses = self._config.get("module_uses_remaining", {})
        configs = self.active_laser_configs()
        ships_seen: set[str] = set()
        for c in configs:
            if not c.ship_id or c.active_module_uses == 0:
                continue
            if c.ship_id not in ships_seen:
                ships_seen.add(c.ship_id)
                s_hdr = QLabel(c.ship_display, frame)
                s_hdr.setStyleSheet(
                    f"font-family: Consolas; font-size: 9pt; font-weight: bold; "
                    f"color: {ACCENT}; background: transparent; border: none; padding-top: 4px;"
                )
                main_layout.addWidget(s_hdr)

            color = ACCENT if c.active_uses_remaining > 0 else "#ff4444"
            turret_label = f"T{c.turret_index+1}"
            if c.active_module_names:
                turret_label += f": {c.active_module_names}"
            lbl = QLabel(
                f"  {turret_label} ({c.active_uses_remaining}/{c.active_module_uses} uses)",
                frame,
            )
            lbl.setStyleSheet(f"{_item_style} color: {color};")
            main_layout.addWidget(lbl)

        main_layout.addStretch(1)
        popup.adjustSize()
        popup.move(
            self.mapToGlobal(self.rect().center())
            - popup.rect().center()
        )
        popup.show()

    def _dismiss_scanning(self) -> None:
        """Hide the 'Scanning' placeholder if it's showing."""
        if self._scan_bubble.isVisible() and not self._scan_bubble._matches:
            self._scan_bubble.hide()

    @Slot()
    def _maybe_show_scanning(self) -> None:
        """Re-show the 'Scanning' bubble if we're in scan mode and have no results."""
        if self._scan_timer is None:
            return  # not scanning
        # Only show if no signal match and no HUD data are currently displayed
        if self._scan_bubble._matches:
            return  # signal bubble is showing a result
        if self._break_bubble.isVisible():
            return  # break bubble is showing
        # Re-show the scanning placeholder
        bubble_pos = self._config.get("bubble_position")
        if bubble_pos:
            self._scan_bubble.show_scanning(bubble_pos["x"], bubble_pos["y"])
        else:
            region = self._config.get("ocr_region", {})
            self._scan_bubble.show_scanning(
                region.get("x", 500) + region.get("w", 200) + 10,
                region.get("y", 400),
            )

    def _get_mass_resistance(self) -> tuple[float | None, float | None]:
        """Get mass/resistance for breakability display.

        Priority (HUD OCR wins over stale manual input):
        1. Live HUD OCR values (``_last_hud_mass`` / ``_last_hud_resistance``)
           — these reflect the current rock being scanned, so they
           take precedence whenever the scan pipeline has a result.
        2. Manual input text fields — only consulted when the HUD
           OCR has nothing for that field (panel not visible, or
           OCR couldn't converge on the current frame).

        Previously the priority was reversed (manual first) and
        stale typed values silently overrode live OCR reads — a
        confusing UX trap where the bubble would freeze on whatever
        the user typed last, even though new rocks were being
        scanned successfully. Swapped so fresh OCR data always wins.
        """
        mass = self._last_hud_mass
        resistance = self._last_hud_resistance

        # Fallback to manual inputs when the corresponding HUD OCR
        # value is unavailable.
        if mass is None:
            try:
                mt = self._mass_input.text().strip()
                if mt:
                    mass = float(mt)
            except (ValueError, AttributeError):
                pass
        if resistance is None:
            try:
                rt = self._resistance_input.text().strip()
                if rt:
                    resistance = float(rt)
            except (ValueError, AttributeError):
                pass

        return mass, resistance

    def _sync_table_min_width(self) -> None:
        """Force the signal table to be at least as wide as the sum of
        its (content-sized) columns so the break panel next to it
        can't squeeze the columns behind a horizontal scrollbar.

        Called after every ``set_data`` since column widths change
        when the row contents change (longer resource names, etc.).
        """
        table = getattr(self, "_table", None)
        if table is None:
            return
        header = table.horizontalHeader()
        total = sum(header.sectionSize(i) for i in range(header.count()))
        # Room for the vertical scroll bar + a small frame margin.
        total += 20
        if total > 0:
            table.setMinimumWidth(total)

    def _refresh_break_panel(self) -> None:
        """Push the current rock + loadout state into the side panel.

        Safe to call from any point: input change, HUD OCR, ship
        swap, calc-mode toggle, etc.  No-ops silently if the panel
        was never built (e.g. early shutdown).
        """
        panel = getattr(self, "_break_panel", None)
        if panel is None:
            return

        mass, resistance = self._get_mass_resistance()
        instability = self._last_hud_instability
        ship_label = self._active_ship_label()

        configs = self.active_laser_configs()
        if not configs:
            panel.update_state(
                mass=mass, resistance=resistance, instability=instability,
                ship_label=ship_label, result=None, no_ship=True,
            )
            return

        if mass is None or resistance is None:
            panel.update_state(
                mass=mass, resistance=resistance, instability=instability,
                ship_label=ship_label, result=None,
            )
            return

        try:
            result = self._run_breakability(mass, resistance, configs)
        except Exception:  # pragma: no cover — compute should not raise
            result = None
        panel.update_state(
            mass=mass, resistance=resistance, instability=instability,
            ship_label=ship_label, result=result,
        )

    def _active_ship_label(self) -> str:
        """Return a short human-readable description of the active ship."""
        active = self._config.get("active_ship")
        if active == "fleet":
            n = len(self._fleet_snapshots)
            return f"Fleet — {n} ship{'s' if n != 1 else ''}"
        if active:
            display = dict(SHIP_SLOTS).get(active, active.title())
            snap = self._ship_snapshots.get(active)
            if snap is not None:
                try:
                    desc = describe_snapshot(snap)
                    if desc:
                        return f"{display} · {desc}"
                except Exception:
                    pass
            return display
        return "— no ship —"

    def _on_break_inputs_changed(self, _text: str = "") -> None:
        """Recompute breakability when the user types mass/resistance."""
        # Always refresh the side panel alongside the inline result label.
        self._refresh_break_panel()
        text = self._format_breakability()
        if text:
            cannot = "CANNOT" in text or "UNBREAKABLE" in text
            color = "#ff4444" if cannot else ACCENT
            self._break_result.setStyleSheet(
                f"font-family: Consolas, monospace; font-size: 9pt; "
                f"font-weight: bold; color: {color}; background: transparent; "
                f"padding: 0 8px;"
            )
            self._break_result.setText(text)

            # Show Substitute button in fleet mode when the USER's ship
            # can't solo but the fleet has alternatives
            show_sub = False
            if self._config.get("active_ship") == "fleet" and self._fleet_snapshots:
                mass, resistance = self._get_mass_resistance()
                if mass is not None and resistance is not None:
                    user_id = self._fleet_snapshots[0].source_path
                    configs = self.active_laser_configs()
                    user_configs = [c for c in configs if c.ship_id == user_id]
                    if user_configs:
                        gadgets, always_best = self._build_gadget_infos()
                        solo = compute_with_gadgets(
                            mass, resistance, user_configs, gadgets, always_best,
                        )
                        show_sub = solo.insufficient and not cannot
            self._btn_substitute.setVisible(show_sub)
        else:
            self._break_result.setStyleSheet(
                f"font-family: Consolas, monospace; font-size: 9pt; "
                f"font-weight: bold; color: {P.fg_dim}; background: transparent; "
                f"padding: 0 8px;"
            )
            self._break_result.setText("")
            self._btn_substitute.setVisible(False)

    def _format_breakability(self) -> str | None:
        """Compute breakability from current inputs/HUD and return text."""
        mass, resistance = self._get_mass_resistance()
        if mass is None or resistance is None:
            return None

        configs = self.active_laser_configs()
        if not configs:
            return "Select a mining ship first"

        result = self._run_breakability(mass, resistance, configs)

        if result.unbreakable:
            return "UNBREAKABLE at this resistance"

        parts: list[str] = []
        if result.insufficient:
            parts.append(f"CANNOT BREAK (+{result.missing_power:,.0f} MW needed)")
        else:
            lasers_str = ", ".join(result.used_lasers)
            parts.append(f"{result.percentage:.0f}% power ({lasers_str})")

        if result.active_modules_needed > 0:
            parts.append(f"Activate modules ({result.active_modules_needed}x)")
        if result.gadget_used:
            parts.append(f"Use {result.gadget_used}")

        return " | ".join(parts)

    def _find_row_by_name(self, name: str) -> dict | None:
        """Return the table-data row for *name*, or None."""
        for row in self._all_table_data:
            if row.get("name") == name:
                return row
        return None

    def closeEvent(self, event) -> None:
        if self._scan_timer:
            self._scan_timer.stop()
        if self._refinery_monitor is not None:
            self._refinery_monitor.stop()
        if self._refinery_scan_timer is not None:
            self._refinery_scan_timer.stop()
        if self._refinery_countdown_timer is not None:
            self._refinery_countdown_timer.stop()
        self._scan_bubble.hide()
        self._break_bubble.hide()
        chart_bubble.close_singleton()
        # Terminate the PaddleOCR sidecar daemon if it was started
        # during this session. Lazy import keeps the dark-only path
        # from paying any module-load cost.
        try:
            from ocr import paddle_client
            paddle_client.shutdown()
        except Exception:
            pass
        super().closeEvent(event)


# ---------------------------------------------------------------------------
# Entry-point helper
# ---------------------------------------------------------------------------

def main() -> None:
    """Launch Mining Signals from the command line."""
    from shared.crash_logger import init_crash_logging
    log = init_crash_logging("mining_signals")
    try:
        set_dpi_awareness()

        parsed = parse_cli_args(sys.argv[1:], {"w": 980, "h": 960})

        app = QApplication(sys.argv)
        apply_theme(app)

        window = MiningSignalsApp(
            x=parsed["x"],
            y=parsed["y"],
            w=parsed["w"],
            h=parsed["h"],
            opacity=parsed["opacity"],
            cmd_file=parsed["cmd_file"],
        )
        window.show()
        window.raise_()
        window.activateWindow()
        sys.exit(app.exec())
    except Exception:
        log.critical("FATAL crash in mining_signals main()", exc_info=True)
        sys.exit(1)
