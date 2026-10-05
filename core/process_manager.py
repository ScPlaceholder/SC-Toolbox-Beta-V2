"""
Centralised process manager for skill subprocesses.

Responsibilities:
- Track all child processes by skill ID
- Start / stop / restart with graceful shutdown
- Prevent orphan processes via atexit cleanup
- Health checks (is process alive?)
- Capture subprocess stdout/stderr to log files
"""
from __future__ import annotations

import atexit
import logging
import os
import subprocess
import tempfile
import time
import threading
from typing import Any, Callable

from shared.constants import PROCESS_START_COOLDOWN, PROCESS_MAX_COOLDOWN, PROCESS_SHUTDOWN_TIMEOUT
from shared.ipc import ipc_write
from shared.logging_config import get_subprocess_log_path

log = logging.getLogger(__name__)

# Windows subprocess startup — hide the console window without using
# CREATE_NO_WINDOW (0x08000000), which causes PySide6 to segfault on
# Python 3.14+ because Qt requires a valid window station.
# STARTUPINFO with SW_HIDE hides the console while preserving the
# window station that Qt needs.
def _hidden_startupinfo():
    """Return a STARTUPINFO that hides the console window (Windows only)."""
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0  # SW_HIDE
    return si


def _pid_alive(pid: int) -> bool:
    """Check if *pid* is alive without killing it (Windows-safe)."""
    try:
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if h:
            ctypes.windll.kernel32.CloseHandle(h)
            return True
        return False
    except (OSError, ValueError, AttributeError):
        return False


class ManagedProcess:
    """Wraps a single skill subprocess with IPC and lifecycle management."""

    def __init__(
        self,
        skill_id: str,
        python_exe: str,
        script: str,
        cwd: str,
        args: list[str],
        base_dir: str,
        env: dict[str, str] | None = None,
    ) -> None:
        self.skill_id = skill_id
        self._python = python_exe
        self._script = script
        self._cwd = cwd
        self._extra_args = args
        self._base_dir = base_dir
        self._env = env or {}

        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._cmd_file: str | None = None
        self._log_file: Any | None = None
        self._stopping = False
        self._visible = False
        self._tab: str | None = None   # last tab asked for (tools whose window has tabs)
        self._last_start: float = 0.0  # monotonic timestamp of last start
        self._START_COOLDOWN: float = PROCESS_START_COOLDOWN
        self._MAX_COOLDOWN: float = PROCESS_MAX_COOLDOWN
        self._consecutive_crashes: int = 0  # crash counter for backoff
        self._launch_seq: int = 0           # unique IPC file counter
        self._on_exit: Callable | None = None  # called (on bg thread) when process exits

    # ── Properties ───────────────────────────────────────────────────────

    def set_on_exit(self, callback: Callable | None) -> None:
        """Register a callback invoked (on a background thread) when the
        process exits for any reason.  The launcher uses this for instant
        auto-hide detection instead of polling."""
        self._on_exit = callback

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def visible(self) -> bool:
        return self._visible

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc else None

    @property
    def unexpectedly_died(self) -> bool:
        """True when the process has exited abnormally without being asked to stop.

        A crash is when the process is dead (``_proc.poll() is not None``),
        we never asked it to stop (``_stopping`` is False), **and** it exited
        with a non-zero exit code.  Exit code 0 means the user closed the
        window normally — that is not a crash.
        """
        return (
            self._proc is not None
            and self._proc.poll() is not None
            and self._proc.returncode != 0
            and not self._stopping
        )

    @property
    def cmd_file(self) -> str | None:
        return self._cmd_file

    @property
    def _current_cooldown(self) -> float:
        """Exponential backoff: 5s, 10s, 20s, 40s, capped at 60s."""
        cd = self._START_COOLDOWN * (2 ** self._consecutive_crashes)
        return min(cd, self._MAX_COOLDOWN)

    def _record_crash(self) -> None:
        """Track consecutive crashes for backoff calculation."""
        self._consecutive_crashes = min(self._consecutive_crashes + 1, 5)
        log.warning("process_manager: %s crash #%d, next cooldown %.0fs",
                    self.skill_id, self._consecutive_crashes, self._current_cooldown)

    # ── Lifecycle ────────────────────────────────────────────────────────

    def start(self) -> bool:
        """Launch the subprocess.  Returns True on success."""
        with self._lock:
            return self._start_unlocked()

    def _start_unlocked(self) -> bool:
        if self.running:
            log.debug("process_manager: %s already running (PID %s)", self.skill_id, self.pid)
            return True

        # Clean up dead process state before attempting restart
        if self._proc is not None and self._proc.poll() is not None:
            log.warning("process_manager: %s dead (PID %s, exit=%s)",
                        self.skill_id, self._proc.pid, self._proc.returncode)
            self._proc = None
            self._visible = False
            self._cleanup_files()

        # Prevent rapid relaunch (e.g. user clicking while cache loads)
        now = time.monotonic()
        cooldown = self._current_cooldown
        if now - self._last_start < cooldown:
            log.debug("process_manager: %s start cooldown (%0.1fs remaining)",
                      self.skill_id, cooldown - (now - self._last_start))
            return False

        # Create IPC file with unique name per launch (PID + monotonic counter)
        self._launch_seq += 1
        self._cmd_file = os.path.join(
            tempfile.gettempdir(),
            f"sc_toolbox_{self.skill_id}_{os.getpid()}_{self._launch_seq}.jsonl",
        )
        with open(self._cmd_file, "w"):
            pass

        # Open log file for subprocess output capture
        log_path = get_subprocess_log_path(self.skill_id, self._base_dir)
        try:
            self._log_file = open(log_path, "a", encoding="utf-8")
        except OSError as exc:
            log.warning("process_manager: cannot open log %s: %s", log_path, exc)
            self._log_file = subprocess.DEVNULL

        cmd = [self._python, self._script] + self._extra_args + [self._cmd_file]
        if self._env:
            merged = {**os.environ, **self._env}
            # Empty-string values mean "unset this variable"
            proc_env = {k: v for k, v in merged.items() if v != ""}
        else:
            proc_env = None
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=self._log_file,
                stderr=self._log_file,
                cwd=self._cwd,
                env=proc_env,
                startupinfo=_hidden_startupinfo(),
            )
            self._visible = True
            self._last_start = time.monotonic()
            log.info("process_manager: started %s (PID %d)", self.skill_id, self._proc.pid)
            # Start a daemon thread that waits for the process to exit,
            # then fires the on_exit callback so the launcher can react
            # instantly (e.g. re-show itself for auto-hide).
            if self._on_exit:
                proc_ref = self._proc
                cb = self._on_exit
                sid = self.skill_id
                def _wait():
                    try:
                        proc_ref.wait()
                    except OSError:
                        pass
                    log.debug("process_manager: %s exit detected by watcher", sid)
                    try:
                        cb()
                    except Exception as exc:
                        log.error("process_manager: on_exit callback error: %s", exc)
                t = threading.Thread(target=_wait, daemon=True,
                                     name=f"ExitWatch-{sid}")
                t.start()
            return True
        except (OSError, subprocess.SubprocessError) as exc:
            log.error("process_manager: failed to start %s: %s", self.skill_id, exc)
            self._record_crash()
            self._cleanup_files()
            return False

    def stop(self, timeout: float = 3.0) -> None:
        """Gracefully stop the subprocess."""
        with self._lock:
            self._stop_unlocked(timeout)

    def _stop_unlocked(self, timeout: float = 3.0) -> None:
        if not self.running or self._stopping:
            return
        self._stopping = True
        graceful = False
        try:
            # 1. Ask nicely via IPC
            self._send_unlocked({"type": "quit"})
            try:
                self._proc.wait(timeout=timeout)
                log.info("process_manager: %s exited gracefully (exit code %s)",
                         self.skill_id, self._proc.returncode)
                graceful = True
            except subprocess.TimeoutExpired:
                # 2. SIGTERM
                log.warning("process_manager: %s did not exit, terminating", self.skill_id)
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=PROCESS_SHUTDOWN_TIMEOUT)
                    graceful = True
                except subprocess.TimeoutExpired:
                    # 3. Force-kill the process tree
                    self._force_kill()
            if not graceful:
                self._record_crash()
            self._proc = None
            self._visible = False
            self._cleanup_files()
        finally:
            self._stopping = False

    def restart(self) -> bool:
        """Stop then start."""
        with self._lock:
            self._stop_unlocked()
            return self._start_unlocked()

    def send(self, cmd: dict[str, Any]) -> bool:
        """Send an IPC command.  Returns True on success."""
        with self._lock:
            return self._send_unlocked(cmd)

    def _send_unlocked(self, cmd: dict[str, Any]) -> bool:
        if self._cmd_file and self.running:
            return ipc_write(self._cmd_file, cmd)
        return False

    def mark_ready(self) -> None:
        """Called when the subprocess signals it is ready to accept commands."""
        with self._lock:
            self._visible = True
            self._consecutive_crashes = 0
            log.debug("process_manager: %s marked ready (crash counter reset)", self.skill_id)

    def show(self) -> None:
        with self._lock:
            if not self.running:
                self._start_unlocked()
            else:
                self._send_unlocked({"type": "show"})
                self._visible = True

    def hide(self) -> None:
        with self._lock:
            if self.running:
                self._send_unlocked({"type": "hide"})
                self._visible = False

    def toggle(self) -> None:
        with self._lock:
            if not self.running:
                # If we had a process that died unexpectedly, record the crash
                if self._proc is not None and self._proc.poll() is not None:
                    self._record_crash()
                self._start_unlocked()
                return
            if self._visible:
                self._send_unlocked({"type": "hide"})
                self._visible = False
            else:
                self._send_unlocked({"type": "show"})
                self._visible = True

    # ── Tabs ─────────────────────────────────────────────────────────────
    # A tool whose window has tabs (the Toolbox Assistant: Assistant + SuitMk2)
    # is one process reached by several hotkeys, one per tab. show_tab sends
    # the ordinary "show" with the tab's key added; a tool without tabs never
    # gets one.
    #
    # A hotkey or the tile is toggle_tab, and what it sends to a running
    # window is {"type": "toggle", "tab": <key>}: the WINDOW decides whether
    # that shows, switches tab or hides, from what it really is (hidden, or
    # which tab is in front). The launcher cannot decide it: the X on the
    # window and a click on a tab both happen in the tool's process and
    # nothing tells the launcher. When it decided from its own record
    # (``_visible``, ``_tab``) it sent "hide" to a window the user had already
    # closed with X (the press did nothing), and "hide" when the user had
    # clicked the other tab and pressed this tab's hotkey to come back (the
    # window vanished). Both seen on a real launcher, 2026-10-04.
    #
    # ``_visible`` and ``_tab`` are still kept, as the launcher's best guess:
    # the tile's state and the launcher's own auto-hide read ``visible``. They
    # are right whenever the user has only used the hotkeys and the tile, and
    # can be one step behind after an X or a click on a tab, exactly as
    # ``_visible`` already is for every tool whose X hides its window.

    def show_tab(self, tab: str) -> None:
        """show(), on one tab. Starts the process if needed, and the tab
        request waits in the command file until the tool reads it."""
        with self._lock:
            if not self.running and not self._start_unlocked():
                return
            self._send_unlocked({"type": "show", "tab": tab})
            self._visible = True
            self._tab = tab

    def toggle_tab(self, tab: str) -> None:
        """toggle(), per tab: the window hides only when it is showing THIS
        tab; otherwise it shows itself on it. The window makes that choice
        (see the note above); a window that had to be started is told "show"
        instead, because it comes up shown and a toggle would hide it."""
        with self._lock:
            if not self.running:
                # If we had a process that died unexpectedly, record the crash
                if self._proc is not None and self._proc.poll() is not None:
                    self._record_crash()
                if self._start_unlocked():
                    self._send_unlocked({"type": "show", "tab": tab})
                    self._tab = tab
                return
            self._send_unlocked({"type": "toggle", "tab": tab})
            if self._visible and self._tab == tab:
                self._visible = False
            else:
                self._visible = True
                self._tab = tab

    def start_with_env(
        self,
        extra_env: dict[str, str],
        visible_after: bool = True,
    ) -> bool:
        """Spawn the subprocess with extra env vars merged on top of the
        registered env, for THIS launch only.  Subsequent spawns
        (from show/toggle/etc.) use the original registered env.

        Use case: pre-spawning a skill in hidden / pre-warm mode by
        injecting ``SC_TOOLBOX_PRELOAD=1`` into its env once, without
        biasing every later cold-spawn from a user toggle into the same
        hidden-startup behaviour.

        ``visible_after`` controls the post-spawn ``_visible`` flag.
        Default True matches normal start behaviour.  Set False when
        the subprocess is being launched in pre-warm-hidden mode so
        the next ``toggle()`` correctly issues ``show`` instead of
        ``hide`` — and so the launcher doesn't need to send an
        immediate ``hide`` IPC (which races against the subprocess's
        own pre-warm timer in mouse_blocker_app.py).

        Returns True on successful spawn, False otherwise (already
        running, cooldown, etc.).
        """
        with self._lock:
            if self.running:
                return False
            original_env = self._env
            try:
                merged: dict[str, str] = {}
                if original_env:
                    merged.update(original_env)
                if extra_env:
                    merged.update(extra_env)
                self._env = merged
                ok = self._start_unlocked()
            finally:
                self._env = original_env
            if ok and not visible_after:
                self._visible = False
            return ok

    def health_check(self) -> dict[str, Any]:
        """Return a health-status dict for this process."""
        with self._lock:
            return {
                "skill_id": self.skill_id,
                "running": self.running,
                "visible": self._visible,
                "pid": self.pid,
            }

    # ── Internal ─────────────────────────────────────────────────────────

    def _force_kill(self) -> None:
        """Kill the entire process tree via taskkill (Windows)."""
        if not self._proc or self._proc.poll() is not None:
            return
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(self._proc.pid)],
                capture_output=True,
                timeout=5,
                startupinfo=_hidden_startupinfo(),
            )
            log.warning("process_manager: force-killed %s (PID %d)", self.skill_id, self._proc.pid)
        except (OSError, subprocess.SubprocessError) as exc:
            log.error("process_manager: taskkill failed for %s: %s", self.skill_id, exc)

    def _cleanup_files(self) -> None:
        """Remove IPC temp files and close log handle."""
        if self._cmd_file:
            for path in (self._cmd_file, self._cmd_file + ".lock"):
                try:
                    os.remove(path)
                except OSError:
                    pass
            self._cmd_file = None
        if self._log_file and self._log_file is not subprocess.DEVNULL:
            try:
                self._log_file.close()
            except OSError:
                pass
            self._log_file = None


class ProcessManager:
    """Registry of all managed skill processes.

    Provides a single point of control for starting, stopping, and
    querying all subprocesses.  Registers an atexit handler to prevent
    orphan processes on unclean shutdown.
    """

    def __init__(self) -> None:
        self._processes: dict[str, ManagedProcess] = {}
        self._lock = threading.Lock()
        atexit.register(self.shutdown_all)

    def register(
        self,
        skill_id: str,
        python_exe: str,
        script: str,
        cwd: str,
        args: list[str],
        base_dir: str,
        env: dict[str, str] | None = None,
    ) -> ManagedProcess:
        """Register (but don't start) a skill process."""
        with self._lock:
            mp = ManagedProcess(
                skill_id=skill_id,
                python_exe=python_exe,
                script=script,
                cwd=cwd,
                args=args,
                base_dir=base_dir,
                env=env,
            )
            self._processes[skill_id] = mp
            return mp

    def get(self, skill_id: str) -> ManagedProcess | None:
        with self._lock:
            return self._processes.get(skill_id)

    def start(self, skill_id: str) -> bool:
        mp = self.get(skill_id)
        return mp.start() if mp else False

    def stop(self, skill_id: str) -> None:
        mp = self.get(skill_id)
        if mp:
            mp.stop()

    def stop_all(self) -> None:
        with self._lock:
            ids = list(self._processes.keys())
        for sid in ids:
            self.stop(sid)

    def shutdown_all(self) -> None:
        """Stop every process — called by atexit to prevent orphans."""
        log.debug("process_manager: atexit shutdown_all")
        self.stop_all()

    def health(self) -> dict[str, dict[str, Any]]:
        """Return health info for all registered processes."""
        with self._lock:
            return {sid: mp.health_check() for sid, mp in self._processes.items()}

    def kill_orphan_skill_processes(self, skill_scripts: list[str]) -> int:
        """Kill orphan skill processes from previous launcher sessions.

        *skill_scripts* is a list of script basenames (e.g. ["cargo_app.py",
        "dps_calc_app.py"]) to search for in running python.exe processes.
        Never kills the current process or its parent.
        Returns the number of processes killed.
        """
        count = 0
        my_pid = os.getpid()
        my_ppid = os.getppid()
        if not skill_scripts:
            return 0
        # Single consolidated `wmic` query across all skill scripts
        # instead of one call per skill. Each wmic invocation takes
        # 2–5 s on cold Windows — the old per-skill loop was the
        # dominant reason the launcher stalled for ~25 s before the
        # UI appeared. One call + one pass through the result returns
        # in roughly the same time as a single per-skill query.
        clauses = " or ".join(
            f"commandline like '%{script}%'" for script in skill_scripts
        )
        where = f"name='python.exe' and ({clauses})"
        try:
            result = subprocess.run(
                ["wmic", "process", "where", where,
                 "get", "processid,commandline"],
                capture_output=True, text=True, timeout=12,
                startupinfo=_hidden_startupinfo(),
            )
            lines = result.stdout.strip().splitlines()
        except (OSError, subprocess.SubprocessError,
                subprocess.TimeoutExpired) as exc:
            log.debug("process_manager: orphan scan failed: %s", exc)
            return 0

        # wmic output is "CommandLine ... ProcessId" with whitespace
        # separation. ProcessId is always the last token on each line;
        # every other column belongs to CommandLine.
        for line in lines:
            line = line.strip()
            if not line:
                continue
            parts = line.rsplit(None, 1)
            if len(parts) != 2:
                continue
            cmd_part, pid_str = parts
            if not pid_str.isdigit():
                continue
            pid = int(pid_str)
            if pid in (my_pid, my_ppid):
                continue
            # Match which script this is (for the log line only).
            script = next(
                (s for s in skill_scripts if s in cmd_part), "<unknown>",
            )
            log.info(
                "process_manager: killing orphan %s (PID %d)", script, pid,
            )
            try:
                subprocess.run(
                    ["taskkill", "/F", "/PID", str(pid)],
                    capture_output=True, timeout=5,
                    startupinfo=_hidden_startupinfo(),
                )
                count += 1
            except (OSError, subprocess.SubprocessError):
                pass
        if count:
            log.info("process_manager: killed %d orphan process(es)", count)
        return count

    def cleanup_stale_ipc_files(self) -> int:
        """Remove leftover IPC temp files from dead processes.

        Handles both old format (sc_toolbox_{skill}_{pid}.jsonl) and
        new format (sc_toolbox_{skill}_{pid}_{seq}.jsonl).
        Returns the number of files cleaned up.
        """
        import glob as _glob
        import re as _re
        count = 0
        pattern = os.path.join(tempfile.gettempdir(), "sc_toolbox_*")
        for f in _glob.glob(pattern):
            try:
                basename = os.path.basename(f)
                # Extract all numeric segments after skill name
                # Old: sc_toolbox_dps_12345.jsonl → PID=12345
                # New: sc_toolbox_dps_12345_2.jsonl → PID=12345
                stripped = basename.replace(".jsonl", "").replace(".lock", "")
                nums = _re.findall(r'_(\d+)', stripped)
                if not nums:
                    continue
                # PID is always the first numeric segment after skill name
                pid = int(nums[0])
                if _pid_alive(pid):
                    continue
            except (ValueError, IndexError):
                continue  # Can't parse PID — skip
            try:
                os.remove(f)
                count += 1
            except OSError:
                pass
        if count:
            log.info("process_manager: cleaned up %d stale IPC file(s)", count)
        return count
