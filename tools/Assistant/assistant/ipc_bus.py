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
  * ensure_skill(id)        -- show the skill if it is running, else spawn
    it with the launcher's OWN argv contract and env, so the assistant can
    open any discovered tool even when the launcher reads no commands.
  * ensure_trade_hub()      -- ensure_skill("trade"), kept as a name.

Commands are plain dicts; the receiving side is the skill's existing
_dispatch. New command types must be handled there — the assistant
ships with one: Trade Hub's ``route_detail`` (pinned popup + map).

Why ensure_skill exists at all: ``launch_tool`` used to be a pure relay to
the launcher, and the launcher only reads a command file when WingmanAI's
main.py started it (it appends one as argv[6]). LAUNCH.bat passes the
literal ``nul`` and SC_Toolbox.vbs passes no args, so in both of the ways
a person actually starts the toolbox, skill_launcher.py takes the
``cmd_file == os.devnull`` branch and never starts its IPC reader. That
left the assistant able to open exactly ONE of its fourteen tools — Trade
Hub, and only because the old ensure_trade_hub() spawned it directly with
a hand-copied argv layout. ensure_skill() is that same trick derived from
core.skill_registry instead of hardcoded, so it covers all fourteen.
"""
from __future__ import annotations

import glob
import json
import logging
import os
import re
import subprocess
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


def _launcher_settings(base_dir: str) -> dict:
    """The launcher's raw settings dict (language, ui_scale, geometry...)."""
    try:
        from shared.user_settings import launcher_settings, load_json
    except ImportError as exc:      # run without the toolbox root on sys.path
        log.debug("ipc_bus: no launcher settings (%s)", exc)
        return {}
    data = load_json(*launcher_settings(base_dir))
    return data if isinstance(data, dict) else {}


def _spawn_env(base_dir: str) -> dict:
    """The env skill_launcher.py registers for every skill it starts.

    Mirrors skill_launcher.py's ``lang_env``: language, an optional Qt
    scale factor, and exit-on-close when the launcher auto-hides. A skill
    the assistant spawns must behave like one the launcher spawned.
    """
    cfg = _launcher_settings(base_dir)
    env = dict(os.environ)
    env["SC_TOOLBOX_LANG"] = str(cfg.get("language", "en"))
    try:
        scale = float(cfg.get("ui_scale", 1.0))
    except (TypeError, ValueError):
        scale = 1.0
    if scale != 1.0:
        env["QT_SCALE_FACTOR"] = str(scale)
    else:
        env.pop("QT_SCALE_FACTOR", None)   # launcher passes "" == unset
    if cfg.get("hide_on_tool_active"):
        env["SC_TOOLBOX_EXIT_ON_CLOSE"] = "1"
    else:
        env.pop("SC_TOOLBOX_EXIT_ON_CLOSE", None)
    return env


def _spawn_geometry(base_dir: str, skill_id: str, script: str) -> tuple:
    """(x, y, w, h, opacity) the launcher would use for this skill.

    Same precedence as skill_launcher.py: the launcher settings' per-skill
    ``<id>_x`` block, overridden by the geometry the skill itself saved on
    its last close (``logs/<script stem>_window.json``).
    """
    cfg = _launcher_settings(base_dir)
    x = cfg.get(f"{skill_id}_x", 100)
    y = cfg.get(f"{skill_id}_y", 100)
    w = cfg.get(f"{skill_id}_w", 1300)
    h = cfg.get(f"{skill_id}_h", 800)
    opacity = cfg.get(f"{skill_id}_opacity", 0.95)

    stem = os.path.splitext(os.path.basename(script))[0]
    saved_path = os.path.join(base_dir, "logs", f"{stem}_window.json")
    try:
        with open(saved_path, encoding="utf-8") as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            x = saved.get("x", x)
            y = saved.get("y", y)
            w = saved.get("w", w)
            h = saved.get("h", h)
            opacity = saved.get("opacity", opacity)
    except (OSError, ValueError):
        pass

    def _i(v, d):
        try:
            return int(v)
        except (TypeError, ValueError):
            return d

    def _f(v, d):
        try:
            return float(v)
        except (TypeError, ValueError):
            return d

    # Floor the opacity. The launcher does not (WindowGeometry keeps whatever
    # was saved), but a saved 0.0 here means the assistant reports "opened"
    # for a window nobody can see — the exact failure this module is fixing.
    return (_i(x, 100), _i(y, 100), _i(w, 1300), _i(h, 800),
            max(0.3, min(1.0, _f(opacity, 0.95))))


def _skill_python() -> str:
    """The interpreter the launcher runs skills with."""
    from .worker_pool import find_toolbox_python
    return find_toolbox_python()


def spawn_plan(base_dir: str, skill_id: str, cmd_file: str = "<cmd_file>") -> dict | None:
    """What spawn_skill() WOULD run, without running it.

    Returns ``{"argv": [...], "cwd": ..., "script": ..., "cmd_file": ...}``
    or None when the skill is not discoverable / has no script on disk.

    This is the launcher's positional argv contract
    (``<x> <y> <w> <h> [custom_args...] <opacity> <cmd_file>``, see
    shared/data_utils.parse_cli_args) derived from core.skill_registry, so
    the per-skill custom args (Trade Hub's refresh + max_routes) and the
    saved window geometry are the ones the launcher itself would pass. It
    is split out from spawn_skill so the selftest can check the shape for
    every discovered skill without putting fourteen windows on screen.
    """
    try:
        from core.skill_registry import (discover_skills, resolve_script_path,
                                         resolve_skill_path)
    except ImportError as exc:
        log.warning("ipc_bus: skill registry unavailable: %s", exc)
        return None
    skill = next((s for s in discover_skills(base_dir) if s.id == skill_id), None)
    if skill is None:
        log.warning("ipc_bus: no discovered skill %r", skill_id)
        return None
    script = resolve_script_path(skill, base_dir)
    folder = resolve_skill_path(skill, base_dir)
    if not script or not folder:
        log.warning("ipc_bus: %s has no entry script on disk", skill_id)
        return None

    x, y, w, h, opacity = _spawn_geometry(base_dir, skill_id, script)
    argv = ([_skill_python(), script, str(x), str(y), str(w), str(h)]
            + [str(a) for a in (skill.custom_args or [])]
            + [str(opacity), cmd_file])
    return {"argv": argv, "cwd": folder, "script": script, "cmd_file": cmd_file,
            "custom_args": list(skill.custom_args or [])}


def spawn_skill(base_dir: str, skill_id: str) -> bool:
    """Start *skill_id* directly, the way the launcher would.

    The command file goes on argv last, which is what makes find_cmd_file()
    able to see the new process afterwards.
    """
    seq = int(time.time()) % 100000
    cmd_file = os.path.join(
        tempfile.gettempdir(),
        f"{_PREFIX}{skill_id}_{os.getpid()}_{seq}.jsonl")
    plan = spawn_plan(base_dir, skill_id, cmd_file)
    if plan is None:
        return False
    try:
        open(cmd_file, "w").close()
    except OSError as exc:
        log.warning("ipc_bus: cannot create %s: %s", cmd_file, exc)
        return False

    argv, folder = plan["argv"], plan["cwd"]
    try:
        subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=folder,
            env=_spawn_env(base_dir),
            startupinfo=_hidden_startupinfo(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("ipc_bus: failed to spawn %s: %s", skill_id, exc)
        return False
    log.info("ipc_bus: spawned %s (cmd_file=%s)", skill_id, cmd_file)
    return True


def ensure_skill(base_dir: str, skill_id: str, show: bool = True) -> bool:
    """Make sure *skill_id* is running; spawn it if it is not.

    True means "there is (or will shortly be) a process for it" — poll
    wait_ready() when the caller needs to talk to it.
    """
    if is_running(skill_id):
        if show:
            send(skill_id, {"type": "show"})
        return True
    return spawn_skill(base_dir, skill_id)


def ensure_trade_hub(base_dir: str, show: bool = True) -> bool:
    """Make sure Trade Hub is running; spawn it if needed."""
    return ensure_skill(base_dir, "trade", show=show)


def wait_ready(skill_id: str, timeout: float = 12.0) -> bool:
    """Poll until *skill_id* has a live command file (spawn warm-up)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_running(skill_id):
            return True
        time.sleep(0.25)
    return False
