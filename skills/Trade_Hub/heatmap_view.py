"""Trade Heatmap view — a tabular breakdown of the reported-route network by the
same categories as the star-map overlay layers:

* **Top Routes** — most profit per run.
* **Trade Flows** — busiest terminal-to-terminal links (how many routes share a link).
* **Activity** — freshest UEX reports (most recently submitted data).

Each row is a real route; double-click opens the standard Route Detail popup
(with its Pin button), so heatmap routes select and pin exactly like the main
route table. Data comes from the Trade Hub's already-loaded ``_all_routes``.
"""
from __future__ import annotations

from collections import Counter
from typing import Callable, List

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QHeaderView, QLabel, QScrollArea, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from shared.qt.theme import P

from starmap.trade_overlay import terminal_recency

LIMIT = 25   # rows per category


def _f(n) -> str:
    try:
        return f"{float(n):,.0f}"
    except (TypeError, ValueError):
        return "—"


class HeatmapView(QWidget):
    def __init__(self, th, parent=None) -> None:
        super().__init__(parent)
        self._th = th
        self._table_routes: dict = {}        # QTableWidget -> [Route] (row index map)
        self._tables: dict = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        head = QLabel("  TRADE HEATMAP   ·   reported-route breakdown — double-click a row to open & pin")
        head.setStyleSheet(
            f"background:{P.bg_header}; color:{P.tool_trade}; font-family:Consolas; "
            f"font-size:10pt; font-weight:bold; padding:8px 6px;")
        root.addWidget(head)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(f"QScrollArea{{border:none; background:{P.bg_primary};}}")
        root.addWidget(scroll, 1)
        body = QWidget()
        scroll.setWidget(body)
        self._body = QVBoxLayout(body)
        self._body.setContentsMargins(12, 10, 12, 14)
        self._body.setSpacing(8)

        for key, title, sub, metric, accent in (
            ("top", "TOP ROUTES", "most profit per run", "Margin/SCU", P.tool_trade),
            ("flows", "TRADE FLOWS", "busiest terminal-to-terminal links", "On link", P.energy_cyan),
            ("activity", "ACTIVITY", "freshest UEX reports", "Reported", P.green),
        ):
            lbl = QLabel(f"{title}")
            lbl.setStyleSheet(f"color:{accent}; font-family:Consolas; font-size:11pt; font-weight:bold;")
            sb = QLabel(sub)
            sb.setStyleSheet(f"color:{P.fg_dim}; font-size:8pt;")
            self._body.addWidget(lbl)
            self._body.addWidget(sb)
            t = self._make_table(metric)
            self._body.addWidget(t)
            self._tables[key] = t
        self._body.addStretch(1)

    # ── construction ──────────────────────────────────────────────────────────
    def _make_table(self, metric_header: str) -> QTableWidget:
        t = QTableWidget(0, 5)
        t.setHorizontalHeaderLabels(["Commodity", "Buy @", "Sell @", "Profit (aUEC)", metric_header])
        t.verticalHeader().setVisible(False)
        t.setEditTriggers(QAbstractItemView.NoEditTriggers)
        t.setSelectionBehavior(QAbstractItemView.SelectRows)
        t.setSelectionMode(QAbstractItemView.SingleSelection)
        t.setCursor(Qt.PointingHandCursor)
        t.cellDoubleClicked.connect(lambda r, _c, tb=t: self._row_open(tb, r))
        t.setStyleSheet(
            f"QTableWidget{{background:{P.bg_primary}; color:{P.fg}; border:1px solid {P.border}; "
            f"gridline-color:{P.border}; font-size:9pt;}} "
            f"QTableWidget::item:selected{{background:{P.bg_card}; color:{P.fg_bright};}} "
            f"QHeaderView::section{{background:{P.bg_header}; color:{P.fg_dim}; border:none; "
            f"padding:4px; font-weight:bold;}}")
        hdr = t.horizontalHeader()
        for col in (0, 1, 2):
            hdr.setSectionResizeMode(col, QHeaderView.Stretch)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        return t

    # ── data ──────────────────────────────────────────────────────────────────
    def refresh(self, routes) -> None:
        routes = list(routes or [])
        rec = terminal_recency()
        traffic = Counter((r.buy_location, r.sell_location) for r in routes)

        def fresh(r):
            vals = [rec[t] for t in (r.id_terminal_buy, r.id_terminal_sell) if t in rec]
            return max(vals) if vals else None

        def prof(r):
            return self._profit(r)

        top = sorted(routes, key=lambda r: -prof(r))[:LIMIT]
        flows = sorted(routes, key=lambda r: (-traffic[(r.buy_location, r.sell_location)], -prof(r)))[:LIMIT]
        freshest = sorted(
            routes, key=lambda r: (-(fresh(r) if fresh(r) is not None else -1.0), -prof(r)))[:LIMIT]

        self._fill(self._tables["top"], top, lambda r: _f(r.margin))
        self._fill(self._tables["flows"], flows,
                   lambda r: str(traffic[(r.buy_location, r.sell_location)]))
        self._fill(self._tables["activity"], freshest, lambda r: self._age(fresh(r)))

    def _fill(self, table: QTableWidget, routes: List, metric: Callable) -> None:
        self._table_routes[table] = routes
        table.setRowCount(len(routes))
        for i, r in enumerate(routes):
            table.setItem(i, 0, QTableWidgetItem(r.commodity))
            table.setItem(i, 1, QTableWidgetItem(f"{r.buy_location}  ·  {r.buy_system}"))
            table.setItem(i, 2, QTableWidgetItem(f"{r.sell_location}  ·  {r.sell_system}"))
            pit = QTableWidgetItem(_f(self._profit(r)))
            pit.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            table.setItem(i, 3, pit)
            mit = QTableWidgetItem(metric(r))
            mit.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            table.setItem(i, 4, mit)
        table.setMinimumHeight(min(640, 40 + len(routes) * 24))

    def _profit(self, r) -> float:
        """Per-run profit for the user's current ship (caps SCU like the route
        detail does + respects the Max-Profit / Reported-Demand toggle) — NOT the
        raw UEX max-quantity 'profit' field, which assumes you move all stock."""
        scu = getattr(self._th, "_ship_scu", 0) or 0
        try:
            return float(r.estimated_profit(scu))
        except Exception:
            return float(getattr(r, "profit", 0) or 0)

    @staticmethod
    def _age(fresh) -> str:
        if fresh is None:
            return "—"
        d = round((1.0 - fresh) * 30.0)
        return "today" if d < 1 else f"~{d}d ago"

    def _row_open(self, table: QTableWidget, row: int) -> None:
        routes = self._table_routes.get(table) or []
        if 0 <= row < len(routes) and hasattr(self._th, "_open_route_detail"):
            self._th._open_route_detail(routes[row])
