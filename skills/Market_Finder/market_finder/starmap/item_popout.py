"""Pop-out item bubble for the star map.

Reuses the main window's ItemDetailBubble (full item details + prices) and
adds the shopping-list affordances the map needs: a "drag me" bar whose
drag carries the SC_ITEM_MIME payload (so it can be dropped onto the
Shopping List) and a one-click "add to shopping list" button.
"""
from __future__ import annotations

import json
from typing import Callable, Optional

from PySide6.QtCore import Qt, QMimeData, QPoint
from PySide6.QtGui import QDrag, QPixmap, QPainter, QColor
from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget

from shared.qt.theme import P
from shared.qt.data_table import SC_ITEM_MIME

from ..ui.widgets import ItemDetailBubble


class _DragBar(QWidget):
    """Bar that starts a shopping-list drag on left-press."""

    def __init__(self, item: dict, parent: QWidget) -> None:
        super().__init__(parent)
        self._item = item
        self.setCursor(Qt.DragMoveCursor)
        self.setToolTip("Drag me onto the Shopping List")
        self.setStyleSheet(f"""
            background: {P.bg_header};
            border-bottom: 1px solid {P.border};
        """)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 3, 8, 3)
        lbl = QLabel("\U0001f6d2  drag me onto the Shopping List  ⇢")
        lbl.setStyleSheet(
            f"font-family: Consolas; font-size: 8pt; color: {P.tool_market}; "
            f"background: transparent;")
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
        pixmap = QPixmap(160, 28)
        pixmap.fill(QColor(P.bg_card))
        painter = QPainter(pixmap)
        painter.setPen(QColor(P.tool_market))
        painter.drawText(pixmap.rect(), Qt.AlignCenter,
                         label[:24] + ("…" if len(label) > 24 else ""))
        painter.end()
        drag.setPixmap(pixmap)
        drag.setHotSpot(QPoint(12, 14))
        drag.exec(Qt.CopyAction)


class ItemPopOut(ItemDetailBubble):
    """ItemDetailBubble + grocery drag bar + add-to-list button."""

    def __init__(self, item: dict,
                 on_add_to_grocery: Optional[Callable[[dict], None]] = None,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._mf_item = item
        self._on_add = on_add_to_grocery

        self._drag_bar = _DragBar(item, self)
        self._content_layout.insertWidget(0, self._drag_bar)

        self._add_btn: Optional[QLabel] = None
        if on_add_to_grocery is not None:
            add = QLabel("＋ add to Shopping List")
            add.setCursor(Qt.PointingHandCursor)
            add.setStyleSheet(
                f"font-family: Consolas; font-size: 8pt; font-weight: bold; "
                f"color: {P.tool_market}; background: transparent; padding: 3px 8px;")
            add.setToolTip("Add this item to the shopping list")
            add.mousePressEvent = lambda _e: self._add()
            self._add_btn = add
            self._content_layout.insertWidget(1, add)

    def _clear_content(self) -> None:
        """ItemDetailBubble._clear_content() wipes EVERY row in the content
        layout — including our drag bar and add button, which were inserted
        before show_item()/show_ship() runs.  Clear first, then re-attach the
        grocery affordances so they survive every (re)render."""
        super()._clear_content()
        self._content_layout.insertWidget(0, self._drag_bar)
        if self._add_btn is not None:
            self._content_layout.insertWidget(1, self._add_btn)

    def _add(self) -> None:
        if self._on_add is not None:
            self._on_add(self._mf_item)
