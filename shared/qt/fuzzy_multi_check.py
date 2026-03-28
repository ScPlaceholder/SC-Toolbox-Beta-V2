"""
SCFuzzyMultiCheck -- searchable multi-select checkbox dropdown.

A button that opens a popup with a search input and checkboxes.
Fuzzy-matching filters the list while preserving check state.
"""

from __future__ import annotations
from typing import List, Optional, Sequence

from PySide6.QtCore import Qt, Signal, QEvent, QPoint, QTimer
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QPushButton,
    QCheckBox, QScrollArea, QFrame, QApplication,
)

from shared.qt.fuzzy_combo import _fuzzy_match
from shared.qt.theme import P


class SCFuzzyMultiCheck(QWidget):
    """Searchable multi-select dropdown with checkboxes.

    Emits ``selection_changed(list[str])`` when the user toggles any item.
    """

    selection_changed = Signal(list)

    def __init__(
        self,
        label: str = "All",
        items: Sequence[str] = (),
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self._items: List[str] = list(items)
        self._checked: set[str] = set()
        self._label = label
        self._checkboxes: List[QCheckBox] = []

        # ── Trigger button ──
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._btn = QPushButton(label, self)
        self._btn.setFixedHeight(24)
        self._btn.setCursor(QCursor(Qt.PointingHandCursor))
        self._btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {P.bg_input};
                color: {P.fg};
                border: 1px solid {P.border};
                border-radius: 4px;
                padding: 2px 8px;
                font-family: Consolas;
                font-size: 9pt;
                text-align: left;
            }}
            QPushButton:hover {{ border-color: {P.accent}; }}
        """)
        layout.addWidget(self._btn)

        # ── Popup container (top-level tool window) ──
        self._popup = QFrame()
        self._popup.setWindowFlags(
            Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
        )
        self._popup.setStyleSheet(f"""
            QFrame {{
                background-color: {P.bg_secondary};
                border: 1px solid {P.accent};
                border-radius: 4px;
            }}
        """)
        self._popup.hide()

        popup_layout = QVBoxLayout(self._popup)
        popup_layout.setContentsMargins(4, 4, 4, 4)
        popup_layout.setSpacing(2)

        # Search input inside popup
        self._search = QLineEdit(self._popup)
        self._search.setPlaceholderText("Search...")
        self._search.setClearButtonEnabled(True)
        self._search.setStyleSheet(f"""
            QLineEdit {{
                background-color: {P.bg_input};
                color: {P.fg};
                border: 1px solid {P.border};
                border-radius: 3px;
                padding: 4px 8px;
                font-family: Consolas;
                font-size: 9pt;
            }}
            QLineEdit:focus {{ border-color: {P.accent}; }}
        """)
        popup_layout.addWidget(self._search)

        # Scrollable checkbox area
        self._scroll = QScrollArea(self._popup)
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet(f"""
            QScrollArea {{
                background-color: {P.bg_secondary};
                border: none;
            }}
            QScrollArea > QWidget > QWidget {{
                background-color: {P.bg_secondary};
            }}
            QScrollBar:vertical {{
                background: {P.bg_secondary};
                width: 8px;
            }}
            QScrollBar::handle:vertical {{
                background: {P.border};
                border-radius: 4px;
                min-height: 20px;
            }}
        """)
        self._cb_container = QWidget()
        self._cb_layout = QVBoxLayout(self._cb_container)
        self._cb_layout.setContentsMargins(0, 0, 0, 0)
        self._cb_layout.setSpacing(0)
        self._scroll.setWidget(self._cb_container)
        popup_layout.addWidget(self._scroll, 1)

        self._rebuild_checkboxes()

        # ── Connections ──
        self._btn.clicked.connect(self._toggle_popup)
        self._search.textChanged.connect(self._on_search)
        self._search.installEventFilter(self)

    # ── Public API (matches SCMultiCheck) ──────────────────────────────────

    def set_items(self, items: Sequence[str]) -> None:
        self._items = list(items)
        self._checked.clear()
        self._rebuild_checkboxes()
        self._update_text()

    def get_selected(self) -> List[str]:
        return [i for i in self._items if i in self._checked]

    def set_selected(self, selected: Sequence[str]) -> None:
        self._checked = set(selected)
        for cb in self._checkboxes:
            cb.blockSignals(True)
            cb.setChecked(cb.text() in self._checked)
            cb.blockSignals(False)
        self._update_text()

    # ── Internals ──────────────────────────────────────────────────────────

    def _rebuild_checkboxes(self) -> None:
        # Clear old checkboxes
        for cb in self._checkboxes:
            self._cb_layout.removeWidget(cb)
            cb.deleteLater()
        self._checkboxes.clear()

        for item in self._items:
            cb = QCheckBox(item)
            cb.setChecked(item in self._checked)
            cb.setStyleSheet(f"""
                QCheckBox {{
                    padding: 4px 8px;
                    color: {P.fg};
                    font-family: Consolas;
                    font-size: 9pt;
                    background: transparent;
                }}
                QCheckBox:hover {{
                    background-color: {P.selection};
                }}
            """)
            cb.toggled.connect(lambda checked, name=item: self._on_toggle(name, checked))
            self._cb_layout.addWidget(cb)
            self._checkboxes.append(cb)

    def _on_toggle(self, name: str, checked: bool) -> None:
        if checked:
            self._checked.add(name)
        else:
            self._checked.discard(name)
        self._update_text()
        self.selection_changed.emit(self.get_selected())

    def _update_text(self) -> None:
        n = len(self._checked)
        if n == 0:
            self._btn.setText(self._label)
        elif n <= 2:
            self._btn.setText(", ".join(sorted(self._checked)))
        else:
            self._btn.setText(f"{n} selected")

    def _on_search(self, text: str) -> None:
        query = text.strip()
        for cb in self._checkboxes:
            if not query:
                cb.setVisible(True)
            else:
                cb.setVisible(_fuzzy_match(query, cb.text()))

    def _toggle_popup(self) -> None:
        if self._popup.isVisible():
            self._hide_popup()
        else:
            self._show_popup()

    def _show_popup(self) -> None:
        self._search.clear()
        self._on_search("")  # show all items

        # Position below the button
        pos = self._btn.mapToGlobal(QPoint(0, self._btn.height()))
        width = max(self._btn.width(), 200)
        # Height: search bar (~30) + up to 10 items (~28 each) + padding
        visible_count = min(len(self._items), 10)
        height = 36 + visible_count * 28 + 8
        self._popup.setGeometry(pos.x(), pos.y(), width, height)
        self._popup.show()
        self._search.setFocus()

        # Install app-wide event filter to detect clicks outside
        app = QApplication.instance()
        if app:
            app.installEventFilter(self)

    def _hide_popup(self) -> None:
        self._popup.hide()
        app = QApplication.instance()
        if app:
            app.removeEventFilter(self)

    # ── Events ─────────────────────────────────────────────────────────────

    def eventFilter(self, obj, event):
        # Close popup on click outside
        if (
            self._popup.isVisible()
            and event.type() == QEvent.MouseButtonPress
            and obj is not self._search
        ):
            # Check if click is inside popup or button
            if hasattr(event, "globalPosition"):
                click_pos = event.globalPosition().toPoint()
            elif hasattr(event, "globalPos"):
                click_pos = event.globalPos()
            else:
                return super().eventFilter(obj, event)

            popup_rect = self._popup.geometry()
            btn_rect = self._btn.rect()
            btn_global = self._btn.mapToGlobal(QPoint(0, 0))
            btn_rect_global = btn_rect.translated(btn_global)

            if not popup_rect.contains(click_pos) and not btn_rect_global.contains(click_pos):
                self._hide_popup()

        # Close on Escape
        if (
            self._popup.isVisible()
            and event.type() == QEvent.KeyPress
            and hasattr(event, "key")
            and event.key() == Qt.Key_Escape
        ):
            self._hide_popup()
            return True

        return super().eventFilter(obj, event)

    def hideEvent(self, event):
        self._hide_popup()
        super().hideEvent(event)
