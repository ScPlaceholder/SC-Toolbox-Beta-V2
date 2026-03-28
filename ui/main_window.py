"""
Main launcher window — PySide6 MobiGlas-style implementation.

Assembles header, tile grid, and settings panel using the shared Qt library.
"""
import logging
import webbrowser
from typing import Callable, Dict, List, Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont, QColor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QGridLayout,
    QScrollArea, QFrame, QSizePolicy,
)

from shared.config_models import SkillConfig, WindowGeometry
from shared.i18n import _ as _t
from shared.qt.theme import P
from shared.qt.base_window import SCWindow
from shared.qt.title_bar import SCTitleBar
from shared.qt.hud_widgets import HUDPanel, GlowEffect
from shared.qt.animated_button import SCButton
from ui.tiles import SkillTile, build_tile_grid
from ui.settings_panel import SettingsPanel

log = logging.getLogger(__name__)


def get_hotkey_display(key: str) -> str:
    """Format a pynput hotkey string for badge display."""
    if not key:
        return "\u2014"
    s = key
    s = s.replace("<shift>+", "\u21e7")
    s = s.replace("<ctrl>+", "^")
    s = s.replace("<alt>+", "\u2325")
    s = s.replace("<cmd>+", "\u2318")
    s = s.replace("<", "").replace(">", "")
    return s


class LauncherWindow(SCWindow):
    """The top-level SC Toolbox launcher window (PySide6)."""

    def __init__(
        self,
        geometry: WindowGeometry,
        skills: List[SkillConfig],
        availability: Dict[str, bool],
        launcher_hotkey: str,
        python_info: str,
        on_toggle_skill: Callable[[str], None],
        on_apply_hotkeys: Callable[[str, Dict[str, str]], None],
        on_shutdown: Callable[[], None],
        current_language: str = "en",
        available_languages: Optional[List[str]] = None,
        on_language_change: Optional[Callable[[str], None]] = None,
    ) -> None:
        super().__init__(
            title="SC_Toolbox",
            width=geometry.w,
            height=geometry.h,
            min_w=400,
            min_h=200,
            opacity=geometry.opacity,
        )
        self._skills = skills
        self._on_shutdown = on_shutdown

        self.restore_geometry_from_args(geometry.x, geometry.y, geometry.w, geometry.h, geometry.opacity)

        # ── Title bar ──
        self._title_bar = SCTitleBar(
            window=self,
            title="SC Toolbox",
            icon_text="",
            accent_color=P.accent,
            hotkey_text=get_hotkey_display(launcher_hotkey),
            show_minimize=True,
        )
        self._title_bar.minimize_clicked.connect(self.showMinimized)
        self._title_bar.close_clicked.connect(self._on_close)
        self.content_layout.addWidget(self._title_bar)

        # ── Header info bar ──
        header = QWidget(self)
        header.setFixedHeight(28)
        header.setStyleSheet(f"background-color: {P.bg_header};")
        h_layout = QHBoxLayout(header)
        h_layout.setContentsMargins(12, 0, 12, 0)
        h_layout.setSpacing(12)

        # Pledge Store link
        pledge = QLabel(_t("PLEDGE STORE"), header)
        pledge.setStyleSheet(f"""
            font-family: Consolas; font-size: 8pt; font-weight: bold;
            color: #00ff66; background: transparent;
        """)
        pledge.setCursor(Qt.PointingHandCursor)
        pledge.mousePressEvent = lambda e: webbrowser.open("https://robertsspaceindustries.com/en/pledge")
        h_layout.addWidget(pledge)

        h_layout.addStretch(1)

        # Status
        self._status_label = QLabel(_t("Ready"), header)
        self._status_label.setStyleSheet(f"""
            font-family: Consolas; font-size: 8pt;
            color: {P.fg_dim}; background: transparent;
        """)
        h_layout.addWidget(self._status_label)

        # Python info
        if python_info:
            py_label = QLabel(python_info, header)
            py_label.setStyleSheet(f"""
                font-family: Consolas; font-size: 7pt;
                color: {P.fg_disabled}; background: transparent;
            """)
            h_layout.addWidget(py_label)
        else:
            py_label = QLabel(_t("Python not found!"), header)
            py_label.setStyleSheet(f"""
                font-family: Consolas; font-size: 7pt;
                color: {P.red}; background: transparent;
            """)
            h_layout.addWidget(py_label)

        # Discord link
        discord = QLabel(_t("DISCORD"), header)
        discord.setStyleSheet(f"""
            font-family: Consolas; font-size: 8pt; font-weight: bold;
            color: #7289da; background: transparent;
        """)
        discord.setCursor(Qt.PointingHandCursor)
        discord.mousePressEvent = lambda e: webbrowser.open("https://discord.gg/A7JDCxmC")
        h_layout.addWidget(discord)

        self.content_layout.addWidget(header)

        # ── Separator ──
        sep = QFrame(self)
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"background-color: {P.border};")
        self.content_layout.addWidget(sep)

        # ── Tile grid ──
        tiles_container = QWidget(self)
        tiles_container.setStyleSheet(f"background-color: {P.bg_primary};")
        tiles_layout = QVBoxLayout(tiles_container)
        tiles_layout.setContentsMargins(10, 10, 10, 10)

        self._tiles = build_tile_grid(
            parent=tiles_container,
            skills=skills,
            availability=availability,
            on_toggle=on_toggle_skill,
        )
        self.content_layout.addWidget(tiles_container, stretch=1)

        # Set initial hotkey badges
        for skill in skills:
            tile = self._tiles.get(skill.id)
            if tile:
                tile.set_hotkey(get_hotkey_display(skill.hotkey))

        # ── Settings panel ──
        self._settings_panel = SettingsPanel(
            parent=self,
            skills=skills,
            launcher_hotkey=launcher_hotkey,
            on_apply=on_apply_hotkeys,
            current_language=current_language,
            available_languages=available_languages or ["en"],
            on_language_change=on_language_change,
        )
        self.content_layout.addWidget(self._settings_panel)

    # ── Public API ──

    def update_tile(self, skill_id: str, running: bool, visible: bool) -> None:
        tile = self._tiles.get(skill_id)
        if tile:
            tile.update_status(running, visible)

    def update_hotkey_badges(self, launcher_hotkey: str, skill_hotkeys: Dict[str, str]) -> None:
        self._title_bar.set_hotkey(get_hotkey_display(launcher_hotkey))
        for skill in self._skills:
            tile = self._tiles.get(skill.id)
            if tile:
                hk = skill_hotkeys.get(skill.id, skill.hotkey)
                tile.set_hotkey(get_hotkey_display(hk))

    def set_status(self, text: str, color: Optional[str] = None) -> None:
        self._status_label.setText(text)
        if color:
            self._status_label.setStyleSheet(f"""
                font-family: Consolas; font-size: 8pt;
                color: {color}; background: transparent;
            """)

    def toggle_visibility(self) -> None:
        if self.isVisible():
            self.hide()
        else:
            self.show()
            self.raise_()

    def schedule(self, delay_ms: int, fn) -> None:
        """Thread-safe callback scheduling (replaces root.after())."""
        QTimer.singleShot(delay_ms, fn)

    def run(self) -> None:
        """Start the event loop (called by SCToolboxApp)."""
        self.show()
        from PySide6.QtWidgets import QApplication
        QApplication.instance().exec()

    def _on_close(self) -> None:
        self._on_shutdown()
        self.close()
