"""
Main launcher window — PySide6 MobiGlas-style implementation.

Assembles header, tile grid, and settings panel using the shared Qt library.
"""
import logging
import webbrowser
from typing import Callable, Dict, List, Optional

from PySide6.QtCore import Qt, QTimer, Signal, QPropertyAnimation, QEasingCurve, QPoint
from PySide6.QtGui import QFont, QColor, QPainter, QPen, QLinearGradient
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QGridLayout,
    QScrollArea, QFrame, QSizePolicy, QPushButton, QGraphicsOpacityEffect,
)

from shared.config_models import SkillConfig, WindowGeometry
from shared.i18n import _ as _t
from shared.qt.theme import P
from shared.qt.base_window import SCWindow
from shared.qt.title_bar import SCTitleBar
from shared.qt.hud_widgets import HUDPanel, GlowEffect
from shared.qt.animated_button import SCButton
from shared.update_checker import UpdateResult, check_for_updates_async
from ui.tiles import SkillTile, build_tile_grid
from ui.settings_panel import SettingsPopup

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


class UpdateBubble(QWidget):
    """HUD-styled floating notification bubble for update alerts."""

    def __init__(self, parent_window: QWidget, result: UpdateResult):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setObjectName("updateBubble")
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(340, 130)
        self._parent_window = parent_window
        self._result = result

        # Position near top-right of parent
        pr = parent_window.geometry()
        self.move(pr.x() + pr.width() - 360, pr.y() + 50)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(12, 10, 12, 10)
        main_layout.setSpacing(8)

        # Title
        title = QLabel(f"UPDATE AVAILABLE", self)
        title.setStyleSheet(f"""
            font-family: Electrolize, Consolas, monospace;
            font-size: 10pt; font-weight: bold;
            color: {P.green}; background: transparent;
            letter-spacing: 2px;
        """)
        main_layout.addWidget(title)

        # Version info
        info = QLabel(f"v{result.current_version}  →  v{result.latest_version}", self)
        info.setStyleSheet(f"""
            font-family: Consolas, monospace; font-size: 9pt;
            color: {P.fg_bright}; background: transparent;
        """)
        main_layout.addWidget(info)

        # Buttons row
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        download_btn = QPushButton("OPEN DOWNLOAD", self)
        download_btn.setCursor(Qt.PointingHandCursor)
        download_btn.setStyleSheet(f"""
            QPushButton {{
                font-family: Consolas; font-size: 8pt; font-weight: bold;
                color: {P.bg_deepest}; background: {P.green};
                border: none; border-radius: 3px; padding: 4px 12px;
            }}
            QPushButton:hover {{ background: #55eebb; }}
        """)
        download_btn.clicked.connect(self._open_download)
        btn_row.addWidget(download_btn)

        dismiss_btn = QPushButton("DISMISS", self)
        dismiss_btn.setCursor(Qt.PointingHandCursor)
        dismiss_btn.setStyleSheet(f"""
            QPushButton {{
                font-family: Consolas; font-size: 8pt; font-weight: bold;
                color: {P.fg_dim}; background: rgba(200,200,200,0.08);
                border: 1px solid {P.border}; border-radius: 3px; padding: 4px 12px;
            }}
            QPushButton:hover {{ background: rgba(200,200,200,0.15); color: {P.fg_bright}; }}
        """)
        dismiss_btn.clicked.connect(self.close)
        btn_row.addWidget(dismiss_btn)

        btn_row.addStretch(1)
        main_layout.addLayout(btn_row)

        # Auto-dismiss after 15 seconds
        QTimer.singleShot(15000, self.close)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        # Background
        bg = QColor(P.bg_header)
        bg.setAlpha(240)
        p.setBrush(bg)
        p.setPen(QPen(QColor(P.green), 1))
        p.drawRoundedRect(1, 1, w - 2, h - 2, 6, 6)
        # Top glow
        glow = QLinearGradient(0, 0, 0, 30)
        gc = QColor(P.green)
        gc.setAlpha(20)
        glow.setColorAt(0.0, gc)
        gc2 = QColor(P.green)
        gc2.setAlpha(0)
        glow.setColorAt(1.0, gc2)
        p.setPen(Qt.NoPen)
        p.setBrush(glow)
        p.drawRoundedRect(1, 1, w - 2, 30, 6, 6)
        p.end()

    def _open_download(self):
        webbrowser.open(self._result.release_url)
        self.close()


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
        on_apply_settings: Callable[[dict], None],
        on_shutdown: Callable[[], None],
        current_language: str = "en",
        available_languages: Optional[List[str]] = None,
        disabled_skills: Optional[List[str]] = None,
        grid_rows: int = 3,
        grid_cols: int = 2,
        grid_layout: Optional[Dict[str, str]] = None,
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
        self._on_apply_settings = on_apply_settings
        self._launcher_hotkey = launcher_hotkey
        self._current_language = current_language
        self._available_languages = available_languages or ["en"]
        self._disabled_skills = disabled_skills or []
        self._grid_rows = grid_rows
        self._grid_cols = grid_cols
        self._grid_layout = grid_layout or {}
        self._settings_popup: Optional[SettingsPopup] = None
        self._update_bubble: Optional[UpdateBubble] = None

        self.restore_geometry_from_args(geometry.x, geometry.y, geometry.w, geometry.h, geometry.opacity)

        # ── Title bar ──
        self._title_bar = SCTitleBar(
            window=self,
            title="SC Toolbox",
            icon_text="",
            accent_color=P.accent,
            hotkey_text=get_hotkey_display(launcher_hotkey),
            show_minimize=True,
            extra_buttons=[
                (_t("GITHUB"), lambda: webbrowser.open("https://github.com/ScPlaceholder/SC-Toolbox")),
                (_t("UPDATE"), self._check_for_updates),
            ],
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

        # Fleet Viewer link
        fleet = QLabel(_t("FLEET VIEWER"), header)
        fleet.setStyleSheet(f"""
            font-family: Consolas; font-size: 8pt; font-weight: bold;
            color: #00ff66; background: transparent;
        """)
        fleet.setCursor(Qt.PointingHandCursor)
        fleet.mousePressEvent = lambda e: webbrowser.open("https://hangar.link/fleet/canvas")
        h_layout.addWidget(fleet)

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
        # Filter out disabled skills
        enabled_skills = [s for s in skills if s.id not in self._disabled_skills]
        tiles_container = QWidget(self)
        tiles_container.setStyleSheet(f"background-color: {P.bg_primary};")
        tiles_layout = QVBoxLayout(tiles_container)
        tiles_layout.setContentsMargins(10, 10, 10, 10)

        self._tiles = build_tile_grid(
            parent=tiles_container,
            skills=enabled_skills,
            availability=availability,
            on_toggle=on_toggle_skill,
            columns=grid_cols,
            grid_layout=self._grid_layout,
        )
        self.content_layout.addWidget(tiles_container, stretch=1)

        # Set initial hotkey badges
        for skill in enabled_skills:
            tile = self._tiles.get(skill.id)
            if tile:
                tile.set_hotkey(get_hotkey_display(skill.hotkey))

        # ── Settings button ──
        from ui.settings_panel import _btn_qss
        settings_btn = SCButton("\u2699 " + _t("Settings"), self, glow_color=P.accent)
        settings_btn.setStyleSheet(_btn_qss("#1a2538", "#223050", P.accent))
        settings_btn.clicked.connect(self._open_settings)
        self.content_layout.addWidget(settings_btn)

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

    def _open_settings(self) -> None:
        """Open the settings popup bubble."""
        if self._settings_popup and self._settings_popup.isVisible():
            self._settings_popup.raise_()
            return
        self._settings_popup = SettingsPopup(
            parent_window=self,
            skills=self._skills,
            launcher_hotkey=self._launcher_hotkey,
            disabled_skills=self._disabled_skills,
            grid_rows=self._grid_rows,
            grid_cols=self._grid_cols,
            grid_layout=self._grid_layout,
            current_language=self._current_language,
            available_languages=self._available_languages,
            on_apply=self._on_apply_settings,
        )
        self._settings_popup.show()

    # ── Update checking ──

    def _check_for_updates(self) -> None:
        """Manual update check triggered by the title-bar button."""
        self.set_status(_t("Checking for updates..."))
        check_for_updates_async(self._on_update_result)

    def check_for_updates_at_startup(self) -> None:
        """Called once after launch to silently check for updates."""
        check_for_updates_async(self._on_startup_update_result)

    def _on_update_result(self, result: UpdateResult) -> None:
        """Callback from manual update check (runs on background thread)."""
        QTimer.singleShot(0, lambda: self._show_update_result(result, silent=False))

    def _on_startup_update_result(self, result: UpdateResult) -> None:
        """Callback from startup update check — only show if update available."""
        QTimer.singleShot(0, lambda: self._show_update_result(result, silent=True))

    def _show_update_result(self, result: UpdateResult, silent: bool) -> None:
        if result.error and not silent:
            self.set_status(_t("Update check failed"), P.red)
            QTimer.singleShot(4000, lambda: self.set_status(_t("Ready")))
            return

        if result.available:
            self.set_status(f"{_t('Update available')}: v{result.latest_version}", P.green)
            if self._update_bubble:
                self._update_bubble.close()
            self._update_bubble = UpdateBubble(self, result)
            self._update_bubble.show()
        elif not silent:
            self.set_status(f"{_t('Up to date')} (v{result.current_version})", P.green)
            QTimer.singleShot(4000, lambda: self.set_status(_t("Ready")))

    def _on_close(self) -> None:
        if self._update_bubble:
            self._update_bubble.close()
        if self._settings_popup:
            self._settings_popup.close()
        self._on_shutdown()
        self.close()
