"""Pop-out item bubble for the Starmap tool (standalone).

Replaces the Market Finder pop-out's dependency on that app's
ItemDetailBubble with a self-contained card: item name, category, the
price seen at the terminal it was popped from, its location, a
drag-me bar carrying the SC_ITEM_MIME payload (drop it on the Grocery
List) and a one-click "add to Grocery List" button.
"""
from __future__ import annotations

import json
from typing import Callable, Optional

from PySide6.QtCore import Qt, QMimeData, QPoint
from PySide6.QtGui import QDrag, QPainter, QPixmap, QColor
from PySide6.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget,
)

from shared.qt.theme import P
from shared.qt.data_table import SC_ITEM_MIME

from .ui import make_close_button

ACCENT = P.energy_cyan


class _DragBar(QWidget):
    """Top bar: left-press drags the bubble as a grocery-list payload."""

    def __init__(self, item: dict, parent: QWidget) -> None:
        super().__init__(parent)
        self._item = item
        self.setCursor(Qt.DragMoveCursor)
        self.setToolTip("Drag me onto the Grocery List")
        self.setStyleSheet(
            f"background: {P.bg_header}; border-bottom: 1px solid {P.border};")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 4, 8, 4)
        lbl = QLabel("⇢  drag me onto the Grocery List")
        lbl.setStyleSheet(
            f"font-family: Consolas; font-size: 8pt; color: {ACCENT}; background: transparent;")
        lay.addWidget(lbl)

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.LeftButton:
            super().mousePressEvent(event)
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
        pixmap = QPixmap(170, 28)
        pixmap.fill(QColor(P.bg_card))
        painter = QPainter(pixmap)
        painter.setPen(QColor(ACCENT))
        painter.drawText(pixmap.rect(), Qt.AlignCenter,
                         label[:26] + ("…" if len(label) > 26 else ""))
        painter.end()
        drag.setPixmap(pixmap)
        drag.setHotSpot(QPoint(12, 14))
        drag.exec(Qt.CopyAction)


class ItemPopOut(QDialog):
    """Draggable, frameless item detail card."""

    def __init__(self, item: dict,
                 on_add_to_grocery: Optional[Callable[[dict], None]] = None,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._item = item
        self._on_add = on_add_to_grocery
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setModal(False)
        self.resize(360, 0)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        card = QFrame()
        card.setStyleSheet(
            f"background: {P.bg_card}; border: 1px solid {ACCENT}; border-radius: 8px;")
        outer.addWidget(card)
        root = QVBoxLayout(card)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(_DragBar(item, card))

        body = QWidget()
        bl = QVBoxLayout(body)
        bl.setContentsMargins(16, 12, 16, 14)
        bl.setSpacing(8)

        head = QHBoxLayout()
        title = QLabel(str(item.get("name") or "Unknown item"))
        title.setWordWrap(True)
        title.setStyleSheet(
            f"color: {ACCENT}; font-family: Consolas; font-size: 12pt; font-weight: bold; "
            f"background: transparent;")
        head.addWidget(title, 1)
        head.addWidget(make_close_button(self.close))
        bl.addLayout(head)

        def _row(label: str, value: str) -> None:
            if not value:
                return
            r = QHBoxLayout()
            k = QLabel(label)
            k.setStyleSheet(
                f"color: {P.fg_dim}; font-family: Consolas; font-size: 8pt; background: transparent;")
            v = QLabel(value)
            v.setStyleSheet(
                f"color: {P.fg}; font-family: Consolas; font-size: 9pt; background: transparent;")
            r.addWidget(k)
            r.addStretch(1)
            r.addWidget(v)
            bl.addLayout(r)

        price = item.get("price") or 0
        _row("CATEGORY", str(item.get("category") or "—"))
        _row("PRICE HERE", f"{price:,.0f} aUEC" if price and price > 0 else "—")
        _row("LOCATION", str(item.get("location") or ""))
        _row("SYSTEM", str(item.get("system") or ""))

        if on_add_to_grocery is not None:
            add = QLabel("＋ add to Grocery List")
            add.setCursor(Qt.PointingHandCursor)
            add.setToolTip("Add this item to the grocery list")
            add.setStyleSheet(
                f"font-family: Consolas; font-size: 9pt; font-weight: bold; "
                f"color: {P.green}; background: transparent; padding-top: 4px;")
            add.mousePressEvent = lambda _e: self._add()
            bl.addWidget(add)

        root.addWidget(body)

    def _add(self) -> None:
        if self._on_add is not None:
            self._on_add(self._item)

    # Draggable by the drag bar (it doubles as the grocery drag handle, so
    # plain-window dragging lives on the card body instead: press anywhere
    # on the card that is not a control and move).
    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if (event.buttons() & Qt.LeftButton) and getattr(self, "_drag_pos", None) is not None:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()
            return
        super().mouseMoveEvent(event)
