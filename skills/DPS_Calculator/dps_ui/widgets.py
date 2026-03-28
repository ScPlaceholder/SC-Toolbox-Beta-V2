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
                 current_ref="", type_color=ACCENT, max_rows=6):
        super().__init__(parent)
        self._cols      = columns
        self._items     = list(items)
        self._on_select = on_select
        self._sel_ref   = current_ref
        self._type_col  = type_color
        self._max_rows  = max_rows
        self._sel_item  = None

        if current_ref:
            for it in self._items:
                if it.get("ref") == current_ref:
                    self._sel_item = it
                    break

        self._row_layout = QHBoxLayout(self)
        self._row_layout.setContentsMargins(0, 0, 0, 0)
        self._row_layout.setSpacing(0)
        self._build_row()

    def _clear_row(self):
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
                row_lay.addWidget(lbl)
        else:
            empty_lbl = QLabel("  " + _("(empty \u2014 click to select)"), container)
            empty_lbl.setStyleSheet(
                f"color: {FG_DIM}; font-family: Consolas; font-size: 8pt; background: transparent;"
            )
            row_lay.addWidget(empty_lbl)

        row_lay.addStretch(1)

        arrow = QLabel("\u25bc", container)
        arrow.setStyleSheet(
            f"color: {FG_DIM}; font-family: Consolas; font-size: 7pt; "
            f"padding: 0 4px; background: transparent;"
        )
        row_lay.addWidget(arrow)

        self._row_layout.addWidget(container)

    def _open_picker(self):
        popup = ComponentPickerPopup(
            self.window(), self, self._items, self._cols,
            self._sel_item.get("name", "") if self._sel_item else "",
            self._on_picked,
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
                 current_name, on_select):
        super().__init__(parent_window)
        self._items = list(items)
        self._columns = columns
        self._on_select = on_select
        self._cur_name = current_name
        self._result_item = None

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
        headers = [_("Sz")] + [h for h, *_ in self._columns]
        self._model.setHorizontalHeaderLabels(headers)

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

            self._model.appendRow(row_items)

        # Resize columns
        self._table.resizeColumnsToContents()

    def _apply_filter(self, text):
        self._proxy.setFilterFixedString(text)

    def _on_header_click(self, col_idx):
        self._proxy.sort(col_idx, Qt.DescendingOrder
                         if self._proxy.sortOrder() == Qt.AscendingOrder
                         else Qt.AscendingOrder)

    def _on_row_click(self, proxy_index: QModelIndex):
        source_row = self._proxy.mapToSource(proxy_index).row()
        if 0 <= source_row < len(self._items):
            self._select(self._items[source_row])

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
