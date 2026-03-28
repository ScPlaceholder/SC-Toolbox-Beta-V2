"""
SCFuzzyCombo – searchable dropdown with fuzzy-matching.

The popup list is parented to the top-level window and positioned
absolutely within it, so z-order fights with WindowStaysOnTopHint
are impossible.
"""

from __future__ import annotations
from typing import List, Optional, Sequence

from PySide6.QtCore import Qt, Signal, QEvent, QPoint, QTimer
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QLineEdit, QPushButton, QListWidget,
    QListWidgetItem,
)
from typing import List, Sequence

import logging
from shared.qt.theme import P

log = logging.getLogger(__name__)


def _fuzzy_match(query: str, text: str) -> bool:
    """Case-insensitive fuzzy match: substring first, then subsequence."""
    q = query.lower()
    t = text.lower()
    if q in t:
        return True
    it = iter(t)
    return all(ch in it for ch in q)


class SCFuzzyCombo(QWidget):
    """Search input with a filterable dropdown list.

    Emits ``item_selected(str)`` when the user picks an item.
    """

    item_selected = Signal(str)
    currentIndexChanged = Signal(int)

    def __init__(
        self,
        placeholder: str = "Search...",
        items: Sequence[str] = (),
        max_visible: int = 10,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self._all_items: List[str] = list(items)
        self._max_visible = max_visible
        self._current_index: int = -1

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)

        self._input = QLineEdit(self)
        self._input.setPlaceholderText(placeholder)
        self._input.setClearButtonEnabled(True)
        self._input.setStyleSheet(f"""
            QLineEdit {{
                background-color: {P.bg_input};
                color: {P.fg};
                border: 1px solid {P.border};
                border-right: none;
                border-radius: 4px 0 0 4px;
                padding: 4px 8px;
                font-family: Consolas;
                font-size: 9pt;
            }}
            QLineEdit:focus {{ border-color: {P.accent}; }}
        """)
        row.addWidget(self._input)

        self._arrow_btn = QPushButton("\u25bc", self)
        self._arrow_btn.setFixedWidth(24)
        self._arrow_btn.setCursor(QCursor(Qt.PointingHandCursor))
        self._arrow_btn.setFocusPolicy(Qt.NoFocus)
        self._arrow_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {P.bg_input};
                color: {P.fg_dim};
                border: 1px solid {P.border};
                border-radius: 0 4px 4px 0;
                font-size: 7pt; padding: 0;
            }}
            QPushButton:hover {{ background-color: {P.selection}; color: {P.fg}; }}
        """)
        row.addWidget(self._arrow_btn)

        # Popup list — created lazily in _ensure_list_parented() so it
        # can be parented to the top-level window (which inherits topmost).
        self._list = QListWidget()
        self._list.setWindowFlags(
            Qt.Tool | Qt.FramelessWindowHint
            | Qt.WindowDoesNotAcceptFocus
        )
        self._list.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self._list_reparented = False
        self._list.setFocusPolicy(Qt.NoFocus)
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._list.hide()
        self._list.setStyleSheet(f"""
            QListWidget {{
                background-color: {P.bg_secondary};
                border: 1px solid {P.accent};
                color: {P.fg};
                font-family: Consolas;
                font-size: 9pt;
                outline: none;
            }}
            QListWidget > QWidget {{
                background-color: {P.bg_secondary};
            }}
            QListWidget::item {{ padding: 5px 8px; }}
            QListWidget::item:hover {{ background-color: {P.selection}; }}
            QListWidget::item:selected {{
                background-color: {P.accent};
                color: {P.bg_primary};
            }}
        """)

        self._input.textChanged.connect(lambda t: self._show_list(t.strip()))
        self._input.returnPressed.connect(self._on_enter)
        self._input.installEventFilter(self)
        self._arrow_btn.clicked.connect(self._toggle)
        self._list.itemClicked.connect(self._on_item_clicked)

    # ── Public API ────────────────────────────────────────────────────────

    def set_items(self, items: Sequence[str]) -> None:
        self._all_items = list(items)
        self._current_index = -1
        if self._list.isVisible():
            self._show_list(self._input.text().strip())

    def current_text(self) -> str:
        return self._input.text()

    # QComboBox-compatible alias
    currentText = current_text

    def set_text(self, text: str) -> None:
        self._input.blockSignals(True)
        self._input.setText(text)
        self._input.blockSignals(False)
        self._list.hide()

    # ── QComboBox-compatible API ───────────────────────────────────────────

    def addItem(self, text: str) -> None:
        """Append a single item (QComboBox compat)."""
        self._all_items.append(text)

    def addItems(self, items: Sequence[str]) -> None:
        """Append multiple items (QComboBox compat)."""
        self._all_items.extend(items)

    def clear(self) -> None:
        """Remove all items and reset input (QComboBox compat)."""
        self._all_items.clear()
        self._current_index = -1
        self._input.blockSignals(True)
        self._input.setText("")
        self._input.blockSignals(False)
        self._list.hide()

    def findText(self, text: str) -> int:
        """Return index of exact match, or -1 (QComboBox compat)."""
        try:
            return self._all_items.index(text)
        except ValueError:
            return -1

    def setCurrentIndex(self, index: int) -> None:
        """Select item by index (QComboBox compat)."""
        if 0 <= index < len(self._all_items):
            text = self._all_items[index]
            self._current_index = index
            self._input.blockSignals(True)
            self._input.setText(text)
            self._input.blockSignals(False)
            self._list.hide()
            self.currentIndexChanged.emit(index)

    def count(self) -> int:
        """Return number of items (QComboBox compat)."""
        return len(self._all_items)

    # ── Events ────────────────────────────────────────────────────────────

    def eventFilter(self, obj, event):
        if obj is self._input:
            t = event.type()
            if t == QEvent.MouseButtonPress:
                self._win32_foreground()
                self._input.setFocus(Qt.MouseFocusReason)
            elif t == QEvent.FocusIn:
                QTimer.singleShot(0, lambda: self._show_list(self._input.text().strip()))
            elif t == QEvent.FocusOut:
                QTimer.singleShot(150, self._maybe_hide)
        return super().eventFilter(obj, event)

    def _maybe_hide(self):
        if self._input.hasFocus():
            return
        if self._list.isVisible() and self._list.underMouse():
            return
        self._list.hide()

    def _win32_foreground(self):
        """Force OS-level foreground activation (bypasses Windows focus-stealing prevention)."""
        w = self.window()
        if not w:
            return
        try:
            import ctypes
            ctypes.windll.user32.SetForegroundWindow(int(w.winId()))
        except Exception:
            pass
        w.activateWindow()
        w.raise_()

    def _activate_and_focus(self):
        """Activate parent window and give focus to the input (toggle button only)."""
        self._win32_foreground()
        self._input.setFocus(Qt.MouseFocusReason)

    def _ensure_list_parented(self):
        """Re-parent the popup list to the top-level window (once).

        A Qt.Tool parented to a WindowStaysOnTopHint window automatically
        appears above it, solving the z-order problem without Win32 hacks.
        """
        if self._list_reparented:
            return
        w = self.window()
        if w and w is not self._list.parent():
            self._list.setParent(w)
            self._list.setWindowFlags(
                Qt.Tool | Qt.FramelessWindowHint
                | Qt.WindowDoesNotAcceptFocus
            )
            self._list.setAttribute(Qt.WA_ShowWithoutActivating, True)
            self._list_reparented = True

    def _toggle(self):
        if self._list.isVisible():
            self._list.hide()
        else:
            self._activate_and_focus()
            self._show_list(self._input.text().strip())

    def hideEvent(self, event):
        self._list.hide()
        super().hideEvent(event)

    # ── Popup ─────────────────────────────────────────────────────────────

    def _show_list(self, query: str) -> None:
        matches = (
            [i for i in self._all_items if _fuzzy_match(query, i)]
            if query else list(self._all_items)
        )
        if not matches:
            self._list.hide()
            return

        self._list.clear()
        for m in matches[:self._max_visible]:
            self._list.addItem(m)

        # Position in global screen coordinates — the list is a top-level
        # Qt.Tool window so it has no z-order fights with sibling widgets.
        row_h = 28
        h = min(len(matches), self._max_visible) * row_h + 4
        pos = self.mapToGlobal(QPoint(0, self.height()))
        self._ensure_list_parented()
        self._list.setGeometry(pos.x(), pos.y(), self.width(), h)
        self._list.show()
        self._list.raise_()

    # ── Selection ─────────────────────────────────────────────────────────

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        text = item.text()
        self._input.blockSignals(True)
        self._input.setText(text)
        self._input.blockSignals(False)
        self._list.hide()
        self.item_selected.emit(text)
        idx = self.findText(text)
        if idx >= 0:
            self._current_index = idx
            self.currentIndexChanged.emit(idx)

    def _on_enter(self) -> None:
        if self._list.count() > 0:
            self._on_item_clicked(self._list.item(0))
        else:
            text = self._input.text().strip()
            if text:
                self.item_selected.emit(text)
