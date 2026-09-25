"""ollama_manager.py - the SuitMk2 tool owns its local model runtime, so the player never has to touch Ollama (stdlib).

    m = OllamaManager()
    st = m.status(required=["suitmk2-elah", "suitmk2-montaigne"])
    st.state   -> NOT_INSTALLED | INSTALLED_NOT_RUNNING | RUNNING | MODELS_MISSING | READY
    m.ensure_running()             # start `ollama serve` hidden if installed but not answering; waits for the API
    m.install(progress, cancel)    # download the official OllamaSetup.exe, verify its signature, run it silently

States (evaluated in this order):
    RUNNING               the API answers and no model list was asked about
    READY                 the API answers and every `required` model is present
    MODELS_MISSING        the API answers, some `required` model is absent (st.missing names them)
    INSTALLED_NOT_RUNNING no API, but ollama.exe was found (%LOCALAPPDATA%\\Programs\\Ollama, or PATH)
    NOT_INSTALLED         no API and no ollama.exe

Rules carried from the rest of the tool:
  * Child processes are started with STARTUPINFO(SW_HIDE), NEVER CREATE_NO_WINDOW (segfaults PySide6 on py3.14;
    process_manager.py:28-38).
  * `ollama serve` we started is ours to stop (stop_if_ours); an Ollama that was already running is never touched.
  * install() refuses to run an installer whose Authenticode signature is not Valid. It is IMPLEMENTED but no test runs
    it for real: the selftest mocks the download and every subprocess.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

DEFAULT_URL = "http://127.0.0.1:11434"
INSTALLER_URL = "https://ollama.com/download/OllamaSetup.exe"
# Inno Setup silent flags; Ollama installs per-user (no elevation), then starts its tray app, which serves the API.
INSTALLER_ARGS = ("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-")
EXPECTED_SIGNER = "Ollama"          # substring of the Authenticode signer subject

NOT_INSTALLED = "NOT_INSTALLED"
INSTALLED_NOT_RUNNING = "INSTALLED_NOT_RUNNING"
RUNNING = "RUNNING"
MODELS_MISSING = "MODELS_MISSING"
READY = "READY"

Progress = Callable[[str, int, int, str], None]      # (stage, done, total, message)


class OllamaError(RuntimeError):
    pass


@dataclass
class Status:
    state: str
    version: Optional[str] = None
    exe: Optional[str] = None
    models: set = field(default_factory=set)
    missing: list = field(default_factory=list)

    def plain(self) -> str:
        """Player-facing wording: no product names."""
        return {NOT_INSTALLED: "The local brain is not installed yet.",
                INSTALLED_NOT_RUNNING: "The local brain is installed but asleep.",
                RUNNING: "The local brain is awake.",
                MODELS_MISSING: "Voices and brain need setting up.",
                READY: "Ready."}[self.state]


def _hidden_startupinfo():
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0          # SW_HIDE
    return si


def default_url() -> str:
    """OLLAMA_HOST if set (host, host:port or full URL), else the default. Mirrors how the CLI reads it."""
    h = os.environ.get("OLLAMA_HOST", "").strip()
    if not h:
        return DEFAULT_URL
    if "://" not in h:
        h = "http://" + h
    scheme, rest = h.split("://", 1)
    host = rest.split("/")[0]
    if host.startswith("0.0.0.0"):
        host = "127.0.0.1" + host[len("0.0.0.0"):]
    if ":" not in host:
        host += ":11434"
    return f"{scheme}://{host}"


class OllamaManager:
    def __init__(self, url: Optional[str] = None, *, exe_candidates: Optional[Iterable[Path]] = None,
                 which: Callable[[str], Optional[str]] = shutil.which, popen=subprocess.Popen,
                 run=subprocess.run, urlopen=urllib.request.urlopen, installer_url: str = INSTALLER_URL):
        self.url = (url or default_url()).rstrip("/")
        self._exe_candidates = exe_candidates
        self._which, self._popen, self._run, self._urlopen = which, popen, run, urlopen
        self.installer_url = installer_url
        self.proc: Optional[subprocess.Popen] = None
        self.log: list = []

    # ---- probing ------------------------------------------------------------------------------------------------
    def _get(self, path: str, timeout: float = 2.0) -> Optional[dict]:
        try:
            with self._urlopen(self.url + path, timeout=timeout) as r:
                return json.loads(r.read() or b"{}")
        except Exception:
            return None

    def version(self) -> Optional[str]:
        v = self._get("/api/version")
        return v.get("version") if v else None

    def models(self) -> Optional[set]:
        """Full names (name:tag) AND bare names of installed models; None if the API does not answer."""
        t = self._get("/api/tags", timeout=5.0)
        if t is None:
            return None
        out = set()
        for m in t.get("models", []):
            n = m.get("name") or m.get("model") or ""
            out.add(n)
            if n.endswith(":latest"):
                out.add(n[: -len(":latest")])
        return out

    def find_exe(self) -> Optional[str]:
        cands = list(self._exe_candidates) if self._exe_candidates is not None else [
            Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "Programs" / "Ollama"
            / "ollama.exe",
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Ollama" / "ollama.exe",
        ]
        for c in cands:
            if Path(c).is_file():
                return str(c)
        return self._which("ollama")

    def status(self, required: Optional[Iterable[str]] = None) -> Status:
        ver = self.version()
        if ver is not None:
            models = self.models() or set()
            if required is None:
                return Status(RUNNING, ver, None, models)
            missing = [m for m in required if m not in models and f"{m}:latest" not in models]
            return Status(MODELS_MISSING if missing else READY, ver, None, models, missing)
        exe = self.find_exe()
        return Status(INSTALLED_NOT_RUNNING if exe else NOT_INSTALLED, None, exe)

    # ---- start / stop -------------------------------------------------------------------------------------------
    def ensure_running(self, wait_s: float = 30.0, progress: Optional[Progress] = None) -> Status:
        st = self.status()
        if st.state == RUNNING:
            return st
        if st.state == NOT_INSTALLED:
            raise OllamaError("not installed")
        env = dict(os.environ)
        if self.url != DEFAULT_URL:
            env["OLLAMA_HOST"] = self.url.split("://", 1)[1]
        if progress:
            progress("start", 0, 1, "Waking the local brain")
        try:
            self.proc = self._popen([st.exe, "serve"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, startupinfo=_hidden_startupinfo(), env=env)
        except OSError as e:
            raise OllamaError(f"could not start {st.exe}: {e}") from e
        t0 = time.time()
        while time.time() - t0 < wait_s:
            if self.version() is not None:
                if progress:
                    progress("start", 1, 1, "Awake")
                return self.status()
            if self.proc.poll() is not None:
                # another instance may have won the port race; that is fine if the API now answers
                if self.version() is not None:
                    self.proc = None
                    return self.status()
                raise OllamaError(f"ollama serve exited rc={self.proc.returncode}")
            time.sleep(0.25)
        raise OllamaError(f"ollama serve started but the API did not answer in {wait_s:.0f}s")

    def stop_if_ours(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.kill()
        self.proc = None

    # ---- install --------------------------------------------------------------------------------------------------
    def download_installer(self, dest: Path, progress: Optional[Progress] = None,
                           cancel: Optional[threading.Event] = None) -> Path:
        tmp = dest.with_suffix(dest.suffix + ".part")
        req = urllib.request.Request(self.installer_url, headers={"User-Agent": "SuitMk2"})
        with self._urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            while True:
                if cancel is not None and cancel.is_set():
                    raise OllamaError("cancelled")
                b = r.read(1 << 20)
                if not b:
                    break
                f.write(b)
                done += len(b)
                if progress:
                    progress("download", done, total, "Downloading the local brain")
        if total and done != total:
            tmp.unlink(missing_ok=True)
            raise OllamaError(f"download truncated: {done} of {total} bytes")
        os.replace(tmp, dest)
        return dest

    def signature_ok(self, exe: Path) -> tuple:
        """(ok, signer). Authenticode via PowerShell: Status must be Valid and the signer must name Ollama."""
        ps = ("$s = Get-AuthenticodeSignature -LiteralPath $args[0]; "
              "Write-Output ($s.Status.ToString() + '|' + $s.SignerCertificate.Subject)")
        try:
            r = self._run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps, str(exe)],
                          capture_output=True, text=True, timeout=60, startupinfo=_hidden_startupinfo())
        except Exception as e:
            return False, f"signature check failed: {e}"
        status, _, subject = (r.stdout or "").strip().partition("|")
        return status == "Valid" and EXPECTED_SIGNER.lower() in subject.lower(), subject or status

    def install(self, progress: Optional[Progress] = None, cancel: Optional[threading.Event] = None,
                workdir: Optional[Path] = None, wait_s: float = 120.0) -> Status:
        """Download, verify, run silently, then wait for the API. Returns the final status."""
        st = self.status()
        if st.state != NOT_INSTALLED:
            return st
        work = Path(workdir or tempfile.mkdtemp(prefix="suitmk2_setup_"))
        work.mkdir(parents=True, exist_ok=True)
        exe = self.download_installer(work / "OllamaSetup.exe", progress, cancel)
        ok, signer = self.signature_ok(exe)
        if not ok:
            exe.unlink(missing_ok=True)
            raise OllamaError(f"installer signature rejected ({signer})")
        if progress:
            progress("install", 0, 1, "Installing the local brain")
        r = self._run([str(exe), *INSTALLER_ARGS], startupinfo=_hidden_startupinfo(), timeout=900)
        exe.unlink(missing_ok=True)
        if getattr(r, "returncode", 1) != 0:
            raise OllamaError(f"installer exited rc={r.returncode}")
        if progress:
            progress("install", 1, 1, "Installed")
        t0 = time.time()                    # the installer launches the tray app, which serves the API
        while time.time() - t0 < wait_s:
            s = self.status()
            if s.state == RUNNING:
                return s
            if s.state == INSTALLED_NOT_RUNNING and time.time() - t0 > 10:
                return self.ensure_running(progress=progress)
            time.sleep(0.5)
        return self.status()


# ================================================================================================================
# selftest: a fake Ollama on port 0, fake executables, mocked download/subprocess. Nothing real is started.
# ================================================================================================================
class FakeOllama:
    """Minimal stand-in for the endpoints the manager + provisioner use. `models` maps name -> dict(from_blob,
    model_info). Hooks let tests inject failures."""

    def __init__(self, models: Optional[dict] = None, version: str = "0.34.2"):
        import hashlib
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        self.models = dict(models or {})
        self.blobs: dict = {}
        self.version = version
        self.pull_script: dict = {}          # name -> list of NDJSON dicts to stream (last may be {"error": ...})
        self.create_error: Optional[str] = None
        self.pull_blob, self.pull_path = "sha256:" + "b" * 64, None   # what a successful pull installs
        self.created: list = []
        self.reset_on_existing_blob = True   # mimic Ollama answering early (and RST) when a blob already exists
        self.requests: list = []
        me = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _json(self, code, obj):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def _ndjson(self, lines):
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for obj in lines:
                    b = (json.dumps(obj) + "\n").encode()
                    self.wfile.write(f"{len(b):x}\r\n".encode() + b + b"\r\n")
                self.wfile.write(b"0\r\n\r\n")

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(n) if n else b""

            def do_HEAD(self):
                me.requests.append(("HEAD", self.path))
                if self.path.startswith("/api/blobs/"):
                    code = 200 if self.path[len("/api/blobs/"):] in me.blobs else 404
                    self.send_response(code)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.send_header("Content-Length", "0")
                    self.end_headers()

            def do_GET(self):
                me.requests.append(("GET", self.path))
                if self.path == "/api/version":
                    return self._json(200, {"version": me.version})
                if self.path == "/api/tags":
                    return self._json(200, {"models": [{"name": n if ":" in n else n + ":latest"}
                                                       for n in me.models]})
                if self.path == "/":
                    return self._json(200, {})
                self._json(404, {"error": "not found"})

            def do_DELETE(self):
                body = json.loads(self._body() or b"{}")
                me.requests.append(("DELETE", self.path, body))
                name = body.get("model", "")
                if me.models.pop(name, None) is None and me.models.pop(name.removesuffix(":latest"), None) is None:
                    return self._json(404, {"error": f"model '{name}' not found"})
                self._json(200, {})

            def do_POST(self):
                if self.path.startswith("/api/blobs/"):
                    dig = self.path[len("/api/blobs/"):]
                    me.requests.append(("POST", self.path))
                    if dig in me.blobs and me.reset_on_existing_blob:
                        self.send_response(200)
                        self.send_header("Content-Length", "0")
                        self.send_header("Connection", "close")
                        self.end_headers()
                        self.close_connection = True
                        return
                    data = self._body()
                    got = "sha256:" + hashlib.sha256(data).hexdigest()
                    if got != dig:
                        return self._json(400, {"error": f"digest mismatch, expected {dig!r}, got {got!r}"})
                    me.blobs[dig] = data
                    self.send_response(201)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = json.loads(self._body() or b"{}")
                me.requests.append(("POST", self.path, body))
                if self.path == "/api/show":
                    m = me.models.get(body.get("model", "").removesuffix(":latest"))
                    if m is None:
                        return self._json(404, {"error": f"model '{body.get('model')}' not found"})
                    frm = m.get("from_path") or "C:\\fake\\blobs\\" + str(m["from_blob"]).replace(":", "-")
                    return self._json(200, {"modelfile": f"# generated\nFROM {frm}\nTEMPLATE {{{{ .Prompt }}}}\n",
                                            "model_info": m.get("model_info", {}), "details": {}})
                if self.path == "/api/pull":
                    name = body.get("model")
                    script = me.pull_script.get(name, [{"status": "pulling manifest"},
                                                       {"status": "pulling x", "digest": "sha256:x", "total": 100,
                                                        "completed": 100}, {"status": "success"}])
                    if script and "error" not in script[-1] and script[-1].get("status") == "success":
                        me.models[name] = {"from_blob": me.pull_blob, "from_path": me.pull_path, "model_info": {}}
                    return self._ndjson(script)
                if self.path == "/api/create":
                    if "adapters" in body:
                        return self._json(400, {"error": "LoRA adapters are no longer supported"})
                    if me.create_error:
                        return self._ndjson([{"status": "parsing GGUF"}, {"error": me.create_error}])
                    files = body.get("files") or {}
                    for dig in files.values():
                        if dig not in me.blobs:
                            return self._json(400, {"error": f"blob {dig} not found"})
                    frm = next(iter(files.values()), None) or body.get("from")
                    me.models[body["model"].removesuffix(":latest")] = {"from_blob": frm, "model_info": {}}
                    me.created.append(body)
                    return self._ndjson([{"status": "parsing GGUF"}, {"status": "writing manifest"},
                                         {"status": "success"}])
                self._json(404, {"error": "not found"})

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()


class _FakeProc:
    def __init__(self, on_start=None, rc=None):
        self.returncode = rc
        if on_start:
            on_start()

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9


class _FakeResp:
    def __init__(self, data: bytes, length: Optional[int] = None):
        import io
        self._b = io.BytesIO(data)
        self.headers = {"Content-Length": str(len(data) if length is None else length)}

    def read(self, n=-1):
        return self._b.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _selftest() -> int:
    results = []

    def case(name, ok):
        results.append((name, bool(ok)))

    dead = "http://127.0.0.1:9"                      # discard port: nothing answers
    td = Path(tempfile.mkdtemp(prefix="suitmk2_om_test_"))
    fake_exe = td / "ollama.exe"
    fake_exe.write_bytes(b"MZ")

    # 1. NOT_INSTALLED: no API, no exe anywhere
    m = OllamaManager(dead, exe_candidates=[td / "nope.exe"], which=lambda _: None)
    case("no API + no exe -> NOT_INSTALLED", m.status().state == NOT_INSTALLED)
    try:
        m.ensure_running()
        case("ensure_running on NOT_INSTALLED raises", False)
    except OllamaError:
        case("ensure_running on NOT_INSTALLED raises", True)

    # 2. INSTALLED_NOT_RUNNING via candidate path, and via PATH
    m = OllamaManager(dead, exe_candidates=[fake_exe], which=lambda _: None)
    st = m.status()
    case("exe at install path -> INSTALLED_NOT_RUNNING", st.state == INSTALLED_NOT_RUNNING and st.exe == str(fake_exe))
    m = OllamaManager(dead, exe_candidates=[], which=lambda _: r"C:\bin\ollama.exe")
    case("exe on PATH -> INSTALLED_NOT_RUNNING", m.status().state == INSTALLED_NOT_RUNNING)

    # 3. RUNNING / MODELS_MISSING / READY against the fake server
    fo = FakeOllama(models={"qwen2.5:1.5b": {"from_blob": "sha256:" + "a" * 64}})
    m = OllamaManager(fo.url)
    case("API up, no requirement -> RUNNING", m.status().state == RUNNING and m.status().version == "0.34.2")
    st = m.status(["suitmk2-elah", "suitmk2-montaigne"])
    case("API up, models absent -> MODELS_MISSING (names both)", st.state == MODELS_MISSING
         and st.missing == ["suitmk2-elah", "suitmk2-montaigne"])
    fo.models["suitmk2-elah"] = fo.models["suitmk2-montaigne"] = {"from_blob": "sha256:c"}
    case("API up, models present (:latest) -> READY", m.status(["suitmk2-elah", "suitmk2-montaigne"]).state == READY)

    # 4. Not running -> ensure_running starts it HIDDEN, then RUNNING. The fake Popen "starts" the fake server.
    fo2_holder = {}
    calls = []

    def fake_popen(args, **kw):
        calls.append((args, kw))
        fo2_holder["srv"] = FakeOllama()
        m2.url = fo2_holder["srv"].url         # the server comes up at the address the manager polls
        return _FakeProc()

    m2 = OllamaManager(dead, exe_candidates=[fake_exe], which=lambda _: None, popen=fake_popen)
    case("before start: INSTALLED_NOT_RUNNING", m2.status().state == INSTALLED_NOT_RUNNING)
    st = m2.ensure_running(wait_s=5)
    args, kw = calls[0]
    si = kw.get("startupinfo")
    case("ensure_running: `<exe> serve`", args == [str(fake_exe), "serve"])
    case("ensure_running: SW_HIDE startupinfo, no CREATE_NO_WINDOW",
         si is not None and si.wShowWindow == 0 and not (kw.get("creationflags", 0) & 0x08000000))
    case("after start: RUNNING", st.state == RUNNING)
    m2.stop_if_ours()
    case("stop_if_ours stops the process it started", m2.proc is None)
    fo2_holder["srv"].stop()

    # 5. serve exits immediately and nothing answers -> OllamaError, not a hang
    m3 = OllamaManager(dead, exe_candidates=[fake_exe], which=lambda _: None, popen=lambda *a, **k: _FakeProc(rc=1))
    try:
        m3.ensure_running(wait_s=3)
        case("serve exits rc=1 -> OllamaError", False)
    except OllamaError as e:
        case("serve exits rc=1 -> OllamaError", "rc=1" in str(e))

    # 6. install(): mocked download, mocked signature + installer; after "install" the API comes up.
    payload = b"MZ" + b"x" * 5000
    prog = []
    ran = []
    state = {"installed": False}

    def fake_urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else req
        if url == INSTALLER_URL:
            return _FakeResp(payload)
        if state["installed"]:
            return urllib.request.urlopen(fo.url + url[len(dead):], timeout=timeout)
        raise urllib.error.URLError("refused")

    def fake_run(args, **kw):
        ran.append(args)
        if args[0] == "powershell":
            class R: stdout = "Valid|CN=Ollama Inc., O=Ollama Inc., C=US"; returncode = 0
            return R()
        state["installed"] = True

        class R2: returncode = 0
        return R2()

    exe_present = {"v": False}
    m4 = OllamaManager(dead, exe_candidates=[], which=lambda _: str(fake_exe) if exe_present["v"] else None,
                       run=fake_run, urlopen=fake_urlopen)
    case("install: starts NOT_INSTALLED", m4.status().state == NOT_INSTALLED)
    st = m4.install(progress=lambda *a: prog.append(a), workdir=td / "dl", wait_s=5)
    case("install: download progress reached total", any(p[0] == "download" and p[1] == p[2] == len(payload)
                                                         for p in prog))
    case("install: signature checked before running", ran[0][0] == "powershell"
         and ran[1][1:] == list(INSTALLER_ARGS))
    case("install: installer file removed afterwards", not (td / "dl" / "OllamaSetup.exe").exists())
    case("install: ends RUNNING", st.state == RUNNING)

    # 7. install(): bad signature -> refused, installer never run
    ran.clear()
    state["installed"] = False

    def bad_sig_run(args, **kw):
        ran.append(args)

        class R: stdout = "HashMismatch|CN=Evil"; returncode = 0
        return R()
    m5 = OllamaManager(dead, exe_candidates=[], which=lambda _: None, run=bad_sig_run, urlopen=fake_urlopen)
    try:
        m5.install(workdir=td / "dl2", wait_s=1)
        case("install: bad signature refused", False)
    except OllamaError as e:
        case("install: bad signature refused", "signature" in str(e) and len(ran) == 1)

    # 8. install(): truncated download detected
    m6 = OllamaManager(dead, exe_candidates=[], which=lambda _: None, run=fake_run,
                       urlopen=lambda req, timeout=None: _FakeResp(payload, length=len(payload) + 10))
    try:
        m6.install(workdir=td / "dl3", wait_s=1)
        case("install: truncated download refused", False)
    except OllamaError as e:
        case("install: truncated download refused", "truncated" in str(e))

    # 9. install(): cancel honoured
    ev = threading.Event()
    ev.set()
    m7 = OllamaManager(dead, exe_candidates=[], which=lambda _: None, run=fake_run,
                       urlopen=lambda req, timeout=None: _FakeResp(payload))
    try:
        m7.install(workdir=td / "dl4", cancel=ev, wait_s=1)
        case("install: cancel honoured", False)
    except OllamaError as e:
        case("install: cancel honoured", "cancelled" in str(e))

    # 10. install() is a no-op when already installed
    m8 = OllamaManager(fo.url)
    case("install: no-op when running", m8.install().state == RUNNING)

    # 11. OLLAMA_HOST parsing
    old = os.environ.get("OLLAMA_HOST")
    for val, want in (("0.0.0.0", "http://127.0.0.1:11434"), ("127.0.0.1:5000", "http://127.0.0.1:5000"),
                      ("http://localhost:7000", "http://localhost:7000")):
        os.environ["OLLAMA_HOST"] = val
        case(f"OLLAMA_HOST={val} -> {want}", default_url() == want)
    if old is None:
        os.environ.pop("OLLAMA_HOST", None)
    else:
        os.environ["OLLAMA_HOST"] = old
    case("plain() wording carries no product name", all("ollama" not in Status(s).plain().lower()
                                                        for s in (NOT_INSTALLED, INSTALLED_NOT_RUNNING, RUNNING,
                                                                  MODELS_MISSING, READY)))
    fo.stop()
    shutil.rmtree(td, ignore_errors=True)
    for name, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    bad = sum(not ok for _, ok in results)
    print(f"ollama_manager selftest: {len(results) - bad}/{len(results)} passed")
    return 1 if bad else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    st = OllamaManager().status()
    print(st.state, st.version or "", st.exe or "", f"{len(st.models)} models" if st.models else "")
