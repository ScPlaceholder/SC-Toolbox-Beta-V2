"""Terminal info panel — shows a location's buy/sell commodities + a Plot Route
button that opens the normal Trade Hub routes view filtered to this location.

Phase 1 sources data from the Trade Hub's already-loaded routes (commodity, price,
SCU). The full UEX-style columns (quality, inventory, stock/price min/max/avg) and
the per-commodity detail view with trend charts are a follow-up that pulls
``commodities_prices`` / ``commodities_prices_history`` directly from the UEX API.
"""
from __future__ import annotations

from typing import Callable, List, Optional, Tuple

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QHBoxLayout, QHeaderView, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from shared.qt.theme import P

from .data import LOC_ALIASES, norm_loc


def _norm(s: str) -> str:
    n = norm_loc(s or "")
    return LOC_ALIASES.get(n, n)


def _dedupe(rows: List[Tuple[str, float, int]]) -> List[Tuple[str, float, int]]:
    """One row per commodity (best price), sorted by stock/demand descending."""
    best = {}
    for name, price, scu in rows:
        if name not in best or price > best[name][1]:
            best[name] = (name, price, scu)
    return sorted(best.values(), key=lambda r: -(r[2] or 0))


class TerminalDialog(QDialog):
    def __init__(self, location: str, system: str, routes: list,
                 on_plot: Callable[[str, str], None],
                 on_commodity: Optional[Callable[[str], None]] = None, parent=None) -> None:
        super().__init__(parent)
        self._location = location
        self._system = system
        self._on_plot = on_plot
        self._on_commodity = on_commodity
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setModal(False)
        self.resize(760, 540)

        tgt = _norm(location)
        buy = _dedupe([(r.commodity, r.price_buy, r.scu_available)
                       for r in routes if _norm(r.buy_location) == tgt and r.price_buy])
        sell = _dedupe([(r.commodity, r.price_sell, r.scu_demand)
                        for r in routes if _norm(r.sell_location) == tgt and r.price_sell])

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        card = QWidget()
        card.setStyleSheet(
            f"background:{P.bg_card}; border:1px solid {P.tool_trade}; border-radius:8px;")
        outer.addWidget(card)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(10)

        head = QHBoxLayout()
        title = QLabel(location)
        title.setStyleSheet(
            f"color:{P.tool_trade}; font-family:Consolas; font-size:15pt; font-weight:bold;")
        head.addWidget(title)
        head.addStretch(1)
        btn_plot = QPushButton("⤳ Plot Route")
        btn_plot.setCursor(Qt.PointingHandCursor)
        btn_plot.setStyleSheet(
            f"QPushButton{{background:{P.tool_trade}; color:#1a1400; border:none; border-radius:5px; "
            f"padding:6px 18px; font-family:Consolas; font-size:10pt; font-weight:bold;}} "
            f"QPushButton:hover{{background:#ffd84a;}}")
        btn_plot.clicked.connect(self._plot)
        head.addWidget(btn_plot)
        btn_close = QPushButton("✕")
        btn_close.setFixedSize(28, 28)
        btn_close.setCursor(Qt.PointingHandCursor)
        btn_close.setStyleSheet(
            f"QPushButton{{background:{P.bg_input}; color:{P.fg_dim}; border:1px solid {P.border}; "
            f"border-radius:5px; font-size:11pt;}} "
            f"QPushButton:hover{{color:{P.red}; border-color:{P.red};}}")
        btn_close.clicked.connect(self.close)
        head.addWidget(btn_close)
        lay.addLayout(head)

        sub = QLabel(f"{system} system   ·   commodity terminal")
        sub.setStyleSheet(f"color:{P.fg_dim}; font-size:9pt;")
        lay.addWidget(sub)

        tables = QHBoxLayout()
        tables.setSpacing(12)
        tables.addWidget(self._table("BUY", ["Commodity", "Buy (UEC)", "Stock (SCU)"], buy, P.green))
        tables.addWidget(self._table("SELL", ["Commodity", "Sell (UEC)", "Demand (SCU)"], sell, P.energy_cyan))
        lay.addLayout(tables, 1)

        note = QLabel("Prices from the Trade Hub's current routes. "
                      "Full UEX stats + per-commodity charts coming next.")
        note.setStyleSheet(f"color:{P.fg_disabled}; font-size:8pt;")
        lay.addWidget(note)

    def _table(self, heading: str, cols: List[str],
               rows: List[Tuple[str, float, int]], accent: str) -> QWidget:
        box = QWidget()
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)
        h = QLabel(f"{heading}   ({len(rows)})")
        h.setStyleSheet(f"color:{accent}; font-family:Consolas; font-size:10pt; font-weight:bold;")
        v.addWidget(h)
        t = QTableWidget(len(rows), 3)
        t.setHorizontalHeaderLabels(cols)
        t.verticalHeader().setVisible(False)
        t.setEditTriggers(QAbstractItemView.NoEditTriggers)
        t.setSelectionBehavior(QAbstractItemView.SelectRows)
        t.setSelectionMode(QAbstractItemView.SingleSelection)
        t.setCursor(Qt.PointingHandCursor)
        t.cellClicked.connect(lambda r, _c, tbl=t: self._row_clicked(tbl, r))
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
            it1 = QTableWidgetItem(f"{price:,.0f}")
            it1.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            t.setItem(i, 1, it1)
            it2 = QTableWidgetItem(f"{scu:,}" if scu else "—")
            it2.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            t.setItem(i, 2, it2)
        v.addWidget(t, 1)
        return box

    def _row_clicked(self, table, row: int) -> None:
        item = table.item(row, 0)
        if item is not None and self._on_commodity:
            self._on_commodity(item.text(), self._location, self._system)

    def _plot(self) -> None:
        try:
            self._on_plot(self._location, self._system)
        finally:
            QTimer.singleShot(0, self.close)
