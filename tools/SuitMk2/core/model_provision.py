"""model_provision.py - put the character models into the local Ollama over its HTTP API, no CLI (stdlib only).

    p = Provisioner(progress=cb)                 # cb(stage, done, total, message); p.overall is 0..1 for one bar
    p.run()                                      # -> {"elah": "created"|"up to date", "montaigne": ...}
    p.run(include_vision=True)                   # also pulls gemma3:4b (opt-in ONLY)

WHAT SHIPS: models/<speaker>.delta.gguf (~164 MB each) - the character LoRA pre-merged into the attention tensors
(build_character_delta.py). NOT a LoRA: Ollama 0.34.2 answers every adapter create with
400 "LoRA adapters are no longer supported" (probed 2026-09-23). gguf_stitch splices the delta into the base.

PER CHARACTER, every step idempotent, so an interrupted run simply resumes at the first step not yet done:
  1. base      POST /api/pull {"model": BASE_TAG, "stream": true} only if /api/tags lacks it (Ollama resumes partial
               pulls itself). Streamed NDJSON {"status","digest","total","completed"} -> progress; {"error"} -> PullError.
  2. locate    POST /api/show {"model": BASE_TAG} -> the "FROM <path>" line of its modelfile is the base GGUF blob on
               disk, as the SERVER sees it (honours a custom OLLAMA_MODELS). Its sha256 is in the file name.
  3. skip?     POST /api/show {"model": <name>}: if its FROM blob digest equals the stitched digest -> "up to date".
               The stitched digest is cached in provision_state.json keyed by (base digest, delta sha256), so a
               re-check costs one delta hash (~0.2 s), not a 1 GB pass.
  4. hash      pass 1 over the stitched stream (never written to disk) -> sha256:<hex>
  5. upload    HEAD /api/blobs/<digest> -> 200 skip | 404 -> POST the stream with Content-Length (201 = stored).
               QUIRK: a POST for a blob that already exists is answered 200 EARLY and the connection is reset while
               we are still sending (measured). So: HEAD first, and after any connection error, HEAD again and
               trust that answer.  Disk: refused up front if the models drive lacks size + 1 GB.
  6. create    POST /api/create {"model", "files": {"<speaker>.gguf": digest}, "template": "{{ .Prompt }}",
               "parameters": {"temperature": 0}, "stream": true} -> NDJSON statuses, {"error"} -> CreateError.
               ("from" + "adapters" is the shape Ollama refuses; "files" is the one it takes.)
Raw-mode generation (pair_realizer) ignores the template, but "{{ .Prompt }}" keeps any non-raw caller honest too.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import logging
import os
import re
import shutil
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Iterator, Optional

_LOG = logging.getLogger("suitmk2.provision")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from gguf_stitch import GGUFError, Stitch, file_sha256  # noqa: E402
from ollama_manager import DEFAULT_URL, OllamaManager, default_url  # noqa: E402

BASE_TAG = "qwen2.5:1.5b"                 # Qwen2.5-1.5B-Instruct Q4_K_M, 986 MB (verified via /api/show)
VISION_TAG = "gemma3:4b"
# The Toolbox Assistant's own small model (tools/Assistant/assistant/config.py, LLMConfig.model). The Assistant is a
# tab of the same window and nothing else fetches this for it, so the one setup click does. The size is the sum of
# the layers in the registry manifest (read 2026-10-06): 397,821,319 bytes.
ASSISTANT_TAG = "qwen2.5:0.5b"
ASSISTANT_BYTES = 397_821_319
ASSISTANT_LABEL = "Downloading the Assistant's brain"
ASSISTANT_CONFIG = Path.home() / ".sctoolbox" / "assistant_llm.json"
SPEAKERS = ("elah", "montaigne")
MODEL_PREFIX = "suitmk2-"
MODELS_DIR = HERE.parent / "models"
STATE_PATH = Path.home() / ".sctoolbox" / "suitmk2" / "provision_state.json"
TEMPLATE = "{{ .Prompt }}"
PARAMETERS = {"temperature": 0}
DISK_MARGIN = 1 << 30

Progress = Callable[[str, int, int, str], None]


class ProvisionError(RuntimeError):
    pass


class PullError(ProvisionError):
    pass


class UploadError(ProvisionError):
    pass


class CreateError(ProvisionError):
    pass


class Cancelled(ProvisionError):
    pass


def assistant_tag_wanted(path: Optional[Path | str] = None) -> Optional[str]:
    """The model the Toolbox Assistant would ask this PC's runtime for, or None when it would not ask it for
    ASSISTANT_TAG: its settings file says router only, another service, or another model (then it is the player's
    own choice and nothing is fetched for it). No settings file is the Assistant's defaults, which is ASSISTANT_TAG.
    Reads one small file and never raises."""
    try:
        cfg = json.loads(Path(path or ASSISTANT_CONFIG).read_text(encoding="utf-8"))
    except Exception:
        return ASSISTANT_TAG
    if not isinstance(cfg, dict):
        return ASSISTANT_TAG
    if str(cfg.get("mode") or "").strip().lower() == "router":
        return None
    if str(cfg.get("provider") or "openai").strip().lower() != "openai":
        return None
    host = urllib.parse.urlsplit(str(cfg.get("base_url") or DEFAULT_URL))
    if (host.hostname or "").lower() not in ("127.0.0.1", "localhost") or (host.port or 0) != 11434:
        return None
    return ASSISTANT_TAG if str(cfg.get("model") or ASSISTANT_TAG).strip().lower() == ASSISTANT_TAG else None


def model_name(speaker: str, prefix: str = MODEL_PREFIX) -> str:
    return prefix + speaker


def _digest_from_from_line(modelfile: str) -> tuple:
    """('sha256:<hex>', path) from the FROM line of an /api/show modelfile, or (None, None)."""
    for line in modelfile.splitlines():
        if line.startswith("FROM "):
            path = line[5:].strip()
            m = re.search(r"sha256[-:]([0-9a-f]{64})", path)
            return ("sha256:" + m.group(1) if m else None), path
    return None, None


class Provisioner:
    def __init__(self, url: Optional[str] = None, models_dir: Path | str = MODELS_DIR, *,
                 prefix: str = MODEL_PREFIX, speakers=SPEAKERS, base_tag: str = BASE_TAG,
                 state_path: Path | str = STATE_PATH, progress: Optional[Progress] = None,
                 cancel: Optional[threading.Event] = None, timeout: float = 30.0):
        self.url = (url or default_url()).rstrip("/")
        u = urllib.parse.urlsplit(self.url)
        self.host, self.port = u.hostname or "127.0.0.1", u.port or 11434
        self.models_dir, self.prefix, self.speakers = Path(models_dir), prefix, tuple(speakers)
        self.base_tag, self.state_path, self.timeout = base_tag, Path(state_path), timeout
        self._progress, self.cancel = progress, cancel or threading.Event()
        self.overall = 0.0
        self._w0, self._w1 = 0.0, 1.0          # current step's slice of the overall bar
        self.log: list = []
        self._progress_broken = False          # a failing UI callback is reported once, not per NDJSON line

    # ---- plumbing ---------------------------------------------------------------------------------------------
    def _emit(self, stage: str, done: int, total: int, msg: str) -> None:
        frac = (done / total) if total else 0.0
        self.overall = self._w0 + (self._w1 - self._w0) * min(1.0, frac)
        if self._progress:
            try:
                self._progress(stage, done, total, msg)
            except Exception as e:
                # LEFT BROAD DELIBERATELY. self._progress is caller-supplied UI code (a Qt slot touching widgets
                # that may already be destroyed), so it can raise anything, and _emit is called from inside the
                # streamed pull / hash / upload loops. An escape would abort a multi-gigabyte provisioning run
                # because a PROGRESS BAR failed - and the run's own steps are idempotent, so the correct response
                # to a broken bar is to keep going blind, not to stop.
                # Reported once per Provisioner: this runs per NDJSON status line, thousands of times per pull.
                if not self._progress_broken:
                    self._progress_broken = True
                    _LOG.warning("provision: the progress callback raised (%s: %s); provisioning continues but the "
                                 "UI will not update again", type(e).__name__, e, exc_info=True)
                    self.log.append(f"progress callback broken: {type(e).__name__}: {e}")

    def _slice(self, a: float, b: float) -> None:
        self._w0, self._w1 = a, b
        self.overall = a

    def _check_cancel(self) -> None:
        if self.cancel.is_set():
            raise Cancelled("cancelled")

    def _post(self, path: str, body: dict, timeout: Optional[float] = None) -> dict:
        req = urllib.request.Request(self.url + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
            raw = r.read()
            return json.loads(raw) if raw.strip() else {}

    def _stream(self, path: str, body: dict, timeout: float = 600.0) -> Iterator[dict]:
        """POST and yield NDJSON objects. HTTP errors become {"error": msg} so callers handle one shape."""
        req = urllib.request.Request(self.url + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            r = urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read()).get("error", str(e))
            except Exception:
                msg = str(e)
            yield {"error": msg, "http": e.code}
            return
        with r:
            for line in r:
                self._check_cancel()
                line = line.strip()
                if line:
                    yield json.loads(line)

    def _show(self, model: str) -> Optional[dict]:
        try:
            return self._post("/api/show", {"model": model})
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise

    def _tags(self) -> set:
        with urllib.request.urlopen(self.url + "/api/tags", timeout=self.timeout) as r:
            names = {m["name"] for m in json.loads(r.read()).get("models", [])}
        return names | {n[: -len(":latest")] for n in names if n.endswith(":latest")}

    def _state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_state(self, st: dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=1), encoding="utf-8")
        os.replace(tmp, self.state_path)

    # ---- steps ------------------------------------------------------------------------------------------------
    def pull(self, tag: str, label: str = "Downloading the brain") -> str:
        """Ensure `tag` is present; pull it with streamed progress if not. Returns 'present' or 'pulled'."""
        if tag in self._tags():
            self._emit("pull", 1, 1, f"{label}: already here")
            return "present"
        layers: dict = {}
        last = None
        for ev in self._stream("/api/pull", {"model": tag, "stream": True}, timeout=3600):
            if "error" in ev:
                raise PullError(f"{tag}: {ev['error']}")
            last = ev.get("status")
            if ev.get("digest") and ev.get("total"):
                layers[ev["digest"]] = (ev.get("completed", 0), ev["total"])
                done, total = sum(c for c, _ in layers.values()), sum(t for _, t in layers.values())
                self._emit("pull", done, total, label)
        if last != "success" or tag not in self._tags():
            raise PullError(f"{tag}: pull ended with status {last!r}")
        self._emit("pull", 1, 1, f"{label}: done")
        return "pulled"

    def locate_base(self) -> tuple:
        """(digest, path) of the base GGUF as the server stores it."""
        show = self._show(self.base_tag)
        if show is None:
            raise ProvisionError(f"{self.base_tag} not installed")
        digest, path = _digest_from_from_line(show.get("modelfile", ""))
        if not path or not Path(path).is_file():
            raise ProvisionError(f"cannot read the base model file ({path!r}); is the model store on this machine?")
        return digest, path

    def delta_path(self, speaker: str) -> Path:
        p = self.models_dir / f"{speaker}.delta.gguf"
        if not p.is_file():
            raise ProvisionError(f"missing {p.name} in {self.models_dir}")
        return p

    def verify_manifest(self, speaker: str, sha: str) -> None:
        mf = self.models_dir / "manifest.json"
        if not mf.is_file():
            return
        want = json.loads(mf.read_text(encoding="utf-8")).get(f"{speaker}.delta.gguf", {}).get("sha256")
        if want and want != sha:
            raise ProvisionError(f"{speaker}.delta.gguf is damaged (checksum mismatch); reinstall the tool")

    def stitched(self, speaker: str, base_digest: str, base_path: str) -> tuple:
        """(Stitch, stitched digest). Digest from cache when (base digest, delta sha) was hashed before."""
        dp = self.delta_path(speaker)
        dsha = file_sha256(dp)
        self.verify_manifest(speaker, dsha)
        s = Stitch(base_path, dp, {"suitmk2.delta.sha256": dsha, "suitmk2.base.digest": base_digest or ""})
        key = f"{speaker}|{base_digest}|{dsha}"
        st = self._state()
        dig = st.get("stitched", {}).get(key)
        if dig and st.get("sizes", {}).get(key) == s.size:
            return s, dig
        t0 = time.time()

        def prog(done, total):
            self._check_cancel()
            self._emit("hash", done, total, f"Preparing {speaker.title()}")
        dig = s.sha256(prog)
        st.setdefault("stitched", {})[key] = dig
        st.setdefault("sizes", {})[key] = s.size
        self._save_state(st)
        self.log.append(f"{speaker}: hashed {s.size / 1e9:.2f} GB in {time.time() - t0:.1f}s")
        return s, dig

    def blob_exists(self, digest: str) -> bool:
        c = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        try:
            c.request("HEAD", f"/api/blobs/{digest}")
            return c.getresponse().status == 200
        finally:
            c.close()

    def upload(self, stitch: Stitch, digest: str, speaker: str) -> str:
        if self.blob_exists(digest):
            self._emit("upload", 1, 1, f"Installing {speaker.title()}: already uploaded")
            return "present"
        base_dir = Path(stitch.base.path).parent
        try:
            free = shutil.disk_usage(base_dir).free
            if free < stitch.size + DISK_MARGIN:
                raise ProvisionError(f"not enough disk space: need {(stitch.size + DISK_MARGIN) / 1e9:.1f} GB, "
                                     f"{free / 1e9:.1f} GB free")
        except OSError:
            pass
        sent = [0]

        def body():
            for b in stitch.chunks():
                self._check_cancel()
                sent[0] += len(b)
                self._emit("upload", sent[0], stitch.size, f"Installing {speaker.title()}")
                yield b

        c = http.client.HTTPConnection(self.host, self.port, timeout=600)
        try:
            c.request("POST", f"/api/blobs/{digest}", body=body(),
                      headers={"Content-Length": str(stitch.size), "Content-Type": "application/octet-stream"})
            r = c.getresponse()
            payload = r.read()
            if r.status not in (200, 201):
                try:
                    msg = json.loads(payload).get("error", payload[:200])
                except Exception:
                    msg = payload[:200]
                raise UploadError(f"upload refused ({r.status}): {msg}")
        except Cancelled:
            raise
        except (ConnectionError, http.client.HTTPException, OSError) as e:
            if not self.blob_exists(digest):          # the early-200-then-reset quirk: trust HEAD, not the socket
                raise UploadError(f"upload interrupted: {type(e).__name__}: {e}") from e
        finally:
            c.close()
        if not self.blob_exists(digest):
            raise UploadError("upload finished but the blob is not there")
        return "uploaded"

    def create(self, name: str, digest: str, speaker: str) -> None:
        body = {"model": name, "files": {f"{speaker}.gguf": digest}, "template": TEMPLATE,
                "parameters": PARAMETERS, "stream": True}
        last = None
        for ev in self._stream("/api/create", body, timeout=900):
            if "error" in ev:
                raise CreateError(f"{name}: {ev['error']}")
            last = ev.get("status")
            self._emit("create", 0, 1, f"Finishing {speaker.title()}")
        if last != "success":
            raise CreateError(f"{name}: create ended with status {last!r}")
        self._emit("create", 1, 1, f"{speaker.title()} ready")

    def current_digest(self, name: str) -> Optional[str]:
        show = self._show(name)
        return None if show is None else _digest_from_from_line(show.get("modelfile", ""))[0]

    # ---- orchestration ---------------------------------------------------------------------------------------
    def provision_speaker(self, speaker: str, base_digest: str, base_path: str, a: float, b: float) -> str:
        name = model_name(speaker, self.prefix)
        self._slice(a, a + (b - a) * 0.35)
        s, dig = self.stitched(speaker, base_digest, base_path)
        self._check_cancel()
        if self.current_digest(name) == dig:
            self._slice(b, b)
            self._emit("create", 1, 1, f"{speaker.title()} already up to date")
            return "up to date"
        self._slice(a + (b - a) * 0.35, a + (b - a) * 0.9)
        self.upload(s, dig, speaker)
        self._check_cancel()
        self._slice(a + (b - a) * 0.9, b)
        self.create(name, dig, speaker)
        if self.current_digest(name) != dig:
            raise CreateError(f"{name}: created, but it does not point at the expected weights")
        return "created"

    def run(self, include_vision: bool = False, assistant_tag: Optional[str] = None) -> dict:
        """assistant_tag: also fetch the Toolbox Assistant's small model, after the characters and in the same bar.
        That one download failing does NOT fail the run: the characters are already in place, so the answer carries
        out["assistant"] = "failed" and out["assistant_error"], and the caller says so. Cancelled still stops."""
        out: dict = {}
        n = len(self.speakers)
        base_share = 0.3 if include_vision else 0.4
        vision_share = 0.2 if include_vision else 0.0
        assistant_share = 0.1 if assistant_tag else 0.0
        self._slice(0.0, base_share)
        out["base"] = self.pull(self.base_tag, "Downloading the brain")
        self._check_cancel()
        base_digest, base_path = self.locate_base()
        per = (1.0 - base_share - vision_share - assistant_share) / max(n, 1)
        for i, spk in enumerate(self.speakers):
            self._check_cancel()
            out[spk] = self.provision_speaker(spk, base_digest, base_path, base_share + i * per,
                                              base_share + (i + 1) * per)
        if assistant_tag:
            self._slice(1.0 - vision_share - assistant_share, 1.0 - vision_share)
            try:
                out["assistant"] = self.pull(assistant_tag, ASSISTANT_LABEL)
            except Cancelled:
                raise
            except Exception as e:                    # the companions are set up; this one download is not theirs
                out["assistant"], out["assistant_error"] = "failed", f"{type(e).__name__}: {e}"
                self.log.append(f"assistant model not fetched: {type(e).__name__}: {e}")
        if include_vision:
            self._slice(1.0 - vision_share, 1.0)
            out["vision"] = self.pull(VISION_TAG, "Downloading the eyes")
        self._slice(1.0, 1.0)
        self._emit("done", 1, 1, "Ready")
        return out

    def remove(self, speakers=None) -> list:
        """Delete this prefix's character models (tests / uninstall). Never touches other names."""
        gone = []
        for spk in speakers or self.speakers:
            name = model_name(spk, self.prefix)
            req = urllib.request.Request(self.url + "/api/delete", data=json.dumps({"model": name}).encode(),
                                         method="DELETE", headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout):
                    gone.append(name)
            except urllib.error.HTTPError:
                pass
        return gone


class SetupJob:
    """Everything the "Set up voices & brain" button does, in order, as one call with one progress bar:
    install the runtime if absent (opt-out with allow_install=False) -> wake it -> provision the characters
    (-> the vision model only if include_vision). Safe to re-run at any point; every step is idempotent.

        job = SetupJob(progress=cb)            # cb(stage, done, total, message); job.overall is 0..1
        job.run()                              # -> {"runtime": "...", "base": ..., "elah": ..., "montaigne": ...}
        job.cancel.set()                       # from the UI thread; the worker raises Cancelled at the next chunk
    """

    def __init__(self, progress: Optional[Progress] = None, *, include_vision: bool = False,
                 allow_install: bool = True, manager: Optional[OllamaManager] = None,
                 provisioner_kw: Optional[dict] = None, assistant_tag: Optional[str] = None):
        self.mgr = manager or OllamaManager()
        self.assistant_tag = assistant_tag
        self._progress, self.include_vision, self.allow_install = progress, include_vision, allow_install
        self.cancel = threading.Event()
        self.provisioner_kw = provisioner_kw or {}
        self.overall, self.message, self.stage = 0.0, "", ""
        self._split = 0.0
        self._progress_broken = False          # see Provisioner._emit: reported once, not per NDJSON line

    def _cb(self, stage: str, done: int, total: int, msg: str) -> None:
        self.stage, self.message = stage, msg
        if stage in ("download", "install", "start"):
            frac = (done / total) if total else 0.0
            self.overall = self._split * min(1.0, frac) if stage == "download" else self.overall
        if self._progress:
            try:
                self._progress(stage, done, total, msg)
            except Exception as e:
                # LEFT BROAD for the same reason as Provisioner._emit above: this is the first-run Setup panel's
                # own callback, arbitrary UI code, called per progress line during a runtime install and two model
                # builds. An escape would abort the whole first-run setup because the bar failed to paint.
                # self.stage / self.message / self.overall are already updated, so a UI that polls still recovers.
                if not self._progress_broken:
                    self._progress_broken = True
                    _LOG.warning("setup job: the progress callback raised (%s: %s); setup continues but the UI will "
                                 "not update again", type(e).__name__, e, exc_info=True)

    def run(self) -> dict:
        from ollama_manager import INSTALLED_NOT_RUNNING, NOT_INSTALLED, OllamaError
        out: dict = {}
        st = self.mgr.status()
        if st.state == NOT_INSTALLED:
            if not self.allow_install:
                raise ProvisionError("the local runtime is not installed")
            self._split = 0.35
            st = self.mgr.install(self._cb, self.cancel)
            out["runtime"] = "installed"
        if st.state == INSTALLED_NOT_RUNNING:
            try:
                st = self.mgr.ensure_running(progress=self._cb)
            except OllamaError as e:
                raise ProvisionError(f"could not start the local runtime: {e}") from e
            out.setdefault("runtime", "started")
        out.setdefault("runtime", "running")
        if self.cancel.is_set():
            raise Cancelled("cancelled")
        a = self._split
        p = Provisioner(self.mgr.url, progress=lambda *x: (setattr(self, "overall", a + (1 - a) * p.overall),
                                                             self._cb(*x)),
                        cancel=self.cancel, **self.provisioner_kw)
        out.update(p.run(include_vision=self.include_vision, assistant_tag=self.assistant_tag))
        self.overall = 1.0
        return out


def write_manifest(models_dir: Path | str = MODELS_DIR) -> dict:
    """BUILD TIME: record sha256 + size of each shipped delta so a damaged install is caught before upload."""
    d = Path(models_dir)
    man = {p.name: {"sha256": file_sha256(p), "size": p.stat().st_size} for p in sorted(d.glob("*.delta.gguf"))}
    (d / "manifest.json").write_text(json.dumps(man, indent=1), encoding="utf-8")
    return man


# ================================================================================================================
# selftest: fake Ollama (ollama_manager.FakeOllama) + synthetic GGUFs. No real models touched.
# ================================================================================================================
def _selftest() -> int:
    import tempfile
    from gguf_stitch import GGUFFile, write_test_gguf
    from ollama_manager import FakeOllama, OllamaManager, INSTALLED_NOT_RUNNING, RUNNING, READY, MODELS_MISSING
    results = []

    def case(name, ok):
        results.append((name, bool(ok)))

    td = Path(tempfile.mkdtemp(prefix="suitmk2_prov_test_"))
    a = bytes(range(256)) * 2
    q4 = b"\x11" * 144 * 2
    base_file = td / ("sha256-" + "b" * 64)
    write_test_gguf(base_file, {"general.architecture": "qwen2", "general.finetune": "Instruct"},
                    [("tok.weight", (128,), 0, a), ("blk.0.attn_q.weight", (256, 2), 12, q4),
                     ("blk.0.ffn.weight", (128,), 0, bytes(reversed(a)))])
    fp = GGUFFile(base_file).fingerprint()
    models = td / "models"
    models.mkdir()
    for i, spk in enumerate(SPEAKERS):
        write_test_gguf(models / f"{spk}.delta.gguf", {"suitmk2.delta.speaker": spk, "suitmk2.delta.arch": "qwen2",
                                                       "suitmk2.delta.base_fingerprint": fp,
                                                       "suitmk2.require.general.finetune": "Instruct"},
                        [("blk.0.attn_q.weight", (256, 2), 8, bytes([0x30 + i]) * 34 * 16)])
    write_manifest(models)

    def fresh_server(with_base: bool):
        fo = FakeOllama()
        fo.pull_path = str(base_file)
        fo.pull_blob = "sha256:" + "b" * 64
        if with_base:
            fo.models[BASE_TAG] = {"from_blob": fo.pull_blob, "from_path": str(base_file)}
        return fo

    def prov(fo, **kw):
        ev = []
        p = Provisioner(fo.url, models, state_path=td / f"state_{len(results)}.json",
                        progress=lambda *x: ev.append(x), **kw)
        return p, ev

    # 1. FRESH: no base, no models -> pull, hash, upload, create for both; progress monotone; overall ends at 1.0
    fo = fresh_server(with_base=False)
    p, ev = prov(fo)
    overall = []
    p._progress = lambda *x: (ev.append(x), overall.append(p.overall))
    r = p.run()
    case("fresh: base pulled, both created", r == {"base": "pulled", "elah": "created", "montaigne": "created"})
    case("fresh: create used files (not adapters), raw template", all(
        "files" in b and "adapters" not in b and b["template"] == TEMPLATE for b in fo.created))
    case("fresh: each model points at its uploaded blob", all(fo.models[f"suitmk2-{s}"]["from_blob"] in fo.blobs
                                                             for s in SPEAKERS))
    case("fresh: the two characters got different blobs",
         fo.models["suitmk2-elah"]["from_blob"] != fo.models["suitmk2-montaigne"]["from_blob"])
    up = fo.blobs[fo.models["suitmk2-elah"]["from_blob"]]
    s = Stitch(base_file, models / "elah.delta.gguf")
    case("fresh: uploaded bytes are a valid stitched GGUF", up[:4] == b"GGUF" and len(up) > s.size - 1)
    case("fresh: overall progress is monotone and ends at 1.0",
         overall == sorted(overall) and abs(overall[-1] - 1.0) < 1e-9)
    case("fresh: stages seen pull/hash/upload/create/done",
         {"pull", "hash", "upload", "create", "done"} <= {e[0] for e in ev})
    case("fresh: manager now says READY", OllamaManager(fo.url).status(
        [model_name(s) for s in SPEAKERS]).state == READY)

    # 2. ALREADY PROVISIONED: second run changes nothing (no upload, no create); cached digest, no re-hash
    n_created, n_posts = len(fo.created), sum(1 for q in fo.requests if q[0] == "POST" and "/api/blobs/" in q[1])
    p2 = Provisioner(fo.url, models, state_path=p.state_path)
    r2 = p2.run()
    case("already provisioned: both 'up to date'", r2 == {"base": "present", "elah": "up to date",
                                                         "montaigne": "up to date"})
    case("already provisioned: no create, no blob upload", len(fo.created) == n_created and n_posts == sum(
        1 for q in fo.requests if q[0] == "POST" and "/api/blobs/" in q[1]))
    case("already provisioned: digest came from the cache (no hash log line)", not p2.log)

    # 3. CHANGED DELTA (retrained character): only that one is rebuilt
    orig = (models / "montaigne.delta.gguf").read_bytes()
    write_test_gguf(models / "montaigne.delta.gguf", {"suitmk2.delta.speaker": "montaigne", "suitmk2.delta.arch": "qwen2",
                                                      "suitmk2.delta.base_fingerprint": fp},
                    [("blk.0.attn_q.weight", (256, 2), 8, b"\x77" * 34 * 16)])
    (models / "manifest.json").unlink()
    r3 = Provisioner(fo.url, models, state_path=p.state_path).run()
    case("changed delta: only montaigne rebuilt", r3["elah"] == "up to date" and r3["montaigne"] == "created")
    (models / "montaigne.delta.gguf").write_bytes(orig)
    write_manifest(models)
    fo.stop()

    # 4. PARTIAL: base present, elah blob uploaded but create never happened (crash between steps) -> resumes
    fo = fresh_server(with_base=True)
    p, _ = prov(fo)
    base_digest, base_path = p.locate_base()
    st, dig = p.stitched("elah", base_digest, base_path)
    p.upload(st, dig, "elah")
    posts_before = sum(1 for q in fo.requests if q[0] == "POST" and "/api/blobs/" in q[1])
    r = p.run()
    posts_after = sum(1 for q in fo.requests if q[0] == "POST" and "/api/blobs/" in q[1])
    case("partial: base 'present', both created", r == {"base": "present", "elah": "created", "montaigne": "created"})
    case("partial: elah blob NOT re-uploaded (HEAD said present), montaigne uploaded once",
         posts_after - posts_before == 1)

    # 5. EARLY-200-THEN-RESET quirk: uploading an existing blob without the HEAD pre-check must not fail
    p.blob_exists_orig = p.blob_exists
    calls = {"n": 0}

    def head_lies_once(d):
        calls["n"] += 1
        return False if calls["n"] == 1 else p.blob_exists_orig(d)
    p.blob_exists = head_lies_once
    try:
        res = p.upload(st, dig, "elah")
        case("upload of existing blob survives early-200/reset", res == "uploaded")
    except Exception as e:
        case(f"upload of existing blob survives early-200/reset ({type(e).__name__}: {e})", False)
    fo.stop()

    # 6. PULL FAILURE: error line mid-stream -> PullError, nothing created
    fo = fresh_server(with_base=False)
    fo.pull_script[BASE_TAG] = [{"status": "pulling manifest"},
                                {"status": "pulling x", "digest": "sha256:x", "total": 100, "completed": 40},
                                {"error": "max retries exceeded: connection reset"}]
    p, _ = prov(fo)
    try:
        p.run()
        case("pull failure -> PullError", False)
    except PullError as e:
        case("pull failure -> PullError", "max retries" in str(e) and not fo.created)
    # ...and a retry after the network recovers completes (resumable)
    del fo.pull_script[BASE_TAG]
    r = Provisioner(fo.url, models, state_path=p.state_path).run()
    case("pull failure then retry -> completes", r["elah"] == r["montaigne"] == "created")
    fo.stop()

    # 7. CREATE FAILURE: error in the create stream -> CreateError; blob kept; retry after fix only creates
    fo = fresh_server(with_base=True)
    fo.create_error = "invalid file magic"
    p, _ = prov(fo)
    try:
        p.run()
        case("create failure -> CreateError", False)
    except CreateError as e:
        case("create failure -> CreateError", "invalid file magic" in str(e))
    blobs_before = len(fo.blobs)
    fo.create_error = None
    r = Provisioner(fo.url, models, state_path=p.state_path).run()
    case("create failure then retry -> created, elah blob reused", r["elah"] == "created"
         and len(fo.blobs) == blobs_before + 1)
    fo.stop()

    # 8. CANCEL: set before hashing -> Cancelled, nothing created
    fo = fresh_server(with_base=True)
    ev = threading.Event()
    p = Provisioner(fo.url, models, state_path=td / "state_cancel.json", cancel=ev,
                    progress=lambda st, d, t, m: ev.set() if st == "pull" else None)
    try:
        p.run()
        case("cancel -> Cancelled", False)
    except Cancelled:
        case("cancel -> Cancelled, nothing created", not fo.created)
    fo.stop()

    # 9. DAMAGED DELTA: manifest mismatch refused before any upload
    fo = fresh_server(with_base=True)
    man = json.loads((models / "manifest.json").read_text())
    man["elah.delta.gguf"]["sha256"] = "sha256:" + "0" * 64
    (models / "manifest.json").write_text(json.dumps(man))
    p, _ = prov(fo)
    try:
        p.run()
        case("damaged delta refused", False)
    except ProvisionError as e:
        case("damaged delta refused before upload", "damaged" in str(e) and not fo.blobs)
    write_manifest(models)
    fo.stop()

    # 10. BASE OF THE WRONG KIND: layout/require mismatch refused (GGUFError), nothing uploaded
    fo = fresh_server(with_base=True)
    other = td / ("sha256-" + "c" * 64)
    write_test_gguf(other, {"general.architecture": "qwen2", "general.finetune": "Coder"},
                    [("tok.weight", (128,), 0, a), ("blk.0.attn_q.weight", (256, 2), 12, q4),
                     ("blk.0.ffn.weight", (128,), 0, a)])
    fo.models[BASE_TAG]["from_path"] = str(other)
    p, _ = prov(fo)
    try:
        p.run()
        case("wrong base refused", False)
    except GGUFError as e:
        case("wrong base refused (require key)", "finetune" in str(e) and not fo.blobs)
    fo.stop()

    # 11. OLLAMA NOT RUNNING, THEN STARTED: manager starts it, provisioning follows, state MODELS_MISSING -> READY
    holder = {}

    def fake_popen(args, **kw):
        holder["fo"] = fresh_server(with_base=True)
        mgr.url = holder["fo"].url
        from ollama_manager import _FakeProc
        return _FakeProc()
    exe = td / "ollama.exe"
    exe.write_bytes(b"MZ")
    mgr = OllamaManager("http://127.0.0.1:9", exe_candidates=[exe], which=lambda _: None, popen=fake_popen)
    names = [model_name(s) for s in SPEAKERS]
    s0 = mgr.status(names).state
    s1 = mgr.ensure_running(wait_s=5).state
    s2 = mgr.status(names).state
    r = Provisioner(mgr.url, models, state_path=td / "state_start.json").run()
    s3 = mgr.status(names).state
    case("not running -> started -> provisioned: INSTALLED_NOT_RUNNING/RUNNING/MODELS_MISSING/READY",
         (s0, s1, s2, s3) == (INSTALLED_NOT_RUNNING, RUNNING, MODELS_MISSING, READY) and r["elah"] == "created")
    holder["fo"].stop()

    # 12. VISION is opt-in only
    fo = fresh_server(with_base=True)
    Provisioner(fo.url, models, state_path=td / "state_v.json").run()
    no_vision = not any(q[0] == "POST" and q[1] == "/api/pull" and q[2].get("model") == VISION_TAG
                        for q in fo.requests)
    Provisioner(fo.url, models, state_path=td / "state_v.json").run(include_vision=True)
    pulled_vision = any(q[0] == "POST" and q[1] == "/api/pull" and q[2].get("model") == VISION_TAG
                        for q in fo.requests)
    case("vision: not pulled by default, pulled when requested", no_vision and pulled_vision)
    # remove() only touches this prefix
    fo.models["realizer-elah"] = {"from_blob": "sha256:keep"}
    gone = Provisioner(fo.url, models, state_path=td / "state_v.json").remove()
    case("remove(): deletes suitmk2-* only", sorted(gone) == names and "realizer-elah" in fo.models)
    fo.stop()

    # 13. SetupJob: NOT_INSTALLED -> (mocked) install -> RUNNING -> provisioned; overall reaches 1.0
    from ollama_manager import _FakeResp, INSTALLER_URL
    holder = {}
    payload = b"MZ" + b"y" * 3000

    def fake_run(args, **kw):
        class R:
            returncode = 0
            stdout = "Valid|CN=Ollama Inc."
        if args[0] != "powershell":
            holder["fo"] = fresh_server(with_base=True)
            mgr2.url = holder["fo"].url
        return R()

    def fake_urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else req
        if url == INSTALLER_URL:
            return _FakeResp(payload)
        return urllib.request.urlopen(url, timeout=timeout)
    mgr2 = OllamaManager("http://127.0.0.1:9", exe_candidates=[], which=lambda _: None, run=fake_run,
                         urlopen=fake_urlopen)
    seen = []
    job = SetupJob(lambda *x: seen.append((x[0], job.overall)), manager=mgr2,
                   provisioner_kw={"models_dir": models, "state_path": td / "state_job.json"})
    r = job.run()
    ov = [o for _, o in seen]
    case("SetupJob: install -> provision, runtime 'installed', both created",
         r.get("runtime") == "installed" and r["elah"] == r["montaigne"] == "created")
    case("SetupJob: overall monotone to 1.0, download stage first",
         ov == sorted(ov) and job.overall == 1.0 and seen[0][0] == "download")
    job2 = SetupJob(manager=OllamaManager(holder["fo"].url),
                    provisioner_kw={"models_dir": models, "state_path": td / "state_job.json"})
    r2 = job2.run()
    case("SetupJob re-run: runtime 'running', everything up to date",
         r2["runtime"] == "running" and r2["elah"] == r2["montaigne"] == "up to date")
    job3 = SetupJob(manager=OllamaManager("http://127.0.0.1:9", exe_candidates=[], which=lambda _: None),
                    allow_install=False)
    try:
        job3.run()
        case("SetupJob allow_install=False on a bare PC -> ProvisionError", False)
    except ProvisionError:
        case("SetupJob allow_install=False on a bare PC -> ProvisionError", True)
    holder["fo"].stop()

    shutil.rmtree(td, ignore_errors=True)
    for name, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    bad = sum(not ok for _, ok in results)
    print(f"model_provision selftest: {len(results) - bad}/{len(results)} passed")
    return 1 if bad else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    if "--write-manifest" in sys.argv:
        print(json.dumps(write_manifest(), indent=1))
        sys.exit(0)
    prefix = MODEL_PREFIX
    if "--prefix" in sys.argv:
        prefix = sys.argv[sys.argv.index("--prefix") + 1]

    def show(stage, done, total, msg, _last=[0.0]):
        now = time.time()
        if now - _last[0] > 1.0 or stage in ("done", "create"):
            _last[0] = now
            print(f"  [{stage:6s}] {msg} {done}/{total}" if total > 1 else f"  [{stage:6s}] {msg}", flush=True)
    p = Provisioner(prefix=prefix, progress=show)
    t0 = time.time()
    print(p.run(include_vision="--vision" in sys.argv), f"{time.time() - t0:.0f}s")
    for line in p.log:
        print(" ", line)
