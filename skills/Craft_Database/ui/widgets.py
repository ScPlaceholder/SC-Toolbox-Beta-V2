"""Custom widgets for the Craft Database UI."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
    QGridLayout,
    QPushButton,
    QSizePolicy,
)

from shared.qt.theme import P
from domain.models import Blueprint
from ui.constants import (
    CARD_BG,
    CARD_BORDER,
    CARD_HOVER_BORDER,
    TAG_COLORS,
    TOOL_COLOR,
    CATEGORY_COLORS,
)


# ── Ingredient tag ───────────────────────────────────────────────────────


class IngredientTag(QFrame):
    """Small coloured pill showing resource name + quantity."""

    def __init__(self, name: str, qty: float, unit: str = "scu", parent=None):
        super().__init__(parent)
        color = TAG_COLORS.get(name, TAG_COLORS["default"])
        qty_str = f"{qty:g}" if qty == int(qty) else f"{qty:.2f}"
        display_unit = "cSCU" if unit == "scu" else unit

        self.setStyleSheet(
            f"background: {P.bg_input}; border: 1px solid {color};"
            f"border-radius: 3px; padding: 1px 5px;"
        )
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        name_lbl = QLabel(name)
        name_lbl.setStyleSheet(f"color: {color}; border: none; font-size: 8pt; font-weight: bold;")
        lay.addWidget(name_lbl)

        qty_lbl = QLabel(f"{qty_str} {display_unit}")
        qty_lbl.setStyleSheet(f"color: {P.fg_dim}; border: none; font-size: 8pt;")
        lay.addWidget(qty_lbl)


# ── Blueprint card ───────────────────────────────────────────────────────


class BlueprintCard(QFrame):
    """A single blueprint card in the grid."""

    clicked = Signal(object)
    expand_clicked = Signal(object)

    def __init__(self, bp: Blueprint, parent=None):
        super().__init__(parent)
        self.bp = bp
        self.setFixedHeight(140)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setCursor(Qt.PointingHandCursor)
        self._hovered = False

        self._update_style(False)
        self._build()

    def _update_style(self, hovered: bool):
        border = CARD_HOVER_BORDER if hovered else CARD_BORDER
        self.setStyleSheet(
            f"BlueprintCard {{ background: {CARD_BG}; border: 1px solid {border};"
            f"border-radius: 4px; }}"
        )

    def _build(self):
        main_lay = QVBoxLayout(self)
        main_lay.setContentsMargins(10, 8, 10, 8)
        main_lay.setSpacing(4)

        # ── Header row: name + craft time + expand btn
        header = QHBoxLayout()
        header.setSpacing(6)

        name_lbl = QLabel(self.bp.name)
        name_lbl.setStyleSheet(
            f"color: {P.fg_bright}; font-size: 10pt; font-weight: bold; border: none;"
        )
        name_lbl.setWordWrap(True)
        header.addWidget(name_lbl, 1)

        time_lbl = QLabel(self.bp.craft_time_display)
        cat_color = CATEGORY_COLORS.get(self.bp.category_type, TOOL_COLOR)
        time_lbl.setStyleSheet(
            f"color: {P.fg}; background: {P.bg_input}; border: 1px solid {cat_color};"
            f"border-radius: 3px; padding: 1px 6px; font-size: 8pt;"
        )
        header.addWidget(time_lbl, 0, Qt.AlignTop)

        expand_btn = QPushButton("\u2197")
        expand_btn.setFixedSize(22, 22)
        expand_btn.setStyleSheet(
            f"QPushButton {{ color: {P.fg_dim}; background: transparent;"
            f"border: 1px solid {P.border}; border-radius: 3px; font-size: 10pt; }}"
            f"QPushButton:hover {{ color: {TOOL_COLOR}; border-color: {TOOL_COLOR}; }}"
        )
        expand_btn.clicked.connect(lambda: self.expand_clicked.emit(self.bp))
        header.addWidget(expand_btn, 0, Qt.AlignTop)

        main_lay.addLayout(header)

        # ── Ingredients label
        ing_header = QLabel("INGREDIENTS")
        ing_header.setStyleSheet(
            f"color: {P.fg_dim}; font-size: 7pt; font-weight: bold; border: none;"
            f"letter-spacing: 1px;"
        )
        main_lay.addWidget(ing_header)

        # ── Ingredient tags
        tag_row = QHBoxLayout()
        tag_row.setSpacing(4)
        for slot in self.bp.ingredients[:4]:
            tag = IngredientTag(slot.name, slot.quantity_scu)
            tag_row.addWidget(tag)
        tag_row.addStretch()
        main_lay.addLayout(tag_row)

        main_lay.addStretch()

        # ── Missions count
        if self.bp.mission_count > 0:
            missions_lbl = QLabel(f"{self.bp.mission_count} missions \u2022")
            missions_lbl.setStyleSheet(
                f"color: {P.fg_dim}; font-size: 8pt; border: none;"
            )
            main_lay.addWidget(missions_lbl)

    def enterEvent(self, event):
        self._hovered = True
        self._update_style(True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        self._update_style(False)
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self.bp)
        super().mousePressEvent(event)


# ── Blueprint grid ───────────────────────────────────────────────────────


class BlueprintGrid(QScrollArea):
    """Scrollable grid of blueprint cards."""

    card_clicked = Signal(object)
    card_expand = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setStyleSheet(
            f"QScrollArea {{ background: transparent; border: none; }}"
        )

        self._container = QWidget()
        self._container.setStyleSheet("background: transparent;")
        self._grid = QGridLayout(self._container)
        self._grid.setSpacing(8)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self.setWidget(self._container)

        self._cards: list[BlueprintCard] = []
        self._spacers: list[QWidget] = []
        self._cols = 3

    def set_blueprints(self, blueprints: list[Blueprint]):
        # Clear old cards and spacers
        for card in self._cards:
            self._grid.removeWidget(card)
            card.deleteLater()
        self._cards.clear()

        for spacer in self._spacers:
            self._grid.removeWidget(spacer)
            spacer.deleteLater()
        self._spacers.clear()

        for i, bp in enumerate(blueprints):
            card = BlueprintCard(bp)
            card.clicked.connect(self.card_clicked.emit)
            card.expand_clicked.connect(self.card_expand.emit)
            row = i // self._cols
            col = i % self._cols
            self._grid.addWidget(card, row, col)
            self._cards.append(card)

        # Fill remaining cols in last row with spacers
        remainder = len(blueprints) % self._cols
        if remainder:
            last_row = len(blueprints) // self._cols
            for c in range(remainder, self._cols):
                spacer = QWidget()
                spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
                self._grid.addWidget(spacer, last_row, c)
                self._spacers.append(spacer)

    def set_columns(self, cols: int):
        if cols != self._cols:
            self._cols = max(1, cols)


# ── Pagination bar ───────────────────────────────────────────────────────


class PaginationBar(QWidget):
    """Page navigation bar."""

    page_changed = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._page = 1
        self._pages = 1

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 4, 0, 4)
        lay.setSpacing(8)

        self._prev_btn = QPushButton("\u25c0 Prev")
        self._prev_btn.setFixedWidth(80)
        self._prev_btn.setStyleSheet(
            f"QPushButton {{ color: {P.fg}; background: {P.bg_input};"
            f"border: 1px solid {P.border}; border-radius: 3px; padding: 3px 8px; }}"
            f"QPushButton:hover {{ border-color: {TOOL_COLOR}; }}"
            f"QPushButton:disabled {{ color: {P.fg_disabled}; }}"
        )
        self._prev_btn.clicked.connect(self._go_prev)
        lay.addWidget(self._prev_btn)

        lay.addStretch()

        self._page_lbl = QLabel("1 / 1")
        self._page_lbl.setStyleSheet(f"color: {P.fg_dim}; font-size: 9pt;")
        lay.addWidget(self._page_lbl)

        lay.addStretch()

        self._next_btn = QPushButton("Next \u25b6")
        self._next_btn.setFixedWidth(80)
        self._next_btn.setStyleSheet(self._prev_btn.styleSheet())
        self._next_btn.clicked.connect(self._go_next)
        lay.addWidget(self._next_btn)

    def set_pagination(self, page: int, pages: int):
        self._page = page
        self._pages = pages
        self._page_lbl.setText(f"{page} / {pages}")
        self._prev_btn.setEnabled(page > 1)
        self._next_btn.setEnabled(page < pages)

    def _go_prev(self):
        if self._page > 1:
            self.page_changed.emit(self._page - 1)

    def _go_next(self):
        if self._page < self._pages:
            self.page_changed.emit(self._page + 1)
