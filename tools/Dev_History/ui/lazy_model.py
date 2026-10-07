"""A read-only table model that holds every row but hands them to the view in batches.

Why: the result lists can be tens of thousands of rows (every Dev Tracker post, every
match for a common word). Building one QTableWidgetItem per cell up front costs seconds;
this model builds nothing. Qt asks for cell data only for the rows on screen, and the view
calls ``fetchMore`` when the user scrolls near the bottom, so rows appear in batches as
you scroll (infinite scrolling) while the full list is already in memory and the total is
known immediately.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor, QFont

ROLE_ROW = Qt.UserRole          # the row's dict, from column 0


@dataclass
class Column:
    header: str
    text: Callable[[dict], str]
    color: Optional[Callable[[dict], Optional[str]]] = None
    bold: bool = False
    align_center: bool = False
    tooltip: Optional[Callable[[dict], str]] = None


class LazyTableModel(QAbstractTableModel):
    def __init__(self, columns: list[Column], batch: int = 200, parent=None) -> None:
        super().__init__(parent)
        self._cols = columns
        self._batch = max(1, batch)
        self._rows: list[dict] = []
        self._shown = 0
        self._bold = QFont("Consolas")
        self._bold.setBold(True)
        self._colors: dict[str, QColor] = {}

    # ── data in ──────────────────────────────────────────────────────────────
    def set_rows(self, rows: list[dict], show_at_least: int = 0) -> None:
        """Replace everything. The first batch (or ``show_at_least`` rows) is shown at once."""
        self.beginResetModel()
        self._rows = list(rows)
        self._shown = min(len(self._rows), max(self._batch, show_at_least))
        self.endResetModel()

    def total(self) -> int:
        return len(self._rows)

    def row(self, i: int) -> Optional[dict]:
        return self._rows[i] if 0 <= i < self._shown else None

    def ensure_shown(self, i: int) -> None:
        """Load rows up to index *i* (to select a row that is not on screen yet)."""
        if i >= self._shown and i < len(self._rows):
            new = min(len(self._rows), (i // self._batch + 1) * self._batch)
            self.beginInsertRows(QModelIndex(), self._shown, new - 1)
            self._shown = new
            self.endInsertRows()

    def find(self, pred: Callable[[dict], bool]) -> int:
        """Index of the first row (shown or not) matching *pred*, or -1."""
        for i, r in enumerate(self._rows):
            if pred(r):
                return i
        return -1

    # ── incremental fetch ────────────────────────────────────────────────────
    def canFetchMore(self, parent=QModelIndex()) -> bool:
        return not parent.isValid() and self._shown < len(self._rows)

    def fetchMore(self, parent=QModelIndex()) -> None:
        if parent.isValid() or self._shown >= len(self._rows):
            return
        new = min(len(self._rows), self._shown + self._batch)
        self.beginInsertRows(QModelIndex(), self._shown, new - 1)
        self._shown = new
        self.endInsertRows()

    # ── model API ────────────────────────────────────────────────────────────
    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else self._shown

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._cols)

    def headerData(self, section: int, orientation, role=Qt.DisplayRole) -> Any:
        if orientation == Qt.Horizontal and role == Qt.DisplayRole and 0 <= section < len(self._cols):
            return self._cols[section].header
        return None

    def _qcolor(self, name: str) -> QColor:
        c = self._colors.get(name)
        if c is None:
            c = self._colors[name] = QColor(name)
        return c

    def data(self, index: QModelIndex, role=Qt.DisplayRole) -> Any:
        if not index.isValid() or index.row() >= self._shown:
            return None
        r = self._rows[index.row()]
        col = self._cols[index.column()]
        if role == Qt.DisplayRole:
            return col.text(r)
        if role == ROLE_ROW and index.column() == 0:
            return r
        if role == Qt.ForegroundRole and col.color is not None:
            name = col.color(r)
            return self._qcolor(name) if name else None
        if role == Qt.FontRole and col.bold:
            return self._bold
        if role == Qt.TextAlignmentRole and col.align_center:
            return int(Qt.AlignCenter)
        if role == Qt.ToolTipRole:
            return col.tooltip(r) if col.tooltip else col.text(r)
        return None
