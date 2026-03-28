"""Filter sidebar for Craft Database."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from shared.qt.theme import P
from shared.qt.fuzzy_combo import SCFuzzyCombo
from domain.models import FilterHints
from ui.constants import TOOL_COLOR


class FilterPanel(QFrame):
    """Left-side filter panel mirroring sc-craft.tools filters."""

    filters_changed = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(230)
        self.setStyleSheet(
            f"FilterPanel {{ background: {P.bg_secondary};"
            f"border-right: 1px solid {P.border}; }}"
        )

        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")

        self._container = QWidget()
        self._container.setStyleSheet("background: transparent;")
        self._layout = QVBoxLayout(self._container)
        self._layout.setContentsMargins(10, 8, 10, 8)
        self._layout.setSpacing(6)
        self._scroll.setWidget(self._container)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._scroll)

        self._build_static()

    def _build_static(self):
        # Header
        header_row = QHBoxLayout()
        filter_icon = QLabel("\u2630")
        filter_icon.setStyleSheet(f"color: {P.fg_dim}; font-size: 10pt;")
        header_row.addWidget(filter_icon)
        header_lbl = QLabel("Filters")
        header_lbl.setStyleSheet(
            f"color: {P.fg_bright}; font-size: 11pt; font-weight: bold;"
        )
        header_row.addWidget(header_lbl, 1)
        self._layout.addLayout(header_row)

        self._layout.addSpacing(4)

        # Ownable checkbox
        self._ownable_cb = QCheckBox("Ownable")
        self._ownable_cb.setChecked(True)
        self._ownable_cb.setStyleSheet(f"color: {P.fg}; font-size: 9pt;")
        self._ownable_cb.stateChanged.connect(self._emit_filters)
        self._layout.addWidget(self._ownable_cb)

        self._layout.addSpacing(8)

        # ── CRAFT section
        self._add_section("CRAFT")

        # Blueprint Type (category)
        self._add_label("BLUEPRINT TYPE")
        self._category_combo = SCFuzzyCombo(placeholder="All categories")
        self._category_combo.item_selected.connect(lambda _: self._emit_filters())
        self._layout.addWidget(self._category_combo)

        self._layout.addSpacing(6)

        # Resource
        self._add_label("RESOURCE NEEDED")
        self._resource_combo = SCFuzzyCombo(placeholder="All resources")
        self._resource_combo.item_selected.connect(lambda _: self._emit_filters())
        self._layout.addWidget(self._resource_combo)

        self._layout.addSpacing(6)

        # Mission type
        self._add_label("MISSION TYPE")
        self._mission_combo = SCFuzzyCombo(placeholder="All mission types")
        self._mission_combo.item_selected.connect(lambda _: self._emit_filters())
        self._layout.addWidget(self._mission_combo)

        self._layout.addSpacing(6)

        # Location
        self._add_label("LOCATION")
        self._location_combo = SCFuzzyCombo(placeholder="All locations")
        self._location_combo.item_selected.connect(lambda _: self._emit_filters())
        self._layout.addWidget(self._location_combo)

        self._layout.addSpacing(6)

        # Contractor
        self._add_label("CONTRACTOR")
        self._contractor_combo = SCFuzzyCombo(placeholder="All contractors")
        self._contractor_combo.item_selected.connect(lambda _: self._emit_filters())
        self._layout.addWidget(self._contractor_combo)

        self._layout.addStretch()

        # Clear button
        self._clear_btn = QPushButton("Clear all filters")
        self._clear_btn.setStyleSheet(
            f"QPushButton {{ color: {P.fg}; background: {P.red};"
            f"border: none; border-radius: 4px; padding: 6px; font-weight: bold; }}"
            f"QPushButton:hover {{ background: #ff6644; }}"
        )
        self._clear_btn.clicked.connect(self._clear_all)
        self._layout.addWidget(self._clear_btn)

    def _add_section(self, text: str):
        lbl = QLabel(text)
        lbl.setStyleSheet(
            f"color: {P.fg_dim}; font-size: 7pt; font-weight: bold;"
            f"letter-spacing: 2px;"
        )
        self._layout.addWidget(lbl)

    def _add_label(self, text: str):
        lbl = QLabel(text)
        lbl.setStyleSheet(
            f"color: {P.fg_dim}; font-size: 7pt; font-weight: bold;"
            f"letter-spacing: 1px;"
        )
        self._layout.addWidget(lbl)

    def set_hints(self, hints: FilterHints):
        self._category_combo.clear()
        self._category_combo.addItem("")
        self._category_combo.addItems(hints.categories)

        self._resource_combo.clear()
        self._resource_combo.addItem("")
        self._resource_combo.addItems(hints.resources)

        self._mission_combo.clear()
        self._mission_combo.addItem("")
        self._mission_combo.addItems(hints.mission_types)

        self._location_combo.clear()
        self._location_combo.addItem("")
        self._location_combo.addItems(hints.locations)

        self._contractor_combo.clear()
        self._contractor_combo.addItem("")
        self._contractor_combo.addItems(hints.contractors)

    def get_filters(self) -> dict:
        return {
            "ownable": self._ownable_cb.isChecked(),
            "category": self._category_combo.current_text().strip(),
            "resource": self._resource_combo.current_text().strip(),
            "mission_type": self._mission_combo.current_text().strip(),
            "location": self._location_combo.current_text().strip(),
            "contractor": self._contractor_combo.current_text().strip(),
        }

    def _emit_filters(self, *_args):
        self.filters_changed.emit(self.get_filters())

    def _clear_all(self):
        self._ownable_cb.setChecked(True)
        self._category_combo.set_text("")
        self._resource_combo.set_text("")
        self._mission_combo.set_text("")
        self._location_combo.set_text("")
        self._contractor_combo.set_text("")
        self._emit_filters()
