"""MarketView — the Market Finder browsing view, docked in the Starmap panel.

Copied from the Market Finder tool terminal/items browsing: pick a
terminal on the left (or search every terminal at once), filter items by
name and category, see prices, pop an item out for details, or add it
straight to the grocery list. Data comes from the Starmap terminal to
items index — the same UEX items_prices_all payload the Market Finder
tool joins against the items metadata endpoint.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QPushButton, QSplitter, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from shared.qt.theme import P

_ITEM_ROLE = Qt.UserRole
_MAX_ROWS = 500


def _btn_ss() -> str:
    return (
        f"QPushButton {{ background: {P.bg_card}; color: {P.fg}; "
        f"border: 1px solid {P.border}; padding: 4px 12px; "
        f"font-family: Consolas; font-size: 9pt; }} "
        f"QPushButton:hover {{ color: {P.fg_bright}; border-color: {P.energy_cyan}; }} "
        f"QPushButton:disabled {{ color: {P.fg_disabled}; border-color: {P.border}; }}"
    )


def _money(v) -> str:
    try:
        return f"{float(v):,.0f}"
    except (TypeError, ValueError):
        return "—"


class MarketView(QWidget):
    """Terminal list + item table over the Starmap items index."""

    def __init__(self, panel) -> None:
        super().__init__(panel)
        self._panel = panel
        self._index: Dict[Tuple[str, str], List[dict]] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        head = QLabel("  MARKET FINDER   ·   pick a terminal, or type an item name to search everywhere")
        head.setStyleSheet(f"background:{P.bg_header}; color:{P.energy_cyan}; font-family:Consolas; "
                           f"font-size:10pt; font-weight:bold; padding:8px 6px;")
        root.addWidget(head)

        split = QSplitter(Qt.Horizontal)

        # ── left: terminals ────────────────────────────────────────────────
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(6, 6, 6, 6)
        ll.setSpacing(4)
        self._term_search = QLineEdit()
        self._term_search.setPlaceholderText("Filter terminals...")
        self._term_search.setStyleSheet(self._input_ss())
        self._term_search.textChanged.connect(lambda *_: self._fill_terminals())
        ll.addWidget(self._term_search)
        self._term_list = QListWidget()
        self._term_list.setStyleSheet(self._list_ss())
        self._term_list.currentItemChanged.connect(lambda *_: self._refill())
        ll.addWidget(self._term_list, 1)
        self._term_count = QLabel("")
        self._term_count.setStyleSheet(f"color:{P.fg_dim}; font-size:8pt;")
        ll.addWidget(self._term_count)
        split.addWidget(left)

        # ── right: items ───────────────────────────────────────────────────
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(6, 6, 6, 6)
        rl.setSpacing(4)
        filt = QHBoxLayout()
        filt.setSpacing(4)
        self._item_search = QLineEdit()
        self._item_search.setPlaceholderText("Search items everywhere...")
        self._item_search.setStyleSheet(self._input_ss())
        self._item_search.textChanged.connect(lambda *_: self._refill())
        filt.addWidget(self._item_search, 1)
        self._cat = QComboBox()
        self._cat.setStyleSheet(self._input_ss())
        self._cat.currentIndexChanged.connect(lambda *_: self._refill())
        filt.addWidget(self._cat)
        rl.addLayout(filt)

        self._table = QTableWidget(0, 5)
        self._table.setHorizontalHeaderLabels(["Item", "Category", "Price (aUEC)", "Terminal", "System"])
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setSelectionMode(QTableWidget.SingleSelection)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setSortingEnabled(True)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for c in (1, 2, 3, 4):
            self._table.horizontalHeader().setSectionResizeMode(c, QHeaderView.ResizeToContents)
        self._table.setStyleSheet(self._table_ss())
        self._table.doubleClicked.connect(lambda *_: self._popout())
        self._table.itemSelectionChanged.connect(lambda *_: self._update_buttons())
        rl.addWidget(self._table, 1)

        btns = QHBoxLayout()
        btns.setSpacing(6)
        self._btn_popout = QPushButton("Pop out")
        self._btn_popout.setStyleSheet(_btn_ss())
        self._btn_popout.setToolTip("Full item detail bubble (double-click a row)")
        self._btn_popout.clicked.connect(self._popout)
        self._btn_add = QPushButton("+ Grocery")
        self._btn_add.setStyleSheet(_btn_ss())
        self._btn_add.setToolTip("Add the selected item to the grocery list")
        self._btn_add.clicked.connect(self._add_grocery)
        btns.addWidget(self._btn_popout)
        btns.addWidget(self._btn_add)
        btns.addStretch(1)
        self._rows_lbl = QLabel("")
        self._rows_lbl.setStyleSheet(f"color:{P.fg_dim}; font-size:8pt;")
        btns.addWidget(self._rows_lbl)
        rl.addLayout(btns)
        split.addWidget(right)

        split.setSizes([260, 620])
        root.addWidget(split, 1)
        self._refill()

    # ── data ───────────────────────────────────────────────────────────────
    def set_index(self, index: Dict[Tuple[str, str], List[dict]]) -> None:
        """Swap in a freshly built terminal to items index and refresh."""
        self._index = index or {}
        self._fill_categories()
        self._fill_terminals()
        self._refill()

    def _fill_categories(self) -> None:
        cats = sorted({(e.get("category") or "") for rows in self._index.values()
                       for e in rows} - {""})
        cur = self._cat.currentText()
        self._cat.blockSignals(True)
        self._cat.clear()
        self._cat.addItem("All categories")
        self._cat.addItems(cats)
        if cur in cats or cur == "All categories":
            self._cat.setCurrentText(cur)
        self._cat.blockSignals(False)

    def _fill_terminals(self) -> None:
        t = (self._term_search.text() or "").strip().lower()
        cur = self._term_list.currentItem()
        cur_key = cur.data(_ITEM_ROLE) if cur else None
        self._term_list.blockSignals(True)
        self._term_list.clear()
        all_item = QListWidgetItem("ALL TERMINALS (item search)")
        all_item.setData(_ITEM_ROLE, None)
        self._term_list.addItem(all_item)
        shown = 0
        select_row = 0
        row = 1
        for sys_n, term_n in sorted(self._index.keys(), key=lambda k: (k[1], k[0])):
            label = f"{term_n} · {sys_n}"
            if t and t not in label.lower():
                continue
            it = QListWidgetItem(label)
            it.setData(_ITEM_ROLE, (sys_n, term_n))
            self._term_list.addItem(it)
            if cur_key is not None and (sys_n, term_n) == cur_key:
                select_row = row
            shown += 1
            row += 1
        self._term_list.setCurrentRow(select_row)
        self._term_list.blockSignals(False)
        self._term_count.setText(f"{shown} terminals")

    # ── items table ──────────────────────────────────────────────────────────
    def _wanted_category(self) -> str:
        cur = self._cat.currentText()
        return "" if cur in ("", "All categories") else cur

    def _selected_key(self) -> Optional[Tuple[str, str]]:
        it = self._term_list.currentItem()
        if it is None:
            return None
        return it.data(_ITEM_ROLE)

    def _rows(self) -> List[Tuple[str, str, dict]]:
        """(system, terminal, entry) tuples matching the current filters."""
        cat = self._wanted_category().lower()
        q = (self._item_search.text() or "").strip().lower()
        key = self._selected_key()
        out: List[Tuple[str, str, dict]] = []
        if q:
            # Item search sweeps every terminal (Market Finder behaviour).
            for (sys_n, term_n), entries in self._index.items():
                for e in entries:
                    if q not in (e.get("name") or "").lower():
                        continue
                    if cat and cat not in (e.get("category") or "").lower():
                        continue
                    out.append((sys_n, term_n, e))
        elif key is not None:
            for e in self._index.get(key, []):
                if cat and cat not in (e.get("category") or "").lower():
                    continue
                out.append((key[0], key[1], e))
        out.sort(key=lambda r: (r[2].get("name") or "").lower())
        return out[:_MAX_ROWS]

    def _refill(self) -> None:
        rows = self._rows()
        self._table.setSortingEnabled(False)
        self._table.setRowCount(len(rows))
        for i, (sys_n, term_n, e) in enumerate(rows):
            name = e.get("name") or "Item #%s" % e.get("item_id")
            price = e.get("price_buy") or 0
            values = [name, e.get("category") or "—", _money(price), term_n, sys_n]
            item = {"id": e.get("item_id"), "name": name,
                    "category": e.get("category") or "", "price": price,
                    "location": term_n, "system": sys_n}
            for c, v in enumerate(values):
                cell = QTableWidgetItem(str(v))
                if c == 2:
                    try:
                        cell.setData(Qt.DisplayRole, float(price))
                    except (TypeError, ValueError):
                        pass
                if c == 0:
                    cell.setData(_ITEM_ROLE, item)
                self._table.setItem(i, c, cell)
        self._table.setSortingEnabled(True)
        n = len(rows)
        self._rows_lbl.setText("%d item%s%s" % (n, "" if n == 1 else "s",
                                                " (capped)" if n >= _MAX_ROWS else ""))
        self._update_buttons()

    def _selected_item(self) -> Optional[dict]:
        row = self._table.currentRow()
        if row < 0:
            return None
        cell = self._table.item(row, 0)
        return cell.data(_ITEM_ROLE) if cell is not None else None

    def _update_buttons(self) -> None:
        has = self._selected_item() is not None
        self._btn_popout.setEnabled(has)
        self._btn_add.setEnabled(has)

    # ── actions ──────────────────────────────────────────────────────────────
    def _popout(self) -> None:
        item = self._selected_item()
        if item:
            self._panel._popout_item(item)

    def _add_grocery(self) -> None:
        item = self._selected_item()
        if item:
            self._panel._grocery.add_item(item)
            self._panel.voice_status("added %s to the grocery list" % item.get("name", "item"))

    # ── styles ───────────────────────────────────────────────────────────────
    @staticmethod
    def _input_ss() -> str:
        return (f"QLineEdit, QComboBox {{ background:{P.bg_input}; color:{P.fg}; "
                f"border:1px solid {P.border}; border-radius:4px; padding:5px 8px; "
                f"font-size:9pt; }}")

    @staticmethod
    def _list_ss() -> str:
        return (f"QListWidget {{ background:{P.bg_primary}; color:{P.fg}; "
                f"border:1px solid {P.border}; font-size:9pt; }}")

    @staticmethod
    def _table_ss() -> str:
        return (f"QTableWidget {{ background:{P.bg_primary}; color:{P.fg}; "
                f"border:1px solid {P.border}; gridline-color:{P.border}; "
                f"font-size:9pt; }} "
                f"QHeaderView::section {{ background:{P.bg_header}; color:{P.fg_dim}; "
                f"border:none; border-right:1px solid {P.border}; padding:4px 6px; "
                f"font-family:Consolas; font-size:8pt; }}")
