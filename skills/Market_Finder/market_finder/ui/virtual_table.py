"""Item table using SCTable — PySide6 version."""

from __future__ import annotations

from typing import Callable, Optional

import shared.path_setup  # noqa: E402  # centralised path config

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget, QVBoxLayout

from shared.qt.data_table import SCTable, ColumnDef
from shared.qt.theme import P


class VirtualTable(QWidget):
    """Item table backed by SCTable with sorting and selection.

    Drop-in replacement for the tkinter Canvas-based VirtualTable.
    """

    COLUMNS = [
        ColumnDef("Name", "name", width=260, fg_color=P.fg),
        ColumnDef("Category", "category", width=130, fg_color=P.fg_dim),
        ColumnDef("Section", "section", width=110, fg_color=P.fg_dim),
        ColumnDef("Manufacturer", "company_name", width=130, fg_color=P.fg_dim),
    ]

    def __init__(
        self,
        parent: QWidget,
        on_select: Callable[[dict], None] | None = None,
        on_double_click: Callable[[dict], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self._on_select = on_select
        self._on_double_click = on_double_click

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._table = SCTable(self.COLUMNS, self, sortable=True)
        self._table.row_selected.connect(self._on_row_selected)
        self._table.row_double_clicked.connect(self._on_row_double_clicked)
        layout.addWidget(self._table)

    def set_items(self, items: list[dict]) -> None:
        """Replace the dataset."""
        self._table.set_data(items)

    def _on_row_selected(self, data: dict) -> None:
        if self._on_select:
            self._on_select(data)

    def _on_row_double_clicked(self, data: dict) -> None:
        if self._on_double_click:
            self._on_double_click(data)
