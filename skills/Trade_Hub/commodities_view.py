"""Commodities view — every commodity in the game as a clickable card showing
its category and both its average BUY and SELL price. Click a card to open the
same UEX-style CommodityView popup the star map uses (charts, locations, routes).
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QScrollArea, QVBoxLayout,
    QWidget, QSizePolicy,
)

from shared.qt.theme import P

from starmap import uex

_CARD_MIN_W = 210


def _price(v) -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "—"
    return f"{v:,.0f}" if v else "—"


class _Card(QFrame):
    clicked = Signal(str)

    def __init__(self, c: dict) -> None:
        super().__init__()
        self._name = c.get("name", "?")
        self.setObjectName("commCard")
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(84)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        illegal = bool(c.get("is_illegal"))
        accent = P.red if illegal else P.tool_trade
        self.setStyleSheet(
            f"#commCard{{background:{P.bg_card}; border:1px solid {P.border}; border-radius:6px;}} "
            f"#commCard:hover{{border-color:{accent};}}")
        v = QVBoxLayout(self)
        v.setContentsMargins(11, 8, 11, 8)
        v.setSpacing(2)
        top = QHBoxLayout(); top.setSpacing(4)
        nm = QLabel(self._name)
        nm.setStyleSheet(f"color:{P.fg_bright}; font-family:Consolas; font-size:10pt; font-weight:bold;")
        top.addWidget(nm, 1)
        if illegal:
            warn = QLabel("⚠")
            warn.setStyleSheet(f"color:{P.red}; font-size:10pt;")
            top.addWidget(warn)
        v.addLayout(top)
        kind = QLabel((c.get("kind") or "—").title())
        kind.setStyleSheet(f"color:{P.fg_dim}; font-size:8pt;")
        v.addWidget(kind)
        prices = QHBoxLayout(); prices.setSpacing(14)
        buy = QLabel(f"<span style='color:{P.fg_dim}'>BUY</span> "
                     f"<b style='color:{P.tool_trade}'>{_price(c.get('price_buy'))}</b>")
        sell = QLabel(f"<span style='color:{P.fg_dim}'>SELL</span> "
                      f"<b style='color:{P.green}'>{_price(c.get('price_sell'))}</b>")
        for lab in (buy, sell):
            lab.setTextFormat(Qt.RichText)
            lab.setStyleSheet("font-family:Consolas; font-size:9pt;")
        prices.addWidget(buy); prices.addWidget(sell); prices.addStretch(1)
        v.addLayout(prices)

    def mouseReleaseEvent(self, ev) -> None:
        if ev.button() == Qt.LeftButton and self.rect().contains(ev.position().toPoint()):
            self.clicked.emit(self._name)


class CommoditiesView(QWidget):
    def __init__(self, th, parent=None) -> None:
        super().__init__(parent)
        self._th = th
        self._loaded = False
        self._cards: list = []        # (name_l, code_l, kind_l, _Card)
        self._cols = 4

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        head = QLabel("  COMMODITIES   ·   every commodity in the game — click a card to open its page")
        head.setStyleSheet(f"background:{P.bg_header}; color:{P.tool_trade}; font-family:Consolas; "
                           f"font-size:10pt; font-weight:bold; padding:8px 6px;")
        root.addWidget(head)
        self._search = QLineEdit()
        self._search.setPlaceholderText("Search by name, code or category…")
        self._search.setStyleSheet(
            f"QLineEdit{{background:{P.bg_input}; color:{P.fg}; border:1px solid {P.border}; "
            f"border-radius:4px; padding:6px 10px; font-size:10pt; margin:8px 8px 2px;}}")
        self._search.textChanged.connect(lambda *_: self._relayout())
        root.addWidget(self._search)
        self._count = QLabel("")
        self._count.setStyleSheet(f"color:{P.fg_dim}; font-size:8pt; padding:0 12px 4px;")
        root.addWidget(self._count)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(f"QScrollArea{{border:none; background:{P.bg_primary};}}")
        self._container = QWidget()
        self._grid = QGridLayout(self._container)
        self._grid.setContentsMargins(10, 8, 10, 12)
        self._grid.setSpacing(8)
        self._grid.setAlignment(Qt.AlignTop)
        scroll.setWidget(self._container)
        root.addWidget(scroll, 1)

    # ── lazy load ──────────────────────────────────────────────────────────────
    def showEvent(self, ev) -> None:
        super().showEvent(ev)
        if not self._loaded:
            self._loaded = True
            self._load()

    def _load(self) -> None:
        try:
            coms = uex.commodities()
        except Exception:
            coms = []
        for c in sorted(coms, key=lambda x: (x.get("name") or "").lower()):
            card = _Card(c)
            card.clicked.connect(self._open)
            self._cards.append(((c.get("name") or "").lower(), (c.get("code") or "").lower(),
                                (c.get("kind") or "").lower(), card))
        self._relayout()

    def resizeEvent(self, ev) -> None:
        super().resizeEvent(ev)
        cols = max(1, (self.width() - 36) // _CARD_MIN_W)
        if cols != self._cols and self._cards:
            self._cols = cols
            self._relayout()

    def _relayout(self) -> None:
        while self._grid.count():
            it = self._grid.takeAt(0)
            w = it.widget()
            if w is not None:
                w.setParent(None)
        for i in range(12):
            self._grid.setColumnStretch(i, 1 if i < self._cols else 0)
        t = (self._search.text() or "").strip().lower()
        shown = 0
        for nl, cl, kl, card in self._cards:
            if t and t not in nl and t not in cl and t not in kl:
                continue
            self._grid.addWidget(card, shown // self._cols, shown % self._cols)
            card.show()
            shown += 1
        self._count.setText(f"{shown} commodities")

    def _open(self, name: str) -> None:
        th = self._th
        if name and hasattr(th, "_starmap_panel") and hasattr(th._starmap_panel, "_open_commodity"):
            th._starmap_panel._open_commodity(name)
