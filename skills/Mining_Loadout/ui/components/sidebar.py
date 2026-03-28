"""Left sidebar component — PySide6 version."""
import shared.path_setup  # noqa: E402  # centralised path config
from shared.i18n import s_ as _

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QPushButton,
    QScrollArea, QFrame, QSizePolicy,
)

from shared.qt.theme import P
from shared.qt.animated_button import SCButton
from models.items import SHIPS

SIDEBAR_WIDTH = 195


def _separator(parent_layout):
    line = QFrame()
    line.setFrameShape(QFrame.HLine)
    line.setFixedHeight(1)
    line.setStyleSheet(f"background-color: {P.separator};")
    parent_layout.addWidget(line)


def _section_label(text, parent_layout, pad_top=6):
    lbl = QLabel(text)
    lbl.setStyleSheet(f"""
        font-family: Consolas;
        font-size: 7pt;
        color: {P.fg_dim};
        background: transparent;
        padding-top: {pad_top}px;
    """)
    parent_layout.addWidget(lbl)


def build_sidebar(
    on_ship_changed,
    on_reset,
    on_copy_stats,
) -> dict:
    """Build the sidebar widget and return references.

    Returns dict with keys:
        'widget': QWidget (the sidebar),
        'ship_btns': dict[str, QPushButton],
        'status_label': QLabel,
    """
    # Outer scroll area
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFixedWidth(SIDEBAR_WIDTH)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    scroll.setStyleSheet(f"""
        QScrollArea {{
            background-color: {P.bg_deepest};
            border: none;
        }}
    """)

    container = QWidget()
    container.setStyleSheet(f"background-color: {P.bg_deepest};")
    lay = QVBoxLayout(container)
    lay.setContentsMargins(10, 10, 10, 10)
    lay.setSpacing(2)

    # Ship selector
    _section_label("MINING SHIP:", lay, pad_top=0)
    ship_btns = {}
    for ship in SHIPS:
        btn = QPushButton(ship.upper())
        btn.setCursor(Qt.PointingHandCursor)
        btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {P.bg_card};
                color: {P.fg_dim};
                border: 1px solid {P.border};
                padding: 4px;
                font-family: Consolas;
                font-size: 9pt;
                font-weight: bold;
            }}
            QPushButton:hover {{
                background-color: {P.bg_input};
                border-color: {P.tool_mining};
                color: {P.fg_bright};
            }}
        """)
        btn.clicked.connect(lambda checked=False, s=ship: on_ship_changed(s))
        lay.addWidget(btn)
        ship_btns[ship] = btn

    _separator(lay)

    # Reset button
    rst_btn = SCButton("\u21ba  RESET LOADOUT", glow_color=P.fg_dim)
    rst_btn.setStyleSheet(f"""
        QPushButton {{
            background-color: {P.bg_card};
            color: {P.fg};
            border: 1px solid {P.border};
            padding: 4px;
            font-family: Consolas;
            font-size: 9pt;
        }}
        QPushButton:hover {{
            background-color: {P.selection};
            color: {P.fg_bright};
        }}
    """)
    rst_btn.clicked.connect(on_reset)
    lay.addWidget(rst_btn)

    # Copy stats button
    copy_btn = SCButton("\U0001f4cb  COPY STATS", glow_color=P.green)
    copy_btn.setStyleSheet(f"""
        QPushButton {{
            background-color: {P.bg_card};
            color: {P.green};
            border: 1px solid {P.border};
            padding: 4px;
            font-family: Consolas;
            font-size: 9pt;
        }}
        QPushButton:hover {{
            background-color: {P.selection};
            color: {P.fg_bright};
        }}
    """)
    copy_btn.clicked.connect(on_copy_stats)
    lay.addWidget(copy_btn)

    _separator(lay)

    # Status label
    status_label = QLabel("  " + _("Loading data\u2026"))
    status_label.setWordWrap(True)
    status_label.setStyleSheet(f"""
        font-family: Consolas;
        font-size: 7pt;
        color: {P.fg_dim};
        background: transparent;
    """)
    lay.addWidget(status_label)

    lay.addStretch(1)

    scroll.setWidget(container)

    return {
        "widget": scroll,
        "ship_btns": ship_btns,
        "status_label": status_label,
    }
