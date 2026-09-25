"""Items-at-location dialog for the Market Finder star map.

Answers "what items sell at this location?" — the Market Finder analogue of
the Trade Hub's terminal commodity dialog.  Rows are resolved lazily from
the terminal->items index (which loads off-thread), each row can be

  * clicked / double-clicked  -> pops out a full ItemPopOut detail bubble
  * dragged                    -> dropped onto the Grocery List bubble

Styling follows Market Finder (tool_market accent), not Trade Hub.
"""
from __future__ import annotations

import json
from typing import Callable, List, Optional

from PySide6.QtCore import Qt, QMimeData, QPoint, QTimer
from PySide6.QtGui import QDrag, QPixmap, QPainter, QColor
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QScrollArea, QVBoxLayout, QWidget,
)

from shared.qt.theme import P
from shared.qt.data_table import SC_ITEM_MIME

from .ui import make_close_button

_ROW_CAP = 400     # a hub like Area18 sells thousands; cap keeps the dialog snappy


class _ItemRow(QWidget):
    """One item row: name + price, draggable, click -> pop-out."""

    def __init__(self, item: dict, price: float,
                 on_popout: Callable[[dict], None], parent: QWidget) -> None:
        super().__init__(parent)
        self._item = item
        self._on_popout = on_popout
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("Click: pop out details · Drag: add to Grocery List")
        self.setStyleSheet(f"background-color: {P.bg_primary};")

        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 3, 10, 3)
        lay.setSpacing(8)

        name = str(item.get("name") or "Unknown")
        name_lbl = QLabel(name)
        name_lbl.setStyleSheet(
            f"font-family: Consolas; font-size: 9pt; color: {P.fg}; background: transparent;")
        lay.addWidget(name_lbl, 1)

        cat = item.get("category") or item.get("section") or ""
        if cat:
            cat_lbl = QLabel(str(cat))
            cat_lbl.setStyleSheet(
                f"font-family: Consolas; font-size: 7pt; color: {P.fg_dim}; background: transparent;")
            lay.addWidget(cat_lbl)

        price_txt = f"{price:,.0f} aUEC" if price and price > 0 else "—"
        price_lbl = QLabel(price_txt)
        price_lbl.setMinimumWidth(96)
        price_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        price_lbl.setStyleSheet(
            f"font-family: Consolas; font-size: 9pt; font-weight: bold; "
            f"color: {P.green}; background: transparent;")
        lay.addWidget(price_lbl)

    # click -> pop out (deferred so the click handler isn't torn down mid-event)
    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            QTimer.singleShot(0, lambda: self._on_popout(self._item))
            event.accept()
            return
        super().mousePressEvent(event)

    # drag -> grocery list
    def mouseMoveEvent(self, event) -> None:
        if not (event.buttons() & Qt.LeftButton):
            super().mouseMoveEvent(event)
            return
        try:
            payload = json.dumps(self._item, default=str).encode("utf-8")
        except (TypeError, ValueError):
            return
        mime = QMimeData()
        mime.setData(SC_ITEM_MIME, payload)
        label = str(self._item.get("name") or "")
        mime.setText(label)
        drag = QDrag(self)
        drag.setMimeData(mime)
        pixmap = QPixmap(160, 26)
        pixmap.fill(QColor(P.bg_card))
        painter = QPainter(pixmap)
        painter.setPen(QColor(P.tool_market))
        painter.drawText(pixmap.rect(), Qt.AlignCenter,
                         label[:24] + ("…" if len(label) > 24 else ""))
        painter.end()
        drag.setPixmap(pixmap)
        drag.setHotSpot(QPoint(10, 13))
        drag.exec(Qt.CopyAction)


class ItemsDialog(QDialog):
    """Frameless card listing the items sold at one map location."""

    def __init__(self, location: str, system: str,
                 rows_provider: Callable[[], List[dict]],
                 on_popout: Callable[[dict], None],
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setModal(False)
        self.resize(430, 560)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        card = QWidget()
        card.setStyleSheet(
            f"background: {P.bg_card}; border: 1px solid {P.tool_market}; border-radius: 8px;")
        outer.addWidget(card)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)

        head = QHBoxLayout()
        title = QLabel(location)
        title.setStyleSheet(
            f"color: {P.tool_market}; font-family: Consolas; font-size: 13pt; font-weight: bold;")
        head.addWidget(title)
        head.addStretch(1)
        head.addWidget(make_close_button(self.close))
        lay.addLayout(head)

        sub = QLabel(f"{system} system   ·   items sold here")
        sub.setStyleSheet(f"color: {P.fg_dim}; font-size: 9pt;")
        lay.addWidget(sub)

        self._status = QLabel("Loading item data…")
        self._status.setStyleSheet(
            f"color: {P.fg_dim}; font-family: Consolas; font-size: 9pt; padding: 8px;")
        lay.addWidget(self._status)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet(
            f"QScrollArea {{ background: {P.bg_primary}; border: 1px solid {P.border}; }}")
        self._inner = QWidget()
        self._inner.setStyleSheet(f"background: {P.bg_primary};")
        self._rows_lay = QVBoxLayout(self._inner)
        self._rows_lay.setContentsMargins(0, 0, 0, 0)
        self._rows_lay.setSpacing(0)
        self._scroll.setWidget(self._inner)
        lay.addWidget(self._scroll, 1)

        hint = QLabel("Click a row to pop it out · drag a row onto the Grocery List")
        hint.setStyleSheet(f"color: {P.fg_disabled}; font-size: 8pt;")
        lay.addWidget(hint)

        # Resolve lazily: the index may still be building off-thread.
        def _fill() -> None:
            try:
                rows = rows_provider() or []
            except Exception:
                rows = []
            self._fill_rows(rows, on_popout)
        QTimer.singleShot(0, _fill)

    def _fill_rows(self, rows: List[dict], on_popout) -> None:
        while self._rows_lay.count():
            item = self._rows_lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        total = len(rows)
        if not total:
            self._status.setText("No item data for this location (offline?)")
            return
        self._status.setText(f"{min(total, _ROW_CAP)}{'+' if total > _ROW_CAP else ''} items")
        for i, row in enumerate(rows[:_ROW_CAP]):
            w = _ItemRow(row.get("item") or {}, row.get("price") or 0, on_popout, self._inner)
            if i % 2 == 1:
                w.setStyleSheet(f"background-color: {P.bg_input};")
            self._rows_lay.addWidget(w)
        self._rows_lay.addStretch(1)
