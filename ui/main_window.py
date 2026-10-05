"""
Main launcher window — PySide6 MobiGlas-style implementation.

Assembles header, tile grid, and settings panel using the shared Qt library.
"""
import json
import logging
import os
import queue as _queue
import threading
import webbrowser
from typing import Callable, Dict, List, Optional

from PySide6.QtCore import Qt, QTimer, Signal, QPropertyAnimation, QEasingCurve, QPoint
from PySide6.QtGui import QFont, QColor, QPainter, QPen, QLinearGradient
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QGridLayout,
    QScrollArea, QFrame, QSizePolicy, QPushButton, QGraphicsOpacityEffect,
)

from core.skill_registry import tile_skills
from shared.config_models import SkillConfig, WindowGeometry
from shared.i18n import _ as _t
from shared.qt.theme import P
from shared.tool_tips import tile_sections, tooltip_html
from shared.qt.base_window import SCWindow
from shared.qt.title_bar import SCTitleBar
from shared.qt.hud_widgets import HUDPanel, GlowEffect
from shared.qt.animated_button import SCButton
from shared.update_checker import UpdateResult, check_for_updates_async, get_current_version
from ui.tiles import SkillTile, build_tile_grid
from ui.settings_panel import SettingsPopup

log = logging.getLogger(__name__)

# Cross-process summary written by the PlayTime Calculator tool.  The launcher
# reads it to show the player's total play time (in their preferred format) on
# the main menu without having to launch or rescan anything itself.
_PLAYTIME_SUMMARY = os.path.join(
    os.path.expanduser("~"), ".sctoolbox", "playtime", "summary.json")
_PLAYTIME_REFRESH_MAX_AGE = 3 * 3600  # re-scan in the background if older than this


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
        self.setFixedSize(340, 160)
        self._parent_window = parent_window
        self._result = result
        self._download_url = result.download_url
        self._cancel = threading.Event()
        self._cb_queue = _queue.Queue()

        # Position near top-right of parent
        pr = parent_window.geometry()
        self.move(pr.x() + pr.width() - 360, pr.y() + 50)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(12, 10, 12, 10)
        main_layout.setSpacing(8)

        # Title
        title = QLabel("UPDATE AVAILABLE", self)
        title.setStyleSheet(f"""
            font-family: Electrolize, Consolas, monospace;
            font-size: 10pt; font-weight: bold;
            color: {P.green}; background: transparent;
            letter-spacing: 2px;
        """)
        main_layout.addWidget(title)

        # Version info
        info = QLabel(f"v{result.current_version}  \u2192  v{result.latest_version}", self)
        info.setStyleSheet(f"""
            font-family: Consolas, monospace; font-size: 9pt;
            color: {P.fg_bright}; background: transparent;
        """)
        main_layout.addWidget(info)

        # Buttons row
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        if self._download_url:
            self._update_btn = QPushButton("UPDATE NOW", self)
            self._update_btn.setCursor(Qt.PointingHandCursor)
            self._update_btn.setStyleSheet(f"""
                QPushButton {{
                    font-family: Consolas; font-size: 8pt; font-weight: bold;
                    color: {P.bg_deepest}; background: {P.green};
                    border: none; border-radius: 3px; padding: 4px 12px;
                }}
                QPushButton:hover {{ background: #55eebb; }}
                QPushButton:disabled {{ background: rgba(0,200,100,0.4); color: rgba(0,0,0,0.5); }}
            """)
            self._update_btn.clicked.connect(self._start_update)
            btn_row.addWidget(self._update_btn)
        else:
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

        # Status label
        self._status_lbl = QLabel("", self)
        self._status_lbl.setStyleSheet(f"""
            font-family: Consolas; font-size: 7pt;
            color: {P.fg_dim}; background: transparent;
        """)
        main_layout.addWidget(self._status_lbl)

        # Poll timer — drains _cb_queue on the GUI thread
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(50)
        self._poll_timer.timeout.connect(self._drain_cb_queue)
        self._poll_timer.start()

        # Auto-dismiss after 15 seconds (only if not in the middle of an update)
        QTimer.singleShot(15000, self._maybe_auto_dismiss)

    def _maybe_auto_dismiss(self):
        # Don't auto-dismiss if an update is in progress
        if hasattr(self, "_update_btn") and not self._update_btn.isEnabled():
            return
        self.close()

    def _drain_cb_queue(self):
        while True:
            try:
                fn = self._cb_queue.get_nowait()
            except _queue.Empty:
                break
            try:
                fn()
            except Exception:
                pass

    def _start_update(self):
        self._update_btn.setEnabled(False)
        self._update_btn.setText("Downloading...")

        def _worker():
            tmp_path = None
            try:
                from shared.auto_updater import download, apply_zip, is_installer

                def _on_progress(done, total):
                    if total > 0:
                        pct = int(done * 100 / total)
                        label = f"{pct}%"
                    else:
                        label = f"{done / (1024 * 1024):.1f} MB"
                    self._cb_queue.put(lambda t=label: self._status_lbl.setText(t))

                if is_installer(self._download_url):
                    # The release asset is the setup .exe: run it, then close the
                    # launcher so the installer can replace the files in use.
                    path = download(self._download_url, _on_progress, self._cancel,
                                    suffix=".exe")
                    self._cb_queue.put(lambda p=path: self._launch_installer(p))
                    return

                tmp_path = download(self._download_url, _on_progress, self._cancel)

                self._cb_queue.put(lambda: self._update_btn.setText("Applying..."))

                count = apply_zip(tmp_path)

                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                tmp_path = None

                self._cb_queue.put(lambda c=count: self._on_update_done(c))

            except InterruptedError:
                self._cb_queue.put(self._on_cancelled)
            except Exception as exc:
                if tmp_path:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
                self._cb_queue.put(lambda e=exc: self._on_error(str(e)))

        t = threading.Thread(target=_worker, daemon=True)
        t.start()

    def _launch_installer(self, path):
        from shared.auto_updater import run_installer
        try:
            run_installer(path)
        except Exception as exc:
            self._on_error(f"Could not start the installer: {exc}")
            return
        self._poll_timer.stop()
        self._update_btn.setText("INSTALLING...")
        self._status_lbl.setText("Installer opened - closing the toolbox so it can update")
        from PySide6.QtWidgets import QApplication
        QTimer.singleShot(1500, lambda: QApplication.instance().quit())

    def _on_update_done(self, count):
        self._poll_timer.stop()
        self._update_btn.setText("RESTART NOW")
        self._update_btn.setEnabled(True)
        try:
            self._update_btn.clicked.disconnect()
        except RuntimeError:
            pass
        self._update_btn.clicked.connect(self._restart)
        self._status_lbl.setStyleSheet(f"""
            font-family: Consolas; font-size: 7pt;
            color: {P.green}; background: transparent;
        """)
        self._status_lbl.setText(f"\u2713 {count} files updated \u2014 restart to apply")

    def _on_error(self, msg):
        self._update_btn.setEnabled(True)
        self._update_btn.setText("UPDATE NOW")
        self._status_lbl.setStyleSheet(f"""
            font-family: Consolas; font-size: 7pt;
            color: {P.red}; background: transparent;
        """)
        self._status_lbl.setText(msg)

    def _on_cancelled(self):
        self._update_btn.setEnabled(True)
        self._update_btn.setText("UPDATE NOW")
        self._status_lbl.setStyleSheet(f"""
            font-family: Consolas; font-size: 7pt;
            color: {P.fg_dim}; background: transparent;
        """)
        self._status_lbl.setText("Cancelled")

    def _restart(self):
        import subprocess
        import sys

        self._cancel.set()
        self._poll_timer.stop()

        pw = self._parent_window
        pos = pw.pos()
        size = pw.size()
        opacity = pw.windowOpacity()

        cmd_file = sys.argv[6] if len(sys.argv) > 6 else "nul"

        subprocess.Popen(
            [
                sys.executable,
                sys.argv[0],
                str(pos.x()),
                str(pos.y()),
                str(size.width()),
                str(size.height()),
                str(opacity),
                cmd_file,
            ],
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
            close_fds=True,
        )
        from PySide6.QtWidgets import QApplication
        QApplication.instance().quit()

    def closeEvent(self, event):
        self._cancel.set()
        self._poll_timer.stop()
        super().closeEvent(event)

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
        keybinds_disabled: Optional[List[str]] = None,
        show_hidden_tiles: Optional[List[str]] = None,
        grid_rows: int = 3,
        grid_cols: int = 2,
        grid_layout: Optional[Dict[str, str]] = None,
        scroll_on_hover: bool = False,
        ui_scale: float = 1.0,
        hide_on_tool_active: bool = False,
        on_restart: Optional[Callable[[], None]] = None,
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
        self._availability = availability
        self._on_toggle_skill = on_toggle_skill
        self._on_shutdown = on_shutdown
        self._on_apply_settings = on_apply_settings
        self._launcher_hotkey = launcher_hotkey
        self._current_language = current_language
        self._available_languages = available_languages or ["en"]
        self._disabled_skills = disabled_skills or []
        self._keybinds_disabled = keybinds_disabled or []
        self._show_hidden_tiles = show_hidden_tiles or []
        self._grid_rows = grid_rows
        self._grid_cols = grid_cols
        self._grid_layout = grid_layout or {}
        self._scroll_on_hover = scroll_on_hover
        self._ui_scale = ui_scale
        self._hide_on_tool_active = hide_on_tool_active
        self._on_restart = on_restart
        self._settings_popup: Optional[SettingsPopup] = None
        self._update_bubble: Optional[UpdateBubble] = None

        # Background threads hand GUI work back through this queue, drained by
        # a window-owned QTimer on the GUI thread.  The same pattern UpdateBubble
        # uses for its download worker.  ``QTimer.singleShot`` CANNOT be used to
        # marshal from a plain ``threading.Thread``: the timer is created in the
        # calling thread, which has no Qt event dispatcher, so it never fires and
        # the callback is simply lost.
        self._cb_queue: _queue.Queue = _queue.Queue()
        self._cb_timer = QTimer(self)
        self._cb_timer.setInterval(50)
        self._cb_timer.timeout.connect(self._drain_cb_queue)
        self._cb_timer.start()

        self.restore_geometry_from_args(geometry.x, geometry.y, geometry.w, geometry.h, geometry.opacity)

        # The launcher sits over the game, which has the keyboard: without this
        # Qt shows no tooltip at all on a window that is not the active one.
        self.setAttribute(Qt.WA_AlwaysShowToolTips, True)

        # ── Title bar ──
        self._title_bar = SCTitleBar(
            window=self,
            title=f"SC Toolbox  v{get_current_version()}",
            icon_text="",
            accent_color=P.accent,
            hotkey_text=get_hotkey_display(launcher_hotkey),
            show_minimize=True,
        )
        self._title_bar.minimize_clicked.connect(self.showMinimized)
        self._title_bar.close_clicked.connect(self._on_close)
        self.content_layout.addWidget(self._title_bar)

        # ── Button bar (GITHUB / UPDATE) ──
        btn_bar = QWidget(self)
        btn_bar.setFixedHeight(26)
        btn_bar.setStyleSheet(f"background-color: {P.bg_deepest};")
        btn_bar_layout = QHBoxLayout(btn_bar)
        btn_bar_layout.setContentsMargins(10, 2, 10, 2)
        btn_bar_layout.setSpacing(6)
        btn_bar_layout.addStretch(1)

        # Persistent "update available" notice beside the UPDATE button. The
        # UpdateBubble auto-dismisses after 15 s; this stays until the app
        # restarts, and clicking it reopens the bubble.
        self._update_notice = QPushButton("", btn_bar)
        self._update_notice.setCursor(Qt.PointingHandCursor)
        self._update_notice.setStyleSheet(f"""
            QPushButton {{
                font-family: Consolas, monospace;
                font-size: 7pt; font-weight: bold;
                color: {P.bg_deepest};
                background: {P.green};
                border: none;
                border-radius: 3px;
                padding: 1px 8px;
            }}
            QPushButton:hover {{ background: #33ff99; }}
        """)
        self._update_notice.setVisible(False)
        self._update_notice.clicked.connect(self._reopen_update_bubble)
        self._last_update_result = None
        btn_bar_layout.addWidget(self._update_notice)

        for label, cb in [
            (_t("GITHUB"), lambda: webbrowser.open("https://github.com/ScPlaceholder/SC-Toolbox-Beta-V2")),
            (_t("UPDATE"), self._check_for_updates),
        ]:
            b = QPushButton(label, btn_bar)
            b.setCursor(Qt.PointingHandCursor)
            b.setStyleSheet(f"""
                QPushButton {{
                    font-family: Consolas, monospace;
                    font-size: 7pt; font-weight: bold;
                    color: {P.accent};
                    background: transparent;
                    border: 1px solid {P.accent};
                    border-radius: 3px;
                    padding: 1px 8px;
                }}
                QPushButton:hover {{ background: rgba(68,170,255,0.15); }}
            """)
            b.clicked.connect(cb)
            btn_bar_layout.addWidget(b)
        self.content_layout.addWidget(btn_bar)

        # ── Header info bar ──
        self._header = header = QWidget(self)
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

        # PlayTime Calculator link + live total badge.  The link opens the tool;
        # the badge shows the player's total play time in their preferred format
        # (read from the tool's summary cache — see _refresh_playtime_badge).
        self._playtime_badge: Optional[QLabel] = None
        if self._availability.get("playtime"):
            playtime = QLabel(_t("PLAY TIME"), header)
            playtime.setStyleSheet("""
                font-family: Consolas; font-size: 8pt; font-weight: bold;
                color: #44ccff; background: transparent;
            """)
            playtime.setCursor(Qt.PointingHandCursor)
            playtime.setToolTip(_t("Open the PlayTime Calculator"))
            playtime.mousePressEvent = lambda e: self._launch_playtime()
            h_layout.addWidget(playtime)

            self._playtime_badge = QLabel("", header)
            self._playtime_badge.setStyleSheet("""
                font-family: Consolas; font-size: 8pt; font-weight: bold;
                color: #e8f2ff; background: rgba(68,204,255,0.14); padding: 1px 6px;
            """)
            self._playtime_badge.setCursor(Qt.PointingHandCursor)
            self._playtime_badge.mousePressEvent = lambda e: self._launch_playtime()
            self._playtime_badge.hide()
            h_layout.addWidget(self._playtime_badge)

            self._playtime_timer = QTimer(self)
            self._playtime_timer.setInterval(4000)
            self._playtime_timer.timeout.connect(self._refresh_playtime_badge)
            self._playtime_timer.start()
            QTimer.singleShot(0, self._refresh_playtime_badge)
            QTimer.singleShot(900, self._maybe_refresh_playtime_summary)

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
        discord.mousePressEvent = lambda e: webbrowser.open("https://discord.gg/D3hqGU5hNt")
        h_layout.addWidget(discord)

        self.content_layout.addWidget(header)

        # ── Separator ──
        self._sep = sep = QFrame(self)
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"background-color: {P.border};")
        self.content_layout.addWidget(sep)

        # ── Tile grid ──
        # Tiles: not the tools declared hidden (SkillConfig.hidden, unless the
        # user asked for the tile back) and not the ones the user disabled.
        enabled_skills = [s for s in tile_skills(skills, self._show_hidden_tiles)
                          if s.id not in self._disabled_skills]
        self._tiles_container = tiles_container = QWidget(self)
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

        # Set initial hotkey badges (blank when the keybind is disabled)
        for skill in enabled_skills:
            tile = self._tiles.get(skill.id)
            if tile:
                if skill.id in self._keybinds_disabled:
                    tile.set_hotkey("")
                else:
                    tile.set_hotkey(get_hotkey_display(skill.hotkey))
        self._refresh_tile_tooltips()

        # ── Settings button ──
        from ui.settings_panel import _btn_qss
        self._settings_btn = SCButton("\u2699 " + _t("Settings"), self, glow_color=P.accent)
        self._settings_btn.setStyleSheet(_btn_qss("#1a2538", "#223050", P.accent))
        self._settings_btn.clicked.connect(self._open_settings)
        self.content_layout.addWidget(self._settings_btn)

    # ── Public API ──

    def update_tile(self, skill_id: str, running: bool, visible: bool) -> None:
        tile = self._tiles.get(skill_id)
        if tile:
            tile.update_status(running, visible)

    # ── PlayTime Calculator integration ──

    def _launch_playtime(self) -> None:
        """Open (or toggle) the PlayTime Calculator tool from the header link."""
        try:
            self._on_toggle_skill("playtime")
        except Exception:
            log.exception("failed to toggle playtime tool")

    def _refresh_playtime_badge(self) -> None:
        """Update the main-menu total from the tool's summary cache."""
        if not self._playtime_badge:
            return
        try:
            with open(_PLAYTIME_SUMMARY, encoding="utf-8") as f:
                summary = json.load(f)
        except (OSError, json.JSONDecodeError):
            return
        badge = str(summary.get("badge", "")).strip()
        if not badge:
            return
        self._playtime_badge.setText(badge)
        sessions = summary.get("session_count")
        headline = summary.get("calendar") or summary.get("headline") or ""
        tip = headline
        if sessions:
            tip = f"{headline}  ·  {sessions:,} sessions"
        self._playtime_badge.setToolTip(tip.strip(" ·"))
        self._playtime_badge.show()

    def _maybe_refresh_playtime_summary(self) -> None:
        """If the cached summary is missing or stale, refresh it in the
        background via the tool's headless mode so the badge stays current
        even when the user never opens the tool window."""
        try:
            import time
            age = None
            try:
                age = time.time() - os.path.getmtime(_PLAYTIME_SUMMARY)
            except OSError:
                pass  # missing → refresh
            if age is not None and age < _PLAYTIME_REFRESH_MAX_AGE:
                return
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            script = os.path.join(root, "tools", "PlayTime_Calculator", "playtime_app.py")
            if not os.path.isfile(script):
                return
            import subprocess
            import sys
            creationflags = 0
            if os.name == "nt":
                creationflags = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                                 | getattr(subprocess, "DETACHED_PROCESS", 0))
            subprocess.Popen(
                [sys.executable, script, "--headless"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, cwd=os.path.dirname(script),
                creationflags=creationflags, close_fds=True)
            # Pick up the freshly-written summary shortly after it finishes.
            QTimer.singleShot(3500, self._refresh_playtime_badge)
        except Exception:
            log.exception("playtime: background summary refresh failed")

    def tile_tooltip(self, skill: SkillConfig, skill_hotkeys: Optional[Dict[str, str]] = None) -> str:
        """What the tile of *skill* says on hover: name, summary, the hotkey in force now, and the
        same for each tool that is a tab of it (shared/tool_tips.py)."""
        return tooltip_html(
            tile_sections(
                skill, self._skills,
                hotkeys=skill_hotkeys,
                keybinds_off=self._keybinds_disabled,
                disabled=self._disabled_skills,
                hotkey_label=_t("Hotkey"),
                tab_label=_t("Tab"),
            ),
            title_color=P.fg_bright,
            hotkey_color=P.accent,
        )

    def _refresh_tile_tooltips(self, skill_hotkeys: Optional[Dict[str, str]] = None) -> None:
        """Run whenever a hotkey badge is set, so a tooltip never names a key the badge does not."""
        for tile in self._tiles.values():
            tile.setToolTip(self.tile_tooltip(tile.skill, skill_hotkeys))

    def update_hotkey_badges(self, launcher_hotkey: str, skill_hotkeys: Dict[str, str]) -> None:
        self._title_bar.set_hotkey(get_hotkey_display(launcher_hotkey))
        for skill in self._skills:
            tile = self._tiles.get(skill.id)
            if tile:
                if skill.id in self._keybinds_disabled:
                    tile.set_hotkey("")
                else:
                    hk = skill_hotkeys.get(skill.id, skill.hotkey)
                    tile.set_hotkey(get_hotkey_display(hk))
        self._refresh_tile_tooltips(skill_hotkeys)

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
        # First launch: ask once where Star Citizen is installed, so every tool
        # that reads the game logs shares one answer.  Deferred so the launcher
        # paints first; a failure here must never keep the launcher from running.
        QTimer.singleShot(400, self._first_launch_sc_path)
        from PySide6.QtWidgets import QApplication
        QApplication.instance().exec()

    def _first_launch_sc_path(self) -> None:
        try:
            from shared.qt.sc_path_dialog import maybe_prompt_for_sc_path
            maybe_prompt_for_sc_path(self)
        except Exception:  # noqa: BLE001 - optional onboarding; tools still auto-detect
            log.exception("first-launch Star Citizen path prompt failed")

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
            keybinds_disabled=self._keybinds_disabled,
            show_hidden_tiles=self._show_hidden_tiles,
            grid_rows=self._grid_rows,
            grid_cols=self._grid_cols,
            grid_layout=self._grid_layout,
            current_language=self._current_language,
            available_languages=self._available_languages,
            on_apply=self._on_apply_settings,
            scroll_on_hover=self._scroll_on_hover,
            ui_scale=self._ui_scale,
            hide_on_tool_active=self._hide_on_tool_active,
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

    def _drain_cb_queue(self) -> None:
        """Run queued callbacks on the GUI thread (see ``_cb_queue``)."""
        while True:
            try:
                fn = self._cb_queue.get_nowait()
            except _queue.Empty:
                break
            try:
                fn()
            except Exception:  # noqa: BLE001 - one bad callback must not stop the drain
                log.exception("main_window: queued GUI callback failed")

    def _on_update_result(self, result: UpdateResult) -> None:
        """Callback from manual update check (runs on the update-checker thread).

        Must hand the result to the GUI thread via ``_cb_queue``.  A
        ``QTimer.singleShot`` here would never fire — see ``_cb_queue``.
        """
        self._cb_queue.put(lambda: self._show_update_result(result, silent=False))

    def _on_startup_update_result(self, result: UpdateResult) -> None:
        """Callback from startup update check — only show if update available.

        Runs on the update-checker thread; marshals via ``_cb_queue``.
        """
        self._cb_queue.put(lambda: self._show_update_result(result, silent=True))

    def _show_update_result(self, result: UpdateResult, silent: bool) -> None:
        # A failed check must never render as a success.  Returning here for
        # BOTH silent and loud checks is what makes that structural rather
        # than incidental: a check that could not reach GitHub knows nothing
        # about the latest version, so neither "NEW" nor a green "Up to date"
        # is honest.
        if result.error:
            if not silent:
                self.set_status(_t("Update check failed"), P.red)
                # The reason is worth keeping: "failed" alone does not tell
                # the user whether to retry or check their connection.
                self._status_label.setToolTip(result.error)

                def _restore() -> None:
                    self._status_label.setToolTip("")
                    self.set_status(_t("Ready"))

                QTimer.singleShot(4000, _restore)
            return

        if result.available:
            self.set_status(f"{_t('Update available')}: v{result.latest_version}", P.green)
            self._last_update_result = result
            self._update_notice.setText(f"\u25cf {_t('NEW')} v{result.latest_version}")
            self._update_notice.setToolTip(_t("An update is available. Click for details."))
            self._update_notice.setVisible(True)
            if self._update_bubble:
                self._update_bubble.close()
            self._update_bubble = UpdateBubble(self, result)
            self._update_bubble.show()
        elif not silent:
            self.set_status(f"{_t('Up to date')} (v{result.current_version})", P.green)
            QTimer.singleShot(4000, lambda: self.set_status(_t("Ready")))

    def _reopen_update_bubble(self) -> None:
        """Clicking the NEW vX.Y.Z notice brings the update bubble back."""
        if not self._last_update_result:
            return
        if self._update_bubble:
            self._update_bubble.close()
        self._update_bubble = UpdateBubble(self, self._last_update_result)
        self._update_bubble.show()

    def _on_close(self) -> None:
        if self._update_bubble:
            self._update_bubble.close()
        if self._settings_popup:
            self._settings_popup.close()
        self._on_shutdown()
        self.close()
