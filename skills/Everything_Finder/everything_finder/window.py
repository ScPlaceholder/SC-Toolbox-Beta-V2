# Everything Finder -- agent "everything-finder" (claude-opus-5-5 subagent; no runtime agent id exposed)
# written 2026-10-03T21:47-0400, parent: session:7bee459a
"""EverythingFinderWindow - Item Finder, Trade Hub and Star Map under one roof.

    [ EVERYTHING FINDER title bar                                        ]
    [ ITEM FINDER | TRADE HUB | STAR MAP          [Shopping List  ^]    ]
    [ the selected tool                                                  ]

* Tabs are LAZY (:class:`LazyTabStack`): a tool is imported and constructed the
  first time its tab is selected. Opening the window builds only the tab it
  opens on, one event-loop tick after the window is shown.
* Item Finder and Trade Hub are the real tool windows, constructed as their
  launchers construct them and transplanted into the tab (embed.py).
* Star Map is skills/Starmap's StarmapPanel - the one map, which now carries the
  union of the three star maps' features. When Trade Hub is open too, the map
  is handed Trade Hub's routes (overlays) and a link back to its routes table.
* The Shopping List button opens the shared pop-out (items + commodities,
  routes from Trade Hub's basket planner).

Ctrl+Tab / Ctrl+Shift+Tab cycle the tabs.
"""
from __future__ import annotations

import logging
import sys
from typing import Callable, Dict, Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QApplication, QButtonGroup, QHBoxLayout, QPushButton, QWidget

from shared.qt.base_window import SCWindow
from shared.qt.theme import P
from shared.qt.title_bar import SCTitleBar

from .lazy_tabs import Factory, LazyTabStack

log = logging.getLogger(__name__)

ACCENT = "#55ddaa"
TAB_ITEM, TAB_TRADE, TAB_MAP = "item_finder", "trade_hub", "star_map"
TAB_LABELS = {TAB_ITEM: "ITEM FINDER", TAB_TRADE: "TRADE HUB", TAB_MAP: "STAR MAP"}
TAB_COLORS = {TAB_ITEM: P.tool_market, TAB_TRADE: P.tool_trade, TAB_MAP: P.energy_cyan}


def _tab_ss(color: str) -> str:
    return (f"QPushButton {{ background: {P.bg_card}; color: {P.fg_dim}; border: 1px solid {P.border}; "
            f"border-bottom: none; padding: 6px 18px; font-family: Electrolize, Consolas; "
            f"font-size: 9.5pt; font-weight: bold; letter-spacing: 2px; }} "
            f"QPushButton:hover {{ color: {P.fg_bright}; }} "
            f"QPushButton:checked {{ background: {P.bg_primary}; color: {color}; "
            f"border-color: {color}; }}")


class EverythingFinderWindow(SCWindow):
    """One window, three lazily-built tool tabs, one shared shopping list."""

    def __init__(self, x: int = 100, y: int = 100, w: int = 1400, h: int = 900,
                 opacity: float = 0.95, cmd_file: Optional[str] = None,
                 initial_tab: Optional[str] = None,
                 factories: Optional[Dict[str, Factory]] = None,
                 shopping_list=None, shopping_source=None) -> None:
        super().__init__(title="Everything Finder", width=w, height=h,
                         min_w=900, min_h=560, opacity=opacity, always_on_top=True,
                         accent=ACCENT)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowDoesNotAcceptFocus)
        self.restore_geometry_from_args(x, y, w, h, opacity)
        self._opacity = opacity
        self._inner: Dict[str, object] = {}       # tab key -> the tool object behind it
        self._popout = None
        self._shopping_list = shopping_list
        self._shopping_source = shopping_source
        self._opened = False
        self._initial = initial_tab or self._saved_tab() or TAB_ITEM

        self._build_ui(factories or {})

        self._ipc = None
        if cmd_file:
            from shared.qt.ipc_thread import IPCWatcher
            self._ipc = IPCWatcher(cmd_file, poll_ms=500, parent=self)
            self._ipc.command_received.connect(self._handle_command)
            self._ipc.start()

    # construction
    def _build_ui(self, factories: Dict[str, Factory]) -> None:
        lay = self.content_layout
        tb = SCTitleBar(window=self, title="EVERYTHING FINDER", icon_text="\U0001f50e",
                        accent_color=ACCENT, show_minimize=True)
        tb.close_clicked.connect(self.close)
        tb.minimize_clicked.connect(self.showMinimized)
        lay.addWidget(tb)
        self._title_bar = tb

        bar = QWidget()
        bar.setStyleSheet(f"background: {P.bg_secondary};")
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(10, 6, 10, 0)
        bl.setSpacing(4)
        self._tab_group = QButtonGroup(self)
        self._tab_group.setExclusive(True)
        self._tab_buttons: Dict[str, QPushButton] = {}
        for key in (TAB_ITEM, TAB_TRADE, TAB_MAP):
            b = QPushButton(TAB_LABELS[key])
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setStyleSheet(_tab_ss(TAB_COLORS[key]))
            b.clicked.connect(lambda _c=False, k=key: self.select_tab(k))
            self._tab_group.addButton(b)
            bl.addWidget(b)
            self._tab_buttons[key] = b
        bl.addStretch(1)
        self._btn_shop = QPushButton("\U0001f6d2  Shopping List  ⧉")
        self._btn_shop.setCheckable(True)
        self._btn_shop.setCursor(Qt.PointingHandCursor)
        self._btn_shop.setToolTip("Pop out the shared shopping list: items AND commodities, "
                                  "routes worked out by Trade Hub's planner")
        self._btn_shop.setStyleSheet(
            f"QPushButton {{ background: {P.bg_card}; color: {ACCENT}; border: 1px solid {ACCENT}; "
            f"border-radius: 3px; padding: 4px 12px; margin-bottom: 4px; font-family: Consolas; "
            f"font-size: 9pt; font-weight: bold; }} "
            f"QPushButton:hover {{ background: rgba(85, 221, 170, 0.15); }} "
            f"QPushButton:checked {{ background: {ACCENT}; color: #04140d; }}")
        self._btn_shop.clicked.connect(self.toggle_shopping_list)
        bl.addWidget(self._btn_shop)
        lay.addWidget(bar)

        self._tabs = LazyTabStack(self)
        default = {TAB_ITEM: self._make_item_finder, TAB_TRADE: self._make_trade_hub,
                   TAB_MAP: self._make_star_map}
        for key in (TAB_ITEM, TAB_TRADE, TAB_MAP):
            self._tabs.add_tab(key, TAB_LABELS[key], factories.get(key) or default[key])
        self._tabs.tabBuilt.connect(self._on_tab_built)
        self._tabs.currentKeyChanged.connect(self._on_current_changed)
        lay.addWidget(self._tabs, 1)

        nxt = QShortcut(QKeySequence("Ctrl+Tab"), self)
        nxt.activated.connect(lambda: self._tabs.cycle(1))
        prv = QShortcut(QKeySequence("Ctrl+Shift+Tab"), self)
        prv.activated.connect(lambda: self._tabs.cycle(-1))

    # public API
    @property
    def tabs(self) -> LazyTabStack:
        return self._tabs

    def select_tab(self, key: str):
        w = self._tabs.select(key)
        return w

    def open_initial_tab(self) -> None:
        """Build + show the tab the window opens on (once)."""
        if self._opened:
            return
        self._opened = True
        key = self._initial if self._initial in TAB_LABELS else TAB_ITEM
        self.select_tab(key)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self._opened:
            # One tick later, so the window paints before the first tool loads.
            QTimer.singleShot(0, self.open_initial_tab)

    def inner(self, key: str):
        return self._inner.get(key)

    # tab factories (each imports its tool only when called)
    def _geom(self):
        g = self.geometry()
        return g.x(), g.y(), max(g.width(), 900), max(g.height(), 560)

    def _make_item_finder(self) -> QWidget:
        from .embed import embed_window
        from .tool_loader import load_item_finder_window
        x, y, w, h = self._geom()
        win = load_item_finder_window(x, y, w, h, self._opacity)
        self._inner[TAB_ITEM] = win
        # One star map: inside the Everything Finder, Item Finder's "Star Map"
        # button opens the Star Map TAB instead of its own separate map window.
        # (Instance attribute: the standalone Item Finder is untouched.)
        win._toggle_starmap = lambda: self.select_tab(TAB_MAP)
        # hide_title: Item Finder's own bar still says "MARKET FINDER"; the tab names it.
        return embed_window(win, self, on_reveal=lambda: self._reveal(TAB_ITEM), hide_title=True)

    def _make_trade_hub(self) -> QWidget:
        from .embed import embed_window
        from .tool_loader import load_trade_hub_window
        x, y, w, h = self._geom()
        win = load_trade_hub_window(x, y, w, h, self._opacity)
        self._inner[TAB_TRADE] = win
        # Tell the Star Map whenever Trade Hub's routes (re)load. Connected AFTER
        # Trade Hub's own slots, so its _all_routes is already updated.
        sig = getattr(win, "_route_signal", None)
        if sig is not None:
            for name in ("routes_ready", "distances_ready"):
                s = getattr(sig, name, None)
                if s is not None:
                    s.connect(lambda *_a: QTimer.singleShot(0, self._notify_map_routes))
        return embed_window(win, self, on_reveal=lambda: self._reveal(TAB_TRADE))

    def _make_star_map(self) -> QWidget:
        from .tool_loader import load_starmap_panel
        panel = load_starmap_panel()
        self._inner[TAB_MAP] = panel
        sig = getattr(panel, "tradeHubRequested", None)
        if sig is not None:
            sig.connect(lambda: self.select_tab(TAB_TRADE))
        return panel

    def _on_tab_built(self, key: str) -> None:
        # Cross-wire Star Map <-> Trade Hub whichever opened second.
        if key in (TAB_TRADE, TAB_MAP):
            self._link_map_to_trade()

    def _link_map_to_trade(self) -> None:
        panel = self._inner.get(TAB_MAP)
        hub = self._inner.get(TAB_TRADE)
        if panel is not None and hub is not None and hasattr(panel, "set_trade_hub"):
            try:
                panel.set_trade_hub(hub)
            except Exception:
                log.warning("Everything Finder: could not link the Star Map to Trade Hub", exc_info=True)

    def _notify_map_routes(self) -> None:
        panel = self._inner.get(TAB_MAP)
        if panel is not None and getattr(panel, "_trade_hub", None) is not None:
            try:
                panel.on_routes_loaded()
            except Exception:
                log.warning("Everything Finder: Star Map route refresh failed", exc_info=True)

    def _on_current_changed(self, key: str) -> None:
        b = self._tab_buttons.get(key)
        if b is not None and not b.isChecked():
            b.setChecked(True)
        self._save_tab(key)

    def _reveal(self, key: str) -> None:
        self.show()
        self.raise_()
        self.activateWindow()
        self.select_tab(key)

    # shopping list
    def _ensure_shopping(self):
        if self._shopping_list is None:
            from .shopping_list import ShoppingList
            self._shopping_list = ShoppingList().load()
        if self._shopping_source is None:
            from .shopping_source import ShoppingSource
            self._shopping_source = ShoppingSource(
                item_service_getter=lambda: getattr(self._inner.get(TAB_ITEM), "data", None),
                routes_getter=lambda: list(getattr(self._inner.get(TAB_TRADE), "_all_routes", None) or []))
        return self._shopping_list, self._shopping_source

    def shopping_popout(self):
        return self._popout

    def toggle_shopping_list(self) -> None:
        if self._popout is not None and self._popout.isVisible():
            self._popout.hide()
            self._btn_shop.setChecked(False)
            return
        if self._popout is None:
            from .shopping_popout import ShoppingPopout
            lst, src = self._ensure_shopping()
            self._popout = ShoppingPopout(lst, src, on_show_on_map=self.show_plan_on_map, parent=self)
            self._popout.destroyed.connect(lambda *_: setattr(self, "_popout", None))
            self._popout.installEventFilter(self)
            self._popout.position_beside(self)
        self._popout.show()
        self._popout.raise_()
        self._btn_shop.setChecked(True)

    def eventFilter(self, obj, event) -> bool:
        from PySide6.QtCore import QEvent
        if obj is self._popout and event.type() == QEvent.Type.Hide:
            self._btn_shop.setChecked(False)
        return super().eventFilter(obj, event)

    def show_plan_on_map(self, plan) -> None:
        """Draw a shopping-list plan on the Star Map tab (opening it if needed)."""
        from .shopping_list import plan_stops_for_map
        panel = self.select_tab(TAB_MAP)
        if panel is not None and hasattr(panel, "plot_shopping_route"):
            QTimer.singleShot(0, lambda: panel.plot_shopping_route(plan_stops_for_map(plan)))

    # persistence of the last tab
    @staticmethod
    def _state_path() -> str:
        from shared.user_settings import settings_path
        return settings_path("everything_finder", "window.json")

    def _saved_tab(self) -> str:
        try:
            from shared.user_settings import load_json
            return str(load_json(self._state_path()).get("tab") or "")
        except Exception:
            return ""

    def _save_tab(self, key: str) -> None:
        try:
            from shared.user_settings import save_json
            save_json(self._state_path(), {"tab": key})
        except Exception:
            pass

    # geometry sync + teardown
    def _sync_inner_geometry(self) -> None:
        g = self.geometry()
        for key in (TAB_ITEM, TAB_TRADE):
            win = self._inner.get(key)
            if win is not None:
                try:
                    win.setGeometry(g)
                except RuntimeError:
                    pass

    def moveEvent(self, event) -> None:
        super().moveEvent(event)
        self._sync_inner_geometry()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._sync_inner_geometry()

    def _handle_command(self, cmd: dict) -> None:
        action = cmd.get("type", cmd.get("action", ""))
        if action == "show":
            self.show()
            self.raise_()
        elif action == "hide":
            self.hide()
        elif action == "quit":
            QApplication.instance().quit()
        elif action == "tab":
            key = str(cmd.get("tab") or "")
            if key in TAB_LABELS:
                self.select_tab(key)
        elif action == "map_command":
            # The Assistant heard something for the Star Map (it owns the mic; the
            # map has none). Open the Star Map tab and let its command router run it.
            panel = self.select_tab(TAB_MAP)
            if panel is not None and hasattr(panel, "handle_map_command"):
                panel.handle_map_command(cmd)

    def closeEvent(self, event) -> None:
        if self._ipc is not None:
            self._ipc.stop()
        if self._popout is not None:
            self._popout.close()
        panel = self._inner.get(TAB_MAP)
        if panel is not None and hasattr(panel, "shutdown"):
            try:
                panel.shutdown()
            except Exception:
                log.warning("Everything Finder: Star Map shutdown failed", exc_info=True)
        # The tools' own teardown (IPC watchers, bubbles, their star maps). Their
        # SCWindow.closeEvent also saves geometry under THIS process's key, so it
        # must run before ours: ours saves last and wins.
        for key in (TAB_ITEM, TAB_TRADE):
            win = self._inner.get(key)
            if win is not None:
                try:
                    win.close()
                except RuntimeError:
                    pass
        super().closeEvent(event)
