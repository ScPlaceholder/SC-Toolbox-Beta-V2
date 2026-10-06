"""Parent side of the per-tool worker subprocesses.

One warm worker per tool (see workers/_worker_main.py for the protocol).
A worker keeps its tool's fetched data in memory, so the second question
about trade routes does not re-download 5,000 prices. Around that sits a
short result cache, so asking the same thing twice within a few minutes
costs nothing at all.

Resource rules (a PC running the game has little memory to spare):
  * at most ``max_live`` workers at once; the least recently used one is
    stopped to make room;
  * a worker idle for ``idle_timeout`` seconds is stopped on the next call;
  * every call has a timeout; a timed-out worker is killed, never reused.

Errors are surfaced, not swallowed: a crash comes back as a ToolError
carrying the exception text and the tail of the worker's stderr.
"""
from __future__ import annotations

import collections
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
from typing import Optional

from .tools import ToolError

log = logging.getLogger(__name__)

_HERE = os.path.dirname(os.path.abspath(__file__))
_WORKER_MAIN = os.path.join(_HERE, "workers", "_worker_main.py")
_LOGIC_FILE = os.path.join(_HERE, "logic.py")

# tool key -> (folder relative to the toolbox root, handler file in workers/)
TOOLS = {
    "trade":        (os.path.join("skills", "Trade_Hub"),           "h_trade.py"),
    "market":       (os.path.join("skills", "Market_Finder"),       "h_market.py"),
    "missions":     (os.path.join("skills", "Mission_Database"),    "h_missions.py"),
    "signals":      (os.path.join("tools", "Mining_Signals"),       "h_signals.py"),
    "cargo":        (os.path.join("skills", "Cargo_loader"),        "h_cargo.py"),
    "playtime":     (os.path.join("tools", "PlayTime_Calculator"),  "h_playtime.py"),
    "craft":        (os.path.join("skills", "Craft_Database"),      "h_craft.py"),
    "mining":       (os.path.join("skills", "Mining_Loadout"),      "h_mining.py"),
    "starmap":      (os.path.join("skills", "Starmap"),             "h_starmap.py"),
    "battle_buddy": (os.path.join("tools", "Battle_Buddy"),         "h_battle_buddy.py"),
    "dps":          (os.path.join("skills", "DPS_Calculator"),      "h_dps.py"),
}

# seconds a result stays cached, per worker function (default below)
RESULT_TTL = {
    "current_loadout": 20,
    "playtime_summary": 60,
}
DEFAULT_RESULT_TTL = 180


def find_toolbox_python() -> str:
    """The interpreter the launcher uses.

    The Assistant is itself started by the launcher with that
    interpreter, so sys.executable is right whenever it is a plain
    python.exe; otherwise fall back to the launcher's own discovery.
    """
    exe = sys.executable or ""
    base = os.path.basename(exe).lower()
    if base in ("python.exe", "python3.exe", "python") and os.path.isfile(exe):
        return exe
    local = os.environ.get("LOCALAPPDATA", "")
    pinned = os.path.join(local, "Python", "pythoncore-3.14-64", "python.exe")
    if os.path.isfile(pinned):
        return pinned
    try:
        from shared.python_discovery import find_python
        found = find_python()
        if found:
            return found
    except Exception as exc:                                # noqa: BLE001
        log.warning("worker_pool: python discovery failed: %s", exc)
    return exe or "python"


def _hidden_startupinfo():
    if os.name != "nt":
        return None
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0
    return si


class _Worker:
    def __init__(self, key: str, python: str, root: str) -> None:
        folder, handler = TOOLS[key]
        self.key = key
        self.tool_dir = os.path.join(root, folder)
        if not os.path.isdir(self.tool_dir):
            raise ToolError("%s: that tool is not installed (%s)" % (key, folder.replace(os.sep, "/")))
        env = dict(os.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"     # no __pycache__ into the tools
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        env.pop("PYTHONPATH", None)
        self.proc = subprocess.Popen(
            [python, _WORKER_MAIN, self.tool_dir, root,
             os.path.join(_HERE, "workers", handler), _LOGIC_FILE],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=self.tool_dir, env=env, startupinfo=_hidden_startupinfo(),
        )
        self.stderr_tail: collections.deque = collections.deque(maxlen=60)
        self._out: "queue.Queue" = queue.Queue()
        self._seq = 0
        self.last_used = time.monotonic()
        threading.Thread(target=self._pump_out, daemon=True,
                         name=f"worker-{key}-out").start()
        threading.Thread(target=self._pump_err, daemon=True,
                         name=f"worker-{key}-err").start()

    def _pump_out(self) -> None:
        for raw in self.proc.stdout:
            try:
                self._out.put(json.loads(raw.decode("utf-8")))
            except ValueError:
                self.stderr_tail.append("[non-JSON on protocol] " +
                                        raw.decode("utf-8", "replace")[:300])
        self._out.put(None)   # EOF

    def _pump_err(self) -> None:
        for raw in self.proc.stderr:
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                self.stderr_tail.append(line)

    def alive(self) -> bool:
        return self.proc.poll() is None

    def err_tail(self, n: int = 8) -> str:
        return "\n".join(list(self.stderr_tail)[-n:])

    def _wait_for(self, rid: int, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError
            try:
                msg = self._out.get(timeout=left)
            except queue.Empty:
                raise TimeoutError
            if msg is None:
                raise EOFError
            if msg.get("id") == rid:
                return msg

    def start_wait(self, timeout: float) -> dict:
        return self._wait_for(0, timeout)

    def request(self, fn: str, args: dict, timeout: float) -> dict:
        self._seq += 1
        rid = self._seq
        line = json.dumps({"id": rid, "fn": fn, "args": args},
                          ensure_ascii=False, default=str) + "\n"
        self.proc.stdin.write(line.encode("utf-8"))
        self.proc.stdin.flush()
        self.last_used = time.monotonic()
        msg = self._wait_for(rid, timeout)
        self.last_used = time.monotonic()
        return msg

    def stop(self) -> None:
        if not self.alive():
            return
        try:
            self.proc.stdin.write(b'{"id": -9, "fn": "__quit__"}\n')
            self.proc.stdin.flush()
            self.proc.wait(timeout=3)
        except Exception:                                   # noqa: BLE001
            pass
        if self.alive():
            self.proc.kill()
            try:
                self.proc.wait(timeout=3)
            except Exception:                               # noqa: BLE001
                pass


class WorkerPool:
    def __init__(self, root: str, max_live: int = 2, idle_timeout: float = 600.0,
                 python: Optional[str] = None) -> None:
        self.root = root
        self.max_live = max(1, int(max_live))
        self.idle_timeout = idle_timeout
        self.python = python or find_toolbox_python()
        self._workers: "collections.OrderedDict[str, _Worker]" = collections.OrderedDict()
        self._cache: dict = {}
        self._lock = threading.RLock()

    # ── lifecycle ──
    def _reap(self) -> None:
        now = time.monotonic()
        for key, w in list(self._workers.items()):
            if not w.alive() or now - w.last_used > self.idle_timeout:
                w.stop()
                self._workers.pop(key, None)

    def _get(self, key: str, start_timeout: float) -> _Worker:
        self._reap()
        w = self._workers.get(key)
        if w is not None and w.alive():
            self._workers.move_to_end(key)
            return w
        while len(self._workers) >= self.max_live:
            _, old = self._workers.popitem(last=False)
            old.stop()
        try:
            w = _Worker(key, self.python, self.root)
        except OSError as exc:
            raise ToolError(f"could not start the {key} worker with {self.python}: {exc}")
        try:
            hello = w.start_wait(start_timeout)
        except (TimeoutError, EOFError):
            tail = w.err_tail()
            w.stop()
            raise ToolError(f"{key} worker did not start" + (f": {tail}" if tail else ""))
        if not hello.get("ok"):
            w.stop()
            raise ToolError(f"{key} worker failed to start: {hello.get('error')}"
                            + (f"\n{hello.get('traceback', '')[-800:]}" if hello.get("traceback") else ""))
        self._workers[key] = w
        return w

    def stop(self, key: str) -> None:
        with self._lock:
            w = self._workers.pop(key, None)
            if w:
                w.stop()

    def shutdown(self) -> None:
        with self._lock:
            for w in self._workers.values():
                w.stop()
            self._workers.clear()

    def live(self) -> list:
        return [k for k, w in self._workers.items() if w.alive()]

    def clear_cache(self) -> None:
        self._cache.clear()

    # ── the call ──
    def call(self, key: str, fn: str, args: Optional[dict] = None,
             timeout: float = 90.0, use_cache: bool = True):
        args = dict(args or {})
        ck = json.dumps([key, fn, args], sort_keys=True, default=str)
        ttl = RESULT_TTL.get(fn, DEFAULT_RESULT_TTL)
        now = time.monotonic()
        if use_cache:
            hit = self._cache.get(ck)
            if hit and now - hit[0] < ttl:
                return hit[1]
        with self._lock:
            w = self._get(key, start_timeout=min(timeout, 60.0))
            try:
                msg = w.request(fn, args, timeout)
            except TimeoutError:
                tail = w.err_tail()
                self._workers.pop(key, None)
                w.proc.kill()
                raise ToolError(f"{fn} timed out after {timeout:.0f}s"
                                + (f" (worker log: {tail[-400:]})" if tail else ""))
            except (EOFError, OSError, ValueError) as exc:
                tail = w.err_tail()
                self._workers.pop(key, None)
                w.stop()
                raise ToolError(f"{fn}: the {key} worker died ({type(exc).__name__})"
                                + (f": {tail[-600:]}" if tail else ""))
        if not msg.get("ok"):
            kind = msg.get("kind")
            if kind == "tool":
                raise ToolError(msg.get("error") or f"{fn} failed")
            tb = (msg.get("traceback") or "").strip().splitlines()
            where = tb[-3] if len(tb) >= 3 else ""
            raise ToolError(f"{fn} crashed: {msg.get('error')}"
                            + (f" [{where.strip()}]" if where else ""))
        result = msg.get("result")
        self._cache[ck] = (time.monotonic(), result)
        if len(self._cache) > 200:
            for k in sorted(self._cache, key=lambda k: self._cache[k][0])[:50]:
                self._cache.pop(k, None)
        return result


_POOLS: dict = {}


def get_pool(root: str) -> WorkerPool:
    """Process-wide pool for a toolbox root."""
    root = os.path.normpath(root)
    pool = _POOLS.get(root)
    if pool is None:
        pool = WorkerPool(root)
        _POOLS[root] = pool
    return pool


def shutdown_all() -> None:
    for p in _POOLS.values():
        p.shutdown()
