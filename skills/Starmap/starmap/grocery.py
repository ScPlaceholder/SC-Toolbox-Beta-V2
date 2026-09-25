"""Grocery list for the standalone Starmap tool.

A slim port of the Market Finder grocery concept: item pop-outs and
items-dialog rows drop onto this panel (SC_ITEM_MIME) or add via button,
the list persists to ~/.sctoolbox/starmap/grocery.json, and "Plot
route" hands the panel the shopping-stop systems so it can draw a
multi-stop jump route on the galaxy view.
"""
from __future__ import annotations

import json
import os
from typing import Callable, List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QScrollArea, QToolButton, QVBoxLayout, QWidget,
)

from shared.qt.theme import P
from shared.qt.data_table import SC_ITEM_MIME

_STORE_DIR = os.path.join(os.path.expanduser("~"), ".sctoolbox", "starmap")
_STORE_PATH = os.path.join(_STORE_DIR, "grocery.json")

ACCENT = P.energy_cyan


def load_items() -> List[dict]:
    try:
        with open(_STORE_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return [it for it in data if isinstance(it, dict)] if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def save_items(items: List[dict]) -> None:
    try:
        os.makedirs(_STORE_DIR, exist_ok=True)
        tmp = _STORE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(items, fh, default=str)
        os.replace(tmp, _STORE_PATH)
    except OSError:
        pass


class _GroceryRow(QWidget):
    def __init__(self, item: dict, on_remove: Callable[[dict], None], parent: QWidget) -> None:
        super().__init__(parent)
        self._item = item
        self.setStyleSheet(f"background: {P.bg_primary};")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 4, 8, 4)
        lay.setSpacing(8)

        name = QLabel(str(item.get("name") or "Unknown"))
        name.setStyleSheet(
            f"font-family: Consolas; font-size: 9pt; color: {P.fg}; background: transparent;")
        lay.addWidget(name, 1)

        where = item.get("system") or ""
        if where:
            w = QLabel(str(where))
            w.setStyleSheet(
                f"font-family: Consolas; font-size: 7pt; color: {P.fg_dim}; background: transparent;")
            lay.addWidget(w)

        price = item.get("price") or 0
        if price and price > 0:
            p = QLabel(f"{price:,.0f}")
            p.setStyleSheet(
                f"font-family: Consolas; font-size: 8pt; color: {P.green}; background: transparent;")
            lay.addWidget(p)

        rm = QToolButton()
        rm.setText("X")
        rm.setCursor(Qt.PointingHandCursor)
        rm.setToolTip("Remove")
        rm.setStyleSheet(
            f"QToolButton {{ color: {P.red}; background: transparent; border: none; "
            f"font-family: Consolas; font-size: 9pt; }} "
            f"QToolButton:hover {{ color: {P.fg_bright}; }}")
        rm.clicked.connect(lambda _=False: on_remove(self._item))
        lay.addWidget(rm)


class GroceryPanel(QWidget):
    """Dockable grocery list; accepts SC_ITEM_MIME drops."""

    plotRequested = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._items: List[dict] = load_items()
        self.setAcceptDrops(True)
        self.setMinimumWidth(240)
        self.setStyleSheet(f"background: {P.bg_secondary};")

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        head = QHBoxLayout()
        self._title = QLabel("")
        self._title.setStyleSheet(
            f"color: {ACCENT}; font-family: Consolas; font-size: 9pt; font-weight: bold; "
            f"background: transparent;")
        head.addWidget(self._title)
        head.addStretch(1)
        clear = QToolButton()
        clear.setText("clear")
        clear.setCursor(Qt.PointingHandCursor)
        clear.setStyleSheet(
            f"QToolButton {{ color: {P.fg_dim}; background: transparent; border: none; "
            f"font-family: Consolas; font-size: 8pt; }} "
            f"QToolButton:hover {{ color: {P.red}; }}")
        clear.setToolTip("Remove everything")
        clear.clicked.connect(self.clear)
        head.addWidget(clear)
        root.addLayout(head)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setStyleSheet(
            f"QScrollArea {{ background: {P.bg_primary}; border: 1px solid {P.border}; }}")
        self._inner = QWidget()
        self._inner.setStyleSheet(f"background: {P.bg_primary};")
        self._rows = QVBoxLayout(self._inner)
        self._rows.setContentsMargins(0, 0, 0, 0)
        self._rows.setSpacing(0)
        self._scroll.setWidget(self._inner)
        root.addWidget(self._scroll, 1)

        self._plot_btn = QToolButton()
        self._plot_btn.setText("Plot shopping route")
        self._plot_btn.setCursor(Qt.PointingHandCursor)
        self._plot_btn.setStyleSheet(
            f"QToolButton {{ color: #10161f; background: {ACCENT}; border: none; "
            f"border-radius: 4px; padding: 6px 10px; font-family: Consolas; "
            f"font-size: 9pt; font-weight: bold; }} "
            f"QToolButton:hover {{ background: {P.sc_cyan}; }} "
            f"QToolButton:disabled {{ background: {P.bg_input}; color: {P.fg_disabled}; }}")
        self._plot_btn.setToolTip(
            "Draw a multi-stop jump route through every system on the list (nearest-first)")
        self._plot_btn.clicked.connect(lambda: self.plotRequested.emit())
        root.addWidget(self._plot_btn)

        hint = QLabel("Drop item pop-outs here,")
        hint2 = QLabel("or use + add to Grocery List")
        for h in (hint, hint2):
            h.setStyleSheet(
                f"color: {P.fg_disabled}; font-size: 7pt; background: transparent;")
            root.addWidget(h)

        self._refresh()

    # api
    def items(self) -> List[dict]:
        return list(self._items)

    def add_item(self, item: dict) -> None:
        if not isinstance(item, dict):
            return
        self._items.append(dict(item))
        save_items(self._items)
        self._refresh()

    def remove_item(self, item: dict) -> None:
        for i, it in enumerate(self._items):
            if it is item or it == item:
                del self._items[i]
                break
        save_items(self._items)
        self._refresh()

    def clear(self) -> None:
        if not self._items:
            return
        self._items = []
        save_items(self._items)
        self._refresh()

    def systems(self) -> List[str]:
        out: List[str] = []
        for it in self._items:
            s = str(it.get("system") or "").strip()
            if s and s not in out:
                out.append(s)
        return out

    # internals
    def _refresh(self) -> None:
        while self._rows.count():
            it = self._rows.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        for i, item in enumerate(self._items):
            row = _GroceryRow(item, self.remove_item, self._inner)
            if i % 2 == 1:
                row.setStyleSheet(f"background: {P.bg_input};")
            self._rows.addWidget(row)
        self._rows.addStretch(1)
        n = len(self._items)
        self._title.setText(f"GROCERY LIST ({n})")
        self._plot_btn.setEnabled(n > 0)

    # dnd
    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasFormat(SC_ITEM_MIME):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        raw = bytes(event.mimeData().data(SC_ITEM_MIME)).decode("utf-8", "replace")
        try:
            item = json.loads(raw)
        except (ValueError, TypeError):
            return
        self.add_item(item)
        event.acceptProposedAction()
