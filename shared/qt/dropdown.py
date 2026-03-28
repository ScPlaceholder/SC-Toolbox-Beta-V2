"""
SCComboBox – styled combo box.
SCMultiCheck – multi-select check-dropdown for filters.
"""

from __future__ import annotations
from typing import List, Optional, Sequence

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QComboBox, QWidget, QPushButton, QMenu, QWidgetAction,
    QCheckBox, QVBoxLayout, QFrame,
)

from shared.qt.theme import P


class SCComboBox(QComboBox):
    """Themed combo box – no extra logic, just ensures consistent styling."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setMaxVisibleItems(20)

    def showPopup(self) -> None:
        """Ensure the popup renders above WindowStaysOnTopHint parents."""
        super().showPopup()
        popup = self.view().window()
        if popup and popup is not self.window():
            popup.setWindowFlags(popup.windowFlags() | Qt.WindowStaysOnTopHint)
            popup.show()


class SCMultiCheck(QPushButton):
    """Button that opens a popup with checkboxes for multi-select filtering.

    Emits ``selection_changed(list[str])`` when the user toggles any item.
    """

    selection_changed = Signal(list)

    def __init__(
        self,
        label: str = "Filter",
        items: Sequence[str] = (),
        parent: Optional[QWidget] = None,
    ):
        super().__init__(label, parent)
        self._items: List[str] = list(items)
        self._checked: set[str] = set()
        self._label = label
        self.setCursor(Qt.PointingHandCursor)

        self._menu = QMenu(self)
        self._menu.setStyleSheet(f"""
            QMenu {{
                background-color: {P.bg_secondary};
                border: 1px solid {P.border};
                padding: 4px;
            }}
        """)
        self.setMenu(self._menu)
        self._rebuild()

    def set_items(self, items: Sequence[str]) -> None:
        self._items = list(items)
        self._checked.clear()
        self._rebuild()
        self._update_text()

    def get_selected(self) -> List[str]:
        return [i for i in self._items if i in self._checked]

    def set_selected(self, selected: Sequence[str]) -> None:
        self._checked = set(selected)
        self._rebuild()
        self._update_text()

    def _rebuild(self) -> None:
        self._menu.clear()
        for item in self._items:
            cb = QCheckBox(item)
            cb.setChecked(item in self._checked)
            cb.setStyleSheet(f"""
                QCheckBox {{
                    padding: 4px 8px;
                    color: {P.fg};
                    font-family: Consolas;
                    font-size: 9pt;
                }}
                QCheckBox:hover {{
                    background-color: {P.selection};
                }}
            """)
            cb.toggled.connect(lambda checked, name=item: self._on_toggle(name, checked))
            action = QWidgetAction(self._menu)
            action.setDefaultWidget(cb)
            self._menu.addAction(action)

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
            self.setText(self._label)
        elif n <= 2:
            self.setText(", ".join(sorted(self._checked)))
        else:
            self.setText(f"{n} selected")
