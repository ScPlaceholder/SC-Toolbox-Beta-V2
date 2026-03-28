"""Scrollable helpers — thin wrappers around QScrollArea.

In the PySide6 version these are trivial since QScrollArea handles everything
that required manual Canvas + Scrollbar + mousewheel wiring in tkinter.
"""
from __future__ import annotations
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QScrollArea, QWidget, QVBoxLayout

from shared.qt.theme import P


def make_scrollable_sidebar(parent: Optional[QWidget] = None, width: int = 220) -> QScrollArea:
    """Create a scrollable sidebar widget.

    Returns a QScrollArea whose inner widget has a QVBoxLayout accessible
    via scroll_area.widget().layout().
    """
    scroll = QScrollArea(parent)
    scroll.setFixedWidth(width)
    scroll.setWidgetResizable(True)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    scroll.setStyleSheet(f"""
        QScrollArea {{
            background-color: {P.bg_secondary};
            border: none;
        }}
    """)

    inner = QWidget()
    inner.setStyleSheet(f"background-color: {P.bg_secondary};")
    layout = QVBoxLayout(inner)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)
    scroll.setWidget(inner)
    return scroll
