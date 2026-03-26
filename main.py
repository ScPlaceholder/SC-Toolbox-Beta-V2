"""
SC_Toolbox — WingmanAI skill module.
Unified launcher for all Star Citizen custom skill GUIs.
Launches skill_launcher.py as a subprocess using the system Python (which has tkinter).
"""
import asyncio
import glob
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from typing import TYPE_CHECKING, Optional

# Ensure shared/ package is importable
sys.path.insert(1, os.path.dirname(os.path.abspath(__file__)))
from shared.ipc import ipc_write

from api.enums import LogType
from api.interface import SettingsConfig, SkillConfig, WingmanInitializationError
from services.printr import Printr
from skills.skill_base import Skill, tool

if TYPE_CHECKING:
    from wingmen.open_ai_wingman import OpenAiWingman

_skill_dir  = os.path.dirname(os.path.abspath(__file__))
_app_script = os.path.join(_skill_dir, "skill_launcher.py")


def _pid_alive(pid: int) -> bool:
    """Check if a process is alive without killing it (Windows-safe).

    os.kill(pid, 0) on Windows (Python < 3.12) actually terminates the process.
    Use the Windows API instead.
    """
    try:
        import ctypes
        # PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if h:
            ctypes.windll.kernel32.CloseHandle(h)
            return True
        return False
    except Exception:
        return False

printr = Printr()

_INSTANCES: dict = {}
_INSTANCES_LOCK = threading.RLock()


def _find_python() -> Optional[str]:
    """Find a system Python executable that has tkinter (excludes Windows Store stubs)."""
    import shutil
    candidates = []
    base = os.environ.get("LOCALAPPDATA", "")
    if base:
        for ver in ("314", "313", "312", "311", "310", "39", "38"):
            candidates.append(
                os.path.join(base, "Programs", "Python", f"Python{ver}", "python.exe"))
        candidates.append(os.path.join(base, "Python", "bin", "python.exe"))
        candidates.append(os.path.join(base, "Python", "python.exe"))
    pf   = os.environ.get("ProgramFiles",       "C:\\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)",  "C:\\Program Files (x86)")
    for base_dir in (pf, pf86):
        for ver in ("3.14", "3.13", "3.12", "3.11", "3.10", "3.9", "3.8"):
            candidates.append(
                os.path.join(base_dir, "Python",
                             f"Python{ver.replace('.', '')}", "python.exe"))
    for name in ("python3", "python"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    for exe in candidates:
        if "WindowsApps" in exe:
            continue
        if not os.path.isfile(exe):
            continue
        try:
            result = subprocess.run(
                [exe, "-c", "import tkinter; print('ok')"],
                capture_output=True, timeout=8,
                creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode == 0 and b"ok" in result.stdout:
                return exe
        except Exception:
            continue
    return None


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
        self._proc:        Optional[subprocess.Popen] = None
        self._python_exe:  Optional[str]              = None
        self._cmd_file:    Optional[str]              = None
        self._launch_args: Optional[list]             = None

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
        try:
            for f in glob.glob(os.path.join(tempfile.gettempdir(), "sc_toolbox_*")):
                try:
                    basename = os.path.basename(f)
                    pid_str = basename.replace("sc_toolbox_", "").replace(".jsonl", "").replace(".lock", "")
                    # Last segment is the PID; earlier segments may be skill IDs
                    parts = pid_str.rsplit("_", 1)
                    pid = int(parts[-1])
                    if _pid_alive(pid):
                        continue  # Process alive — skip
                    raise ProcessLookupError  # Dead — fall through to cleanup
                except (ProcessLookupError, OSError):
                    # Process dead or inaccessible — safe to delete
                    try:
                        os.remove(f)
                    except OSError:
                        pass
                except ValueError:
                    pass  # Can't parse PID — skip, don't delete
        except Exception:
            pass

        with _INSTANCES_LOCK:
            existing = _INSTANCES.get(self.singleton_key)
            if existing and isinstance(existing, dict):
                old_proc     = existing.get("proc")
                old_cmd_file = existing.get("cmd_file")
                if old_proc and old_proc.poll() is None:
                    try:
                        if old_cmd_file:
                            ipc_write(old_cmd_file, {"type": "quit"})
                        old_proc.wait(timeout=2.0)
                    except Exception:
                        old_proc.terminate()
                        try:
                            old_proc.wait(timeout=1)
                        except Exception:
                            old_proc.kill()
                if old_cmd_file and os.path.exists(old_cmd_file):
                    try:
                        os.remove(old_cmd_file)
                    except Exception:
                        pass
                _INSTANCES.pop(self.singleton_key, None)

        self._python_exe = _find_python()
        if not self._python_exe:
            await printr.print_async(
                "[SC_Toolbox] No system Python with tkinter found.",
                color=LogType.ERROR, server_only=True)
            return

        await printr.print_async(
            f"[SC_Toolbox] Using Python: {self._python_exe}",
            color=LogType.INFO, server_only=True)

        try:
            win_x = int(self._get_prop("window_x", 100))
        except (ValueError, TypeError):
            win_x = 100
        try:
            win_y = int(self._get_prop("window_y", 100))
        except (ValueError, TypeError):
            win_y = 100
        try:
            win_w = int(self._get_prop("window_width", 500))
        except (ValueError, TypeError):
            win_w = 500
        try:
            win_h = int(self._get_prop("window_height", 400))
        except (ValueError, TypeError):
            win_h = 400
        try:
            opacity = float(self._get_prop("opacity", 0.95))
        except (ValueError, TypeError):
            opacity = 0.95

        self._launch_args = [
            str(win_x), str(win_y), str(win_w), str(win_h), str(opacity)]

        if self._get_prop("launch_at_startup", True):
            with _INSTANCES_LOCK:
                self._launch_proc()
            await asyncio.sleep(1.0)
            if self._proc:
                await printr.print_async(
                    f"[SC_Toolbox] Launcher started (PID {self._proc.pid})",
                    color=LogType.INFO, server_only=True)

    async def unload(self) -> None:
        self._send({"type": "quit"})
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self._proc.terminate()
        self._proc = None
        if self._cmd_file and os.path.exists(self._cmd_file):
            try:
                os.remove(self._cmd_file)
            except Exception:
                pass
            try:
                os.remove(self._cmd_file + ".lock")
            except OSError:
                pass
        self._cmd_file = None
        with _INSTANCES_LOCK:
            _INSTANCES.pop(self.singleton_key, None)
        await super().unload()

    def _launch_proc(self) -> None:
        if not self._python_exe or not self._launch_args:
            return
        # Clean up old cmd file if re-launching
        if self._cmd_file:
            for old in (self._cmd_file, self._cmd_file + ".lock"):
                try:
                    os.remove(old)
                except OSError:
                    pass
        safe_key = self.singleton_key.replace(os.sep, "_").replace(" ", "_")
        self._cmd_file = os.path.join(
            tempfile.gettempdir(), f"sc_toolbox_{safe_key}_{os.getpid()}.jsonl")
        with open(self._cmd_file, "w"):
            pass

        try:
            self._proc = subprocess.Popen(
                [self._python_exe, _app_script]
                + self._launch_args + [self._cmd_file],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=_skill_dir,
                creationflags=subprocess.CREATE_NO_WINDOW)
        except Exception:
            # Clean up leaked temp file
            for f in (self._cmd_file, self._cmd_file + ".lock"):
                try:
                    os.remove(f)
                except OSError:
                    pass
            self._cmd_file = None
            raise
        # Caller must hold _INSTANCES_LOCK
        _INSTANCES[self.singleton_key] = {
            "proc": self._proc, "cmd_file": self._cmd_file}

    async def _ensure_started(self) -> bool:
        with _INSTANCES_LOCK:
            if self._proc and self._proc.poll() is None:
                return True
            if not self._python_exe or not self._launch_args:
                return False
            try:
                self._launch_proc()
            except Exception as e:
                print(f"[SC_Toolbox] _launch_proc failed: {e}")
                return False
        await asyncio.sleep(0.1)
        return self._proc is not None and self._proc.poll() is None

    def _send(self, cmd: dict) -> None:
        with _INSTANCES_LOCK:
            cmd_file = self._cmd_file
            proc = self._proc
        if cmd_file and proc and proc.poll() is None:
            try:
                ipc_write(cmd_file, cmd)
            except Exception as e:
                print(f"[SC_Toolbox] Warning: IPC send failed: {e}")

    # ── Voice tools ──────────────────────────────────────────────────────────

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
