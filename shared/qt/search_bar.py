"""
SCSearchBar – styled search input with debounce timer.
"""

from __future__ import annotations
from typing import Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QLineEdit, QWidget

from shared.qt.theme import P


class SCSearchBar(QLineEdit):
    """Search bar with built-in debounce.

    Emits ``search_changed(str)`` after the user stops typing for
    ``debounce_ms`` milliseconds.
    """

    search_changed = Signal(str)

    def __init__(
        self,
        placeholder: str = "Search...",
        debounce_ms: int = 300,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.setPlaceholderText(placeholder)
        self.setClearButtonEnabled(True)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(debounce_ms)
        self._timer.timeout.connect(self._emit)

        self.textChanged.connect(self._on_text_changed)

    def _on_text_changed(self, _text: str) -> None:
        self._timer.start()

    def _emit(self) -> None:
        self.search_changed.emit(self.text().strip())
