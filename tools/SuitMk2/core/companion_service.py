"""companion_service.py - the local companion sidecar: realizer + eyes behind a localhost HTTP API (2026-09-23).

Why a sidecar: WingmanAI runs skills in its own bundled Python, which has no PyTorch. Rather than install a
multi-GB ML stack into the host app, the heavy parts run here, in their own env, and the skill talks to them over
127.0.0.1. If this service is down, the skill hears nothing back and stays silent; it never crashes and never
falls back to a canned line.

    GET  /health             -> {"ok", "headroom", "loaded", "backend", "device", "backend_info", "realizer": stats,
                                 "eyes": stats|null}   (answers even with NO backend: backend "none" = silence)
    POST /realize  {spec}    -> {"text": str|null}          (pair_realizer: two adapters, headroom-gated)
    POST /reload             -> {"ok", "backend", "device", ...}  drop loaded models, re-resolve the backend
    GET  /eyes               -> eyes.state() or {}          (scene facts; only ever looks while SC is focused)

Backends (--backend, default auto): ollama = suitmk2-* (tool-provisioned) or realizer-* (dev) in Ollama, the first
complete set in pair_realizer.MODEL_PREFIXES; stdlib HTTP only, so
this service runs under the toolbox's own Python with NO torch; hf = transformers+peft (needs the torch env);
auto = ollama if both models exist, else hf if importable, else none. See pair_realizer.resolve_backend.

Bound to 127.0.0.1 only. Nothing here listens on the network.

Run (toolbox Python, no torch needed):  python companion_service.py --backend auto
    --backend auto|ollama|hf|api|none  --ollama-url http://127.0.0.1:11434
    --port 7790  --presence present|occasional|curious|off  --glance (LOCAL vision glance via Ollama; nothing leaves the PC)
    --glance-model gemma3:4b
Selftest (fake realizer/eyes, real HTTP): ... companion_service.py --selftest
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

DEFAULT_PORT = 7790
MAX_BODY = 64 * 1024


class Service:
    def __init__(self, realizer: Any, eyes: Any = None, log=print):
        self.realizer, self.eyes, self.log = realizer, eyes, log
        self.started = time.time()

    def health(self) -> dict:
        r = self.realizer
        info = {}
        if hasattr(r, "backend_info"):
            try:
                info = r.backend_info()
            except Exception as e:      # /health must answer whatever state the realizer is in
                info = {"backend": None, "note": f"backend_info failed: {type(e).__name__}"}
        loaded = info.get("loaded")
        try:
            headroom = r.headroom_state() if hasattr(r, "headroom_state") else None
        except Exception:
            headroom = "TIGHT"
        return {"ok": True, "uptime_s": round(time.time() - self.started),
                "headroom": headroom,
                "loaded": any(loaded.values()) if isinstance(loaded, dict) else getattr(r, "_backend", None) is not None,
                "backend": info.get("backend"), "device": info.get("device"), "backend_info": info or None,
                "realizer": dict(getattr(r, "stats", {})),
                "eyes": vars(self.eyes.stats) if self.eyes is not None else None}

    def reload(self, backend: Optional[str] = None) -> dict:
        if not hasattr(self.realizer, "reload"):
            return {"ok": False, "error": "realizer has no reload"}
        if backend and hasattr(self.realizer, "set_backend"):
            info = self.realizer.set_backend(backend)       # the window switched API on/off: re-resolve by NAME
        else:
            info = self.realizer.reload()
        self.log(f"reload -> backend {info.get('backend')} {info.get('note') or ''}".rstrip())
        return {"ok": True, **info}

    def realize(self, spec: dict) -> Optional[str]:
        return self.realizer(spec)

    def eyes_state(self) -> dict:
        return self.eyes.state() if self.eyes is not None else {}

    def eyes_look(self, reason: str = "curiosity") -> dict:
        """A deliberate curiosity look (J 2026-09-24): eyes.look() describes the screen now, or None if it will not
        (game not in front, headroom TIGHT, budget spent, too soon). Blocks for the glance, a few seconds."""
        fn = getattr(self.eyes, "look", None) if self.eyes is not None else None
        try:
            return {"notable": fn(reason) if fn else None}
        except Exception as e:
            return {"notable": None, "error": f"{type(e).__name__}: {e}"[:160]}

    def eyes_burst(self) -> dict:
        """Weapons fire on screen? eyes.burst_confirm() never blocks: a fresh cached answer, or None while a ~1.5 s
        burst runs in the background. {"fire": True|False|None}. (muzzle-flash detector, 2026-09-23)"""
        fn = getattr(self.eyes, "burst_confirm", None) if self.eyes is not None else None
        try:
            return {"fire": fn() if fn is not None else None}
        except Exception:
            return {"fire": None}


def make_handler(svc: Service):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, obj: dict) -> None:
            body = json.dumps(obj, default=str).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/health":
                return self._send(200, svc.health())
            if self.path == "/eyes":
                return self._send(200, svc.eyes_state())
            if self.path == "/eyes/burst":
                return self._send(200, svc.eyes_burst())
            if self.path.startswith("/eyes/look"):
                from urllib.parse import urlparse, parse_qs
                reason = (parse_qs(urlparse(self.path).query).get("reason") or ["curiosity"])[0][:40]
                return self._send(200, svc.eyes_look(reason))
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            if self.path == "/reload":
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(n) or b"{}") if n else {}
                    b = body.get("backend") if isinstance(body, dict) else None
                    from sidecar import BACKENDS
                    if b is not None and b not in BACKENDS:
                        return self._send(400, {"ok": False, "error": f"unknown backend {str(b)[:20]!r}"})
                    return self._send(200, svc.reload(b))
                except Exception as e:
                    svc.log(f"reload error {type(e).__name__}: {e}")
                    return self._send(500, {"ok": False, "error": type(e).__name__})
            if self.path != "/realize":
                return self._send(404, {"error": "not found"})
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > MAX_BODY:
                return self._send(400, {"error": "bad length"})
            try:
                spec = json.loads(self.rfile.read(n))
                if not isinstance(spec, dict) or "speaker" not in spec or "claims" not in spec:
                    return self._send(400, {"error": "not a spec"})
                t0 = time.time()
                text = svc.realize(spec)
                svc.log(f"realize {spec.get('id')} {spec.get('speaker')} {time.time() - t0:.1f}s -> {text!r}")
                return self._send(200, {"text": text})
            except Exception as e:  # a bad spec must not take the service down
                svc.log(f"realize error {type(e).__name__}: {e}")
                return self._send(500, {"text": None, "error": f"{type(e).__name__}"})

        def log_message(self, fmt, *args):  # quiet; the realize log line is the useful one
            pass
    return Handler


class _ExclusiveServer(ThreadingHTTPServer):
    # HTTPServer sets allow_reuse_address, which on Windows is SO_REUSEADDR: a SECOND process can bind a port that
    # is already in use and requests split between them. Measured 2026-09-23: the selftest bound 7799 "fine" while
    # an orphaned supervisor (pid 16128) owned it and answered every request. Refuse to share; fail loudly instead.
    allow_reuse_address = False

    def server_bind(self):
        import socket
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def serve(svc: Service, port: int) -> ThreadingHTTPServer:
    httpd = _ExclusiveServer(("127.0.0.1", port), make_handler(svc))
    threading.Thread(target=httpd.serve_forever, name="companion_http", daemon=True).start()
    return httpd


# ---- the skill-side clients (stdlib only: they run inside WingmanAI's Python) --------------------------------
class RemoteRealizer:
    """callable(spec) -> str | None over HTTP. Any failure -> None (silence)."""

    def __init__(self, url: str = f"http://127.0.0.1:{DEFAULT_PORT}", timeout: float = 30.0):
        self.url, self.timeout = url.rstrip("/"), timeout

    def __call__(self, spec: dict) -> Optional[str]:
        import urllib.request
        try:
            req = urllib.request.Request(self.url + "/realize", data=json.dumps(spec).encode("utf-8"),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read()).get("text")
        except Exception:
            return None


class RemoteEyes:
    """state() over HTTP with a short timeout, so a slow service never stalls the ambient loop."""

    def __init__(self, url: str = f"http://127.0.0.1:{DEFAULT_PORT}", timeout: float = 0.5):
        self.url, self.timeout = url.rstrip("/"), timeout

    def state(self) -> dict:
        import urllib.request
        try:
            with urllib.request.urlopen(self.url + "/eyes", timeout=self.timeout) as r:
                return json.loads(r.read()) or {}
        except Exception:
            return {}

    def burst_confirm(self) -> Optional[bool]:
        """Weapons fire on screen: True / False, or None (no answer yet, or the service cannot look)."""
        import urllib.request
        try:
            with urllib.request.urlopen(self.url + "/eyes/burst", timeout=self.timeout) as r:
                v = (json.loads(r.read()) or {}).get("fire")
                return None if v is None else bool(v)
        except Exception:
            return None

    def look(self, reason: str = "curiosity", timeout: float = 90.0) -> Optional[str]:
        """Curiosity look over HTTP. Long timeout on purpose: the service runs a vision glance. Call it OFF the
        ambient thread (companion_core does, in a worker), never from the UI."""
        import urllib.request
        from urllib.parse import quote
        try:
            with urllib.request.urlopen(self.url + "/eyes/look?reason=" + quote(reason), timeout=timeout) as r:
                return (json.loads(r.read()) or {}).get("notable") or None
        except Exception:
            return None

    def stop(self) -> None:
        pass


def service_reload(url: str = f"http://127.0.0.1:{DEFAULT_PORT}", timeout: float = 120.0,
                   backend: Optional[str] = None) -> Optional[dict]:
    """POST /reload [{"backend": name}]. The timeout is long on purpose: a reload waits for an in-flight CPU
    generation to finish. With backend, the service switches to it (the window's API toggle)."""
    import urllib.request
    try:
        data = json.dumps({"backend": backend} if backend else {}).encode("utf-8")
        req = urllib.request.Request(url.rstrip("/") + "/reload", data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


def service_health(url: str = f"http://127.0.0.1:{DEFAULT_PORT}", timeout: float = 1.0) -> Optional[dict]:
    import urllib.request
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/health", timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


# ---- selftest ------------------------------------------------------------------------------------------------
def _selftest() -> int:
    results = []

    def case(name, cond):
        results.append((name, bool(cond)))

    class FakeRealizer:
        stats = {"asked": 0}

        def headroom_state(self):
            return "OK"

        def __call__(self, spec):
            self.stats["asked"] += 1
            if spec.get("id") == "boom":
                raise RuntimeError("realizer blew up")
            return f"line for {spec['speaker']}"

    class FakeEyes:
        class stats:
            ticks = 3

        def state(self):
            return {"scene": "hangar", "in_combat": False}

    httpd = serve(Service(FakeRealizer(), FakeEyes(), log=lambda m: None), 0)   # 0 = OS picks a free port
    port = httpd.server_address[1]
    url = f"http://127.0.0.1:{port}"
    spec = {"id": "t1", "speaker": "montaigne", "claims": [], "rhetoric": [], "required_values": [],
            "length_words": [4, 30], "interpretation": {"text": "x"}}
    h = service_health(url)
    case("health answers", h and h["ok"] and h["headroom"] == "OK")
    case("realize round-trips", RemoteRealizer(url)(spec) == "line for montaigne")
    case("eyes state round-trips", RemoteEyes(url).state() == {"scene": "hangar", "in_combat": False})
    case("realizer exception -> None, service survives", RemoteRealizer(url)(dict(spec, id="boom")) is None
         and service_health(url) is not None)
    import urllib.request
    try:
        urllib.request.urlopen(urllib.request.Request(url + "/realize", data=b'{"nope": 1}',
                                                      headers={"Content-Type": "application/json"}), timeout=2)
        bad = False
    except Exception:
        bad = True
    case("non-spec body rejected", bad)
    httpd.shutdown()
    httpd.server_close()
    case("service down -> realizer silent", RemoteRealizer(url, timeout=1)(spec) is None)
    case("service down -> eyes empty", RemoteEyes(url).state() == {})
    case("service down -> health None", service_health(url) is None)
    # Port collision must fail loudly, not silently share (the 7799 incident).
    blocker = serve(Service(FakeRealizer(), None, log=lambda m: None), 0)
    try:
        serve(Service(FakeRealizer(), None, log=lambda m: None), blocker.server_address[1])
        collided_quietly = True
    except OSError:
        collided_quietly = False
    blocker.shutdown(); blocker.server_close()
    case("a taken port is refused, not shared", not collided_quietly)
    t0 = time.time()
    RemoteEyes("http://10.255.255.1:9", timeout=0.5).state()
    case("dead eyes endpoint costs <= ~0.5s", time.time() - t0 < 1.5)
    # ---- real PairRealizer behind real HTTP, fake Ollama on port 0 (no model, no torch) ----
    import random
    import pair_realizer as pr
    quiet = lambda m: None  # noqa: E731
    # No backend at all: /health still answers, /realize is silence (200 + null), never a crash.
    none_r = pr.PairRealizer(resolver=lambda: pr.plan_for("none", HERE), headroom=lambda: "OK", log=quiet)
    s0 = serve(Service(none_r, None, log=quiet), 0)
    u0 = f"http://127.0.0.1:{s0.server_address[1]}"
    h0 = service_health(u0)
    case("no backend: /health answers", h0 and h0["ok"] and h0["backend"] == "none" and h0["loaded"] is False)
    case("no backend: /realize is silence", RemoteRealizer(u0)(pr._spec()) is None and service_health(u0) is not None)
    s0.shutdown(); s0.server_close()

    # /health reports backend + device, and device follows headroom.
    fo = pr.FakeOllama(models=pr.BOTH)
    head = {"v": "OK"}
    olr = pr.PairRealizer(backend="ollama", ollama_url=fo.url, headroom=lambda: head["v"], log=quiet)
    s1 = serve(Service(olr, None, log=quiet), 0)
    u1 = f"http://127.0.0.1:{s1.server_address[1]}"
    RemoteRealizer(u1)(pr._spec())
    h1 = service_health(u1)
    case("/health: backend ollama on gpu", h1["backend"] == "ollama" and h1["device"] == "gpu" and h1["loaded"])
    head["v"] = "TIGHT"
    RemoteRealizer(u1)(pr._spec())
    h1 = service_health(u1)
    case("/health: TIGHT -> device cpu", h1["device"] == "cpu" and fo.requests[-1]["options"].get("num_gpu") == 0)
    s1.shutdown(); s1.server_close()

    # /reload swaps the backend while /realize requests are in flight; none is dropped.
    rng = random.Random(5)
    phase = {"v": "A"}
    slow_a = pr._FakeBackend([lambda: pr.varied_line(rng)] * 10_000, device="cuda", delay=0.02)
    rs = pr.PairRealizer(resolver=lambda: (pr.BackendPlan("custom", lambda: slow_a, None) if phase["v"] == "A"
                                           else pr.plan_for("ollama", HERE, fo.url)),
                         headroom=lambda: "OK", log=quiet)
    s2 = serve(Service(rs, None, log=quiet), 0)
    u2 = f"http://127.0.0.1:{s2.server_address[1]}"
    case("/health before reload: custom", service_health(u2)["backend"] == "custom")
    outs = []

    def hammer(n):
        rr = RemoteRealizer(u2, timeout=30)
        for i in range(n):
            outs.append(rr(pr._spec(speaker=pr.SPEAKERS[i % 2])))
    before = len(fo.requests)
    ts = [threading.Thread(target=hammer, args=(10,)) for _ in range(4)]
    for t in ts:
        t.start()
    time.sleep(0.15)
    phase["v"] = "B"
    rl = service_reload(u2)
    for t in ts:
        t.join()
    case("/reload answers with the new backend", rl and rl["ok"] and rl["backend"] == "ollama")
    case("/reload mid-run: all 40 requests answered", len(outs) == 40 and all(outs))
    case("/reload mid-run: both backends served", slow_a.calls and len(fo.requests) > before and slow_a.closed)
    case("/reload never closes a backend mid-generation", not slow_a.closed_while_busy and not slow_a.used_after_close)
    h2 = service_health(u2)
    case("/health after reload: ollama, 1 reload", h2["backend"] == "ollama" and h2["realizer"]["reloads"] == 1)
    s2.shutdown(); s2.server_close()
    fo.stop()
    case("service + realizer never imported torch", "torch" not in sys.modules)

    for name, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    bad_n = sum(not ok for _, ok in results)
    print(f"companion_service selftest: {len(results) - bad_n}/{len(results)} passed")
    return 1 if bad_n else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--presence", default="present", choices=["off", "occasional", "present", "curious"])
    ap.add_argument("--glance", action="store_true", help="local vision glance (Ollama on 127.0.0.1)")
    ap.add_argument("--glance-model", default="gemma3:4b")
    ap.add_argument("--adapters", default=str(HERE.parent))
    ap.add_argument("--backend", default="auto", choices=["auto", "ollama", "hf", "api", "none"])
    ap.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return _selftest()

    def log(m):
        print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)

    from pair_realizer import PairRealizer
    realizer = PairRealizer(adapter_dir=Path(a.adapters), log=log, backend=a.backend, ollama_url=a.ollama_url)
    eyes = None
    if a.presence != "off":
        from eyes import Eyes, LocalGlance, SceneClassifier
        glance = None
        if a.glance:
            try:
                glance = LocalGlance(model=a.glance_model)
            except Exception as e:
                log(f"glance unavailable ({type(e).__name__}: {e}); local eyes only")
        keep = None
        try:
            from training_shots import default_store       # keeps nothing unless the pilot opted in
            keep = default_store().keep
        except Exception as e:
            log(f"training shots unavailable ({type(e).__name__}: {e})")
        eyes = Eyes(presence=a.presence, glance=glance, headroom=realizer.headroom_state,
                    classifier=SceneClassifier(HERE / "eyes_scenes.json"), on_glance=keep)
        eyes.run()
    serve(Service(realizer, eyes, log), a.port)
    b = realizer.backend_info()
    log(f"companion service on 127.0.0.1:{a.port} (backend {b['backend']}{' - ' + b['note'] if b['note'] else ''}, "
        f"presence {a.presence}, glance {'on' if a.glance else 'off'}, python {sys.executable})")
    try:
        while True:
            time.sleep(60)
            if eyes is not None:
                eyes.clf.save()
    except KeyboardInterrupt:
        pass
    finally:
        if eyes is not None:
            eyes.stop()
            eyes.clf.save()
        realizer.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
