"""Cross-skill command bus.

The launcher talks to each skill subprocess through a JSONL command file
named ``sc_toolbox_<skill>_<pid>_<seq>.jsonl`` in the system temp dir
(see core/process_manager.py). This module lets the assistant join that
conversation without any launcher changes:

  * find_cmd_file(skill_id) -- newest live command file for a skill,
    validated by PID liveness (mirrors process_manager._pid_alive).
  * send(skill_id, cmd)     -- fire-and-forget IPC write.
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
    segment after the skill name, per process_manager cleanup logic)."""
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


def find_cmd_file(skill_id: str) -> str | None:
    """Newest live command file for *skill_id*, or None.

    Picks the highest sequence number whose owner PID is alive, so a
    stale file from a dead launch never wins over the live one.
    """
    pattern = os.path.join(tempfile.gettempdir(),
                           f"{_PREFIX}{skill_id}_*.jsonl")
    candidates = []
    for path in glob.glob(pattern):
        pid = _cmd_file_pid(path)
        if pid is not None and _pid_alive(pid):
            candidates.append((os.path.getmtime(path), path))
    if not candidates:
        return None
    candidates.sort()
    return candidates[-1][1]


def is_running(skill_id: str) -> bool:
    return find_cmd_file(skill_id) is not None


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
