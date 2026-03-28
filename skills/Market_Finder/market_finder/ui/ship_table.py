"""Ship / vehicle table — PySide6 version using SCTable."""

from __future__ import annotations

import logging
from typing import Callable, Optional

import shared.path_setup  # noqa: E402  # centralised path config
from shared.i18n import s_ as _

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
)

from shared.qt.theme import P
from shared.qt.data_table import SCTable, ColumnDef
from shared.qt.search_bar import SCSearchBar

from ..config import SHIP_TABLE_BATCH
from ..service import DataService

log = logging.getLogger(__name__)


def _fmt_num(val: object) -> str:
    if not val:
        return "\u2014"
    try:
        n = float(val)
        if n >= 1_000_000:
            return f"{n / 1_000_000:.1f}M"
        if n >= 1000:
            return f"{n / 1000:.0f}K"
        return f"{n:,.0f}"
    except (ValueError, TypeError):
        return str(val)


class ShipTable(QWidget):
    """All vehicles with specs, type filter pills, and SCTable display."""

    def __init__(
        self,
        parent: QWidget,
        data: DataService,
        on_select: Callable[[dict], None] | None = None,
        on_double_click: Callable[[dict], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.data = data
        self._source_vehicles: list[dict] = []
        self._filtered: list[dict] = []
        self._on_select = on_select
        self._on_double_click = on_double_click
        self._show_space = False
        self._show_ground = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Filter bar
        filt_bar = QWidget()
        filt_bar.setFixedHeight(30)
        filt_bar.setStyleSheet(f"background-color: {P.bg_secondary};")
        filt_lay = QHBoxLayout(filt_bar)
        filt_lay.setContentsMargins(8, 0, 8, 0)
        filt_lay.setSpacing(4)

        search_icon = QLabel("\U0001f50d")
        search_icon.setStyleSheet(f"font-size: 9pt; color: {P.fg_dim}; background: transparent;")
        filt_lay.addWidget(search_icon)

        self._filter_input = SCSearchBar(placeholder=_("Filter ships..."), debounce_ms=200)
        self._filter_input.search_changed.connect(self._on_filter)
        self._filter_input.textChanged.connect(lambda _: self._on_filter(self._filter_input.text()))
        filt_lay.addWidget(self._filter_input, 1)

        for t_label in (_("Spaceship"), _("Ground")):
            btn = QPushButton(t_label)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: {P.bg_card};
                    color: {P.fg_dim};
                    border: none;
                    font-family: Consolas;
                    font-size: 7pt;
                    padding: 1px 6px;
                }}
                QPushButton:hover {{
                    color: {P.accent};
                }}
            """)
            btn.setCheckable(True)
            btn.toggled.connect(lambda checked, tl=t_label, b=btn: self._on_type_toggle(tl, checked, b))
            filt_lay.addWidget(btn)

        layout.addWidget(filt_bar)

        # Table columns
        columns = [
            ColumnDef("Ship", "name", width=180, fg_color=P.fg),
            ColumnDef("Manufacturer", "company_name", width=120, fg_color=P.fg_dim),
            ColumnDef("Size", "pad_type", width=45, fg_color=P.fg_dim),
            ColumnDef("Buy Price", "_best_buy_str", width=80, fg_color=P.green),
            ColumnDef("SCU", "scu", width=50, fg_color=P.accent, fmt=lambda v: _fmt_num(v)),
            ColumnDef("Crew", "crew", width=45, fg_color=P.fg_dim, fmt=lambda v: str(v) if v else "\u2014"),
            ColumnDef("QT Fuel", "fuel_quantum", width=55, fg_color=P.fg_dim, fmt=lambda v: _fmt_num(v)),
            ColumnDef("Mass", "mass", width=65, fg_color=P.fg_dim, fmt=lambda v: _fmt_num(v)),
        ]
        self._table = SCTable(columns, self, sortable=True)
        self._table.row_selected.connect(self._on_row_selected)
        self._table.row_double_clicked.connect(self._on_row_double_clicked)
        layout.addWidget(self._table, 1)

        # Count label
        self._count_lbl = QLabel("")
        self._count_lbl.setStyleSheet(f"""
            font-family: Consolas;
            font-size: 8pt;
            color: {P.fg_dim};
            background: transparent;
            padding: 2px 8px;
        """)
        layout.addWidget(self._count_lbl)

    def set_vehicles(self, vehicles: list[dict]) -> None:
        self._source_vehicles = list(vehicles)
        self._apply_filter_and_sort()

    def _on_type_toggle(self, t_label: str, checked: bool, btn: QPushButton) -> None:
        if t_label == "Spaceship":
            self._show_space = checked
        else:
            self._show_ground = checked
        btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {"#1a3030" if checked else P.bg_card};
                color: {P.accent if checked else P.fg_dim};
                border: none;
                font-family: Consolas;
                font-size: 7pt;
                padding: 1px 6px;
            }}
        """)
        self._apply_filter_and_sort()

    def _on_filter(self, text: str = "") -> None:
        self._apply_filter_and_sort()

    def _get_best_buy(self, veh: dict) -> int:
        vid = veh.get("id")
        purchases = self.data.purchase_by_vehicle.get(vid, [])
        if not purchases:
            return 0
        return min(
            (p.get("price_buy", 0) for p in purchases if p.get("price_buy")),
            default=0,
        )

    def _apply_filter_and_sort(self) -> None:
        items = list(self._source_vehicles)
        q = (self._filter_input.text() or "").lower().strip()

        if self._show_space and not self._show_ground:
            items = [v for v in items if v.get("is_spaceship")]
        elif self._show_ground and not self._show_space:
            items = [v for v in items if v.get("is_ground_vehicle")]

        if q:
            items = [
                v for v in items
                if q in (v.get("name") or "").lower()
                or q in (v.get("name_full") or "").lower()
                or q in (v.get("company_name") or "").lower()
            ]

        # Annotate with best buy price string for display
        for v in items:
            best = self._get_best_buy(v)
            v["_best_buy_str"] = _fmt_num(best) if best else "\u2014"

        self._filtered = items
        self._count_lbl.setText(f"{len(items)} ships")
        self._table.set_data(items)

    def _on_row_selected(self, data: dict) -> None:
        if self._on_select:
            self._on_select(data)

    def _on_row_double_clicked(self, data: dict) -> None:
        if self._on_double_click:
            self._on_double_click(data)
