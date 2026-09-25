"""Cross-skill command bus.

The launcher talks to each skill subprocess through a JSONL command file
named ``sc_toolbox_<skill>_<pid>_<seq>.jsonl`` in the system temp dir
(see core/process_manager.py). This module lets the assistant join that
conversation without any launcher changes:

  * find_cmd_file(skill_id) -- command file of the running skill, keyed
    on a live process that holds that file on its command line (the PID
    in the file NAME is its creator's, not the skill's).
  * send(skill_id, cmd)     -- fire-and-forget IPC write.
  * send_to_launcher(cmd)   -- write to the launcher's own command file
    (``launch_skill`` etc.), when the launcher reads one.
  * ensure_trade_hub()      -- spawn Trade Hub when it is not running,
    using its documented argv layout, so display actions (pinned route
    popups) always have a window to land in.

Commands are plain dicts; the receiving side is the skill's existing
_dispatch. New command types must be handled there — the assistant
ships with one: Trade Hub's ``route_detail`` (pinned popup + map).
"""
from __future__ import annotations

import glob
import logging
import os
import re
import subprocess
import sys
import tempfile
import time

from shared.ipc import ipc_write

log = logging.getLogger(__name__)

_PREFIX = "sc_toolbox_"


def _pid_alive(pid: int) -> bool:
    """Match process_manager._pid_alive (OpenProcess probe, no kill)."""
    try:
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if h:
            ctypes.windll.kernel32.CloseHandle(h)
            return True
        return False
    except (OSError, ValueError, AttributeError):
        return False


def _cmd_file_pid(path: str) -> int | None:
    """Extract the owner PID from a command-file name (first numeric
    segment after the skill name, per process_manager cleanup logic).

    NOTE: that PID is the process that CREATED the file (the launcher, or
    the Assistant for a Trade Hub it spawned itself), not the skill. It
    says nothing about whether the skill is alive, so liveness below is
    keyed on a process that actually holds the file on its command line.
    """
    base = os.path.basename(path)
    if base.endswith(".lock"):
        return None
    stripped = base.replace(".jsonl", "")
    nums = re.findall(r"_(\d+)", stripped)
    if not nums:
        return None
    try:
        return int(nums[0])
    except ValueError:
        return None


# ── liveness: which command files does a running process actually read? ──

_SCAN_TTL = 1.5
_scan_cache: dict = {"ts": 0.0, "files": {}}


def _norm_path(p: str) -> str:
    return os.path.normcase(os.path.normpath(p))


def _scan_psutil() -> dict | None:
    try:
        import psutil
    except ImportError:
        return None
    out: dict = {}
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if not name.startswith("python"):
                continue
            for arg in proc.info.get("cmdline") or []:
                if arg.endswith(".jsonl") and _PREFIX in os.path.basename(arg):
                    out[_norm_path(arg)] = proc.info["pid"]
        except (psutil.Error, OSError):
            continue
    return out


def _scan_cim() -> dict:
    """Fallback without psutil: one PowerShell CIM query."""
    ps = ("Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | "
          "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=10,
                           startupinfo=_hidden_startupinfo())
        import json
        rows = json.loads(r.stdout or "[]")
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        log.warning("ipc_bus: process scan failed: %s", exc)
        return {}
    if isinstance(rows, dict):
        rows = [rows]
    out: dict = {}
    pat = re.compile(r'"?([^"\s]*' + re.escape(_PREFIX) + r'[^"\s]*\.jsonl)"?')
    for row in rows or []:
        for m in pat.finditer(row.get("CommandLine") or ""):
            out[_norm_path(m.group(1))] = row.get("ProcessId")
    return out


def live_cmd_files(force: bool = False) -> dict:
    """{normalised cmd-file path: pid} for every running python process
    that was handed an sc_toolbox_*.jsonl command file. Cached briefly
    because wait_ready() polls it."""
    now = time.monotonic()
    if not force and now - _scan_cache["ts"] < _SCAN_TTL:
        return _scan_cache["files"]
    files = _scan_psutil()
    if files is None:
        files = _scan_cim()
    _scan_cache.update(ts=now, files=files)
    return files


def _skill_file_re(skill_id: str):
    # exact skill id: "mining" must not match sc_toolbox_mining_signals_*
    return re.compile(r"^" + re.escape(_PREFIX + skill_id) + r"_(\d+)(?:_(\d+))?\.jsonl$",
                      re.IGNORECASE)


def find_cmd_file(skill_id: str) -> str | None:
    """Command file of the live process running *skill_id*, or None.

    A file counts only if a running process has it on its command line;
    a leftover file whose creator (launcher/Assistant) is still alive
    but whose skill has died no longer reads as running.
    """
    rx = _skill_file_re(skill_id)
    live = live_cmd_files()
    candidates = []
    for path in glob.glob(os.path.join(tempfile.gettempdir(), f"{_PREFIX}{skill_id}_*.jsonl")):
        if not rx.match(os.path.basename(path)):
            continue
        if _norm_path(path) in live:
            candidates.append((os.path.getmtime(path), path))
    if not candidates:
        return None
    candidates.sort()
    return candidates[-1][1]


def is_running(skill_id: str) -> bool:
    return find_cmd_file(skill_id) is not None


def launcher_cmd_file() -> str | None:
    """The launcher's own command file (skill_launcher.py's last argv).

    Present when the launcher was started by the WingmanAI skill
    (main.py); a launcher started from LAUNCH.bat reads no command file.
    """
    live = live_cmd_files(force=True)
    try:
        import psutil
        for proc in psutil.process_iter(["name", "cmdline"]):
            try:
                cl = proc.info.get("cmdline") or []
                if any(os.path.basename(a).lower() == "skill_launcher.py" for a in cl):
                    last = cl[-1] if cl else ""
                    if last.endswith(".jsonl") and os.path.isfile(last):
                        return last
            except (psutil.Error, OSError):
                continue
        return None
    except ImportError:
        pass
    for path in live:   # fallback: cmd files with no skill-id match
        base = os.path.basename(path)
        if base.startswith(_PREFIX + "sc_toolbox_proc_") and os.path.isfile(path):
            return path
    return None


def send_to_launcher(cmd: dict) -> bool:
    """Write a command to the launcher (e.g. launch_skill). False when no
    launcher command file is being read."""
    path = launcher_cmd_file()
    if not path:
        return False
    return ipc_write(path, cmd)


def send(skill_id: str, cmd: dict) -> bool:
    """Write a command to a live skill process. False if not running."""
    path = find_cmd_file(skill_id)
    if not path:
        log.info("ipc_bus: %s is not running", skill_id)
        return False
    ok = ipc_write(path, cmd)
    log.debug("ipc_bus: %s <- %s (%s)", skill_id, cmd.get("type"), ok)
    return ok


def _hidden_startupinfo():
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0
    return si


def ensure_trade_hub(base_dir: str, show: bool = True) -> bool:
    """Make sure Trade Hub is running; spawn it if needed.

    Uses Trade Hub's entry contract (trade_hub_app.py __main__):
    ``<x> <y> <w> <h> <refresh> <max_routes> <opacity> <cmd_file>``.
    Returns True when a live command file exists afterwards.
    """
    if is_running("trade"):
        if show:
            send("trade", {"type": "show"})
        return True

    script = os.path.join(base_dir, "skills", "Trade_Hub", "trade_hub_app.py")
    if not os.path.isfile(script):
        log.warning("ipc_bus: trade_hub_app.py not found at %s", script)
        return False

    seq = int(time.time()) % 100000
    cmd_file = os.path.join(
        tempfile.gettempdir(),
        f"{_PREFIX}trade_{os.getpid()}_{seq}.jsonl")
    open(cmd_file, "w").close()

    args = [sys.executable, script,
            "80", "80", "1400", "900", "300", "500", "0.95",
            cmd_file]
    try:
        subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=os.path.dirname(script),
            startupinfo=_hidden_startupinfo(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("ipc_bus: failed to spawn Trade Hub: %s", exc)
        return False
    log.info("ipc_bus: spawned Trade Hub (cmd_file=%s)", cmd_file)
    return True


def wait_ready(skill_id: str, timeout: float = 12.0) -> bool:
    """Poll until *skill_id* has a live command file (spawn warm-up)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_running(skill_id):
            return True
        time.sleep(0.25)
    return False
