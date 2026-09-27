"""Reusable UI widgets -- ComponentTable and ComponentPickerPopup (PySide6)."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal, QPoint, QSortFilterProxyModel, QModelIndex
from PySide6.QtGui import QColor, QFont, QStandardItemModel, QStandardItem
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QLabel, QPushButton,
    QDialog, QLineEdit, QTableView, QHeaderView, QAbstractItemView,
    QFrame, QSizePolicy,
)

from shared.i18n import s_ as _
from dps_ui.constants import (
    BG, BG2, BG3, BG4, BORDER, FG, FG_DIM, FG_DIMMER, ACCENT, GREEN, YELLOW,
    ORANGE, CYAN, PURPLE, PHYS_COL, ENERGY_COL, DIST_COL, HEADER_BG,
    CARD_EVEN, CARD_ODD, ROW_EVEN, ROW_ODD,
)
from shared.qt.theme import P
from dps_ui.tooltips import tip_for


class ComponentTable(QWidget):
    """Lightweight component slot row. Shows the currently selected component
    as a single erkul-style data row. Click to open ComponentPickerPopup for
    changing.

    Layout (one row, ~28px):
      [3px accent stripe] [Sz badge] [Name (orange)] [stat1] [stat2] ... [v]
    """

    selection_changed = Signal(object)  # emits item dict or None

    _SEL_BG  = "#1e2840"
    _HOVER   = "#222840"
    _EMPTY_BG = CARD_ODD

    def __init__(self, parent, columns, items, on_select, *,
                 current_ref="", type_color=ACCENT, max_rows=6, tip_ns=""):
        super().__init__(parent)
        self._cols      = columns
        self._items     = list(items)
        self._on_select = on_select
        self._sel_ref   = current_ref
        self._type_col  = type_color
        self._max_rows  = max_rows
        self._sel_item  = None
        # ⚠ `tip_ns` ("weapon", "shield", "cooler", ...) exists ONLY so a column can be identified
        #   for help text. A field key is NOT unique across tables — "name" appears in nearly every
        #   *_TABLE_COLS spec, and shield "hp" and weapon "wp_hp" are deliberately different things
        #   — so a lookup keyed on the field alone would answer the wrong table's question and look
        #   right doing it. It is deliberately NOT keyed on the visible header either, which is a
        #   `_()` translation lookup: that would stop matching the day anyone ships a localisation,
        #   silently, with no error and no missing tooltip to notice.
        #   ⚠ The caller derives it from the column SPEC, not from `section_key` — see
        #     app.py `_COLS_NAMESPACE` for why that distinction is load-bearing.
        self._tip_ns = tip_ns

        if current_ref:
            for it in self._items:
                if it.get("ref") == current_ref:
                    self._sel_item = it
                    break

        self._row_layout = QHBoxLayout(self)
        self._row_layout.setContentsMargins(0, 0, 0, 0)
        self._row_layout.setSpacing(0)
        self._stat_labels: dict = {}  # key -> QLabel for in-place value updates
        self._build_row()

    def update_stat(self, key: str, text: str) -> None:
        """Update the displayed text for a stat column in-place (no row rebuild)."""
        lbl = self._stat_labels.get(key)
        if lbl:
            lbl.setText(text)

    def select_by_name(self, name: str) -> bool:
        """Programmatically select a component by name. Returns True if found."""
        for it in self._items:
            if it.get("name") == name:
                self._sel_item = it
                self._sel_ref = it.get("ref", "")
                self._build_row()
                self._on_select(it)
                return True
        return False

    def _clear_row(self):
        self._stat_labels.clear()
        while self._row_layout.count():
            item = self._row_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

    def _build_row(self):
        self._clear_row()
        item = self._sel_item
        bg = self._SEL_BG if item else self._EMPTY_BG

        container = _ClickableFrame(self)
        container.setStyleSheet(f"background-color: {bg};")
        container.setCursor(Qt.PointingHandCursor)
        container.clicked.connect(self._open_picker)
        row_lay = QHBoxLayout(container)
        row_lay.setContentsMargins(0, 2, 0, 2)
        row_lay.setSpacing(0)

        # Left accent stripe
        stripe = QWidget(container)
        stripe.setFixedWidth(3)
        stripe.setStyleSheet(
            f"background-color: {self._type_col if item else FG_DIMMER};"
        )
        row_lay.addWidget(stripe)

        if item:
            sz = item.get("size", 1)
            sz_lbl = QLabel(f"S{sz}", container)
            sz_lbl.setFixedWidth(24)
            sz_lbl.setAlignment(Qt.AlignCenter)
            sz_lbl.setStyleSheet(
                f"color: {FG_DIM}; font-family: Consolas; font-size: 8pt; background: transparent;"
            )
            row_lay.addWidget(sz_lbl)

            for header, key, cw, color, fmt_fn in self._cols:
                val = item.get(key, 0) or 0
                try:
                    text = fmt_fn(val, item)
                except (TypeError, ValueError, KeyError):
                    text = str(val) if val else "\u2014"
                fg_c = ORANGE if key == "name" else color
                lbl = QLabel(text, container)
                lbl.setFixedWidth(cw * 8)  # approximate char width
                align = Qt.AlignLeft if key == "name" else Qt.AlignRight
                lbl.setAlignment(align | Qt.AlignVCenter)
                lbl.setStyleSheet(
                    f"color: {fg_c}; font-family: Consolas; font-size: 8pt; "
                    f"padding: 0 2px; background: transparent;"
                )
                hint = tip_for(self._tip_ns, key)
                if hint:
                    lbl.setToolTip(hint)
                row_lay.addWidget(lbl)
                self._stat_labels[key] = lbl  # track for in-place updates
        else:
            empty_lbl = QLabel("  " + _("(empty \u2014 click to select)"), container)
            empty_lbl.setStyleSheet(
                f"color: {FG_DIM}; font-family: Consolas; font-size: 8pt; background: transparent;"
            )
            row_lay.addWidget(empty_lbl)

        row_lay.addStretch(1)

        if item:
            cart_btn = QPushButton("\U0001f6d2", container)
            cart_btn.setFixedSize(22, 22)
            cart_btn.setToolTip("Check UEX market price")
            cart_btn.setCursor(Qt.PointingHandCursor)
            cart_btn.setStyleSheet(f"""
                QPushButton {{
                    background: transparent; color: {FG_DIM};
                    border: none; font-size: 10pt; padding: 0px;
                }}
                QPushButton:hover {{ color: {ACCENT}; }}
            """)
            _it = item
            cart_btn.clicked.connect(lambda checked=False, it=_it: self._open_market(it))
            row_lay.addWidget(cart_btn)

        arrow = QLabel("\u25bc", container)
        arrow.setStyleSheet(
            f"color: {FG_DIM}; font-family: Consolas; font-size: 7pt; "
            f"padding: 0 4px; background: transparent;"
        )
        row_lay.addWidget(arrow)

        self._row_layout.addWidget(container)

    def _open_market(self, item: dict):
        from dps_ui.market_bubble import MarketBubble
        bubble = MarketBubble(self.window(), item)
        bubble.show()
        bubble.raise_()
        bubble.activateWindow()

    def _open_picker(self):
        popup = ComponentPickerPopup(
            self.window(), self, self._items, self._cols,
            self._sel_item.get("name", "") if self._sel_item else "",
            self._on_picked, tip_ns=self._tip_ns,
        )
        popup.exec()

    def _on_picked(self, item):
        if item is None:
            self._sel_ref = ""
            self._sel_item = None
            self._on_select(None)
        else:
            self._sel_ref = item.get("ref", "")
            self._sel_item = item
            self._on_select(item)
        self._build_row()
        self.selection_changed.emit(item)

    def set_selected(self, ref):
        self._sel_ref = ref
        self._sel_item = None
        if ref:
            for it in self._items:
                if it.get("ref") == ref:
                    self._sel_item = it
                    break
        self._build_row()

    def refresh(self, items, selected_ref=""):
        self._items = list(items)
        self._sel_ref = selected_ref
        self._sel_item = None
        if selected_ref:
            for it in self._items:
                if it.get("ref") == selected_ref:
                    self._sel_item = it
                    break
        self._build_row()


class _ClickableFrame(QFrame):
    """QFrame that emits clicked on mouse press."""
    clicked = Signal()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class ComponentPickerPopup(QDialog):
    """Erkul-style component picker: table with filter + leave-empty."""

    def __init__(self, parent_window, anchor_widget, items, columns,
                 current_name, on_select, *, tip_ns=""):
        super().__init__(parent_window)
        self._items = list(items)
        self._columns = columns
        self._on_select = on_select
        self._cur_name = current_name
        self._result_item = None
        #: Which table's columns these are, so a header can be explained. See ComponentTable.
        self._tip_ns = tip_ns

        self.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint)
        self.setStyleSheet(f"""
            QDialog {{
                background-color: {BORDER};
                border: 1px solid {BORDER};
            }}
        """)

        self._build_ui()
        self._position(anchor_widget)
        self._filter_entry.setFocus()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(1, 1, 1, 1)
        layout.setSpacing(0)

        # Top bar
        top = QWidget(self)
        top.setStyleSheet(f"background-color: {BG2};")
        top_lay = QHBoxLayout(top)
        top_lay.setContentsMargins(8, 6, 8, 6)
        top_lay.setSpacing(4)

        lbl_sel = QLabel(_("Select component or"), top)
        lbl_sel.setStyleSheet(
            f"color: {FG_DIM}; font-family: Consolas; font-size: 9pt; background: transparent;"
        )
        top_lay.addWidget(lbl_sel)

        btn_empty = QPushButton(_("leave empty"), top)
        btn_empty.setCursor(Qt.PointingHandCursor)
        btn_empty.setStyleSheet(f"""
            QPushButton {{
                background-color: {BG3}; color: {FG_DIM};
                font-family: Consolas; font-size: 9pt; font-weight: bold;
                border: none; padding: 3px 10px;
            }}
            QPushButton:hover {{
                background-color: {BORDER}; color: {FG};
            }}
        """)
        btn_empty.clicked.connect(lambda: self._select(None))
        top_lay.addWidget(btn_empty)

        top_lay.addSpacing(16)

        lbl_filter = QLabel(_("Filter"), top)
        lbl_filter.setStyleSheet(
            f"color: {FG_DIM}; font-family: Consolas; font-size: 9pt; background: transparent;"
        )
        top_lay.addWidget(lbl_filter)

        self._filter_entry = QLineEdit(top)
        self._filter_entry.setStyleSheet(f"""
            QLineEdit {{
                background-color: {BG3}; color: {FG};
                font-family: Consolas; font-size: 9pt;
                border: 1px solid {BORDER}; padding: 4px 6px;
            }}
            QLineEdit:focus {{
                border-color: {ACCENT};
            }}
        """)
        self._filter_entry.setFixedWidth(160)
        self._filter_entry.textChanged.connect(self._apply_filter)
        top_lay.addWidget(self._filter_entry)
        top_lay.addStretch(1)

        layout.addWidget(top)

        # Table view
        self._model = QStandardItemModel(self)
        self._proxy = QSortFilterProxyModel(self)
        self._proxy.setSourceModel(self._model)
        self._proxy.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self._proxy.setFilterKeyColumn(1)  # name column

        self._table = QTableView(self)
        self._table.setModel(self._proxy)
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        self._table.setShowGrid(False)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.horizontalHeader().setSectionsClickable(True)
        self._table.horizontalHeader().sectionClicked.connect(self._on_header_click)
        self._table.setStyleSheet(f"""
            QTableView {{
                background-color: {BG};
                alternate-background-color: {ROW_ODD};
                color: {FG};
                border: none;
                font-family: Consolas; font-size: 9pt;
                selection-background-color: #1e2840;
                selection-color: {FG};
                outline: none;
            }}
            QTableView::item {{
                padding: 3px 4px;
            }}
            QHeaderView::section {{
                background-color: {HEADER_BG};
                color: {FG_DIM};
                border: none;
                border-bottom: 1px solid {BORDER};
                padding: 4px 6px;
                font-family: Consolas; font-size: 8pt; font-weight: bold;
            }}
            QHeaderView::section:hover {{
                color: {ACCENT};
            }}
        """)
        self._table.clicked.connect(self._on_row_click)
        self._table.doubleClicked.connect(self._on_row_click)
        layout.addWidget(self._table, 1)

        self._populate()

    def _populate(self):
        self._model.clear()
        headers = [_("Sz")] + [h for h, *_ in self._columns] + ["\U0001f6d2"]
        self._model.setHorizontalHeaderLabels(headers)

        # ── Column help, the thing issue #7 actually asked for ────────────────
        # "These columns lack descriptions to understand what they refer to."
        # ⚠ The field order here must mirror `headers` above: Sz, then one per column, then the
        #   cart glyph. `table.size` and `table.cart` are pseudo-columns with no field key of
        #   their own, which is why they are named as literals rather than read off a spec.
        for i, field in enumerate(["size"] + [k for _h, k, *_r in self._columns] + ["cart"]):
            hint = tip_for(self._tip_ns, field)
            item = self._model.horizontalHeaderItem(i)
            if hint and item is not None:
                item.setToolTip(hint)

        for item in self._items:
            row_items = []
            sz = item.get("size", 1)
            sz_item = QStandardItem(f"S{sz}")
            sz_item.setData(sz, Qt.UserRole + 1)
            sz_item.setTextAlignment(Qt.AlignCenter)
            row_items.append(sz_item)

            for header, key, cw, color, fmt_fn in self._columns:
                val = item.get(key, 0) or 0
                try:
                    text = fmt_fn(val, item)
                except (TypeError, ValueError, KeyError):
                    text = str(val) if val else "\u2014"
                si = QStandardItem(text)
                si.setForeground(QColor(color))
                # Store numeric value for sorting
                sort_val = val if isinstance(val, (int, float)) else 0
                si.setData(sort_val, Qt.UserRole + 1)
                align = Qt.AlignLeft if key == "name" else Qt.AlignRight
                si.setTextAlignment(align | Qt.AlignVCenter)
                row_items.append(si)

            # Cart column — store item ref for click handler
            cart_item = QStandardItem("\U0001f6d2")
            cart_item.setData(item, Qt.UserRole + 2)
            cart_item.setTextAlignment(Qt.AlignCenter | Qt.AlignVCenter)
            cart_item.setForeground(QColor(FG_DIM))
            row_items.append(cart_item)

            self._model.appendRow(row_items)

        # Resize columns
        self._table.resizeColumnsToContents()
        # Fix cart column width
        cart_col = len(self._columns) + 1
        self._table.setColumnWidth(cart_col, 30)

    def _apply_filter(self, text):
        self._proxy.setFilterFixedString(text)

    def _on_header_click(self, col_idx):
        self._proxy.sort(col_idx, Qt.DescendingOrder
                         if self._proxy.sortOrder() == Qt.AscendingOrder
                         else Qt.AscendingOrder)

    def _on_row_click(self, proxy_index: QModelIndex):
        cart_col = len(self._columns) + 1
        if proxy_index.column() == cart_col:
            source_row = self._proxy.mapToSource(proxy_index).row()
            if 0 <= source_row < len(self._items):
                self._open_market(self._items[source_row])
            return
        source_row = self._proxy.mapToSource(proxy_index).row()
        if 0 <= source_row < len(self._items):
            self._select(self._items[source_row])

    def _open_market(self, item: dict):
        from dps_ui.market_bubble import MarketBubble
        bubble = MarketBubble(self.window(), item)
        bubble.show()
        bubble.raise_()
        bubble.activateWindow()

    def _select(self, item):
        self._result_item = item
        self._on_select(item)
        self.accept()

    def _position(self, anchor: QWidget):
        global_pos = anchor.mapToGlobal(QPoint(0, anchor.height() + 2))
        w = max(580, anchor.width())
        h = 420
        screen = self.screen()
        if screen:
            sg = screen.availableGeometry()
            if global_pos.x() + w > sg.right():
                global_pos.setX(sg.right() - w - 10)
            if global_pos.y() + h > sg.bottom():
                global_pos.setY(anchor.mapToGlobal(QPoint(0, 0)).y() - h - 2)
            global_pos.setX(max(sg.x(), global_pos.x()))
            global_pos.setY(max(sg.y(), global_pos.y()))
        self.setGeometry(global_pos.x(), global_pos.y(), w, h)


def _picker_btn(parent, bg, text=None, width=28):
    """Styled button that mimics a combobox for opening ComponentPickerPopup."""
    if text is None:
        text = _("Select\u2026")
    btn = QPushButton(text, parent)
    btn.setCursor(Qt.PointingHandCursor)
    btn.setStyleSheet(f"""
        QPushButton {{
            background-color: {BG3}; color: {FG};
            font-family: Consolas; font-size: 9pt;
            border: 1px solid {BORDER}; padding: 3px 6px;
            text-align: left;
        }}
        QPushButton:hover {{
            border-color: {ACCENT}; color: {P.fg_bright};
        }}
    """)
    return btn
