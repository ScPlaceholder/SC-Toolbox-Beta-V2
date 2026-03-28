# SC_Toolbox — unified skill launcher with global hotkeys (PySide6)
"""
SC_Toolbox: a PySide6 MobiGlas-style overlay that shows skill tiles and
provides global hotkeys (via pynput) to toggle each skill's window.

Usage:
    python skill_launcher.py <x> <y> <w> <h> <opacity> <cmd_file>

Architecture:
    - shared/          — python discovery, IPC, logging, config models
    - shared/qt/       — PySide6 theme, base window, HUD widgets
    - core/            — process manager, skill registry
    - ui/              — main window, tiles, settings panel (PySide6)
    - skill_launcher   — this file: wires everything together
"""

import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
from typing import Dict, Optional

# Ensure shared/ and project root are importable
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import shared.path_setup  # noqa: E402  # centralised path config

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer

from shared.config_models import LauncherSettings, SkillConfig, WindowGeometry
from shared.ipc import ipc_read_and_clear
from shared.logging_config import setup_logging
from shared.python_discovery import find_python
from shared.qt.theme import apply_theme
from shared import i18n
from core.process_manager import ProcessManager
from core.skill_registry import discover_skills, resolve_script_path, resolve_skill_path
from ui.main_window import LauncherWindow, get_hotkey_display

logger = logging.getLogger(__name__)

_skill_dir = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(_skill_dir, "skill_launcher_settings.json")


# ── Settings persistence ────────────────────────────────────────────────────

def _load_settings_raw() -> dict:
    try:
        if os.path.isfile(SETTINGS_FILE):
            with open(SETTINGS_FILE, encoding="utf-8") as f:
                return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not load settings: %s", exc)
    return {}


def _save_settings_raw(data: dict) -> None:
    try:
        tmp = SETTINGS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, SETTINGS_FILE)
    except (OSError, TypeError) as exc:
        logger.warning("Could not save settings: %s", exc)


# ══════════════════════════════════════════════════════════════════════════════
# Application controller
# ══════════════════════════════════════════════════════════════════════════════

class SCToolboxApp:
    """Wires together process management, skill registry, and the UI."""

    def __init__(self, geometry: WindowGeometry, cmd_file: str) -> None:
        self.cmd_file = cmd_file
        self._running = threading.Event()
        self._running.set()

        # ── Discover Python ──
        self._python = find_python()
        if self._python:
            parts = self._python.split(os.sep)
            self._python_info = parts[-2] if len(parts) >= 2 else "Python"
        else:
            self._python_info = ""

        # ── Discover skills ──
        self._skills = discover_skills(_skill_dir)

        # ── Load settings ──
        raw_settings = _load_settings_raw()
        self._settings = LauncherSettings.from_dict(raw_settings, self._skills)

        # ── Initialise i18n ──
        i18n.init(self._settings.language, os.path.join(_skill_dir, "locales"))

        # Apply saved hotkeys to skill configs
        for skill in self._skills:
            saved_hk = self._settings.skill_hotkeys.get(skill.id)
            if saved_hk:
                skill.hotkey = saved_hk

        self._launcher_hotkey = self._settings.hotkey_launcher

        # ── Process manager ──
        self._pm = ProcessManager()
        self._pm.cleanup_stale_ipc_files()

        # Kill orphan skill processes from previous launcher sessions
        skill_scripts = [s.script for s in self._skills if s.script]
        self._pm.kill_orphan_skill_processes(skill_scripts)

        # Determine availability and register processes
        self._availability: Dict[str, bool] = {}
        lang_env = {"SC_TOOLBOX_LANG": self._settings.language}
        for skill in self._skills:
            script_path = resolve_script_path(skill, _skill_dir)
            available = script_path is not None
            self._availability[skill.id] = available

            if available and self._python:
                folder = resolve_skill_path(skill, _skill_dir)
                geom = self._settings.skill_windows.get(
                    skill.id, WindowGeometry())
                args = [str(geom.x), str(geom.y), str(geom.w), str(geom.h)]
                args.extend(skill.custom_args)
                args.append(str(geom.opacity))
                self._pm.register(
                    skill_id=skill.id,
                    python_exe=self._python,
                    script=script_path,
                    cwd=folder,
                    args=args,
                    base_dir=_skill_dir,
                    env=lang_env,
                )

        # ── Build UI ──
        self._geometry = geometry
        self._window = LauncherWindow(
            geometry=geometry,
            skills=self._skills,
            availability=self._availability,
            launcher_hotkey=self._launcher_hotkey,
            python_info=self._python_info,
            on_toggle_skill=self._toggle_skill,
            on_apply_settings=self._apply_settings,
            on_shutdown=self._shutdown,
            current_language=self._settings.language,
            available_languages=i18n.available_languages(_skill_dir),
            disabled_skills=self._settings.disabled_skills,
            grid_rows=self._settings.grid_rows,
            grid_cols=self._settings.grid_cols,
            grid_layout=self._settings.grid_layout,
        )

        # ── Thread-safe dispatch queue ──
        # pynput hotkey callbacks and IPC watcher run on background threads.
        # QTimer.singleShot(0, fn) from a non-GUI thread is unreliable in PySide6.
        # Instead, callbacks enqueue work and a main-thread timer drains the queue.
        self._dispatch_queue: queue.Queue = queue.Queue()
        self._queue_timer = QTimer()
        self._queue_timer.setInterval(50)  # 50ms poll — responsive enough for hotkeys
        self._queue_timer.timeout.connect(self._drain_queue)
        self._queue_timer.start()

        # ── Hotkeys ──
        self._hotkey_listener = None
        self._start_hotkeys()

        # ── Auto-check for updates (2s after launch) ──
        QTimer.singleShot(2000, self._window.check_for_updates_at_startup)

        # ── IPC command watcher ──
        self._start_cmd_watcher()

    # ── Thread-safe dispatch queue helpers ─────────────────────────────

    def _enqueue(self, fn) -> None:
        """Put a callable on the dispatch queue (safe from any thread)."""
        self._dispatch_queue.put(fn)

    def _drain_queue(self) -> None:
        """Called on the main thread by QTimer; runs all queued callables."""
        while True:
            try:
                fn = self._dispatch_queue.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception as exc:  # broad catch intentional: arbitrary queued callables
                logger.error("Dispatch queue error: %s", exc)

    # ── Skill toggle ─────────────────────────────────────────────────────

    def _toggle_skill(self, skill_id: str) -> None:
        mp = self._pm.get(skill_id)
        if not mp:
            return
        mp.toggle()
        self._window.update_tile(skill_id, mp.running, mp.visible)

    # ── Hotkey management ────────────────────────────────────────────────

    def _start_hotkeys(self) -> None:
        try:
            from pynput.keyboard import GlobalHotKeys
        except ImportError:
            self._window.set_status(i18n._("pynput not installed — hotkeys disabled"))
            return

        bindings = self._build_hotkey_bindings()
        if not bindings:
            return
        try:
            self._hotkey_listener = GlobalHotKeys(bindings)
            self._hotkey_listener.daemon = True
            self._hotkey_listener.start()
        except (ValueError, RuntimeError, OSError) as exc:
            self._window.set_status(f"{i18n._('Hotkey error')}: {exc}")
            logger.error("Hotkey listener failed: %s", exc)

    def _stop_hotkeys(self) -> None:
        if self._hotkey_listener:
            try:
                self._hotkey_listener.stop()
            except (RuntimeError, OSError):
                pass
            self._hotkey_listener = None

    def _build_hotkey_bindings(self) -> dict:
        bindings = {}
        disabled = set(self._settings.disabled_skills)
        if self._launcher_hotkey:
            bindings[self._launcher_hotkey] = lambda: self._enqueue(
                self._window.toggle_visibility)
        for skill in self._skills:
            if skill.id in disabled:
                continue
            hk = skill.hotkey
            sid = skill.id
            if hk:
                bindings[hk] = lambda s=sid: self._enqueue(
                    lambda s=s: self._toggle_skill(s))
        return bindings

    def _apply_settings(self, settings_dict: dict) -> None:
        """Called by the settings popup when Apply & Restart is clicked.

        Saves all settings to disk and relaunches the launcher process.
        """
        # Update hotkeys
        new_launcher = settings_dict.get("hotkey_launcher", self._launcher_hotkey)
        new_skill_hotkeys = settings_dict.get("skill_hotkeys", {})

        self._settings.hotkey_launcher = new_launcher
        for skill in self._skills:
            if skill.id in new_skill_hotkeys:
                skill.hotkey = new_skill_hotkeys[skill.id]
                self._settings.skill_hotkeys[skill.id] = skill.hotkey
                if skill.settings_key:
                    self._settings.raw[skill.settings_key] = skill.hotkey
        self._settings.raw["hotkey_launcher"] = new_launcher

        # Update grid settings
        self._settings.grid_rows = settings_dict.get("grid_rows", self._settings.grid_rows)
        self._settings.grid_cols = settings_dict.get("grid_cols", self._settings.grid_cols)
        self._settings.grid_layout = settings_dict.get("grid_layout", self._settings.grid_layout)

        # Update disabled skills
        self._settings.disabled_skills = settings_dict.get("disabled_skills", [])

        # Update language
        new_lang = settings_dict.get("language", self._settings.language)
        self._settings.language = new_lang
        self._settings.raw["language"] = new_lang

        # Save to disk
        _save_settings_raw(self._settings.to_dict())

        # Relaunch
        self._relaunch()

    def _relaunch(self) -> None:
        """Stop everything and spawn a fresh launcher process, then quit."""
        logger.info("Relaunching launcher...")

        # Capture current window geometry for the new process
        pos = self._window.pos()
        size = self._window.size()
        opacity = self._window.windowOpacity()

        # Stop child processes and hotkeys
        self._stop_hotkeys()
        self._pm.stop_all()

        # Build the command to relaunch
        args = [
            sys.executable,
            os.path.abspath(__file__),
            str(pos.x()), str(pos.y()),
            str(size.width()), str(size.height()),
            str(opacity),
            self.cmd_file,
        ]

        # Spawn the new process detached
        try:
            subprocess.Popen(
                args,
                cwd=_skill_dir,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
                close_fds=True,
            )
        except OSError as exc:
            logger.error("Failed to relaunch: %s", exc)
            return

        # Quit the current process
        self._running.clear()
        self._queue_timer.stop()
        self._window.close()
        app = QApplication.instance()
        if app:
            app.quit()

    # ── IPC command watcher ──────────────────────────────────────────────

    def _start_cmd_watcher(self) -> None:
        if not self.cmd_file or self.cmd_file == os.devnull:
            return
        t = threading.Thread(target=self._watch_cmds, daemon=True)
        t.start()

    def _watch_cmds(self) -> None:
        while self._running.is_set():
            try:
                if not os.path.isfile(self.cmd_file):
                    time.sleep(0.5)
                    continue
                try:
                    commands = ipc_read_and_clear(self.cmd_file)
                except (OSError, IOError):
                    time.sleep(0.5)
                    continue
                if not commands:
                    time.sleep(0.3)
                    continue
                for cmd in commands:
                    self._enqueue(lambda c=cmd: self._dispatch(c))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                logger.error("Command watcher error: %s", exc)
            time.sleep(0.3)

    def _dispatch(self, cmd: dict) -> None:
        t = cmd.get("type", "")
        if t == "show":
            self._window.show()
            self._window.raise_()
        elif t == "hide":
            self._window.hide()
        elif t == "quit":
            self._shutdown()
        elif t == "toggle_skill":
            sid = cmd.get("skill_id", "")
            if sid:
                self._toggle_skill(sid)
        elif t == "launch_skill":
            sid = cmd.get("skill_id", "")
            mp = self._pm.get(sid)
            if mp:
                mp.show()
                self._window.update_tile(sid, mp.running, mp.visible)
        elif t == "stop_skill":
            sid = cmd.get("skill_id", "")
            mp = self._pm.get(sid)
            if mp:
                mp.stop()
                self._window.update_tile(sid, mp.running, mp.visible)
        else:
            logger.warning("Unknown IPC command type: %r", t)

    # ── Shutdown ─────────────────────────────────────────────────────────

    def _shutdown(self) -> None:
        self._running.clear()
        self._queue_timer.stop()
        self._stop_hotkeys()
        self._pm.stop_all()
        self._window.close()
        app = QApplication.instance()
        if app:
            app.quit()

    def run(self) -> None:
        self._window.run()


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    from shared.platform_utils import set_dpi_awareness
    set_dpi_awareness()
    setup_logging(name="skill_launcher")

    args = sys.argv[1:]

    def _int(idx: int, default: int) -> int:
        try:
            return int(args[idx]) if len(args) > idx else default
        except (ValueError, IndexError):
            return default

    def _float(idx: int, default: float) -> float:
        try:
            return float(args[idx]) if len(args) > idx else default
        except (ValueError, IndexError):
            return default

    geom = WindowGeometry(
        x=_int(0, 100),
        y=_int(1, 100),
        w=_int(2, 500),
        h=_int(3, 400),
        opacity=_float(4, 0.95),
    )
    cmd_file = args[5] if len(args) > 5 else os.devnull

    # Create QApplication first (required before any Qt widgets)
    qt_app = QApplication(sys.argv)

    # Apply MobiGlas theme
    apply_theme(qt_app)

    # Clamp to screen bounds
    screen = qt_app.primaryScreen()
    if screen:
        sg = screen.availableGeometry()
        geom = geom.clamp_to_screen(sg.width(), sg.height())

    app = SCToolboxApp(geom, cmd_file)
    app.run()


if __name__ == "__main__":
    main()
