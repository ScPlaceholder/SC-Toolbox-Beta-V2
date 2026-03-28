"""Rental table with expandable rows — PySide6 version using SCTable."""

from __future__ import annotations

import logging

import shared.path_setup  # noqa: E402  # centralised path config

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget, QVBoxLayout

from shared.qt.theme import P
from shared.qt.data_table import SCTable, ColumnDef

from ..service import DataService

log = logging.getLogger(__name__)


class RentalTable(QWidget):
    """Displays rentable vehicles with expandable rental-location rows.

    Uses a hybrid approach: the main vehicle list uses SCTable,
    and expanded rental rows are shown in a separate area below
    when the user clicks a row.
    """

    def __init__(
        self,
        parent: QWidget,
        data: DataService,
        on_select: callable = None,
        on_double_click: callable = None,
    ) -> None:
        super().__init__(parent)
        self.data = data
        self._vehicles: list[dict] = []
        self._on_select_cb = on_select
        self._on_double_click_cb = on_double_click

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        columns = [
            ColumnDef("Ship", "name", width=220, fg_color=P.fg),
            ColumnDef("Manufacturer", "company_name", width=140, fg_color=P.fg_dim),
            ColumnDef("SCU", "scu", width=60, fg_color=P.fg_dim,
                       fmt=lambda v: str(v) if v else "\u2014"),
            ColumnDef("Crew", "crew", width=50, fg_color=P.fg_dim,
                       fmt=lambda v: str(v) if v else "\u2014"),
            ColumnDef("Locations", "_rental_count", width=80, fg_color=P.accent,
                       fmt=lambda v: str(v) if v else "0",
                       alignment=Qt.AlignRight),
        ]
        self._table = SCTable(columns, self, sortable=True)
        self._table.row_selected.connect(self._on_row_selected)
        self._table.row_double_clicked.connect(self._on_row_double_clicked)
        layout.addWidget(self._table, 1)


    def set_vehicles(self, vehicles: list[dict]) -> None:
        rentable_ids = set(self.data.rental_by_vehicle.keys())
        self._vehicles = [v for v in vehicles if v.get("id") in rentable_ids]
        # Annotate with rental count
        for v in self._vehicles:
            vid = v.get("id")
            v["_rental_count"] = len(self.data.rental_by_vehicle.get(vid, []))

        self._table.set_data(self._vehicles)

    def _on_row_selected(self, data: dict) -> None:
        if self._on_select_cb:
            self._on_select_cb(data)

    def _on_row_double_clicked(self, data: dict) -> None:
        if self._on_double_click_cb:
            self._on_double_click_cb(data)

