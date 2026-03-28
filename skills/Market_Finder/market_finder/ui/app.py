"""Main application window — MarketFinderApp — PySide6 version."""

from __future__ import annotations

import logging
import os
import sys
import threading

import shared.path_setup  # noqa: E402  # centralised path config
from shared.i18n import s_ as _

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QFrame, QSplitter, QSlider, QCheckBox, QComboBox, QPushButton,
    QSizePolicy,
)

from shared.qt.theme import P, apply_theme
from shared.qt.base_window import SCWindow
from shared.qt.title_bar import SCTitleBar
from shared.qt.search_bar import SCSearchBar
from shared.qt.animated_button import SCButton
from shared.qt.ipc_thread import IPCWatcher

from shared.platform_utils import set_dpi_awareness
from shared.data_utils import parse_cli_args

from ..config import (
    AUTO_REFRESH_MS, AUTO_REFRESH_RETRY_MS,
    CACHE_TTL_OPTIONS, POLL_LOADING_MS, SEARCH_DEBOUNCE_MS,
    TAB_DEFS,
    item_tab,
)
from ..service import DataService
from .detail_panel import DetailPanel
from .rental_table import RentalTable
from .ship_table import ShipTable
from .virtual_table import VirtualTable
from .widgets import SearchBubble, ItemDetailBubble

log = logging.getLogger(__name__)


class MarketFinderApp(SCWindow):
    """Top-level application — owns the DataService and all UI widgets."""

    def __init__(
        self,
        data: DataService,
        x: int = 100,
        y: int = 100,
        w: int = 1100,
        h: int = 720,
        opacity: float = 0.95,
        cmd_file: str | None = None,
    ) -> None:
        super().__init__(
            title="Market Finder",
            width=w,
            height=h,
            min_w=700,
            min_h=400,
            opacity=opacity,
            always_on_top=True,
        )
        self.move(x, y)

        self.data = data
        self._current_tab: str = "All"
        self._search_text: str = ""
        self._bubble: SearchBubble | None = None
        self._detail_bubbles: list[ItemDetailBubble] = []
        self._cmd_file = cmd_file
        self._settings_visible: bool = False
        self._opacity: float = opacity
        self._always_on_top: bool = True
        self._auto_refresh_timer: QTimer | None = None
        self._fetching: bool = False

        # IPC watcher
        self._ipc_watcher: IPCWatcher | None = None

        self._build_ui()
        self._start_loading()

        if cmd_file:
            self._ipc_watcher = IPCWatcher(cmd_file, poll_ms=500, parent=self)
            self._ipc_watcher.command_received.connect(self._handle_command)
            self._ipc_watcher.start()

    def closeEvent(self, event) -> None:
        if self._ipc_watcher:
            self._ipc_watcher.stop()
        super().closeEvent(event)

    # -- UI construction -----------------------------------------------------

    def _build_ui(self) -> None:
        layout = self.content_layout

        # Title bar
        title_bar = SCTitleBar(
            window=self,
            title=_("MARKET FINDER"),
            icon_text="\U0001f6d2",
            accent_color=P.tool_market,
            show_minimize=False,
        )
        title_bar.close_clicked.connect(self.close)

        # Add status and gear to title bar
        tb_layout = title_bar.layout()

        self._status_lbl = QLabel(_("Loading..."))
        self._status_lbl.setStyleSheet(f"""
            font-family: Consolas;
            font-size: 8pt;
            color: {P.fg_dim};
            background: transparent;
        """)

        gear_btn = QLabel("\u2699")
        gear_btn.setStyleSheet(f"""
            font-family: Consolas;
            font-size: 14pt;
            color: {P.fg_dim};
            background: transparent;
        """)
        gear_btn.setCursor(Qt.PointingHandCursor)
        gear_btn.mousePressEvent = lambda _: self._toggle_settings()

        tutorial_btn = QLabel("? Tutorial")
        tutorial_btn.setStyleSheet(f"""
            font-family: Consolas;
            font-size: 8pt;
            font-weight: bold;
            color: {P.tool_market};
            background: transparent;
            border: 1px solid {P.tool_market};
            border-radius: 3px;
            padding: 2px 8px;
        """)
        tutorial_btn.setCursor(Qt.PointingHandCursor)
        tutorial_btn.mousePressEvent = lambda _: self._show_tutorial()

        # Insert before window controls (before the stretch)
        for i in range(tb_layout.count()):
            item = tb_layout.itemAt(i)
            if item.spacerItem() is not None:
                tb_layout.insertWidget(i + 1, self._status_lbl)
                tb_layout.insertWidget(i + 2, gear_btn)
                tb_layout.insertWidget(i + 3, tutorial_btn)
                break

        layout.addWidget(title_bar)

        # Settings panel (hidden)
        self._settings_frame = QWidget()
        self._settings_frame.setStyleSheet(f"background-color: {P.bg_input};")
        self._settings_frame.hide()
        self._build_settings()
        layout.addWidget(self._settings_frame)

        # Search bar
        search_frame = QWidget()
        search_frame.setFixedHeight(34)
        search_frame.setStyleSheet(f"background-color: {P.bg_secondary};")
        search_lay = QHBoxLayout(search_frame)
        search_lay.setContentsMargins(8, 4, 8, 4)
        search_lay.setSpacing(4)

        search_icon = QLabel("\U0001f50d")
        search_icon.setStyleSheet(f"font-size: 10pt; color: {P.fg_dim}; background: transparent;")
        search_lay.addWidget(search_icon)

        self._search_bar = SCSearchBar(placeholder=_("Search items..."), debounce_ms=SEARCH_DEBOUNCE_MS)
        self._search_bar.search_changed.connect(self._on_search_debounced)
        self._search_bar.textChanged.connect(self._on_search_text_changed)
        self._search_bar.returnPressed.connect(self._on_search_enter)
        search_lay.addWidget(self._search_bar, 1)

        layout.addWidget(search_frame)

        # Tab bar
        tab_frame = QWidget()
        tab_frame.setStyleSheet(f"background-color: {P.bg_primary};")
        tab_lay = QHBoxLayout(tab_frame)
        tab_lay.setContentsMargins(6, 0, 6, 0)
        tab_lay.setSpacing(2)

        self._tab_labels: dict[str, QLabel] = {}
        for emoji, name in TAB_DEFS:
            lbl = _TabLabel(f" {emoji} {_(name)} ", name)
            lbl.setStyleSheet(f"""
                QLabel {{
                    font-family: Consolas;
                    font-size: 9pt;
                    color: {P.fg_dim};
                    background: transparent;
                    padding: 3px 4px;
                }}
                QLabel:hover {{
                    background-color: {P.bg_input};
                }}
            """)
            lbl.setCursor(Qt.PointingHandCursor)
            lbl.tab_clicked.connect(self._select_tab)
            tab_lay.addWidget(lbl)
            self._tab_labels[name] = lbl
        tab_lay.addStretch(1)
        self._select_tab("All", update_view=False)
        layout.addWidget(tab_frame)

        # Separator
        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"background-color: {P.border};")
        layout.addWidget(sep)

        # Main content — split pane
        self._splitter = QSplitter(Qt.Horizontal)
        self._splitter.setStyleSheet(f"""
            QSplitter::handle {{
                background-color: {P.border};
                width: 3px;
            }}
            QSplitter::handle:hover {{
                background-color: {P.accent};
            }}
        """)

        self._left_frame = QWidget()
        self._left_frame.setStyleSheet(f"background-color: {P.bg_primary};")
        self._left_layout = QVBoxLayout(self._left_frame)
        self._left_layout.setContentsMargins(0, 0, 0, 0)
        self._left_layout.setSpacing(0)

        self._right_frame = QWidget()
        self._right_frame.setStyleSheet(f"background-color: {P.bg_primary};")
        right_lay = QVBoxLayout(self._right_frame)
        right_lay.setContentsMargins(0, 0, 0, 0)
        right_lay.setSpacing(0)

        self._item_table = VirtualTable(self._left_frame, on_select=self._on_item_select, on_double_click=self._on_item_double_click)
        self._left_layout.addWidget(self._item_table)

        self._rental_table = RentalTable(self._left_frame, self.data, on_select=self._on_ship_select, on_double_click=self._on_ship_double_click)
        self._left_layout.addWidget(self._rental_table)
        self._rental_table.hide()

        self._ship_table = ShipTable(self._left_frame, self.data, on_select=self._on_ship_select, on_double_click=self._on_ship_double_click)
        self._left_layout.addWidget(self._ship_table)
        self._ship_table.hide()

        self._detail_panel = DetailPanel(self._right_frame, self.data)
        right_lay.addWidget(self._detail_panel)

        self._splitter.addWidget(self._left_frame)
        self._splitter.addWidget(self._right_frame)
        self._splitter.setStretchFactor(0, 7)
        self._splitter.setStretchFactor(1, 3)

        layout.addWidget(self._splitter, 1)

    def _build_settings(self) -> None:
        f = self._settings_frame
        f_lay = QVBoxLayout(f)
        f_lay.setContentsMargins(10, 8, 10, 8)
        f_lay.setSpacing(4)

        # Row 1: Opacity
        row1 = QWidget()
        row1_lay = QHBoxLayout(row1)
        row1_lay.setContentsMargins(0, 0, 0, 0)
        row1_lay.setSpacing(8)
        row1_lay.addWidget(QLabel("Opacity:"))
        self._opacity_slider = QSlider(Qt.Horizontal)
        self._opacity_slider.setRange(30, 100)
        self._opacity_slider.setValue(int(self._opacity * 100))
        self._opacity_slider.valueChanged.connect(self._on_opacity_change)
        row1_lay.addWidget(self._opacity_slider)
        f_lay.addWidget(row1)

        # Row 2: Always on top
        row2 = QWidget()
        row2_lay = QHBoxLayout(row2)
        row2_lay.setContentsMargins(0, 0, 0, 0)
        self._topmost_cb = QCheckBox("Always on top")
        self._topmost_cb.setChecked(True)
        self._topmost_cb.toggled.connect(self._on_topmost_change)
        row2_lay.addWidget(self._topmost_cb)
        f_lay.addWidget(row2)

        # Row 3: Cache TTL + Refresh
        row3 = QWidget()
        row3_lay = QHBoxLayout(row3)
        row3_lay.setContentsMargins(0, 0, 0, 0)
        row3_lay.setSpacing(8)
        row3_lay.addWidget(QLabel("Cache TTL:"))
        self._ttl_combo = QComboBox()
        self._ttl_combo.addItems(list(CACHE_TTL_OPTIONS.keys()))
        self._ttl_combo.setCurrentText("2h")
        self._ttl_combo.currentTextChanged.connect(self._on_ttl_change)
        row3_lay.addWidget(self._ttl_combo)

        refresh_btn = SCButton("  Refresh Data  ", glow_color=P.accent)
        refresh_btn.setProperty("primary", True)
        refresh_btn.clicked.connect(self._refresh_data)
        row3_lay.addWidget(refresh_btn)
        row3_lay.addStretch(1)
        f_lay.addWidget(row3)

    # -- Settings callbacks --------------------------------------------------

    def _show_tutorial(self) -> None:
        from .tutorial import TutorialBubble
        TutorialBubble(self)

    def _toggle_settings(self) -> None:
        if self._settings_visible:
            self._settings_frame.hide()
            self._settings_visible = False
        else:
            self._settings_frame.show()
            self._settings_visible = True

    def _on_opacity_change(self, val: int) -> None:
        self._opacity = val / 100.0
        self.setWindowOpacity(self._opacity)

    def _on_topmost_change(self, checked: bool) -> None:
        self._always_on_top = checked
        flags = self.windowFlags()
        if checked:
            flags |= Qt.WindowStaysOnTopHint
        else:
            flags &= ~Qt.WindowStaysOnTopHint
        self.setWindowFlags(flags | Qt.FramelessWindowHint)
        self.show()

    def _on_ttl_change(self, val: str) -> None:
        self.data.cache_ttl = CACHE_TTL_OPTIONS.get(val, 7200)

    def _refresh_data(self) -> None:
        self.data.cancel()
        self.data.clear_cache()
        self._start_loading(force=True)

    # -- Data loading --------------------------------------------------------

    def _start_loading(self, force: bool = False) -> None:
        if self._fetching:
            return
        self._fetching = True
        self._status_lbl.setText(_("Loading..."))
        self._status_lbl.setStyleSheet(f"font-family: Consolas; font-size: 8pt; color: {P.yellow}; background: transparent;")
        t = threading.Thread(target=self.data.fetch_all, args=(force,), daemon=True)
        t.start()
        self._poll_loading()

    def _poll_loading(self) -> None:
        status = self.data.get_status()
        self._status_lbl.setText(status)
        error = self.data.get_error()
        if self.data.is_loaded():
            if error:
                self._status_lbl.setStyleSheet(f"font-family: Consolas; font-size: 8pt; color: {P.yellow}; background: transparent;")
            else:
                self._status_lbl.setStyleSheet(f"font-family: Consolas; font-size: 8pt; color: {P.green}; background: transparent;")
            self._on_data_loaded()
        elif error and not self.data.is_loaded():
            self._status_lbl.setStyleSheet(f"font-family: Consolas; font-size: 8pt; color: {P.red}; background: transparent;")
            self._fetching = False
        else:
            QTimer.singleShot(POLL_LOADING_MS, self._poll_loading)

    def _on_data_loaded(self) -> None:
        self._fetching = False
        self._update_view()
        self._schedule_auto_refresh()

    def _schedule_auto_refresh(self) -> None:
        if self._auto_refresh_timer:
            self._auto_refresh_timer.stop()
        self._auto_refresh_timer = QTimer(self)
        self._auto_refresh_timer.setSingleShot(True)
        self._auto_refresh_timer.timeout.connect(self._do_auto_refresh)
        self._auto_refresh_timer.start(AUTO_REFRESH_MS)

    def _do_auto_refresh(self) -> None:
        if not self.data.is_loaded():
            self._schedule_auto_refresh()
            return
        if self._fetching:
            QTimer.singleShot(AUTO_REFRESH_RETRY_MS, self._do_auto_refresh)
            return
        self._status_lbl.setText("Auto-refreshing...")
        self._status_lbl.setStyleSheet(f"font-family: Consolas; font-size: 8pt; color: {P.yellow}; background: transparent;")
        self._start_loading(force=True)

    # -- Tab switching -------------------------------------------------------

    def _select_tab(self, name: str, update_view: bool = True) -> None:
        self._current_tab = name
        for tname, lbl in self._tab_labels.items():
            if tname == name:
                lbl.setStyleSheet(f"""
                    QLabel {{
                        font-family: Consolas;
                        font-size: 9pt;
                        color: {P.accent};
                        background: transparent;
                        padding: 3px 4px;
                    }}
                """)
            else:
                lbl.setStyleSheet(f"""
                    QLabel {{
                        font-family: Consolas;
                        font-size: 9pt;
                        color: {P.fg_dim};
                        background: transparent;
                        padding: 3px 4px;
                    }}
                    QLabel:hover {{
                        background-color: {P.bg_input};
                    }}
                """)
        if update_view:
            self._update_view()

    # -- View update ---------------------------------------------------------

    def _update_view(self) -> None:
        if not self.data.is_loaded():
            return

        is_rental = self._current_tab == "Rentals"
        is_ships = self._current_tab == "Ships"

        self._item_table.setVisible(not is_rental and not is_ships)
        self._rental_table.setVisible(is_rental)
        self._ship_table.setVisible(is_ships)

        query = self._search_bar.text().lower().strip()

        if is_rental:
            vehicles = self.data.vehicles
            if query:
                vehicles = [
                    v for v in vehicles
                    if query in (v.get("name") or "").lower()
                    or query in (v.get("company_name") or "").lower()
                ]
            self._rental_table.set_vehicles(vehicles)
        elif is_ships:
            vehicles = self.data.vehicles
            if query:
                vehicles = [
                    v for v in vehicles
                    if query in (v.get("name") or "").lower()
                    or query in (v.get("name_full") or "").lower()
                    or query in (v.get("company_name") or "").lower()
                ]
            self._ship_table.set_vehicles(vehicles)
        else:
            filtered = self._get_filtered_items()
            self._item_table.set_items(filtered)

    def _get_filtered_items(self) -> list[dict]:
        tab = self._current_tab
        query = self._search_bar.text().lower().strip()

        items_by_tab = self.data.items_by_tab
        if items_by_tab:
            items = items_by_tab.get(tab, items_by_tab.get("All", []))
        else:
            items = self.data.items
            if tab != "All":
                items = [it for it in items if item_tab(it) == tab]

        if query:
            si = self.data.search_index
            items = [it for it in items if query in si.get(it.get("id"), "")]

        return items

    # -- Search handling -----------------------------------------------------

    def _on_search_text_changed(self, text: str) -> None:
        self._search_text = text.strip()
        self._update_view()

    def _on_search_debounced(self, text: str) -> None:
        if len(text) >= 2 and self.data.is_loaded():
            results = self._get_search_results(text.lower())
            if results:
                self._show_bubble(results)
            else:
                self._dismiss_bubble()
        else:
            self._dismiss_bubble()

    def _on_search_enter(self) -> None:
        self._dismiss_bubble()
        self._search_text = self._search_bar.text().strip()
        self._update_view()

    def _get_search_results(self, query: str) -> list[dict]:
        matches: list[dict] = []
        for it in self.data.items:
            if query in (it.get("name") or "").lower():
                matches.append(it)
                if len(matches) >= 30:
                    break
        return matches

    def _show_bubble(self, results: list[dict]) -> None:
        self._dismiss_bubble()
        self._bubble = SearchBubble(self, results, self._on_bubble_select)
        self._bubble.position_below(self._search_bar)

    def _dismiss_bubble(self) -> None:
        if self._bubble:
            try:
                self._bubble.close()
            except RuntimeError:
                pass
            self._bubble = None

    def _on_bubble_select(self, item: dict) -> None:
        self._dismiss_bubble()
        tab = item_tab(item)
        self._select_tab(tab)
        self._detail_panel.show_item(item)

    # -- Item / ship selection -----------------------------------------------

    def _on_item_select(self, item: dict) -> None:
        self._detail_panel.show_item(item)

    def _on_ship_select(self, vehicle: dict) -> None:
        self._detail_panel.show_ship(vehicle)

    # -- Double-click → floating detail bubble -------------------------------

    def _cleanup_closed_bubbles(self) -> None:
        """Remove references to bubbles that have been closed."""
        still_open = []
        for b in self._detail_bubbles:
            try:
                if b.isVisible():
                    still_open.append(b)
            except RuntimeError:
                pass  # C++ object already deleted (WA_DeleteOnClose)
        self._detail_bubbles = still_open

    def _on_item_double_click(self, item: dict) -> None:
        self._cleanup_closed_bubbles()
        bubble = ItemDetailBubble(parent=None)
        bubble.show_item(item, self.data)
        bubble.show_near_cursor(QCursor.pos())
        self._detail_bubbles.append(bubble)

    def _on_ship_double_click(self, vehicle: dict) -> None:
        self._cleanup_closed_bubbles()
        bubble = ItemDetailBubble(parent=None)
        bubble.show_ship(vehicle, self.data)
        bubble.show_near_cursor(QCursor.pos())
        self._detail_bubbles.append(bubble)

    # -- IPC command protocol ------------------------------------------------

    def _handle_command(self, cmd: dict) -> None:
        action = cmd.get("type", cmd.get("action", ""))
        if action == "show":
            self.show()
            self.raise_()
        elif action == "hide":
            self.hide()
        elif action == "quit":
            QApplication.instance().quit()
        elif action == "search":
            query = cmd.get("query", "")
            self._search_bar.setText(query)
        elif action == "tab":
            tab = cmd.get("tab", "All")
            self._select_tab(tab)
        elif action == "refresh":
            self._refresh_data()


class _TabLabel(QLabel):
    """Clickable tab label."""
    from PySide6.QtCore import Signal
    tab_clicked = Signal(str)

    def __init__(self, text: str, tab_name: str, parent=None) -> None:
        super().__init__(text, parent)
        self._tab_name = tab_name

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.tab_clicked.emit(self._tab_name)
        super().mousePressEvent(event)


# ---------------------------------------------------------------------------
# Entry-point helper
# ---------------------------------------------------------------------------

def main() -> None:
    """Launch Market Finder from the command line."""
    from shared.crash_logger import init_crash_logging
    log = init_crash_logging("market")
    try:
        set_dpi_awareness()

        parsed = parse_cli_args(sys.argv[1:], {"w": 1100, "h": 720})

        app = QApplication(sys.argv)
        apply_theme(app)

        data = DataService()
        window = MarketFinderApp(
            data,
            x=parsed["x"],
            y=parsed["y"],
            w=parsed["w"],
            h=parsed["h"],
            opacity=parsed["opacity"],
            cmd_file=parsed["cmd_file"],
        )
        window.show()
        sys.exit(app.exec())
    except Exception:
        log.critical("FATAL crash in market main()", exc_info=True)
        sys.exit(1)
