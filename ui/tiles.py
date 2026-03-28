"""
Skill tile widgets for the launcher grid — PySide6 MobiGlas implementation.

Each tile is an HUDPanel with corner brackets, tool accent stripe,
hover glow, and status/launch controls.
"""
import logging
from typing import Callable, Dict

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QColor, QPainter, QPen, QCursor
from PySide6.QtWidgets import (
    QWidget, QFrame, QHBoxLayout, QVBoxLayout, QGridLayout,
    QLabel, QSizePolicy, QGraphicsDropShadowEffect,
)

from shared.config_models import SkillConfig
from shared.i18n import _ as _t
from shared.qt.theme import P
from shared.qt.hud_widgets import HUDPanel
from shared.qt.animated_button import SCButton

log = logging.getLogger(__name__)


class SkillTile(QFrame):
    """A single skill card in the launcher grid."""

    clicked = Signal(str)  # emits skill_id

    def __init__(
        self,
        skill: SkillConfig,
        available: bool,
        on_toggle: Callable[[str], None],
        parent: QWidget = None,
    ):
        super().__init__(parent)
        self.skill = skill
        self._on_toggle = on_toggle
        self._available = available
        self._accent = skill.color if available else P.fg_disabled

        self.setCursor(Qt.PointingHandCursor if available else Qt.ArrowCursor)
        self.setStyleSheet(f"""
            SkillTile {{
                background-color: {P.bg_card};
                border: 1px solid {P.border};
            }}
            SkillTile:hover {{
                border-color: {self._accent};
            }}
        """)

        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Left accent stripe
        stripe = QWidget(self)
        stripe.setFixedWidth(3)
        stripe.setStyleSheet(f"background-color: {self._accent};")
        main_layout.addWidget(stripe)

        # Content area
        content = QWidget(self)
        content.setStyleSheet("background: transparent;")
        c_layout = QVBoxLayout(content)
        c_layout.setContentsMargins(12, 10, 12, 10)
        c_layout.setSpacing(6)

        # Row 1: icon + name + hotkey badge
        row1 = QHBoxLayout()
        row1.setSpacing(8)

        icon_label = QLabel(skill.icon, content)
        icon_label.setStyleSheet(f"""
            font-size: 16pt; color: {self._accent}; background: transparent;
        """)
        row1.addWidget(icon_label)

        name_label = QLabel(skill.name, content)
        name_label.setStyleSheet(f"""
            font-family: Electrolize, Consolas, monospace;
            font-size: 10pt; font-weight: bold;
            color: {P.fg if available else P.fg_disabled};
            background: transparent;
        """)
        row1.addWidget(name_label, stretch=1)

        self._hotkey_label = QLabel("", content)
        self._hotkey_label.setStyleSheet(f"""
            font-family: Consolas; font-size: 8pt; font-weight: bold;
            color: {self._accent};
            background-color: {P.bg_input};
            padding: 2px 6px;
            border: 1px solid {P.border};
        """)
        row1.addWidget(self._hotkey_label)

        c_layout.addLayout(row1)

        # Row 2: status + launch button
        row2 = QHBoxLayout()
        row2.setSpacing(8)

        self._status_label = QLabel(
            _t("Available") if available else _t("Not installed"), content)
        status_color = P.fg_dim if available else P.red
        self._status_label.setStyleSheet(f"""
            font-family: Consolas; font-size: 8pt;
            color: {status_color}; background: transparent;
        """)
        row2.addWidget(self._status_label, stretch=1)

        if available:
            launch_btn = SCButton("\u25b6 " + _t("Launch"), self, glow_color=self._accent)
            launch_btn.setProperty("success", True)
            launch_btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: #1a3020;
                    color: {P.green};
                    border: none;
                    font-family: Consolas; font-size: 8pt; font-weight: bold;
                    padding: 4px 10px;
                }}
                QPushButton:hover {{
                    background-color: #1f3a28;
                    color: {P.fg_bright};
                }}
            """)
            launch_btn.clicked.connect(lambda: self._on_toggle(skill.id))
            row2.addWidget(launch_btn)
        else:
            dash = QLabel("\u2014", content)
            dash.setStyleSheet(f"""
                font-family: Consolas; font-size: 8pt;
                color: {P.fg_disabled}; background: transparent;
            """)
            row2.addWidget(dash)

        c_layout.addLayout(row2)
        main_layout.addWidget(content, stretch=1)

    def set_hotkey(self, text: str) -> None:
        self._hotkey_label.setText(text)

    def update_status(self, running: bool, visible: bool) -> None:
        if running:
            if visible:
                self._status_label.setText(_t("Running"))
                self._status_label.setStyleSheet(f"""
                    font-family: Consolas; font-size: 8pt;
                    color: {P.green}; background: transparent;
                """)
            else:
                self._status_label.setText(_t("Hidden"))
                self._status_label.setStyleSheet(f"""
                    font-family: Consolas; font-size: 8pt;
                    color: {P.yellow}; background: transparent;
                """)
        else:
            self._status_label.setText(_t("Available"))
            self._status_label.setStyleSheet(f"""
                font-family: Consolas; font-size: 8pt;
                color: {P.fg_dim}; background: transparent;
            """)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self._available:
            self._on_toggle(self.skill.id)
        super().mousePressEvent(event)


def build_tile_grid(
    parent: QWidget,
    skills: list[SkillConfig],
    availability: Dict[str, bool],
    on_toggle: Callable[[str], None],
    columns: int = 2,
    grid_layout: Dict[str, str] | None = None,
) -> Dict[str, SkillTile]:
    """Create the full grid of tiles and return a dict keyed by skill ID.

    Parameters
    ----------
    grid_layout:
        Optional mapping of ``"row,col"`` → ``skill_id`` for explicit
        cell placement.  Skills not assigned to a cell are placed in
        the remaining empty slots in order.
    """
    grid = QGridLayout()
    grid.setSpacing(8)

    # Find or create the layout on the parent
    layout = parent.layout()
    if layout is None:
        layout = QVBoxLayout(parent)
        layout.setContentsMargins(0, 0, 0, 0)
    layout.addLayout(grid)

    skill_by_id = {s.id: s for s in skills}
    tiles: Dict[str, SkillTile] = {}

    # Phase 1: place explicitly assigned skills
    placed_ids: set[str] = set()
    occupied_cells: set[tuple[int, int]] = set()

    if grid_layout:
        for cell_key, sid in grid_layout.items():
            if sid not in skill_by_id:
                continue
            parts = cell_key.split(",")
            if len(parts) != 2:
                continue
            try:
                r, c = int(parts[0]), int(parts[1])
            except ValueError:
                continue
            if c >= columns:
                continue
            skill = skill_by_id[sid]
            tile = SkillTile(
                skill=skill,
                available=availability.get(skill.id, False),
                on_toggle=on_toggle,
                parent=parent,
            )
            grid.addWidget(tile, r, c)
            tiles[skill.id] = tile
            placed_ids.add(sid)
            occupied_cells.add((r, c))

    # Phase 2: auto-fill remaining skills into empty cells
    remaining = [s for s in skills if s.id not in placed_ids]
    cell_idx = 0
    for skill in remaining:
        # Find next empty cell
        while True:
            r = cell_idx // columns
            c = cell_idx % columns
            cell_idx += 1
            if (r, c) not in occupied_cells:
                break
        tile = SkillTile(
            skill=skill,
            available=availability.get(skill.id, False),
            on_toggle=on_toggle,
            parent=parent,
        )
        grid.addWidget(tile, r, c)
        tiles[skill.id] = tile

    # Make columns stretch equally
    for c in range(columns):
        grid.setColumnStretch(c, 1)

    return tiles
