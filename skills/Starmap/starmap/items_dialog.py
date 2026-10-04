"""Items-at-location dialog for the Market Finder star map.

Answers "what items sell at this location?" — the Market Finder analogue of
the Trade Hub's terminal commodity dialog.  Rows are resolved lazily from
the terminal->items index (which loads off-thread), each row can be

  * clicked / double-clicked  -> pops out a full ItemPopOut detail bubble
  * dragged                    -> dropped onto the Grocery List bubble

Styling follows Market Finder (tool_market accent), not Trade Hub.

Everything Finder port (2026-10-03): the collapsible per-category grouping and
the refill-when-the-index-lands behaviour come from the Item Finder star map
(market_finder/starmap/items_dialog.py), so the one Star Map has both. The
visibility guard on refill() is dropped here: in this tool the dialog is
embedded as the LocationDialog "Items" tab, and a tab that is not the current
one is hidden, so that guard would have stopped it from ever filling.
"""
from __future__ import annotations

import json
from typing import Callable, Dict, List, Optional

from PySide6.QtCore import Qt, QMimeData, QPoint, QTimer
from PySide6.QtGui import QDrag, QPixmap, QPainter, QColor
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QScrollArea, QVBoxLayout, QWidget,
)

from shared.qt.theme import P
from shared.qt.data_table import SC_ITEM_MIME

from .ui import make_close_button

_ROW_CAP = 400       # per CATEGORY now; rows are only built when a category is opened
_AUTO_EXPAND_MAX = 40  # a location with more items than this opens with every category collapsed


def _category_of(item: dict) -> str:
    return str(item.get("category") or item.get("section") or "Other").strip() or "Other"


class _CategoryHeader(QWidget):
    """Clickable category bar: arrow, name, item count, cheapest price. Click toggles its rows."""

    def __init__(self, name: str, count: int, cheapest: float,
                 on_toggle: Callable[[], None], parent: QWidget) -> None:
        super().__init__(parent)
        self._name, self._count, self._on_toggle = name, count, on_toggle
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("Click to show / hide this category")
        self.setStyleSheet(f"background-color: {P.bg_card};")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 5, 10, 5)
        lay.setSpacing(8)
        self._label = QLabel()
        self._label.setStyleSheet(
            f"font-family: Consolas; font-size: 9pt; font-weight: bold; "
            f"color: {P.tool_market}; background: transparent;")
        lay.addWidget(self._label, 1)
        if cheapest and cheapest > 0:
            low = QLabel(f"from {cheapest:,.0f} aUEC")
            low.setStyleSheet(
                f"font-family: Consolas; font-size: 8pt; color: {P.fg_dim}; background: transparent;")
            lay.addWidget(low)
        self.set_open(False)

    def set_open(self, is_open: bool) -> None:
        arrow = "▾" if is_open else "▸"
        self._label.setText(f"{arrow} {self._name}  ({self._count})")

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            QTimer.singleShot(0, self._on_toggle)
            event.accept()
            return
        super().mousePressEvent(event)


class _ItemRow(QWidget):
    """One item row: name + price, draggable, click -> pop-out."""

    def __init__(self, item: dict, price: float,
                 on_popout: Callable[[dict], None], parent: QWidget,
                 show_category: bool = True) -> None:
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
        if cat and show_category:
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

        # Resolve lazily: the index may still be building off-thread; the map
        # panel calls refill() again when the index done signal lands.
        self._rows_provider = rows_provider
        self._on_popout_cb = on_popout
        QTimer.singleShot(0, self.refill)

    def refill(self) -> None:
        """(Re)resolve rows from the provider.

        Called once right after opening and again when the background items
        index finishes, so a dialog that rendered "no item data" while the
        index was still building gets a second chance to fill.
        """
        try:
            rows = self._rows_provider() or []
        except Exception:
            rows = []
        try:
            self._fill_rows(rows, self._on_popout_cb)
        except RuntimeError:
            pass            # C++ dialog already destroyed

    def _fill_rows(self, rows: List[dict], on_popout) -> None:
        while self._rows_lay.count():
            item = self._rows_lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        total = len(rows)
        if not total:
            self._status.setText("No item data for this location (offline?)")
            return

        # Group by category under a collapsible header per group. Rows arrive
        # pre-sorted by price, so cheapest-first holds inside each group;
        # groups sort A-Z with 'Other' last. Small locations (<= _AUTO_EXPAND_MAX
        # items) start fully expanded; bigger ones start collapsed so the
        # dialog opens as a tidy category list instead of a 400-row wall.
        groups: Dict[str, List[dict]] = {}
        for row in rows:
            groups.setdefault(_category_of(row.get("item") or {}), []).append(row)

        auto_open = total <= _AUTO_EXPAND_MAX
        for cat in sorted(groups, key=lambda c: (c == "Other", c.lower())):
            grows = groups[cat]
            body = QWidget()
            body.setStyleSheet(f"background: {P.bg_primary};")
            body_lay = QVBoxLayout(body)
            body_lay.setContentsMargins(0, 0, 0, 0)
            body_lay.setSpacing(0)
            for i, row in enumerate(grows[:_ROW_CAP]):
                w = _ItemRow(row.get("item") or {}, row.get("price") or 0,
                             on_popout, body, show_category=False)
                if i % 2 == 1:
                    w.setStyleSheet(f"background-color: {P.bg_input};")
                body_lay.addWidget(w)
            count = len(grows)
            if count > _ROW_CAP:
                count = f"{_ROW_CAP}+"
            cheapest = min((r.get("price") or 0) for r in grows
                           if r.get("price")) or 0
            pair: dict = {}
            header = _CategoryHeader(
                cat, count, cheapest,
                on_toggle=lambda p=pair: _toggle_category(p),
                parent=self._inner)
            pair["h"] = header
            pair["b"] = body
            body.setVisible(auto_open)
            header.set_open(auto_open)
            self._rows_lay.addWidget(header)
            self._rows_lay.addWidget(body)

        self._status.setText(f"{total} items · {len(groups)} categories")
        self._rows_lay.addStretch(1)


def _toggle_category(pair: dict) -> None:
    """Collapse / expand one category section and sync the header arrow."""
    body = pair["b"]
    header = pair["h"]
    open_now = not body.isVisible()
    body.setVisible(open_now)
    header.set_open(open_now)
