"""Custom widgets for the Craft Database UI."""

from __future__ import annotations

from PySide6.QtCore import Qt, QRect, QPoint, QSize, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QMessageBox,
    QScrollArea,
    QVBoxLayout,
    QWidget,
    QGridLayout,
    QPushButton,
    QSizePolicy,
)


# ── Flow layout (wrapping) ──────────────────────────────────────────────


class FlowLayout(QLayout):
    """Simple flow layout that wraps items to the next row when out of space."""

    def __init__(self, parent=None, spacing=4):
        super().__init__(parent)
        self._items = []
        self._spacing = spacing

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index):
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do_layout(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, apply=True)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        return size

    def _do_layout(self, rect, apply):
        x = rect.x()
        y = rect.y()
        row_height = 0

        for item in self._items:
            item_size = item.sizeHint()
            if x + item_size.width() > rect.right() and row_height > 0:
                x = rect.x()
                y += row_height + self._spacing
                row_height = 0
            if apply:
                item.setGeometry(QRect(QPoint(x, y), item_size))
            x += item_size.width() + self._spacing
            row_height = max(row_height, item_size.height())

        return y + row_height - rect.y()

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
    ownership_toggled = Signal(object, bool)  # (blueprint, is_now_owned)

    def __init__(self, bp: Blueprint, owned: bool = False, inventory_mode: bool = False, parent=None):
        super().__init__(parent)
        self.bp = bp
        self._owned = owned
        self._inventory_mode = inventory_mode
        self.setMinimumHeight(100)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Minimum)
        self.setMinimumWidth(0)
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

        # ── Header row: name + ownership btn + craft time + expand btn
        header = QHBoxLayout()
        header.setSpacing(6)

        name_lbl = QLabel(self.bp.name)
        name_lbl.setStyleSheet(
            f"color: {P.fg_bright}; font-size: 10pt; font-weight: bold; border: none;"
        )
        name_lbl.setWordWrap(True)
        header.addWidget(name_lbl, 1)

        # Ownership button — compact, placed in header
        own_btn = self._make_ownership_btn()
        header.addWidget(own_btn, 0, Qt.AlignTop)

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

        # ── Ingredient tags (flow layout wraps to next line)
        tag_container = QWidget()
        tag_container.setStyleSheet("background: transparent;")
        tag_flow = FlowLayout(tag_container, spacing=4)
        for slot in self.bp.ingredients[:4]:
            tag = IngredientTag(slot.name, slot.quantity_scu, slot.unit)
            tag_flow.addWidget(tag)
        main_lay.addWidget(tag_container)

        # ── How it is obtained: missions (legacy data) or reward pools
        src = ""
        if self.bp.mission_count > 0:
            src = f"{self.bp.mission_count} missions \u2022"
        elif self.bp.default_owned:
            src = "Known by default"
        elif self.bp.sources:
            n = len(self.bp.sources)
            src = f"{n} reward pool{'s' if n != 1 else ''} \u2022"
        if src:
            missions_lbl = QLabel(src)
            missions_lbl.setStyleSheet(
                f"color: {P.fg_dim}; font-size: 8pt; border: none;"
            )
            main_lay.addWidget(missions_lbl)

    def _make_ownership_btn(self) -> QPushButton:
        if self._inventory_mode:
            btn = QPushButton("Unown")
            btn.setFixedHeight(22)
            btn.setStyleSheet(
                f"QPushButton {{ color: {P.fg}; background: {P.bg_input};"
                f"border: 1px solid #cc4444; border-radius: 3px;"
                f"padding: 1px 6px; font-size: 7pt; }}"
                f"QPushButton:hover {{ border-color: #ff6666; color: #ff6666; }}"
            )
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(self._confirm_unown)
            return btn

        if self._owned:
            btn = QPushButton("Owned")
            btn.setFixedHeight(22)
            btn.setEnabled(False)
            btn.setStyleSheet(
                f"QPushButton {{ color: {P.fg_dim}; background: {P.bg_input};"
                f"border: 1px solid {P.border}; border-radius: 3px;"
                f"padding: 1px 6px; font-size: 7pt; }}"
            )
            return btn

        btn = QPushButton("Own")
        btn.setFixedHeight(22)
        btn.setStyleSheet(
            f"QPushButton {{ color: {P.fg}; background: {P.bg_input};"
            f"border: 1px solid {TOOL_COLOR}; border-radius: 3px;"
            f"padding: 1px 6px; font-size: 7pt; }}"
            f"QPushButton:hover {{ border-color: {TOOL_COLOR}; color: {TOOL_COLOR}; }}"
        )
        btn.setCursor(Qt.PointingHandCursor)
        btn.clicked.connect(lambda: self.ownership_toggled.emit(self.bp, True))
        return btn

    def _confirm_unown(self):
        msg = QMessageBox(self)
        msg.setWindowTitle("Remove from Inventory")
        msg.setText(f"Are you sure you want to remove \"{self.bp.name}\" from your inventory?")
        msg.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        msg.setDefaultButton(QMessageBox.No)
        msg.setStyleSheet(
            f"QMessageBox {{ background: {P.bg_primary}; color: {P.fg}; }}"
            f"QPushButton {{ color: {P.fg}; background: {P.bg_input};"
            f"border: 1px solid {P.border}; border-radius: 3px;"
            f"padding: 4px 16px; min-width: 60px; }}"
            f"QPushButton:hover {{ border-color: {TOOL_COLOR}; }}"
        )
        if msg.exec() == QMessageBox.Yes:
            self.ownership_toggled.emit(self.bp, False)

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
    card_ownership = Signal(object, bool)

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
        self._stretch_row = -1

        # Equal column widths
        for c in range(self._cols):
            self._grid.setColumnStretch(c, 1)

    def set_blueprints(
        self,
        blueprints: list[Blueprint],
        owned_ids: set[str] | None = None,
        inventory_mode: bool = False,
    ):
        owned_ids = owned_ids or set()

        # Remove old bottom stretch
        if self._stretch_row >= 0:
            self._grid.setRowStretch(self._stretch_row, 0)
            self._stretch_row = -1

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
            is_owned = bp.blueprint_id in owned_ids
            card = BlueprintCard(bp, owned=is_owned, inventory_mode=inventory_mode)
            card.clicked.connect(self.card_clicked.emit)
            card.expand_clicked.connect(self.card_expand.emit)
            card.ownership_toggled.connect(self.card_ownership.emit)
            row = i // self._cols
            col = i % self._cols
            self._grid.addWidget(card, row, col)
            self._cards.append(card)

        # Fill remaining cols in last row with spacers
        remainder = len(blueprints) % self._cols
        last_row = len(blueprints) // self._cols if remainder else max(0, len(blueprints) // self._cols - 1)
        if remainder:
            for c in range(remainder, self._cols):
                spacer = QWidget()
                spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
                self._grid.addWidget(spacer, last_row, c)
                self._spacers.append(spacer)

        # Push all cards to the top
        self._stretch_row = last_row + 1
        self._grid.setRowStretch(self._stretch_row, 1)

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
