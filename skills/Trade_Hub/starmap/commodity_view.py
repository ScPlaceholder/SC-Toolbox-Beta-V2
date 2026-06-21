"""Commodity detail page — clones the UEX commodity-info layout.

Best & historical-average buy/sell cards, Buy/Sell/Profit trend line charts, a
Datarunner Reports bar chart, a Locations table, and Wiki/Routes buttons. Data
is fetched live from UEX in a worker thread (uex.CommodityFetcher) so the UI
never blocks; missing/sparse data degrades gracefully.
"""
from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QFrame, QGridLayout, QHBoxLayout, QHeaderView,
    QLabel, QPushButton, QScrollArea, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from shared.qt.theme import P

from .charts import BarChart, LineChart
from .uex import CommodityFetcher


def _fmt(n) -> str:
    try:
        return f"{float(n):,.0f}"
    except (TypeError, ValueError):
        return "—"


class CommodityView(QDialog):
    def __init__(self, name: str, on_routes: Optional[Callable[[str], None]] = None,
                 on_route: Optional[Callable[[str, str], None]] = None,
                 parent=None) -> None:
        super().__init__(parent)
        self._name = name
        self._on_routes = on_routes
        self._on_route = on_route
        self._wiki_url = ""
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setModal(False)
        self.resize(1020, 780)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        card = QWidget()
        card.setStyleSheet(
            f"background:{P.bg_primary}; border:1px solid {P.tool_trade}; border-radius:8px;")
        outer.addWidget(card)
        root = QVBoxLayout(card)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        head = QHBoxLayout()
        head.setContentsMargins(18, 12, 12, 10)
        self._title = QLabel(name)
        self._title.setStyleSheet(
            f"color:{P.tool_trade}; font-family:Consolas; font-size:16pt; font-weight:bold;")
        head.addWidget(self._title)
        head.addStretch(1)
        self._btn_wiki = QPushButton("Wiki")
        self._btn_routes = QPushButton("⤳ Routes")
        for b in (self._btn_wiki, self._btn_routes):
            b.setCursor(Qt.PointingHandCursor)
            b.setStyleSheet(self._btn_ss())
        self._btn_wiki.clicked.connect(self._open_wiki)
        self._btn_routes.clicked.connect(self._routes)
        head.addWidget(self._btn_wiki)
        head.addWidget(self._btn_routes)
        close = QPushButton("✕")
        close.setFixedSize(28, 28)
        close.setCursor(Qt.PointingHandCursor)
        close.setStyleSheet(
            f"QPushButton{{background:{P.bg_input}; color:{P.fg_dim}; border:1px solid {P.border}; "
            f"border-radius:5px; font-size:11pt;}} QPushButton:hover{{color:{P.red}; border-color:{P.red};}}")
        close.clicked.connect(self.close)
        head.addWidget(close)
        headw = QWidget()
        headw.setLayout(head)
        headw.setStyleSheet(f"background:{P.bg_header};")
        root.addWidget(headw)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(f"QScrollArea{{border:none; background:{P.bg_primary};}}")
        root.addWidget(scroll, 1)
        content = QWidget()
        scroll.setWidget(content)
        self._body = QVBoxLayout(content)
        self._body.setContentsMargins(18, 14, 18, 18)
        self._body.setSpacing(14)
        self._loading = QLabel(f"Loading {name} from UEX…")
        self._loading.setAlignment(Qt.AlignCenter)
        self._loading.setStyleSheet(f"color:{P.fg_dim}; font-size:11pt; padding:40px;")
        self._body.addWidget(self._loading)

        self._fetcher = CommodityFetcher()
        self._fetcher.done.connect(self._populate)
        self._fetcher.fetch(name)

    # ── populate (main thread, via queued signal) ───────────────────────────
    def _populate(self, page) -> None:
        self._loading.hide()
        if not page:
            err = QLabel("No UEX data for this commodity (offline or unrecognised).")
            err.setAlignment(Qt.AlignCenter)
            err.setStyleSheet(f"color:{P.fg_dim}; padding:30px;")
            self._body.addWidget(err)
            return

        c = page["commodity"]
        best = page.get("best", {})
        avgs = page.get("avgs", {})
        hist = page.get("history", {}) or {}
        locs = page.get("locations", [])
        self._wiki_url = c.get("wiki", "")
        self._title.setText(f"{c.get('name', self._name)}  ({c.get('code', '')})")

        # Cards
        grid = QGridLayout()
        grid.setSpacing(10)
        bs, ss = best.get("buy_stock"), best.get("sell_stock")
        bp, sp = best.get("buy_price"), best.get("sell_price")
        grid.addWidget(self._card("Buy — Best", f"{_fmt(bs[0])} SCU" if bs else "—",
                                  bs[1] if bs else "", P.tool_trade), 0, 0)
        grid.addWidget(self._card("Sell — Best", f"{_fmt(ss[0])} SCU" if ss else "—",
                                  ss[1] if ss else "", P.green), 0, 1)
        grid.addWidget(self._card("Buy — Best", f"{_fmt(bp[0])} UEC" if bp else "—",
                                  bp[1] if bp else "", P.tool_trade), 0, 2)
        grid.addWidget(self._card("Sell — Best", f"{_fmt(sp[0])} UEC" if sp else "—",
                                  sp[1] if sp else "", P.green), 0, 3)
        grid.addWidget(self._card("Buy — Avg", f"{_fmt(avgs.get('buy_scu'))} SCU", "historical", P.fg), 1, 0)
        grid.addWidget(self._card("Sell — Avg", f"{_fmt(avgs.get('sell_scu'))} SCU", "historical", P.fg), 1, 1)
        grid.addWidget(self._card("Buy — Avg", f"{_fmt(avgs.get('buy_price'))} UEC", "historical", P.fg), 1, 2)
        grid.addWidget(self._card("Sell — Avg", f"{_fmt(avgs.get('sell_price'))} UEC", "historical", P.fg), 1, 3)
        gw = QWidget()
        gw.setLayout(grid)
        self._body.addWidget(gw)

        # Trend charts
        charts = QGridLayout()
        charts.setSpacing(10)
        profit = LineChart("Profit Trend", "Per SCU")
        profit.set_series([{"points": hist.get("profit", []), "color": P.accent, "width": 2.4}])
        buyc = LineChart("Buy Price Trend", "Per SCU")
        buyc.set_series([{"points": hist.get("buy", []), "color": P.orange, "width": 2.4}])
        sellc = LineChart("Sell Price Trend", "Per SCU")
        sellc.set_series([{"points": hist.get("sell", []), "color": P.green, "width": 2.4}])
        for col, ch in enumerate((profit, buyc, sellc)):
            charts.addWidget(ch, 0, col)
        cw = QWidget()
        cw.setLayout(charts)
        self._body.addWidget(cw)
        bar = BarChart("Datarunner Reports", P.purple)
        bar.set_bars(hist.get("reports", []))
        self._body.addWidget(bar)
        if not (hist.get("buy") or hist.get("sell")):
            note = QLabel("Trend lines need several days of UEX price snapshots — "
                          "UEX's history is sparse right now and fills in as "
                          "data-runners resubmit prices over the coming days.")
            note.setStyleSheet(f"color:{P.fg_disabled}; font-size:8pt;")
            self._body.addWidget(note)

        # Locations table
        lt = QLabel(f"Locations   ({len(locs)})   ·   double-click a row to route there")
        lt.setStyleSheet(f"color:{P.energy_cyan}; font-family:Consolas; font-size:11pt; font-weight:bold;")
        self._body.addWidget(lt)
        table = QTableWidget(len(locs), 5)
        table.setHorizontalHeaderLabels(["Terminal", "System", "Buy (UEC)", "Sell (UEC)", "Stock (SCU)"])
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.SingleSelection)
        table.setCursor(Qt.PointingHandCursor)
        table.cellDoubleClicked.connect(lambda r, _c, t=table: self._loc_dbl(t, r))
        table.setStyleSheet(self._table_ss())
        hdr = table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        for col in (1, 2, 3, 4):
            hdr.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        for i, l in enumerate(locs):
            table.setItem(i, 0, QTableWidgetItem(l["terminal"]))
            table.setItem(i, 1, QTableWidgetItem(l["system"]))
            for col, key in ((2, "price_buy"), (3, "price_sell"), (4, "scu_sell")):
                it = QTableWidgetItem(f"{l[key]:,.0f}" if l[key] else "—")
                it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                table.setItem(i, col, it)
        table.setMinimumHeight(min(380, 44 + len(locs) * 26))
        self._body.addWidget(table)

    def _card(self, title: str, value: str, sub: str, accent: str) -> QFrame:
        f = QFrame()
        f.setStyleSheet(f"background:{P.bg_card}; border:1px solid {P.border}; border-radius:6px;")
        v = QVBoxLayout(f)
        v.setContentsMargins(12, 10, 12, 10)
        v.setSpacing(2)
        t = QLabel(title); t.setStyleSheet(f"color:{P.fg_dim}; font-size:8pt;")
        val = QLabel(value)
        val.setStyleSheet(f"color:{accent}; font-family:Consolas; font-size:14pt; font-weight:bold;")
        s = QLabel(sub); s.setStyleSheet(f"color:{P.fg_dim}; font-size:8pt;")
        v.addWidget(t); v.addWidget(val); v.addWidget(s)
        return f

    def _open_wiki(self) -> None:
        if self._wiki_url:
            QDesktopServices.openUrl(QUrl(self._wiki_url))

    def _routes(self) -> None:
        if self._on_routes:
            self._on_routes(self._name)
        QTimer.singleShot(0, self.close)

    def _loc_dbl(self, table, row: int) -> None:
        term = table.item(row, 0)
        syst = table.item(row, 1)
        if term is not None and self._on_route:
            self._on_route(term.text(), syst.text() if syst is not None else "")
            QTimer.singleShot(0, self.close)

    def _btn_ss(self) -> str:
        return (f"QPushButton{{background:{P.bg_card}; color:{P.fg}; border:1px solid {P.border}; "
                f"border-radius:5px; padding:5px 14px; font-family:Consolas; font-size:9pt;}} "
                f"QPushButton:hover{{color:{P.fg_bright}; border-color:{P.tool_trade};}}")

    def _table_ss(self) -> str:
        return (f"QTableWidget{{background:{P.bg_primary}; color:{P.fg}; border:1px solid {P.border}; "
                f"gridline-color:{P.border}; font-size:9pt;}} "
                f"QHeaderView::section{{background:{P.bg_header}; color:{P.fg_dim}; border:none; "
                f"padding:4px; font-weight:bold;}}")
