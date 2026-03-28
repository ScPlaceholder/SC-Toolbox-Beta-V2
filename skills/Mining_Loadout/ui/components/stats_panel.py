"""Stats panel component — PySide6 version."""
import shared.path_setup  # noqa: E402  # centralised path config
from shared.i18n import s_ as _

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame,
)

from shared.qt.theme import P
from ui.constants import STATS_DISPLAY


def _separator(parent_layout):
    line = QFrame()
    line.setFrameShape(QFrame.HLine)
    line.setFixedHeight(1)
    line.setStyleSheet(f"background-color: {P.separator};")
    parent_layout.addWidget(line)


def build_stats_panel(parent: QWidget) -> dict:
    """Build the stats panel and return references.

    Returns dict with keys:
        'stat_labels': dict[str, QLabel]  (value labels),
        'stat_directions': dict[str, int],
        'price_detail_label': QLabel,
        'src_detail_label': QLabel,
    """
    layout = QVBoxLayout(parent)
    layout.setContentsMargins(8, 0, 8, 8)
    layout.setSpacing(0)

    # Header
    hdr = QLabel("  " + _("LOADOUT STATS"))
    hdr.setStyleSheet(f"""
        font-family: Electrolize, Consolas;
        font-size: 10pt;
        font-weight: bold;
        color: {P.tool_mining};
        background: transparent;
        padding-top: 12px;
        padding-bottom: 2px;
    """)
    layout.addWidget(hdr)
    _separator(layout)

    stat_labels = {}
    stat_directions = {}

    for entry in STATS_DISPLAY:
        if entry is None:
            _separator(layout)
            continue
        key, label, unit, direction = entry

        row = QWidget()
        row.setStyleSheet("background: transparent;")
        row_lay = QHBoxLayout(row)
        row_lay.setContentsMargins(4, 1, 4, 1)
        row_lay.setSpacing(4)

        name_lbl = QLabel(f"{label}:")
        name_lbl.setStyleSheet(f"""
            font-family: Consolas;
            font-size: 8pt;
            color: {P.fg_dim};
            background: transparent;
        """)
        name_lbl.setFixedWidth(100)
        row_lay.addWidget(name_lbl)

        val_lbl = QLabel("\u2014")
        val_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        val_lbl.setStyleSheet(f"""
            font-family: Consolas;
            font-size: 9pt;
            font-weight: bold;
            color: {P.fg};
            background: transparent;
        """)
        row_lay.addWidget(val_lbl)

        layout.addWidget(row)
        stat_labels[key] = val_lbl
        stat_directions[key] = direction

    _separator(layout)

    # Price section
    price_hdr = QLabel("  " + _("LOADOUT PRICE"))
    price_hdr.setStyleSheet(f"""
        font-family: Electrolize, Consolas;
        font-size: 9pt;
        font-weight: bold;
        color: {P.tool_mining};
        background: transparent;
        padding-top: 4px;
    """)
    layout.addWidget(price_hdr)

    price_detail_label = QLabel("0 " + _("aUEC"))
    price_detail_label.setStyleSheet(f"""
        font-family: Consolas;
        font-size: 11pt;
        font-weight: bold;
        color: {P.green};
        background: transparent;
        padding-left: 16px;
        padding-top: 2px;
    """)
    layout.addWidget(price_detail_label)

    _separator(layout)

    src_detail_label = QLabel(_("Select a ship and equipment above"))
    src_detail_label.setWordWrap(True)
    src_detail_label.setStyleSheet(f"""
        font-family: Consolas;
        font-size: 7pt;
        color: {P.fg_dim};
        background: transparent;
        padding: 4px;
    """)
    layout.addWidget(src_detail_label)

    layout.addStretch(1)

    return {
        "stat_labels": stat_labels,
        "stat_directions": stat_directions,
        "price_detail_label": price_detail_label,
        "src_detail_label": src_detail_label,
    }
