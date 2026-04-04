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
from PySide6.QtCore import QTimer, QPoint
from PySide6.QtGui import QIcon

from shared.config_models import LauncherSettings, SkillConfig, WindowGeometry
from shared.ipc import ipc_read_and_clear
from shared.logging_config import setup_logging
from shared.python_discovery import find_python
from shared.qt.theme import apply_theme
from shared.qt.base_window import load_window_state
from shared import i18n
from core.process_manager import ProcessManager
from core.skill_registry import discover_skills, resolve_script_path, resolve_skill_path
from ui.main_window import LauncherWindow, get_hotkey_display

logger = logging.getLogger(__name__)

_skill_dir = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(_skill_dir, "skill_launcher_settings.json")


def _find_skill_log(skill_id: str) -> str | None:
    """Return the most informative log path for *skill_id*, or None."""
    log_dir = os.path.join(_skill_dir, "logs")
    # crash.log has tracebacks; prefer it when non-empty
    for stem in (f"{skill_id}.crash", skill_id):
        path = os.path.join(log_dir, f"{stem}.log")
        try:
            if os.path.isfile(path) and os.path.getsize(path) > 0:
                return path
        except OSError:
            pass
    return None


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

        # ── Restore saved launcher opacity ──
        # The CLI arg carries whatever WingmanAI last passed; override it with
        # the opacity the user set via the title-bar slider (persisted on exit).
        geometry.opacity = self._settings.launcher_opacity

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
        lang_env = {
            "SC_TOOLBOX_LANG": self._settings.language,
        }
        if self._settings.ui_scale != 1.0:
            lang_env["QT_SCALE_FACTOR"] = str(self._settings.ui_scale)
        else:
            lang_env["QT_SCALE_FACTOR"] = ""
        if self._settings.hide_on_tool_active:
            lang_env["SC_TOOLBOX_EXIT_ON_CLOSE"] = "1"
        for skill in self._skills:
            script_path = resolve_script_path(skill, _skill_dir)
            available = script_path is not None
            self._availability[skill.id] = available

            if available and self._python:
                folder = resolve_skill_path(skill, _skill_dir)
                geom = self._settings.skill_windows.get(
                    skill.id, WindowGeometry())

                # Override with the geometry the skill last saved on close
                # (position, size, and opacity all persisted automatically)
                script_stem = os.path.splitext(os.path.basename(script_path))[0]
                saved = load_window_state(script_stem)
                if saved:
                    geom = WindowGeometry(
                        x=int(saved.get("x", geom.x)),
                        y=int(saved.get("y", geom.y)),
                        w=int(saved.get("w", geom.w)),
                        h=int(saved.get("h", geom.h)),
                        opacity=float(saved.get("opacity", geom.opacity)),
                    )

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
                # Register exit-watcher so the launcher re-shows instantly
                # when a skill closes (instead of waiting for polling).
                mp = self._pm.get(skill.id)
                if mp:
                    mp.set_on_exit(lambda: self._enqueue(self._auto_hide_check))

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
            scroll_on_hover=self._settings.scroll_on_hover,
            ui_scale=self._settings.ui_scale,
            hide_on_tool_active=self._settings.hide_on_tool_active,
            on_restart=self._relaunch,
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

        # ── Skill crash monitor ──
        # Tracks the last PID for which we already showed a crash dialog so
        # we don't spam the user if the poll fires multiple times before the
        # process is restarted.
        self._last_crash_pid: Dict[str, int] = {}
        self._health_timer = QTimer()
        self._health_timer.setInterval(5000)  # check every 5 s
        self._health_timer.timeout.connect(self._check_skill_health)
        self._health_timer.start()

        # ── Auto-hide state ──
        self._autohide_timer: Optional[QTimer] = None
        self._autohide_stashed = False
        self._autohide_pos = self._window.pos()
        self._sync_autohide_timer()

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

    # ── Skill crash monitor ──────────────────────────────────────────────

    def _check_skill_health(self) -> None:
        """Detect skills that died without being asked to stop and show their log."""
        for skill in self._skills:
            mp = self._pm.get(skill.id)
            if not mp or not mp.unexpectedly_died:
                continue
            pid = mp.pid
            if pid is None or self._last_crash_pid.get(skill.id) == pid:
                continue  # already shown dialog for this particular process death
            self._last_crash_pid[skill.id] = pid
            self._window.update_tile(skill.id, False, False)
            # When hide_on_tool_active is enabled, skills exit intentionally
            # via user_close() — that's not a crash, so skip the dialog.
            if self._settings.hide_on_tool_active:
                continue
            logger.warning("skill crash detected: %s (PID %d)", skill.id, pid)
            log_path = _find_skill_log(skill.id)
            if log_path:
                from shared.qt.crash_dialog import show_crash_dialog
                show_crash_dialog(log_path, skill_name=skill.name, parent=self._window)
        self._auto_hide_check()

    # ── Skill toggle ─────────────────────────────────────────────────────

    def _toggle_skill(self, skill_id: str) -> None:
        mp = self._pm.get(skill_id)
        if not mp:
            return
        mp.toggle()
        self._window.update_tile(skill_id, mp.running, mp.visible)
        self._auto_hide_check()

    def _sync_autohide_timer(self) -> None:
        """Start or stop the auto-hide poll based on the setting.

        Fires every 500ms so the launcher reappears almost instantly
        when the user closes a skill window from within the tool itself.
        """
        if self._settings.hide_on_tool_active:
            if self._autohide_timer is None:
                self._autohide_timer = QTimer()
                self._autohide_timer.setInterval(500)
                self._autohide_timer.timeout.connect(self._auto_hide_check)
            if not self._autohide_timer.isActive():
                self._autohide_timer.start()
        else:
            if self._autohide_timer is not None and self._autohide_timer.isActive():
                self._autohide_timer.stop()

    def _auto_hide_check(self) -> None:
        """Hide the launcher when any tool is visible, re-show when none are."""
        if not self._settings.hide_on_tool_active:
            return
        any_visible = False
        for skill in self._skills:
            mp = self._pm.get(skill.id)
            if not mp:
                continue
            if mp.visible and mp.running:
                any_visible = True
            elif mp.visible and not mp.running:
                self._window.update_tile(skill.id, False, False)
        if any_visible and not self._autohide_stashed:
            self._autohide_stashed = True
            self._autohide_pos = self._window.pos()
            self._window.move(-32000, -32000)
        elif not any_visible and self._autohide_stashed:
            self._autohide_stashed = False
            self._window.move(self._autohide_pos)
            self._window.raise_()
            self._window.activateWindow()

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

    def _save_launcher_opacity(self) -> None:
        """Persist the current window opacity to the settings file."""
        self._settings.launcher_opacity = self._window.windowOpacity()
        _save_settings_raw(self._settings.to_dict())

    def _apply_settings(self, settings_dict: dict) -> None:
        """Called by the settings popup when Apply is clicked.

        Saves all settings to disk and rebuilds the UI in-place.
        Only does a full process relaunch when QT_SCALE_FACTOR changes
        (since that must be set before QApplication creation).
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

        # Update scroll on hover
        self._settings.scroll_on_hover = settings_dict.get("scroll_on_hover", self._settings.scroll_on_hover)

        # Update language
        new_lang = settings_dict.get("language", self._settings.language)
        self._settings.language = new_lang
        self._settings.raw["language"] = new_lang

        # Update auto-hide
        self._settings.hide_on_tool_active = settings_dict.get("hide_on_tool_active", self._settings.hide_on_tool_active)
        self._autohide_stashed = False
        self._sync_autohide_timer()

        # Update UI scale
        old_scale = self._settings.ui_scale
        self._settings.ui_scale = settings_dict.get("ui_scale", self._settings.ui_scale)

        # Capture current opacity so it survives
        self._settings.launcher_opacity = self._window.windowOpacity()

        # Save to disk
        _save_settings_raw(self._settings.to_dict())

        # UI scale requires a full process restart (QT_SCALE_FACTOR is read
        # once at QApplication creation).  Everything else can be rebuilt
        # in-place without the flicker of spawning a new process.
        if self._settings.ui_scale != old_scale:
            self._relaunch()
        else:
            self._rebuild_ui()

    def _rebuild_ui(self) -> None:
        """Tear down and recreate the window + hotkeys without spawning a new
        process.  This is nearly instant compared to a full relaunch."""
        # If auto-hidden, use the stashed position instead of the off-screen one
        if self._autohide_stashed:
            pos = self._autohide_pos
            self._autohide_stashed = False
        else:
            pos = self._window.pos()
        size = self._window.size()
        opacity = self._window.windowOpacity()

        # Stop hotkeys (will be re-bound below)
        self._stop_hotkeys()

        # Reload i18n in case language changed
        i18n.init(self._settings.language, os.path.join(_skill_dir, "locales"))

        # Update the launcher hotkey reference
        self._launcher_hotkey = self._settings.hotkey_launcher

        # Close the old window (doesn't quit the app)
        self._window.close()

        # Build a fresh geometry from the old position
        geom = WindowGeometry(
            x=pos.x(), y=pos.y(),
            w=size.width(), h=size.height(),
            opacity=opacity,
        )

        # Create the new window
        self._window = LauncherWindow(
            geometry=geom,
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
            scroll_on_hover=self._settings.scroll_on_hover,
            ui_scale=self._settings.ui_scale,
            hide_on_tool_active=self._settings.hide_on_tool_active,
            on_restart=self._relaunch,
        )

        # Restore tile states for any skills that are already running
        for skill in self._skills:
            mp = self._pm.get(skill.id)
            if mp:
                self._window.update_tile(skill.id, mp.running, mp.visible)

        self._window.show()

        # Restart hotkeys with updated bindings
        self._start_hotkeys()

    def _relaunch(self) -> None:
        """Stop everything and spawn a fresh launcher process, then quit."""
        logger.info("Relaunching launcher...")

        # Capture current window geometry for the new process
        pos = self._window.pos()
        size = self._window.size()
        opacity = self._window.windowOpacity()

        # Stop child processes, hotkeys, and timers
        self._health_timer.stop()
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

        # Quit the current process.
        # Delay close/quit by ~400ms to give the new process time to show its
        # window before ours disappears, reducing the visible blank gap.
        self._running.clear()
        self._queue_timer.stop()
        self._window.setEnabled(False)
        QTimer.singleShot(400, self._window.close)
        app = QApplication.instance()
        if app:
            QTimer.singleShot(500, app.quit)

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
                self._auto_hide_check()
        elif t == "stop_skill":
            sid = cmd.get("skill_id", "")
            mp = self._pm.get(sid)
            if mp:
                mp.stop()
                self._window.update_tile(sid, mp.running, mp.visible)
                self._auto_hide_check()
        else:
            logger.warning("Unknown IPC command type: %r", t)

    # ── Shutdown ─────────────────────────────────────────────────────────

    def _shutdown(self) -> None:
        self._save_launcher_opacity()
        self._running.clear()
        self._queue_timer.stop()
        self._health_timer.stop()
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

    # ── Launcher self-crash dialog ──
    # If an unhandled exception escapes to the top level, show the log before
    # Python exits so the user can copy and report it.
    _launcher_log = os.path.join(_skill_dir, "logs", "skill_launcher.log")
    _orig_excepthook = sys.excepthook

    def _launcher_excepthook(exc_type, exc_value, exc_tb):
        _orig_excepthook(exc_type, exc_value, exc_tb)
        if issubclass(exc_type, (SystemExit, KeyboardInterrupt)):
            return
        try:
            from PySide6.QtWidgets import QApplication
            if QApplication.instance():
                from shared.qt.crash_dialog import show_crash_dialog
                show_crash_dialog(
                    _launcher_log,
                    skill_name="SC Toolbox Launcher",
                    blocking=True,
                )
        except Exception:
            pass  # never let the crash dialog crash the crash handler

    sys.excepthook = _launcher_excepthook

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

    # Apply UI scale factor before QApplication is created
    raw = _load_settings_raw()
    ui_scale = raw.get("ui_scale", 1.0)
    try:
        ui_scale = max(0.75, min(3.0, float(ui_scale)))
    except (TypeError, ValueError):
        ui_scale = 1.0
    if ui_scale != 1.0:
        os.environ["QT_SCALE_FACTOR"] = str(ui_scale)
    else:
        os.environ.pop("QT_SCALE_FACTOR", None)

    # Create QApplication first (required before any Qt widgets)
    qt_app = QApplication(sys.argv)

    # Set app icon (taskbar, window title bar, Alt+Tab)
    _ico = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sc_toolbox.ico")
    if os.path.isfile(_ico):
        qt_app.setWindowIcon(QIcon(_ico))

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
