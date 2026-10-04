"""Turret panel component — PySide6 version."""
import shared.path_setup  # noqa: E402  # centralised path config
from shared.i18n import s_ as _

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QSizePolicy,
)

from shared.qt.theme import P
from shared.qt.dropdown import SCComboBox
from models.items import MAX_MODULE_SLOTS, NONE_LASER, NONE_MODULE
from ui.components.crafted_controls import CraftedControls


def build_turret_panel(
    turret_name: str,
    laser_size: int,
    turret_index: int,
    on_changed,
    on_laser_info,
    on_module_info,
    num_module_slots: int = MAX_MODULE_SLOTS,
    craft_index=None,
) -> dict:
    """Build a single turret panel widget.

    Returns dict with keys:
        'widget': QWidget,
        'laser_combo': SCComboBox,
        'module_combos': list[SCComboBox],
        'module_slot_containers': list[QWidget],
        'crafted': CraftedControls (hidden when there is no blueprint data),
    """
    outer = QFrame()
    outer.setStyleSheet(f"""
        QFrame {{
            background-color: {P.bg_card};
            border: 1px solid {P.tool_mining};
        }}
    """)

    outer_lay = QVBoxLayout(outer)
    outer_lay.setContentsMargins(0, 0, 0, 0)
    outer_lay.setSpacing(0)

    # Title bar
    tbar = QWidget()
    tbar.setStyleSheet(f"background-color: {P.tool_mining};")
    tbar_lay = QHBoxLayout(tbar)
    tbar_lay.setContentsMargins(8, 4, 8, 4)
    tbar_lay.setSpacing(4)

    tbar_name = QLabel(f"  {turret_name.upper()}")
    tbar_name.setStyleSheet(f"""
        font-family: Consolas;
        font-size: 9pt;
        font-weight: bold;
        color: {P.bg_primary};
        background: transparent;
    """)
    tbar_lay.addWidget(tbar_name)
    tbar_lay.addStretch(1)

    tbar_size = QLabel(f"{_('SIZE')} {laser_size}  ")
    tbar_size.setStyleSheet(f"""
        font-family: Consolas;
        font-size: 7pt;
        color: {P.bg_primary};
        background: transparent;
    """)
    tbar_lay.addWidget(tbar_size)
    outer_lay.addWidget(tbar)

    # Content area
    content = QWidget()
    content.setStyleSheet(f"background-color: {P.bg_card}; border: none;")
    content_lay = QVBoxLayout(content)
    content_lay.setContentsMargins(8, 8, 8, 8)
    content_lay.setSpacing(2)

    # Laser dropdown
    laser_lbl = QLabel(_("LASER HEAD"))
    laser_lbl.setStyleSheet(f"""
        font-family: Consolas;
        font-size: 7pt;
        color: {P.fg_dim};
        background: transparent;
    """)
    content_lay.addWidget(laser_lbl)

    laser_combo = SCComboBox()
    laser_combo.addItem(NONE_LASER)
    laser_combo.currentIndexChanged.connect(lambda _: on_changed())
    content_lay.addWidget(laser_combo)

    # Laser info link
    li = QLabel(" \u24d8 " + _("Details"))
    li.setStyleSheet(f"""
        font-family: Consolas;
        font-size: 8pt;
        color: {P.accent};
        background: transparent;
    """)
    li.setCursor(Qt.PointingHandCursor)
    li.mousePressEvent = lambda _, ti=turret_index: on_laser_info(ti)
    content_lay.addWidget(li)

    # Crafted laser: tick box + material qualities (from the blueprint data)
    crafted = CraftedControls(craft_index)
    crafted.changed.connect(on_changed)
    content_lay.addWidget(crafted)

    # Module slots
    module_combos = []
    module_slot_containers = []
    for slot in range(num_module_slots):
        slot_container = QWidget()
        slot_container.setStyleSheet("background: transparent;")
        slot_lay = QVBoxLayout(slot_container)
        slot_lay.setContentsMargins(0, 0, 0, 0)
        slot_lay.setSpacing(2)

        mlbl = QLabel(f"{_('MODULE SLOT')} {slot + 1}")
        mlbl.setStyleSheet(f"""
            font-family: Consolas;
            font-size: 7pt;
            color: {P.fg_dim};
            background: transparent;
            padding-top: 4px;
        """)
        slot_lay.addWidget(mlbl)

        mc = SCComboBox()
        mc.addItem(NONE_MODULE)
        mc.currentIndexChanged.connect(lambda _: on_changed())
        slot_lay.addWidget(mc)
        module_combos.append(mc)

        mi = QLabel(" \u24d8 " + _("Details"))
        mi.setStyleSheet(f"""
            font-family: Consolas;
            font-size: 8pt;
            color: {P.accent};
            background: transparent;
        """)
        mi.setCursor(Qt.PointingHandCursor)
        mi.mousePressEvent = lambda _, ti=turret_index, sl=slot: on_module_info(ti, sl)
        slot_lay.addWidget(mi)

        content_lay.addWidget(slot_container)
        module_slot_containers.append(slot_container)

    content_lay.addStretch(1)
    outer_lay.addWidget(content, 1)

    return {
        "widget": outer,
        "laser_combo": laser_combo,
        "module_combos": module_combos,
        "module_slot_containers": module_slot_containers,
        "crafted": crafted,
    }
