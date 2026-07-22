"""
SCTable – reusable data table with alternating rows, sort, and hover.

Replaces ComponentTable (DPS), VirtualTable (Market), Treeview (Trade Hub),
and custom canvas tables throughout the codebase.
"""

from __future__ import annotations
import json
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from PySide6.QtCore import (
    Qt, QAbstractTableModel, QModelIndex, Signal, QSortFilterProxyModel,
    QMimeData, QPoint,
)
from PySide6.QtGui import QColor, QFont, QFontMetrics, QDrag, QPixmap, QPainter
from PySide6.QtWidgets import (
    QTableView, QWidget, QHeaderView, QAbstractItemView, QStyledItemDelegate,
)

from shared.qt.theme import P

# MIME type used when a row is dragged out of an SCTable.  The payload is the
# row's data dict serialized as UTF-8 JSON, so a drop target in any window can
# reconstruct the item without sharing the source model.
SC_ITEM_MIME = "application/x-sc-item"


class ColumnDef:
    """Definition for a single table column."""

    __slots__ = ("header", "key", "width", "alignment", "fg_color", "fmt", "tooltip")

    def __init__(
        self,
        header: str,
        key: str,
        width: int = 100,
        alignment: Qt.AlignmentFlag = Qt.AlignLeft,
        fg_color: str = "",
        fmt: Optional[Callable[[Any], str]] = None,
        tooltip: str = "",
    ):
        self.header = header
        self.key = key
        self.width = width
        self.alignment = alignment
        self.fg_color = fg_color
        self.fmt = fmt
        self.tooltip = tooltip


class SCTableModel(QAbstractTableModel):
    """Table model backed by a list of dicts."""

    def __init__(self, columns: List[ColumnDef], parent=None, draggable: bool = False):
        super().__init__(parent)
        self._columns = columns
        self._data: List[Dict[str, Any]] = []
        self._draggable = draggable

    def set_data(self, data: List[Dict[str, Any]]) -> None:
        self.beginResetModel()
        self._data = data
        self.endResetModel()

    def row_data(self, row: int) -> Optional[Dict[str, Any]]:
        if 0 <= row < len(self._data):
            return self._data[row]
        return None

    # ── QAbstractTableModel interface ──

    def flags(self, index: QModelIndex):
        """Item flags — add ItemIsDragEnabled so the view can start a drag.

        Without this flag QTableView never enters its drag state even when
        ``setDragEnabled(True)`` is called on the view.
        """
        f = super().flags(index)
        if index.isValid() and self._draggable:
            f |= Qt.ItemIsDragEnabled
        return f

    def rowCount(self, parent=QModelIndex()):
        return len(self._data)

    def columnCount(self, parent=QModelIndex()):
        return len(self._columns)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal:
            if 0 <= section < len(self._columns):
                if role == Qt.DisplayRole:
                    return self._columns[section].header
                if role == Qt.ToolTipRole and self._columns[section].tooltip:
                    return self._columns[section].tooltip
        return None

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row, col = index.row(), index.column()
        if row < 0 or row >= len(self._data):
            return None

        col_def = self._columns[col]
        raw = self._data[row].get(col_def.key, "")

        if role == Qt.DisplayRole:
            if col_def.fmt:
                return col_def.fmt(raw)
            return str(raw) if raw is not None else ""

        if role == Qt.TextAlignmentRole:
            return int(col_def.alignment | Qt.AlignVCenter)

        if role == Qt.ForegroundRole and col_def.fg_color:
            return QColor(col_def.fg_color)

        # Provide raw value for sorting
        if role == Qt.UserRole:
            return raw

        return None


class _RowDelegate(QStyledItemDelegate):
    """Custom delegate for alternating row backgrounds and hover highlight."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._even_bg = QColor(P.bg_primary)
        self._odd_bg = QColor(P.bg_card)

    def initStyleOption(self, option, index):
        super().initStyleOption(option, index)
        if index.row() % 2 == 0:
            option.backgroundBrush = self._even_bg
        else:
            option.backgroundBrush = self._odd_bg


class SCTable(QTableView):
    """Styled table view with alternating rows, sorting, and row selection signal."""

    row_selected = Signal(dict)  # emits the selected row's data dict
    row_double_clicked = Signal(dict)

    def __init__(
        self,
        columns: List[ColumnDef],
        parent: Optional[QWidget] = None,
        sortable: bool = True,
        draggable: bool = False,
    ):
        super().__init__(parent)
        self._columns = columns
        self._draggable = draggable

        # Model
        self._source_model = SCTableModel(columns, self, draggable=draggable)
        if sortable:
            self._proxy = QSortFilterProxyModel(self)
            self._proxy.setSourceModel(self._source_model)
            self._proxy.setSortRole(Qt.UserRole)
            self.setModel(self._proxy)
            self.setSortingEnabled(True)
        else:
            self._proxy = None
            self.setModel(self._source_model)

        # Delegate
        self.setItemDelegate(_RowDelegate(self))

        # Appearance
        self.setAlternatingRowColors(True)
        self.setShowGrid(False)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.verticalHeader().setVisible(False)
        self.horizontalHeader().setStretchLastSection(True)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.horizontalHeader().setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)

        # Drag-out support (opt-in).  When enabled, a row can be dragged
        # onto any widget that accepts the SC_ITEM_MIME format.
        if draggable:
            self.setDragEnabled(True)
            self.setDragDropMode(QAbstractItemView.DragOnly)
            self.setDefaultDropAction(Qt.CopyAction)

        # Column widths
        for i, col in enumerate(columns):
            if col.width > 0:
                self.setColumnWidth(i, col.width)

        # Signals
        self.clicked.connect(self._on_click)
        self.doubleClicked.connect(self._on_double_click)

    def set_data(self, data: List[Dict[str, Any]]) -> None:
        self._source_model.set_data(data)

    def get_selected_row(self) -> Optional[Dict[str, Any]]:
        indexes = self.selectionModel().selectedRows()
        if not indexes:
            return None
        idx = indexes[0]
        if self._proxy:
            idx = self._proxy.mapToSource(idx)
        return self._source_model.row_data(idx.row())

    def _on_click(self, index: QModelIndex):
        if self._proxy:
            index = self._proxy.mapToSource(index)
        data = self._source_model.row_data(index.row())
        if data:
            self.row_selected.emit(data)

    def _on_double_click(self, index: QModelIndex):
        if self._proxy:
            index = self._proxy.mapToSource(index)
        data = self._source_model.row_data(index.row())
        if data:
            self.row_double_clicked.emit(data)

    # ── Drag-out ──

    def startDrag(self, supported_actions):
        """Start a copy-drag carrying the selected row as JSON.

        Overrides the default view drag (which only encodes model indexes,
        valid within this model) so the payload survives crossing into
        another top-level window.
        """
        if not self._draggable:
            return
        row = self.get_selected_row()
        if not row:
            return
        try:
            payload = json.dumps(row, default=str).encode("utf-8")
        except (TypeError, ValueError):
            return

        mime = QMimeData()
        mime.setData(SC_ITEM_MIME, payload)
        label = str(row.get("name") or row.get("name_full") or "")
        if label:
            mime.setText(label)

        drag = QDrag(self)
        drag.setMimeData(mime)
        pixmap = self._drag_pixmap(label)
        if pixmap is not None:
            drag.setPixmap(pixmap)
            drag.setHotSpot(QPoint(14, pixmap.height() // 2))
        drag.exec(Qt.CopyAction)

    @staticmethod
    def _drag_pixmap(text: str) -> Optional[QPixmap]:
        """Render a small labelled chip to follow the cursor during a drag."""
        if not text:
            return None
        try:
            font = QFont("Consolas", 9)
            fm = QFontMetrics(font)
            label = text if len(text) <= 32 else text[:31] + "…"
            w = min(260, fm.horizontalAdvance(label) + 24)
            h = 24
            pm = QPixmap(w, h)
            pm.fill(QColor(0, 0, 0, 0))
            painter = QPainter(pm)
            painter.setRenderHint(QPainter.Antialiasing, True)
            bg = QColor(P.bg_card)
            bg.setAlpha(235)
            painter.setBrush(bg)
            painter.setPen(QColor(P.accent))
            painter.drawRoundedRect(0, 0, w - 1, h - 1, 5, 5)
            painter.setFont(font)
            painter.setPen(QColor(P.fg_bright))
            painter.drawText(pm.rect().adjusted(10, 0, -6, 0), Qt.AlignVCenter | Qt.AlignLeft, label)
            painter.end()
            return pm
        except Exception:
            return None
