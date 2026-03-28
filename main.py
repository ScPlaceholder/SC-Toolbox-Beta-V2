"""
SC_Toolbox — WingmanAI skill module.
Unified launcher for all Star Citizen custom skill GUIs.
Launches skill_launcher.py as a subprocess using the system Python (which has tkinter).
"""
import asyncio
import logging
import os
import subprocess
import sys
import threading
from typing import TYPE_CHECKING, Optional

# Ensure shared/ package is importable
sys.path.insert(1, os.path.dirname(os.path.abspath(__file__)))
import shared.path_setup  # noqa: E402  # centralised path config
from shared.ipc import ipc_write
from shared.python_discovery import find_python
from shared.config_models import WindowGeometry

from api.enums import LogType
from api.interface import SettingsConfig, SkillConfig, WingmanInitializationError
from services.printr import Printr
from skills.skill_base import Skill, tool

if TYPE_CHECKING:
    from wingmen.open_ai_wingman import OpenAiWingman

logger = logging.getLogger(__name__)

_skill_dir = os.path.dirname(os.path.abspath(__file__))
_app_script = os.path.join(_skill_dir, "skill_launcher.py")

printr = Printr()

_INSTANCES: dict = {}
_INSTANCES_LOCK = threading.RLock()


class SCToolbox(Skill):
    """WingmanAI skill: unified launcher for all Star Citizen tools."""

    _NAME_MAP = {
        "dps": "dps", "dps calculator": "dps", "calculator": "dps",
        "cargo": "cargo", "cargo loader": "cargo",
        "missions": "missions", "mission database": "missions", "mission": "missions",
        "mining": "mining", "mining loadout": "mining",
        "market": "market", "market finder": "market",
        "trade": "trade", "trade hub": "trade",
    }

    def __init__(
        self,
        config: SkillConfig,
        settings: SettingsConfig,
        wingman: "OpenAiWingman",
    ) -> None:
        super().__init__(config=config, settings=settings, wingman=wingman)
        self._proc: Optional[subprocess.Popen] = None
        self._python_exe: Optional[str] = None
        self._cmd_file: Optional[str] = None
        self._launch_args: Optional[list] = None
        self._log_handle = None

    @property
    def singleton_key(self) -> str:
        return f"sc_toolbox_proc_{self.wingman.name}"

    def _get_prop(self, key: str, default):
        val = self.retrieve_custom_property_value(key, [])
        return val if val is not None else default

    async def validate(self) -> list[WingmanInitializationError]:
        errors = await super().validate()
        if not os.path.isfile(_app_script):
            from api.enums import WingmanInitializationErrorType
            errors.append(
                WingmanInitializationError(
                    wingman_name=self.wingman.name,
                    error_type=WingmanInitializationErrorType.INVALID_CONFIG,
                    message=f"[SC_Toolbox] skill_launcher.py not found at: {_app_script}",
                ))
        return errors

    async def prepare(self) -> None:
        await super().prepare()

        # ── Stale temp file cleanup ──
        from core.process_manager import ProcessManager
        pm = ProcessManager()
        pm.cleanup_stale_ipc_files()

        # ── Tear down any existing instance for this wingman ──
        with _INSTANCES_LOCK:
            existing = _INSTANCES.get(self.singleton_key)
            if existing and isinstance(existing, dict):
                old_proc = existing.get("proc")
                old_cmd_file = existing.get("cmd_file")
                if old_proc and old_proc.poll() is None:
                    try:
                        if old_cmd_file:
                            ipc_write(old_cmd_file, {"type": "quit"})
                        old_proc.wait(timeout=2.0)
                    except subprocess.TimeoutExpired:
                        old_proc.terminate()
                        try:
                            old_proc.wait(timeout=1)
                        except subprocess.TimeoutExpired:
                            old_proc.kill()
                    except OSError as exc:
                        logger.warning("Failed to stop old instance: %s", exc)
                        old_proc.terminate()
                if old_cmd_file and os.path.exists(old_cmd_file):
                    try:
                        os.remove(old_cmd_file)
                    except OSError:
                        pass
                _INSTANCES.pop(self.singleton_key, None)

        self._python_exe = find_python()
        if not self._python_exe:
            await printr.print_async(
                "[SC_Toolbox] No system Python with tkinter found.",
                color=LogType.ERROR, server_only=True)
            return

        await printr.print_async(
            f"[SC_Toolbox] Using Python: {self._python_exe}",
            color=LogType.INFO, server_only=True)

        geom = WindowGeometry(
            x=self._safe_int_prop("window_x", 100),
            y=self._safe_int_prop("window_y", 100),
            w=self._safe_int_prop("window_width", 500),
            h=self._safe_int_prop("window_height", 400),
            opacity=self._safe_float_prop("opacity", 0.95),
        )
        self._launch_args = geom.as_args()

        if self._get_prop("launch_at_startup", True):
            with _INSTANCES_LOCK:
                self._launch_proc()
            await asyncio.sleep(1.0)
            if self._proc:
                await printr.print_async(
                    f"[SC_Toolbox] Launcher started (PID {self._proc.pid})",
                    color=LogType.INFO, server_only=True)

    def _safe_int_prop(self, key: str, default: int) -> int:
        try:
            return int(self._get_prop(key, default))
        except (ValueError, TypeError):
            return default

    def _safe_float_prop(self, key: str, default: float) -> float:
        try:
            return float(self._get_prop(key, default))
        except (ValueError, TypeError):
            return default

    async def unload(self) -> None:
        self._send({"type": "quit"})
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self._proc.terminate()
        self._proc = None
        self._close_log_handle()
        if self._cmd_file and os.path.exists(self._cmd_file):
            for path in (self._cmd_file, self._cmd_file + ".lock"):
                try:
                    os.remove(path)
                except OSError:
                    pass
        self._cmd_file = None
        with _INSTANCES_LOCK:
            _INSTANCES.pop(self.singleton_key, None)
        await super().unload()

    def _launch_proc(self) -> None:
        if not self._python_exe or not self._launch_args:
            return
        import tempfile
        # Clean up old cmd file if re-launching
        if self._cmd_file:
            for old in (self._cmd_file, self._cmd_file + ".lock"):
                try:
                    os.remove(old)
                except OSError:
                    pass
        self._close_log_handle()

        safe_key = self.singleton_key.replace(os.sep, "_").replace(" ", "_")
        self._cmd_file = os.path.join(
            tempfile.gettempdir(), f"sc_toolbox_{safe_key}_{os.getpid()}.jsonl")
        with open(self._cmd_file, "w"):
            pass

        # Capture subprocess output to log file
        from shared.logging_config import get_subprocess_log_path
        log_path = get_subprocess_log_path("skill_launcher", _skill_dir)
        try:
            self._log_handle = open(log_path, "a", encoding="utf-8")
        except OSError:
            self._log_handle = None

        try:
            self._proc = subprocess.Popen(
                [self._python_exe, _app_script]
                + self._launch_args + [self._cmd_file],
                stdin=subprocess.DEVNULL,
                stdout=self._log_handle or subprocess.DEVNULL,
                stderr=self._log_handle or subprocess.DEVNULL,
                cwd=_skill_dir,
                # CREATE_NO_WINDOW causes PySide6 to segfault on Python 3.14+
                # creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            for f in (self._cmd_file, self._cmd_file + ".lock"):
                try:
                    os.remove(f)
                except OSError:
                    pass
            self._cmd_file = None
            self._close_log_handle()
            raise

        _INSTANCES[self.singleton_key] = {
            "proc": self._proc, "cmd_file": self._cmd_file}

    def _close_log_handle(self) -> None:
        if self._log_handle:
            try:
                self._log_handle.close()
            except OSError:
                pass
            self._log_handle = None

    async def _ensure_started(self) -> bool:
        with _INSTANCES_LOCK:
            if self._proc and self._proc.poll() is None:
                return True
            if not self._python_exe or not self._launch_args:
                return False
            try:
                self._launch_proc()
            except (OSError, subprocess.SubprocessError) as exc:
                logger.error("_launch_proc failed: %s", exc)
                return False
        await asyncio.sleep(0.1)
        return self._proc is not None and self._proc.poll() is None

    def _send(self, cmd: dict) -> None:
        with _INSTANCES_LOCK:
            cmd_file = self._cmd_file
            proc = self._proc
        if cmd_file and proc and proc.poll() is None:
            if not ipc_write(cmd_file, cmd):
                logger.warning("IPC send failed for command %s", cmd.get("type"))

    # ── Voice tools ──────────────────────────────────────────────────────

    @tool()
    async def show_toolbox(self) -> str:
        """
        Shows the SC_Toolbox launcher window.
        Call this when the user asks to open the toolbox, skill launcher,
        or wants to see all available tools.
        """
        if not await self._ensure_started():
            return "SC_Toolbox failed to start. Check that Python with tkinter is installed."
        self._send({"type": "show"})
        return "SC_Toolbox launcher is now visible."

    @tool()
    def hide_toolbox(self) -> str:
        """
        Hides the SC_Toolbox launcher window.
        Call this when the user wants to close or hide the toolbox.
        """
        self._send({"type": "hide"})
        return "SC_Toolbox hidden."

    @tool()
    async def launch_skill(self, skill_name: str) -> str:
        """
        Launches a specific skill tool from the SC_Toolbox.
        Call this when the user asks to open a specific tool by name.

        :param skill_name: The skill to launch.
            Options: "dps" (DPS Calculator), "cargo" (Cargo Loader),
            "missions" (Mission Database), "mining" (Mining Loadout),
            "market" (Market Finder), "trade" (Trade Hub).
        """
        if not await self._ensure_started():
            return "SC_Toolbox failed to start. Check that Python with tkinter is installed."
        sid = self._NAME_MAP.get(skill_name.lower(), skill_name.lower())
        self._send({"type": "launch_skill", "skill_id": sid})
        return f"Launching {sid} tool."

    @tool()
    async def toggle_skill(self, skill_name: str) -> str:
        """
        Toggles a specific skill tool's visibility.
        Call this when the user wants to show or hide a specific tool.

        :param skill_name: The skill to toggle.
            Options: "dps" (DPS Calculator), "cargo" (Cargo Loader),
            "missions" (Mission Database), "mining" (Mining Loadout),
            "market" (Market Finder), "trade" (Trade Hub).
        """
        if not await self._ensure_started():
            return "SC_Toolbox failed to start. Check that Python with tkinter is installed."
        sid = self._NAME_MAP.get(skill_name.lower(), skill_name.lower())
        self._send({"type": "toggle_skill", "skill_id": sid})
        return f"Toggled {sid} tool."
