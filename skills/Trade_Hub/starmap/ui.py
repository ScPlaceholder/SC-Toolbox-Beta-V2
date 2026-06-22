"""Small shared Qt widget helpers for the star map's popups/bubbles."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QPushButton


def make_close_button(on_click=None, size: int = 28) -> QPushButton:
    """A square red 'x' close button matching the app's title-bar / route-bubble
    close (square corners, bold 'x'), applied to every bubble/popup so the exit
    is consistent and obvious."""
    b = QPushButton("x")
    b.setFixedSize(size, size)
    b.setCursor(Qt.PointingHandCursor)
    b.setToolTip("Close")
    b.setStyleSheet(
        "QPushButton{background:rgba(220,50,50,0.90); color:#ffffff; border:none; "
        "border-radius:3px; font-family:Consolas; font-size:13pt; font-weight:bold; "
        "padding:0px;}"
        "QPushButton:hover{background:rgba(240,70,70,1.0);}"
        "QPushButton:pressed{background:rgba(180,40,40,1.0);}")
    if on_click is not None:
        b.clicked.connect(on_click)
    return b
