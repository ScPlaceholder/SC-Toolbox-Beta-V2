"""sidecar.py - start, watch and stop the local model service (companion_service.py) for the SuitMk2 tool.

The toolbox has no service management (TOOLBOX_PORT_NOTES.md), so the tool owns its sidecar:
  * ensure(): if http://127.0.0.1:7790/health answers, use it (maybe another toolbox run started it); otherwise launch
    it and wait for health.
  * WHICH PYTHON (2026-09-23): by default the toolbox's OWN interpreter (sys.executable) with `--backend auto`. The
    realizer then runs through Ollama over stdlib HTTP, so no torch is needed. The configured torch Python
    (settings "model_python") is used ONLY when settings say "backend": "hf". Settings "backend" may be
    auto | ollama | hf | none; absent = auto.
  * reload(): POST /reload - pick up newly created Ollama models / retrained adapters without a restart.
  * stop(): only stops a sidecar THIS process started. One that was already running is left alone.
  * Launch uses STARTUPINFO(SW_HIDE), never CREATE_NO_WINDOW (segfaults PySide6 on py3.14, process_manager.py:28-38).
  * LOCAL RUNTIME (2026-09-23): for backend auto/ollama, ensure() first wakes Ollama if it is installed but not
    running (ollama_manager: a hidden `ollama serve`), so the service resolves the ollama backend on its first try.
    Never fatal: no Ollama just means the service starts with backend none and the first-run Setup panel offers
    the fix. It never installs anything and never creates models; that is the Setup panel's job, on a click.
    stop() stops an `ollama serve` only if THIS sidecar started it.
"""
from __future__ import annotations

import http.client
import json
import logging
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Optional

# What a localhost HTTP call to the service can fail with. OSError covers urllib.error.URLError and HTTPError
# (both subclass it) plus ConnectionRefusedError and the socket timeout; http.client.HTTPException covers a
# truncated or malformed status line; ValueError covers json.JSONDecodeError and a non-UTF-8 body.
_HTTP_ERRORS = (OSError, http.client.HTTPException, ValueError)

log = logging.getLogger("suitmk2.sidecar")
URL = "http://127.0.0.1:7790"
BACKENDS = ("auto", "ollama", "hf", "api", "none")   # "api" runs on the toolbox Python (anthropic SDK), like ollama


def health(timeout: float = 1.0) -> Optional[dict]:
    try:
        with urllib.request.urlopen(URL + "/health", timeout=timeout) as r:
            return json.loads(r.read())
    except OSError as e:
        # Nobody listening, or not listening YET: this is the normal answer during _ensure's launch poll, so it is
        # debug rather than a warning. None here means "no service", which is exactly what the caller acts on.
        log.debug("sidecar: no answer from %s/health (%s: %s)", URL, type(e).__name__, e)
        return None
    except (http.client.HTTPException, ValueError) as e:
        # Something IS listening on 7790 and it answered with a body we cannot read. None makes that
        # indistinguishable from a dead port, and _ensure() responds to a dead port by launching a SECOND
        # service on it - so this one has to be loud.
        log.warning("sidecar: %s/health answered but the reply is unreadable (%s: %s); treating the service as "
                    "DOWN, which will launch another one on the same port", URL, type(e).__name__, e)
        return None


def reload(timeout: float = 120.0, backend: Optional[str] = None) -> Optional[dict]:
    """POST /reload [{"backend": name}]. Long timeout: the service finishes an in-flight generation before swapping.
    With backend, the running service switches to it (the window's API toggle) with no restart."""
    try:
        data = json.dumps({"backend": backend} if backend else {}).encode("utf-8")
        req = urllib.request.Request(URL + "/reload", data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except _HTTP_ERRORS as e:
        # None is read by Sidecar.reload() as False and by _ensure() as "the reload did not happen", which is the
        # right behaviour; without a line here a backend switch that never took effect looks like one that did.
        log.warning("sidecar: POST %s/reload failed (%s: %s); the service keeps its current backend%s",
                    URL, type(e).__name__, e, f" (wanted {backend})" if backend else "")
        return None


def _hidden_startupinfo():
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0          # SW_HIDE
    return si


def _configured_backend() -> str:
    try:
        from settings import load
        b = str(load().get("backend") or "auto")
    except Exception:
        b = "auto"
    return b if b in BACKENDS else "auto"


def own_python() -> Optional[str]:
    """The interpreter running the toolbox, as a console python.exe if one sits beside it (pythonw.exe would work
    but loses nothing by being swapped). None if frozen: a frozen exe cannot run a .py script."""
    if getattr(sys, "frozen", False) or not sys.executable:
        return None
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and (exe.parent / "python.exe").exists():
        exe = exe.parent / "python.exe"
    return str(exe)


class Sidecar:
    def __init__(self, python: str, service_py: Path, adapters: Path, presence: str = "present",
                 glance: bool = False, log_path: Optional[Path] = None, backend: Optional[str] = None):
        # `python` is the configured TORCH interpreter (settings "model_python"); kept positional for the UI.
        self.model_python, self.service_py, self.adapters = python, Path(service_py), Path(adapters)
        self.presence, self.glance, self.log_path = presence, glance, log_path
        self.backend = backend if backend in BACKENDS else _configured_backend()
        if self.backend == "hf":
            self.python, self.python_why = python, "configured torch Python (backend=hf)"
        else:
            own = own_python()
            self.python = own or python
            self.python_why = "toolbox Python" if own else "configured Python (toolbox is frozen)"
        self.proc: Optional[subprocess.Popen] = None
        self.status = "not started"
        self.runtime = None             # ollama_manager.OllamaManager, only when WE woke the runtime
        self.runtime_note = ""
        self.runtime_factory = None     # tests inject a fake manager factory; None = ollama_manager.OllamaManager

    def wake_runtime(self, wait_s: float = 20.0) -> str:
        """Start Ollama hidden if it is installed but asleep. Returns a short note; never raises."""
        if self.backend not in ("auto", "ollama"):
            return ""
        try:
            import ollama_manager as om
            m = (self.runtime_factory or om.OllamaManager)()
            st = m.status()
            if st.state == om.INSTALLED_NOT_RUNNING:
                m.ensure_running(wait_s=wait_s)
                if getattr(m, "proc", None) is not None:
                    self.runtime = m        # ours to stop; None if another instance won the port race
                self.runtime_note = "local runtime started"
            elif st.state == om.NOT_INSTALLED:
                self.runtime_note = "local runtime not installed"
            else:
                self.runtime_note = ""
        except Exception as e:
            self.runtime_note = f"local runtime: {type(e).__name__}: {e}"
        return self.runtime_note

    def command(self) -> list:
        args = [self.python, "-u", str(self.service_py), "--presence", self.presence, "--adapters", str(self.adapters),
                "--backend", self.backend]
        if self.glance:
            args.append("--glance")
        return args

    def _describe(self, h: Optional[dict]) -> str:
        if not h or not h.get("backend"):
            return ""
        return f" | backend {h['backend']}" + (f" ({h['device']})" if h.get("device") else "")

    def ensure(self, wait_s: float = 20.0) -> bool:
        ok = self._ensure(wait_s)
        if self.runtime_note:
            self.status += f" | {self.runtime_note}"
        return ok

    def _ensure(self, wait_s: float) -> bool:
        woke = self.wake_runtime() == "local runtime started"
        h = health()
        if h is not None:
            if woke:
                h = reload() or h            # an already-running service resolved 'none' while Ollama slept
            self.status = "running (already up)" + self._describe(h)
            return True
        if not Path(self.python).exists():
            self.status = f"no Python at {self.python} ({self.python_why})"
            return False
        out = open(self.log_path, "a", encoding="utf-8") if self.log_path else subprocess.DEVNULL
        try:
            self.proc = subprocess.Popen(self.command(), cwd=str(self.service_py.parent), stdout=out,
                                         stderr=subprocess.STDOUT, startupinfo=_hidden_startupinfo(),
                                         env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        except Exception as e:
            self.status = f"launch failed: {type(e).__name__}: {e}"
            return False
        t0 = time.time()
        while time.time() - t0 < wait_s:
            if self.proc.poll() is not None:
                self.status = f"exited rc={self.proc.returncode} (see log)"
                return False
            h = health()
            if h is not None:
                self.status = "running (started by SuitMk2)" + self._describe(h)
                return True
            time.sleep(0.5)
        self.status = "started but not answering"
        return False

    def reload(self, backend: Optional[str] = None) -> bool:
        if backend is not None and backend not in BACKENDS:
            return False
        r = reload(backend=backend)
        if r and r.get("ok"):
            if backend is not None:
                self.backend = backend
            self.status = "running (reloaded)" + self._describe(r)
            return True
        return False

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.kill()
            self.status = "stopped"
        if self.runtime is not None:
            try:
                self.runtime.stop_if_ours()
            except (OSError, RuntimeError) as e:
                # terminate()/kill()/wait() on an already-reaped or protected PID (OSError), or OllamaError, which
                # subclasses RuntimeError. self.runtime is only ever set when WE started `ollama serve`, so a
                # failure here leaves a hidden server process running after the tool closed. Say so: the next run
                # will find the port taken and conclude someone else's Ollama owns it.
                log.warning("sidecar: could not stop the `ollama serve` this sidecar started (%s: %s); it is still "
                            "running", type(e).__name__, e)
            self.runtime = None


def _selftest() -> int:
    """Interpreter choice + runtime wake against a FAKE manager - launches nothing real."""
    results = []
    svc = Path(__file__).with_name("companion_service.py")
    torch_py = r"C:\nonexistent\torch_env\python.exe"
    d = Sidecar(torch_py, svc, Path("."), backend="auto")
    results.append(("auto -> toolbox Python", d.python == own_python() and d.python != torch_py))
    results.append(("auto -> --backend auto passed", d.command()[-2:] == ["--backend", "auto"]))
    o = Sidecar(torch_py, svc, Path("."), backend="ollama")
    results.append(("ollama -> toolbox Python", o.python != torch_py and "--backend" in o.command()))
    hf = Sidecar(torch_py, svc, Path("."), backend="hf")
    results.append(("hf -> configured torch Python", hf.python == torch_py and hf.command()[-1] == "hf"))
    results.append(("hf -> never wakes the local runtime", hf.wake_runtime() == "" and hf.runtime is None))
    none_b = Sidecar(torch_py, svc, Path("."), backend="none")
    results.append(("none -> never wakes the local runtime", none_b.wake_runtime() == ""))

    # Runtime wake against a fake manager (nothing is started for real).
    import ollama_manager as om

    class FakeMgr:
        made: list = []

        def __init__(self, state=om.INSTALLED_NOT_RUNNING, fail=False):
            self.state, self.fail, self.proc, self.started, self.stopped = state, fail, None, False, False
            FakeMgr.made.append(self)

        def status(self, required=None):
            return om.Status(self.state)

        def ensure_running(self, wait_s=30.0, progress=None):
            if self.fail:
                raise om.OllamaError("serve exited rc=1")
            self.started, self.proc, self.state = True, object(), om.RUNNING
            return om.Status(om.RUNNING)

        def stop_if_ours(self):
            self.stopped, self.proc = True, None

    a = Sidecar(torch_py, svc, Path("."), backend="auto")
    a.runtime_factory = FakeMgr
    note = a.wake_runtime()
    m = FakeMgr.made[-1]
    results.append(("auto + runtime asleep -> started hidden, remembered as ours",
                    note == "local runtime started" and m.started and a.runtime is m))
    a.stop()
    results.append(("stop() stops the runtime THIS sidecar started", m.stopped and a.runtime is None))
    r = Sidecar(torch_py, svc, Path("."), backend="ollama")
    r.runtime_factory = lambda: FakeMgr(state=om.RUNNING)
    results.append(("runtime already running -> not touched, not ours",
                    r.wake_runtime() == "" and r.runtime is None and not FakeMgr.made[-1].started))
    n = Sidecar(torch_py, svc, Path("."), backend="auto")
    n.runtime_factory = lambda: FakeMgr(state=om.NOT_INSTALLED)
    results.append(("runtime not installed -> a note, nothing installed",
                    n.wake_runtime() == "local runtime not installed" and not FakeMgr.made[-1].started))
    f = Sidecar(torch_py, svc, Path("."), backend="auto")
    f.runtime_factory = lambda: FakeMgr(fail=True)
    results.append(("runtime fails to start -> a note, never an exception",
                    f.wake_runtime().startswith("local runtime: OllamaError") and f.runtime is None))
    bogus = Sidecar(torch_py, svc, Path("."), backend="tensorrt")
    results.append(("unknown backend -> settings/auto, never the torch Python", bogus.backend in BACKENDS
                    and (bogus.backend == "hf" or bogus.python != torch_py)))
    for name, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    bad = sum(not ok for _, ok in results)
    print(f"sidecar selftest: {len(results) - bad}/{len(results)} passed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(_selftest() if "--selftest" in sys.argv else 0)
