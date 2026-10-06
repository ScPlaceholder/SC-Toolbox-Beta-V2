"""Location browser dialog for the standalone Starmap tool.

The merger of the Trade Hub terminal dialog and the Market Finder items
dialog: one frameless card per map location with two tabs —

  * **Commodities** — what this terminal buys and sells, straight from
    UEX ``commodities_prices_all`` (the Trade Hub tab fed these tables
    from its own route engine; the standalone tool has no route engine,
    so it reads UEX directly). Row click opens the full commodity page
    (cards + trend charts, self-contained in :mod:`.commodity_view`).
  * **Items** — what personal items sell here, from the terminal->items
    index (Market Finder feature, embedded via :class:`ItemsDialog`).

Everything loads off-thread; failures degrade to an inline "no data"
note rather than an error.
"""
from __future__ import annotations

import threading
from typing import Callable, List, Optional

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QHBoxLayout, QHeaderView, QLabel, QPushButton,
    QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from shared.qt.theme import P

from .data import LOC_ALIASES, norm_loc
from .ui import make_close_button
from .items_dialog import ItemsDialog

ACCENT = P.energy_cyan


def _norm(s: str) -> str:
    n = norm_loc(s or "")
    return LOC_ALIASES.get(n, n)


class _CommoditiesLoader(QObject):
    """Fetches + filters UEX prices for one location off-thread."""

    done = Signal(list)

    def __init__(self, location: str, system: str, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._location = location
        self._system = system

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True,
                         name="CommoditiesLoad").start()

    def _run(self) -> None:
        rows: List[dict] = []
        try:
            from . import uex
            tgt = _norm(self._location)
            sys_n = _norm(self._system)
            for r in uex.prices_all():
                try:
                    term = r.get("terminal_name") or r.get("space_station_name") \
                        or r.get("outpost_name") or r.get("city_name") or ""
                    n = _norm(str(term))
                    if not n:
                        continue
                    # System guard: only match within this system.
                    rsys = _norm(str(r.get("star_system_name") or ""))
                    if sys_n and rsys and rsys != sys_n:
                        continue
                    if n == tgt or (tgt and (tgt in n or (len(n) >= 4 and n in tgt))):
                        rows.append(r)
                except Exception:
                    continue
        except Exception:
            rows = []
        self.done.emit(rows)


class _CommodityTable(QWidget):
    def __init__(self, heading: str, cols: List[str],
                 rows: List[tuple], accent: str,
                 on_row: Optional[Callable[[str], None]] = None,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)
        h = QLabel(f"{heading}   ({len(rows)})")
        h.setStyleSheet(
            f"color: {accent}; font-family: Consolas; font-size: 10pt; font-weight: bold; "
            f"background: transparent;")
        v.addWidget(h)
        t = QTableWidget(max(len(rows), 1), 3)
        t.setHorizontalHeaderLabels(cols)
        t.verticalHeader().setVisible(False)
        t.setEditTriggers(QAbstractItemView.NoEditTriggers)
        t.setSelectionBehavior(QAbstractItemView.SelectRows)
        t.setSelectionMode(QAbstractItemView.SingleSelection)
        t.setCursor(Qt.PointingHandCursor)
        t.setShowGrid(False)
        t.setStyleSheet(
            f"QTableWidget{{background:{P.bg_primary}; color:{P.fg}; border:1px solid {P.border}; "
            f"gridline-color:{P.border}; font-size:9pt;}} "
            f"QHeaderView::section{{background:{P.bg_header}; color:{P.fg_dim}; border:none; "
            f"padding:4px; font-weight:bold;}}")
        hdr = t.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        for i, (name, price, scu) in enumerate(rows):
            t.setItem(i, 0, QTableWidgetItem(name))
            it1 = QTableWidgetItem(f"{price:,.0f}" if price else "-")
            it1.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            t.setItem(i, 1, it1)
            it2 = QTableWidgetItem(f"{scu:,.0f}" if scu else "-")
            it2.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            t.setItem(i, 2, it2)
        if on_row is not None:
            t.cellClicked.connect(lambda r, _c, tbl=t: self._clicked(tbl, r, on_row))
        v.addWidget(t, 1)

    @staticmethod
    def _clicked(table: QTableWidget, row: int, on_row: Callable[[str], None]) -> None:
        item = table.item(row, 0)
        if item is not None:
            on_row(item.text())


def _term_name(r: dict) -> str:
    return (r.get("terminal_name") or r.get("space_station_name")
            or r.get("outpost_name") or r.get("city_name") or "?")


class LocationDialog(QDialog):
    """Two-tab location card: commodities (UEX live) + items (index)."""

    def __init__(self, location: str, system: str,
                 items_provider: Callable[[], List[dict]],
                 on_popout: Callable[[dict], None],
                 parent: Optional[QWidget] = None,
                 on_trade_routes: Optional[Callable[[], None]] = None,
                 on_commodity_routes: Optional[Callable[[str], None]] = None,
                 on_commodity_route: Optional[Callable[[str, str, str], None]] = None) -> None:
        # The three on_* hooks are the Trade Hub star map's terminal-panel links
        # (Trade_Hub/starmap/terminal_panel.py "Plot Route" + the commodity page's
        # routes buttons), ported for the Everything Finder. They are
        # only wired when a host hands the panel a live Trade Hub; standalone, they
        # stay None and the dialog looks exactly as before.
        super().__init__(parent)
        self._on_commodity_routes = on_commodity_routes
        self._on_commodity_route = on_commodity_route
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setModal(False)
        self.resize(820, 580)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        card = QWidget()
        card.setStyleSheet(
            f"background: {P.bg_card}; border: 1px solid {ACCENT}; border-radius: 8px;")
        outer.addWidget(card)
        root = QVBoxLayout(card)
        root.setContentsMargins(16, 12, 16, 14)
        root.setSpacing(8)

        head = QHBoxLayout()
        title = QLabel(location)
        title.setStyleSheet(
            f"color: {ACCENT}; font-family: Consolas; font-size: 14pt; font-weight: bold; "
            f"background: transparent;")
        head.addWidget(title)
        head.addStretch(1)
        self._btn_trade_routes = None
        if on_trade_routes is not None:
            btn = QPushButton("Trade routes from here")
            btn.setCursor(Qt.PointingHandCursor)
            btn.setToolTip("Open the Trade Hub routes table filtered to routes that start here")
            btn.setStyleSheet(
                f"QPushButton {{ background: {P.bg_primary}; color: {P.tool_trade}; "
                f"border: 1px solid {P.tool_trade}; border-radius: 4px; padding: 3px 10px; "
                f"font-family: Consolas; font-size: 9pt; }} "
                f"QPushButton:hover {{ background: {P.tool_trade}; color: #1a1400; }}")
            btn.clicked.connect(lambda: (self.close(), on_trade_routes()))
            head.addWidget(btn)
            self._btn_trade_routes = btn
        head.addWidget(make_close_button(self.close))
        root.addLayout(head)

        sub = QLabel(f"{system} system   ·   commodities & items")
        sub.setStyleSheet(f"color: {P.fg_dim}; font-size: 9pt; background: transparent;")
        root.addWidget(sub)

        self._tabs = QTabWidget()
        self._tabs.setStyleSheet(
            f"QTabWidget::pane {{ border: 1px solid {P.border}; background: {P.bg_primary}; }}")
        root.addWidget(self._tabs, 1)

        # Commodities tab — filled when the loader returns.
        self._commodity_host = QWidget()
        cl = QVBoxLayout(self._commodity_host)
        cl.setContentsMargins(10, 10, 10, 10)
        self._commodity_status = QLabel("Loading commodity prices…")
        self._commodity_status.setStyleSheet(
            f"color: {P.fg_dim}; font-family: Consolas; font-size: 9pt; padding: 8px; "
            f"background: transparent;")
        cl.addWidget(self._commodity_status)
        self._tabs.addTab(self._commodity_host, "Commodities")

        # Items tab — the Market Finder items dialog, embedded.
        items_dlg = ItemsDialog(location, system, items_provider, on_popout,
                                parent=self._tabs)
        items_dlg.setWindowFlags(Qt.Widget)
        items_dlg.setAttribute(Qt.WA_TranslucentBackground, False)
        self._tabs.addTab(items_dlg, "Items")
        self._items_dlg = items_dlg

        self._loader = _CommoditiesLoader(location, system, parent=self)
        self._loader.done.connect(self._fill_commodities)
        self._loader.start()

    def _open_commodity(self, name: str) -> None:
        from .commodity_view import CommodityView
        view = CommodityView(name, on_routes=self._on_commodity_routes,
                             on_route=(None if self._on_commodity_route is None else
                                       (lambda dloc, dsys, n=name: self._on_commodity_route(n, dloc, dsys))),
                             parent=self)
        self._commodity_view = view     # keep a ref (non-modal)
        view.show()

    def _fill_commodities(self, rows: List[dict]) -> None:
        host = self._commodity_host
        while host.layout().count():
            it = host.layout().takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        if not rows:
            lbl = QLabel("No commodity data for this location (offline?)")
            lbl.setStyleSheet(
                f"color: {P.fg_dim}; font-family: Consolas; font-size: 9pt; padding: 8px; "
                f"background: transparent;")
            host.layout().addWidget(lbl)
            return

        def _dedupe(rs, price_key, scu_key):
            best = {}
            for r in rs:
                name = str(r.get("commodity_name") or r.get("name") or "?")
                price = float(r.get(price_key) or 0)
                scu = float(r.get(scu_key) or 0)
                if name not in best or price > best[name][1]:
                    best[name] = (name, price, scu)
            return sorted(best.values(), key=lambda t: -(t[2] or 0))

        buy = _dedupe([r for r in rows if (r.get("price_buy") or 0) > 0],
                      "price_buy", "scu_buy")
        sell = _dedupe([r for r in rows if (r.get("price_sell") or 0) > 0],
                       "price_sell", "scu_sell_stock")
        tables = QHBoxLayout()
        tables.setSpacing(12)
        tables.addWidget(_CommodityTable(
            "THEY BUY", ["Commodity", "Price (UEC)", "Stock (SCU)"], buy, P.green,
            on_row=self._open_commodity, parent=host))
        tables.addWidget(_CommodityTable(
            "THEY SELL", ["Commodity", "Price (UEC)", "Demand (SCU)"], sell, ACCENT,
            on_row=self._open_commodity, parent=host))
        host.layout().addLayout(tables, 1)
