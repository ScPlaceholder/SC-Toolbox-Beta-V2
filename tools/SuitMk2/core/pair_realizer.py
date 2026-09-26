"""pair_realizer.py - the ambient realizer SuitMk2 plugs in via set_ambient_realizer() (2026-09-23).

    realizer = PairRealizer(backend="auto")       # nothing loads yet; no torch import unless hf is chosen
    suit.set_ambient_realizer(realizer)           # callable(spec) -> str | None
    realizer.reload()                             # drop loaded models, re-resolve the backend (new Ollama models,
                                                  # retrained adapters) without restarting the host

What it adds over the bare hook:
  1. TWO CHARACTER ADAPTERS on ONE base model (Qwen2.5-1.5B + lora_qwen15_elah / lora_qwen15_montaigne),
     switched per line by spec["speaker"]. Held-out result: 122/122 grounded vs 119/122 for one shared adapter
     (eval_20260923_160938.jsonl).
  2. BACKENDS, chosen by resolve_backend(choice):
       ollama - Ollama models suitmk2-elah / suitmk2-montaigne (provisioned by the tool itself on first run:
                model_provision.py, base qwen2.5:1.5b + a Q8_0 attention delta per character) or, for dev,
                realizer-elah / realizer-montaigne (merge_for_ollama.py). The first COMPLETE set in
                MODEL_PREFIXES wins (realizer_prefix). Pure stdlib HTTP: NO torch in this process. GPU when
                headroom is OK/ROOMY (num_gpu omitted = Ollama decides), CPU (num_gpu 0, 2 threads) when TIGHT.
       hf     - transformers + peft on the GPU, Ollama CPU models as the TIGHT fallback if installed.
                torch is imported ONLY inside HfPairBackend.__init__.
       none   - nothing available: every call is silence.
       auto   - ollama if BOTH models of some set exist, else hf if torch+transformers+peft are importable
                (checked with find_spec, nothing is imported), else none.
  3. HEADROOM GATING. A sampler thread feeds hw_monitor.HeadroomController (imported lazily). TIGHT unloads the
     GPU model so the game gets its VRAM back; the CPU path speaks instead if one exists.
  4. NEAR-REPEAT AVOIDANCE: a candidate whose opening words or overall shape match a recent line from the same
     speaker is resampled, up to MAX_TRIES.
  5. Candidates are grounded HERE too, so a failed line costs a resample instead of silence.

Silence is always a valid answer: every failure path returns None, never a canned line.
Selftest (no model, no GPU, fake Ollama on port 0): python pair_realizer.py --selftest
"""
from __future__ import annotations

import difflib
import http.client
import json
import logging
import re
import sys
import threading
import time
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from grounding_validator import ground  # noqa: E402

# Module logger for the module-level helpers. PairRealizer itself reports through the injected self._log callable
# (the window's status line), which is the right channel for anything the pilot should see; these helpers run
# before any PairRealizer exists, so they have nothing else.
_LOG = logging.getLogger("suitmk2.realizer")

# A localhost call to Ollama: OSError covers urllib's URLError/HTTPError, ConnectionRefusedError and the socket
# timeout; http.client.HTTPException covers a malformed status line; ValueError covers a body that is not JSON.
_HTTP_ERRORS = (OSError, http.client.HTTPException, ValueError)

BASE = "Qwen/Qwen2.5-1.5B-Instruct"
SPEAKERS = ("elah", "montaigne")
SYSTEM = ("You turn a planned meaning into ONE natural spoken line in the named speaker's voice. "
          "Use only the given facts. Add no new facts, numbers, names or quotations. Output only the line.")
OLLAMA_URL = "http://127.0.0.1:11434"
# Ollama model-name sets, in preference order. "suitmk2-" = provisioned by the tool itself (model_provision.py:
# base qwen2.5:1.5b + the character's attention delta); "realizer-" = the dev route (merge_for_ollama.py).
# A set counts only when BOTH speakers exist in it; the first complete set wins. The first-run Setup panel uses the
# same rule to decide "already set up" (suit_window passes MODEL_PREFIXES to SetupPanel).
MODEL_PREFIXES = ("suitmk2-", "realizer-")
BACKENDS =("auto", "ollama", "hf", "api", "none")
CPU_THREADS = 2
MAX_TRIES = 4
RECENT_PER_SPEAKER = 8
OPENER_WORDS = 4
SHAPE_LIMIT = 0.72          # difflib ratio on the de-numbered, de-limbed word sequence
SAMPLE_EVERY_S = 2.0

# Words that differ between otherwise identical lines: body parts, sides, digits. Masked before comparing, so
# "Right leg injury, tier 1..." and "Left arm injury, tier 1..." are recognised as the same sentence.
_MASK = re.compile(r"\b(\d+|left|right|arm|arms|leg|legs|head|torso|chest|tier)\b")


def spec_prompt(s: dict) -> str:
    """MUST stay identical to companion_design/train_realizer.spec_prompt: the adapters were trained on it."""
    facts = "; ".join(f"{c['predicate']}={c['value']} ({c['kind']})" for c in s["claims"])
    lo, hi = s["length_words"]
    # A banter reply sees the LINE it answers (2026-09-23, J: "the banter sounds staged because they repeat lines").
    # Appended only when present, so every non-reply prompt is byte-identical to before.
    rt = s.get("responds_to") or {}
    reply = f"\nREPLYING TO: {rt['speaker'].upper()}: {rt['text']}" if rt.get("text") else ""
    return (f"SPEAKER: {s['speaker'].upper()}\nFACTS: {facts}\nVIEW: {s['interpretation']['text']}\n"
            f"MOVES: {', '.join(s['rhetoric'])}\nMUST INCLUDE: {', '.join(s['required_values']) or '-'}\n"
            f"LENGTH: {lo}-{hi} words{reply}")


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z']+|\d+", text.lower())


def _shape(text: str) -> list[str]:
    return [w for w in (_MASK.sub("#", w) for w in _words(text))]


def too_similar(candidate: str, recent: list[str]) -> Optional[str]:
    """Return the recent line the candidate echoes, or None. Two tests, either is enough:
    same opening OPENER_WORDS (masked), or overall masked shape above SHAPE_LIMIT."""
    c = _shape(candidate)
    for prev in recent:
        p = _shape(prev)
        if len(c) >= OPENER_WORDS and c[:OPENER_WORDS] == p[:OPENER_WORDS]:
            return prev
        if difflib.SequenceMatcher(None, c, p).ratio() >= SHAPE_LIMIT:
            return prev
    return None


# ---- backends ----------------------------------------------------------------------------------------------
class HfPairBackend:
    """base + both adapters, transformers/peft. torch is imported HERE and nowhere else in this module."""

    def __init__(self, adapter_dir: Path, device: Optional[str] = None):
        import torch
        from peft import PeftModel
        from transformers import AutoModelForCausalLM, AutoTokenizer
        self.torch = torch
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        dtype = torch.bfloat16 if self.device == "cuda" else torch.float32
        self.tok = AutoTokenizer.from_pretrained(BASE)
        model = AutoModelForCausalLM.from_pretrained(BASE, dtype=dtype, device_map=self.device)
        model = PeftModel.from_pretrained(model, str(adapter_dir / "lora_qwen15_elah"), adapter_name="elah")
        model.load_adapter(str(adapter_dir / "lora_qwen15_montaigne"), adapter_name="montaigne")
        self.model = model.eval()

    def generate(self, speaker: str, prompt: str, temperature: float) -> str:
        self.model.set_adapter(speaker)
        msgs = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]
        text = self.tok.apply_chat_template(msgs, add_generation_prompt=True, tokenize=False)
        ids = self.tok(text, add_special_tokens=False, return_tensors="pt")["input_ids"].to(self.device)
        with self.torch.no_grad():
            if temperature <= 0:
                out = self.model.generate(ids, max_new_tokens=80, do_sample=False)
            else:
                out = self.model.generate(ids, max_new_tokens=80, do_sample=True, temperature=temperature, top_p=0.9)
        return self.tok.decode(out[0][ids.shape[1]:], skip_special_tokens=True).strip().strip('"')

    def close(self) -> None:
        del self.model
        if self.device == "cuda":
            self.torch.cuda.empty_cache()


def _post_json(url: str, body: dict, timeout: float) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def ollama_models(url: str = OLLAMA_URL, timeout: float = 2.0) -> Optional[set]:
    """Base names (tag stripped) of the installed Ollama models, or None if Ollama does not answer."""
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/tags", timeout=timeout) as r:
            return {m["name"].split(":")[0] for m in json.loads(r.read()).get("models", [])}
    except OSError as e:
        # Ollama is not running or not reachable. This is the documented None and the normal case on a PC that has
        # never run setup, so it is debug; resolve_backend() turns it into 'hf' or 'none' and says so on the status
        # line. Nothing is hidden by keeping it quiet here.
        _LOG.debug("realizer: Ollama did not answer at %s (%s: %s)", url, type(e).__name__, e)
        return None
    except (http.client.HTTPException, ValueError, KeyError, TypeError, AttributeError) as e:
        # Ollama DID answer and we could not read the model list: a malformed body, or /api/tags returning a shape
        # without "name" strings (KeyError / TypeError / AttributeError from the comprehension). None then means
        # "no models installed", so resolve_backend picks 'none' and THE COMPANION IS SILENT FOR THE WHOLE SESSION
        # on a PC whose models are in fact present. Indistinguishable from a machine that never ran setup, and the
        # pilot is only told "backend none". This must never be silent.
        _LOG.warning("realizer: Ollama answered at %s but its model list is unreadable (%s: %s); treating it as NO "
                     "models installed, which means no local backend and no speech", url, type(e).__name__, e)
        return None


def complete_prefix(names: Optional[set]) -> Optional[str]:
    """The first MODEL_PREFIXES entry whose BOTH speakers are in `names`, or None. Pure."""
    if names is None:
        return None
    for p in MODEL_PREFIXES:
        if all(f"{p}{s}" in names for s in SPEAKERS):
            return p
    return None


def realizer_prefix(url: str = OLLAMA_URL, names: Optional[set] = None) -> Optional[str]:
    """The first MODEL_PREFIXES entry with BOTH speakers installed, or None (none complete / Ollama unreachable)."""
    return complete_prefix(ollama_models(url) if names is None else names)


def _missing(names: Optional[set]) -> Optional[list]:
    if names is None:
        return None
    if complete_prefix(names) is not None:
        return []
    return [f"{MODEL_PREFIXES[0]}{s}" for s in SPEAKERS if f"{MODEL_PREFIXES[0]}{s}" not in names]


def missing_realizer_models(url: str = OLLAMA_URL) -> Optional[list]:
    """[] = some complete model set is installed; [names] = what the PREFERRED set lacks; None = Ollama unreachable."""
    return _missing(ollama_models(url))


class OllamaPairBackend:
    """The SAME character adapters, as Ollama models <prefix>elah / <prefix>montaigne: suitmk2-* (provisioned by
    the tool) or realizer-* (merge_for_ollama.py, dev). Raw mode with the exact Qwen2.5 chat format the adapters
    were trained on: no Ollama template can drift it. `prefix` None = the first complete set installed now.

    device="gpu": num_gpu omitted (Ollama offloads what fits).   device="cpu": num_gpu 0, `threads` threads, so
    the game keeps the GPU. close() asks Ollama to evict the models THIS backend used (keep_alive 0), which is
    what actually returns the VRAM/RAM - dropping the Python object alone frees nothing in Ollama."""

    def __init__(self, url: str = OLLAMA_URL, device: str = "cpu", threads: int = CPU_THREADS,
                 timeout: float = 60.0, prefix: Optional[str] = None):
        if device not in ("gpu", "cpu"):
            raise ValueError(f"device must be gpu or cpu, not {device!r}")
        self.url, self.device, self.threads, self.timeout = url.rstrip("/"), device, threads, timeout
        self.prefix = prefix or realizer_prefix(self.url) or MODEL_PREFIXES[0]
        self._used: set = set()

    @staticmethod
    def available(url: str = OLLAMA_URL) -> bool:
        return missing_realizer_models(url) == []

    def options(self, temperature: float) -> dict:
        opts = {"temperature": temperature, "top_p": 0.9, "num_predict": 80, "stop": ["<|im_end|>"]}
        if self.device == "cpu":
            opts["num_gpu"] = 0
            opts["num_thread"] = self.threads
        return opts

    def generate(self, speaker: str, prompt: str, temperature: float) -> str:
        text = (f"<|im_start|>system\n{SYSTEM}<|im_end|>\n<|im_start|>user\n{prompt}<|im_end|>\n"
                "<|im_start|>assistant\n")
        model = f"{self.prefix}{speaker}"
        body = {"model": model, "raw": True, "stream": False, "prompt": text, "keep_alive": "10m",
                "options": self.options(temperature)}
        self._used.add(model)
        return _post_json(self.url + "/api/generate", body, self.timeout)["response"].strip().strip('"')

    def close(self) -> None:
        for model in sorted(self._used):
            try:
                _post_json(self.url + "/api/generate", {"model": model, "keep_alive": 0}, 5.0)
            except _HTTP_ERRORS as e:
                # keep_alive 0 is what hands the VRAM back to the game; close() is called by _drop() precisely
                # because headroom went TIGHT. If the release fails, Ollama keeps the model resident for its own
                # 10m keep_alive and the pilot sees a stutter that the unload was supposed to prevent - while the
                # status line has already said "VRAM returned to the game". Ollama being down is the harmless case
                # (nothing is resident), which is why this is a warning and not an error.
                _LOG.warning("realizer: could not release %s from Ollama (%s: %s); its VRAM stays held until "
                             "Ollama's own keep_alive expires", model, type(e).__name__, e)
        self._used.clear()


# ---- API backend (J 2026-09-24: "Yeah let's add an API option then.") -------------------------------------------
# The pipeline is unchanged: events, pacing, the topic walker and the spec decide IF and WHAT; the model only turns
# one spec into one line, grounded HERE exactly like the local model's lines. So an API key buys better wording and
# more variety, never the ability to invent facts. A base model is not fine-tuned on the characters, so the system
# prompt carries them. Any API failure (no key, offline, quota) falls through to the local Ollama models if they
# exist, so the companion never goes silent because a network call failed.
API_MODEL_DEFAULT = "claude-sonnet-5"      # high-volume production tier; configurable in settings ("api_model")
API_SYSTEM = (
    SYSTEM + " "
    "There are two speakers. ELAH is the pilot's suit AI: dry, brief, confident, warm under the edge, practical. "
    "She has been to these places with the pilot, and can search anything for mission prep, so she never uses a "
    "brochure or an advertisement as her source; she may tease Montaigne for believing them. "
    "MONTAIGNE is the ship's AI, slightly broken, who believes he is the essayist Michel de Montaigne: digressive, "
    "self-deprecating, gently sceptical, fond of the pilot. He knows ship specifications firsthand, but knows places "
    "only from travel brochures and commercials, which he quotes with complete faith. "
    "The user message is a plan: SPEAKER, FACTS, VIEW (the stance to take), MOVES (rhetorical moves to perform, "
    "never to name), MUST INCLUDE, LENGTH, and sometimes REPLYING TO (the line being answered; answer it). "
    "Stay inside LENGTH. Use contractions. No stage directions, no quotation marks, no speaker label."
)


def api_key_from_settings() -> str:
    """The key the pilot entered in the SuitMk2 settings, else the standard ANTHROPIC_API_KEY variable."""
    import os
    try:
        import settings as _settings
        key = (_settings.load().get("anthropic_api_key") or "").strip()
    except Exception:
        key = ""
    return key or os.environ.get("ANTHROPIC_API_KEY", "").strip()


def api_check(api_key: str, model: str, client=None) -> tuple[bool, str]:
    """Is this key good for this model? Uses models.retrieve, which costs NO tokens: the window's Test button must
    never spend the pilot's money to answer a yes/no question. -> (ok, one line for the pilot)."""
    if not (api_key or "").strip() and client is None:
        return False, "no key entered"
    try:
        if client is None:
            import anthropic
            client = anthropic.Anthropic(api_key=api_key.strip(), timeout=10.0, max_retries=0)
        m = client.models.retrieve(model)
        return True, f"key OK, {getattr(m, 'display_name', None) or model} available"
    except Exception as e:
        kind = type(e).__name__
        hint = {"AuthenticationError": "the key was refused", "NotFoundError": "this key cannot use that model",
                "PermissionDeniedError": "this key is not allowed that model",
                "APIConnectionError": "could not reach the API (offline?)",
                "APITimeoutError": "the API did not answer in time"}.get(kind, "")
        return False, hint or f"{kind}: {str(e)[:100]}"


class ApiPairBackend:
    """Claude via the official SDK (`anthropic`; verified against 0.112). device 'api' so headroom gating never unloads it: it holds no
    local memory. `fallback` builds a local backend, used for any call the API cannot serve."""
    device = "api"

    def __init__(self, model: str = API_MODEL_DEFAULT, api_key: str | None = None,
                 fallback: Optional[Callable[[], object]] = None, client=None, timeout: float = 15.0):
        self.model = model
        self._fallback_factory, self._fallback = fallback, None
        self.last_error = None
        self.calls = {"api": 0, "fallback": 0}
        if client is not None:
            self._client = client
        else:
            key = api_key if api_key is not None else api_key_from_settings()
            if not key:
                raise RuntimeError("no Anthropic API key: set one in SuitMk2 settings or ANTHROPIC_API_KEY")
            import anthropic
            self._client = anthropic.Anthropic(api_key=key, timeout=timeout, max_retries=1)

    def generate(self, speaker: str, prompt: str, temperature: float) -> str:
        try:
            r = self._client.messages.create(
                model=self.model, max_tokens=120, temperature=max(0.0, min(1.0, float(temperature))),
                system=[{"type": "text", "text": API_SYSTEM, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": prompt}])
            self.calls["api"] += 1
            text = "".join(b.text for b in r.content if getattr(b, "type", "") == "text")
            return text.strip().strip('"')
        except Exception as e:                       # SDK errors are all Exceptions; any of them -> local
            self.last_error = f"{type(e).__name__}: {str(e)[:120]}"
            if self._fallback_factory is None:
                raise
            if self._fallback is None:
                self._fallback = self._fallback_factory()
            self.calls["fallback"] += 1
            return self._fallback.generate(speaker, prompt, temperature)

    def close(self) -> None:
        if self._fallback is not None:
            try:
                self._fallback.close()
            finally:
                self._fallback = None


# ---- backend selection ---------------------------------------------------------------------------------------
def hf_importable() -> bool:
    """torch AND transformers AND peft findable - WITHOUT importing any of them (find_spec only). Torch alone is
    not enough: the toolbox's Python 3.14 has torch 2.11 but neither transformers nor peft (measured 2026-09-23)."""
    import importlib.util
    try:
        return all(importlib.util.find_spec(m) is not None for m in ("torch", "transformers", "peft"))
    except (ImportError, ValueError, AttributeError) as e:
        # find_spec raises ModuleNotFoundError when a parent package is missing, ValueError when an already-imported
        # module has __spec__ None, and AttributeError against a broken custom finder on sys.meta_path.
        # False here is read by resolve_backend as "torch is not installed", so on a machine that HAS the torch env
        # the answer becomes backend 'none' and the companion never speaks. Silence is a valid answer in this
        # module, but only when it is the true one.
        _LOG.warning("realizer: cannot probe for torch/transformers/peft (%s: %s); treating the hf backend as "
                     "unavailable", type(e).__name__, e)
        return False


def resolve_backend(choice: str = "auto", url: str = OLLAMA_URL, *, ollama_ok: Optional[bool] = None,
                    hf_ok: Optional[bool] = None) -> str:
    """auto -> 'ollama' if both models of some MODEL_PREFIXES set exist, else 'hf' if importable, else 'none'. Explicit choices pass
    through unchecked: an explicit backend that cannot run is silence at call time, and /reload retries it."""
    if choice not in BACKENDS:
        raise ValueError(f"backend must be one of {BACKENDS}, not {choice!r}")
    if choice != "auto":
        return choice
    if OllamaPairBackend.available(url) if ollama_ok is None else ollama_ok:
        return "ollama"
    if hf_importable() if hf_ok is None else hf_ok:
        return "hf"
    return "none"


@dataclass
class BackendPlan:
    """What to build, not built yet: factories are called lazily on the first request that needs them."""
    name: str
    gpu: Optional[Callable[[], object]] = None      # OK / ROOMY headroom
    cpu: Optional[Callable[[], object]] = None      # TIGHT headroom
    note: str = ""
    meta: dict = field(default_factory=dict)


def plan_for(name: str, adapter_dir: Path, url: str = OLLAMA_URL, threads: int = CPU_THREADS) -> BackendPlan:
    if name == "ollama":
        names = ollama_models(url)                 # ONE /api/tags read decides both the note and the prefix
        missing, prefix = _missing(names), complete_prefix(names)
        note = ("ollama unreachable" if missing is None else f"missing {', '.join(missing)}" if missing else "")
        return BackendPlan("ollama", lambda: OllamaPairBackend(url, device="gpu", prefix=prefix),
                           lambda: OllamaPairBackend(url, device="cpu", threads=threads, prefix=prefix), note,
                           {"prefix": prefix})
    if name == "hf":
        prefix = realizer_prefix(url)
        cpu = (lambda: OllamaPairBackend(url, device="cpu", threads=threads, prefix=prefix)) if prefix else None
        return BackendPlan("hf", lambda: HfPairBackend(adapter_dir), cpu,
                           "" if cpu else "no Ollama CPU models: TIGHT is silence")
    if name == "api":
        # One API backend serves both headroom states (it holds no VRAM); local Ollama is its per-call fallback.
        prefix = realizer_prefix(url)
        local = (lambda: OllamaPairBackend(url, device="cpu", threads=threads, prefix=prefix)) if prefix else None
        try:
            import settings as _settings
            model = (_settings.load().get("api_model") or API_MODEL_DEFAULT).strip()
        except Exception:
            model = API_MODEL_DEFAULT
        box = {}

        def api():
            if "b" not in box:
                box["b"] = ApiPairBackend(model=model, fallback=local)
            return box["b"]
        return BackendPlan("api", api, api, "" if local else "no local fallback: an API failure is silence",
                           {"model": model})
    return BackendPlan("none", None, None, "no realizer backend available: silence")


def _device_of(backend, default: str) -> str:
    d = getattr(backend, "device", None)
    return {"cuda": "gpu", "gpu": "gpu", "cpu": "cpu"}.get(str(d), default)


# The FIRST attempt's temperature (J 2026-09-24, yes to sampling). Greedy (0.0) was the evaluated setting, and it
# is why every "you're off the ship" line opened the same way: temp 0 picks the single likeliest opening every time.
# Measured on the held-out sets at 0.7: variety roughly doubles for -4 grounded lines, and those 4 are not spoken,
# because an ungrounded candidate is retried and never reaches the speaker. Settings key "first_temperature";
# 0.0 restores greedy exactly.
FIRST_TEMPERATURE_DEFAULT = 0.7
RETRY_TEMPERATURE = 0.8


def first_temperature_from_settings() -> float:
    try:
        import settings as _settings
        v = float(_settings.load().get("first_temperature", FIRST_TEMPERATURE_DEFAULT))
    except Exception:
        return FIRST_TEMPERATURE_DEFAULT
    return max(0.0, min(1.0, v))


class PairRealizer:
    """callable(spec) -> str | None. Thread-safe: one lock serializes generation AND reload, so a reload waits for
    the in-flight request to finish and every later request is served by the new backend. Nothing is dropped;
    requests arriving during a reload simply wait for it."""

    def __init__(self, adapter_dir: Path | str | None = None,
                 backend_factory: Optional[Callable[[], object]] = None,
                 headroom: Optional[Callable[[], str]] = None,
                 log: Callable[[str], None] = lambda m: None,
                 cpu_backend_factory: Optional[Callable[[], object]] = "auto",
                 first_temperature: Optional[float] = None,
                 backend: Optional[str] = None,
                 ollama_url: str = OLLAMA_URL,
                 resolver: Optional[Callable[[], BackendPlan]] = None):
        self.adapter_dir = Path(adapter_dir) if adapter_dir else HERE.parent
        self.ollama_url = ollama_url
        self._log = log
        if first_temperature is None:
            self.first_temperature = first_temperature_from_settings()
        else:
            self.first_temperature = max(0.0, min(1.0, float(first_temperature)))
        if resolver is not None:
            self._resolver = resolver
        elif backend is not None:
            self._resolver = lambda: plan_for(resolve_backend(backend, ollama_url), self.adapter_dir, ollama_url)
        else:
            # Legacy explicit factories (tests, older hosts). "auto" CPU = Ollama CPU models if installed,
            # re-checked on every reload.
            gpu = backend_factory or (lambda: HfPairBackend(self.adapter_dir))
            name = "custom" if backend_factory else "hf"

            def legacy() -> BackendPlan:
                cpu = cpu_backend_factory
                if cpu == "auto":
                    cpu = (lambda: OllamaPairBackend(ollama_url, device="cpu")) \
                        if OllamaPairBackend.available(ollama_url) else None
                return BackendPlan(name, gpu, cpu)
            self._resolver = legacy
        self._backend = None            # the OK/ROOMY backend, once built
        self._cpu = None                # the TIGHT backend, once built
        self.last_device: Optional[str] = None
        self._lock = threading.Lock()
        self._recent = {s: deque(maxlen=RECENT_PER_SPEAKER) for s in SPEAKERS}
        self._stop = threading.Event()
        self.stats = {"asked": 0, "spoke": 0, "tight": 0, "ungrounded": 0, "repeats": 0, "loads": 0, "unloads": 0,
                      "cpu": 0, "errors": 0, "reloads": 0}
        self._plan = self._safe_resolve()
        self._log(f"pair_realizer: backend {self._plan.name}" + (f" ({self._plan.note})" if self._plan.note else ""))
        if headroom is not None:
            self._headroom = headroom          # injected (tests, or a host that already samples)
        else:
            self._headroom = self._start_sampler()

    def _safe_resolve(self) -> BackendPlan:
        try:
            return self._resolver()
        except Exception as e:
            return BackendPlan("none", note=f"resolve failed: {type(e).__name__}: {e}")

    # -- headroom ------------------------------------------------------------------------------------------
    def headroom_state(self) -> str:
        """TIGHT / OK / ROOMY. Shared with eyes.py so the PC is sampled by ONE thread, not two."""
        return self._headroom()

    def _start_sampler(self) -> Callable[[], str]:
        """Background thread: hw_monitor.sample_all -> HeadroomController (imported HERE, lazily). Any sampler
        failure leaves the state at TIGHT: no data never grants headroom."""
        from hw_monitor import HeadroomController, HeadroomState, sample_all
        ctl = HeadroomController(now=time.time(), initial_state=HeadroomState.TIGHT)
        state = {"v": HeadroomState.TIGHT.value}

        def run():
            while not self._stop.is_set():
                try:
                    state["v"] = ctl.update(sample_all(interval=1.0), time.time()).value
                except Exception as e:  # counters vanish on some drivers; stay TIGHT and say why once a minute
                    state["v"] = HeadroomState.TIGHT.value
                    self._log(f"pair_realizer: headroom sample failed ({type(e).__name__}: {e}); holding TIGHT")
                    self._stop.wait(60)
                self._stop.wait(SAMPLE_EVERY_S)

        threading.Thread(target=run, name="pair_realizer_headroom", daemon=True).start()
        return lambda: state["v"]

    # -- load / unload (caller holds the lock) ------------------------------------------------------------
    def _ensure_loaded(self) -> bool:
        if self._backend is None:
            t0 = time.time()
            try:
                self._backend = self._plan.gpu()
            except Exception as e:
                self._log(f"pair_realizer: model load failed ({type(e).__name__}: {e}); staying silent")
                return False
            self.stats["loads"] += 1
            self._log(f"pair_realizer: {self._plan.name} loaded in {time.time() - t0:.1f}s")
        return True

    def _unload(self, why: str = "headroom TIGHT") -> None:
        if self._backend is not None:
            try:
                self._backend.close()
            except Exception as e:
                self._log(f"pair_realizer: close failed ({type(e).__name__}: {e})")
            finally:
                self._backend = None
                self.stats["unloads"] += 1
                self._log(f"pair_realizer: unloaded ({why}) - VRAM returned to the game")

    def _drop_cpu(self) -> None:
        if self._cpu is not None:
            try:
                self._cpu.close()
            except Exception as e:
                # LEFT BROAD, matching the GPU sibling in _drop() eight lines up, which is already broad-and-logged.
                # self._cpu is whatever cpu_backend_factory returned - a caller-injected object in tests and the
                # Ollama CPU backend in production - so there is no known exception set to narrow to. The `finally`
                # below drops the reference either way; what was missing was any trace that the CPU model was never
                # told to release itself.
                self._log(f"pair_realizer: CPU backend close failed ({type(e).__name__}: {e})")
            finally:
                self._cpu = None

    # -- live reload ----------------------------------------------------------------------------------------
    def reload(self) -> dict:
        """Drop every loaded backend and re-resolve. Waits for an in-flight request (same lock); the next request
        builds from the new plan. Repeat memory is kept: a reload is not a reason to repeat a line."""
        with self._lock:
            old = self._plan.name
            self._unload("reload")
            self._drop_cpu()
            self.last_device = None
            self._plan = self._safe_resolve()
            self.stats["reloads"] += 1
        self._log(f"pair_realizer: reload {old} -> {self._plan.name}"
                  + (f" ({self._plan.note})" if self._plan.note else ""))
        return self.backend_info()

    def set_backend(self, name: str) -> dict:
        """Switch backend (the window's API toggle) without restarting the service: new resolver, then reload."""
        from sidecar import BACKENDS
        if name not in BACKENDS:
            raise ValueError(f"unknown backend {name!r}")
        self.backend_name = name
        self._resolver = lambda: plan_for(resolve_backend(name, self.ollama_url), self.adapter_dir, self.ollama_url)
        return self.reload()

    def backend_info(self) -> dict:
        p = self._plan
        return {"backend": p.name, "device": self.last_device, "note": p.note,
                "loaded": {"gpu": self._backend is not None, "cpu": self._cpu is not None},
                "can": {"gpu": p.gpu is not None, "cpu": p.cpu is not None}}

    # -- the hook ------------------------------------------------------------------------------------------
    def __call__(self, spec: dict) -> Optional[str]:
        with self._lock:
            self.stats["asked"] += 1
            speaker = spec.get("speaker")
            if speaker not in SPEAKERS:
                return None
            plan = self._plan
            if self._headroom() == "TIGHT":
                self.stats["tight"] += 1
                self._unload()                          # the game gets the GPU back either way
                if plan.cpu is None:
                    return None                         # no CPU path: silence
                if self._cpu is None:
                    try:
                        self._cpu = plan.cpu()
                    except Exception as e:
                        self._log(f"pair_realizer: CPU backend failed ({type(e).__name__}: {e}); silent")
                        return None
                backend = self._cpu
                self.stats["cpu"] += 1
                device = _device_of(backend, "cpu")
            else:
                if plan.gpu is None:
                    return None
                self._drop_cpu()                        # headroom is back: stop holding a CPU copy in RAM
                if not self._ensure_loaded():
                    return None
                backend = self._backend
                device = _device_of(backend, "gpu")
            self.last_device = device
            prompt = spec_prompt(spec)
            recent = list(self._recent[speaker])
            for attempt in range(MAX_TRIES):
                # First attempt at self.first_temperature (0.7 by default, 0.0 = greedy); retries sample hotter.
                try:
                    text = backend.generate(speaker, prompt,
                                            self.first_temperature if attempt == 0 else RETRY_TEMPERATURE)
                except Exception as e:                  # model missing (404), Ollama down, OOM: silence
                    self.stats["errors"] += 1
                    self._log(f"pair_realizer: generate failed ({type(e).__name__}: {e}); silent")
                    return None
                if not text or ground(spec, text):
                    self.stats["ungrounded"] += 1
                    continue
                if too_similar(text, recent):
                    self.stats["repeats"] += 1
                    continue
                self._recent[speaker].append(text)
                self.stats["spoke"] += 1
                return text
            return None

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            self._unload("close")
            self._drop_cpu()


# ---- test helpers (also used by companion_service's selftest) ------------------------------------------------
def _spec(speaker="elah", part="right leg", tier=1):
    return {"speaker": speaker, "scenario": "injury", "rhetoric": ["plain_fact"], "required_values": [str(tier)],
            "length_words": [4, 30], "interpretation": {"text": "the pilot is hurt"},
            "claims": [{"id": "C1", "kind": "OBSERVED", "predicate": "injury.body_part", "value": part},
                       {"id": "C2", "kind": "OBSERVED", "predicate": "injury.tier", "value": tier}]}


_VOCAB = ("steady easy slow careful quiet hold breathe patch walk rest bandage medbay pilot hangar ship calm "
          "watch favour limp brace wait keep low back soon mend check scan sore bruised grit").split()


def varied_line(rng) -> str:
    """A grounded line for _spec() whose shape differs every call, so repeat-avoidance does not eat it."""
    return " ".join(rng.sample(_VOCAB, 8)).capitalize() + ", right leg at tier 1."


class _FakeBackend:
    """Behaves like HfPairBackend where it matters: close() while a generate() is running, or generate() after
    close(), is a crash in the real thing (close does `del self.model`), so both are recorded here."""

    def __init__(self, script, device=None, delay=0.0):
        self.script, self.calls, self.closed, self.delay = list(script), [], False, delay
        self.busy, self.closed_while_busy, self.used_after_close = 0, False, False
        if device:
            self.device = device

    def generate(self, speaker, prompt, temperature):
        if self.closed:
            self.used_after_close = True
            raise RuntimeError("generate() after close()")
        self.busy += 1
        try:
            self.calls.append((speaker, temperature))
            if self.delay:
                time.sleep(self.delay)
            item = self.script.pop(0) if self.script else ""
            return item() if callable(item) else item
        finally:
            self.busy -= 1

    def close(self):
        self.closed_while_busy |= self.busy > 0
        self.closed = True


class FakeOllama:
    """A stdlib http.server standing in for Ollama on port 0. Records every /api/generate body."""

    def __init__(self, models=(), line: Optional[Callable[[], str]] = None):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import random
        self.models, self.requests, self.unloads = set(models), [], []
        rng, lock = random.Random(7), threading.Lock()
        self.line = line or (lambda: varied_line(rng))
        fake = self

        class H(BaseHTTPRequestHandler):
            def _send(self, code, obj):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                if self.path == "/api/tags":
                    return self._send(200, {"models": [{"name": f"{m}:latest"} for m in sorted(fake.models)]})
                self._send(404, {"error": "not found"})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                if self.path != "/api/generate":
                    return self._send(404, {"error": "not found"})
                if body.get("model", "").split(":")[0] not in fake.models:
                    return self._send(404, {"error": f"model '{body.get('model')}' not found"})
                if "prompt" not in body and body.get("keep_alive") == 0:
                    with lock:
                        fake.unloads.append(body["model"])
                    return self._send(200, {"done": True, "done_reason": "unload"})
                with lock:
                    fake.requests.append(body)
                    text = fake.line()
                self._send(200, {"response": text, "done": True})

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


BOTH = {f"realizer-{s}" for s in SPEAKERS}


# ---- selftest: every branch, no model -----------------------------------------------------------------------
def _selftest() -> int:
    import random
    results = []

    def case(name, cond):
        results.append((name, bool(cond)))

    # 1. TIGHT never loads, never speaks (cpu_backend_factory=None: must not depend on what Ollama has installed).
    made = []
    r = PairRealizer(backend_factory=lambda: made.append(1) or _FakeBackend(["x"]), headroom=lambda: "TIGHT",
                     cpu_backend_factory=None)
    case("TIGHT returns None", r(_spec()) is None)
    case("TIGHT never loads the model", not made)

    # 2. OK loads lazily, greedy first, grounded line returned.
    fb = _FakeBackend(["Right leg injury, tier 1. Find a med bed."])
    r = PairRealizer(backend_factory=lambda: fb, headroom=lambda: "OK", cpu_backend_factory=None,
                     first_temperature=0.0)
    out = r(_spec())
    case("OK speaks a grounded line", out == "Right leg injury, tier 1. Find a med bed.")
    case("first_temperature=0.0 is exactly greedy", fb.calls[0][1] == 0.0)
    fb7 = _FakeBackend(["Right leg injury, tier 1. Find a med bed."])
    r7 = PairRealizer(backend_factory=lambda: fb7, headroom=lambda: "OK", cpu_backend_factory=None,
                      first_temperature=0.7)
    r7(_spec())
    case("first attempt samples at the configured 0.7", fb7.calls[0][1] == 0.7)
    case("the shipped default is 0.7", FIRST_TEMPERATURE_DEFAULT == 0.7)

    # 3. The measured repeat: same sentence, different limb -> resampled, not spoken.
    fb.script = ["Left arm injury, tier 1. Find a med bed.", "Tier 1 on the left arm. It will hold until a med bed."]
    out = r(_spec(part="left arm"))
    case("limb-swapped repeat is rejected", r.stats["repeats"] == 1)
    case("a differently shaped line gets through", out == "Tier 1 on the left arm. It will hold until a med bed.")
    case("resample is not greedy", fb.calls[-1][1] > 0)

    # 4. Ungrounded candidates cost a retry; all bad -> silence, never a canned line.
    fb.script = ["Right leg, tier 3."] * MAX_TRIES
    case("all-ungrounded returns None", r(_spec(part="right leg")) is None)
    case("ungrounded were counted", r.stats["ungrounded"] >= MAX_TRIES)

    # 5. Speakers keep separate memories: Montaigne may say what Elah said.
    fb.script = ["Right leg injury, tier 1. Find a med bed."]
    case("repeat memory is per speaker", r(_spec(speaker="montaigne")) is not None)

    # 6. Going TIGHT after loading unloads (VRAM back to the game).
    state = {"v": "OK"}
    fb2 = _FakeBackend(["Right leg injury, tier 1."])
    r2 = PairRealizer(backend_factory=lambda: fb2, headroom=lambda: state["v"], cpu_backend_factory=None)
    r2(_spec())
    state["v"] = "TIGHT"
    r2(_spec())
    case("TIGHT after load unloads", fb2.closed and r2.stats["unloads"] == 1)

    # 7. A load failure is silence, not a crash.
    def boom():
        raise RuntimeError("no GPU")
    r3 = PairRealizer(backend_factory=boom, headroom=lambda: "ROOMY", cpu_backend_factory=None)
    case("load failure returns None", r3(_spec()) is None)

    # 7b. TIGHT with a CPU model installed: the CPU backend speaks, the GPU backend stays unloaded.
    gpu_made, cpu_fb = [], _FakeBackend(["Right leg injury, tier 1. Find a med bed."])
    r_cpu = PairRealizer(backend_factory=lambda: gpu_made.append(1) or _FakeBackend([]), headroom=lambda: "TIGHT",
                         cpu_backend_factory=lambda: cpu_fb)
    case("TIGHT + CPU model: speaks via CPU", r_cpu(_spec()) == "Right leg injury, tier 1. Find a med bed.")
    case("TIGHT + CPU model: GPU model never loaded", not gpu_made)

    # 7c. A generate() that raises (Ollama 404 / down) is silence, not an exception out of the hook.
    class Raiser:
        def generate(self, *a):
            raise OSError("connection refused")

        def close(self):
            pass
    r4 = PairRealizer(backend_factory=Raiser, headroom=lambda: "OK", cpu_backend_factory=None)
    case("generate() raising returns None", r4(_spec()) is None and r4.stats["errors"] == 1)

    # 8. Unknown speaker is refused.
    case("unknown speaker returns None", PairRealizer(backend_factory=lambda: fb, headroom=lambda: "OK",
                                                      cpu_backend_factory=None)(_spec(speaker="ghost")) is None)

    # 9. The prompt is byte-identical to the one the adapters were trained on.
    try:
        # train_realizer lives in companion_design/ (HERE.parents[2]), not SuitMk2/ - the old path always SKIPPED.
        if len(HERE.parents) > 2:
            sys.path.append(str(HERE.parents[2]))
        from train_realizer import spec_prompt as trained_prompt
        case("spec_prompt matches train_realizer", trained_prompt(_spec()) == spec_prompt(_spec()))
    except Exception as e:  # shipped without the training code: say so, do not claim a pass
        print(f"  SKIP spec_prompt parity: train_realizer not importable here ({type(e).__name__})")

    # 10. Backend auto-selection, every branch (pure).
    case("auto: ollama models present -> ollama", resolve_backend("auto", ollama_ok=True, hf_ok=True) == "ollama")
    case("auto: no ollama, hf importable -> hf", resolve_backend("auto", ollama_ok=False, hf_ok=True) == "hf")
    case("auto: neither -> none", resolve_backend("auto", ollama_ok=False, hf_ok=False) == "none")
    case("explicit hf passes through", resolve_backend("hf", ollama_ok=True, hf_ok=False) == "hf")
    try:
        resolve_backend("tensorrt")
        case("unknown backend name rejected", False)
    except ValueError:
        case("unknown backend name rejected", True)

    # 11. Auto-selection against a fake Ollama's /api/tags: BOTH models required; one is not enough.
    fo = FakeOllama(models={"realizer-elah", "qwen2.5:1.5b"})
    case("auto: only one realizer model -> not ollama", resolve_backend("auto", fo.url, hf_ok=False) == "none")
    fo.models |= BOTH
    case("auto: both realizer models -> ollama", resolve_backend("auto", fo.url, hf_ok=False) == "ollama")
    dead = "http://127.0.0.1:9"
    case("auto: Ollama unreachable -> not ollama", resolve_backend("auto", dead, hf_ok=False) == "none")
    case("module never imported torch", "torch" not in sys.modules)

    # 12. GPU vs CPU options switch with headroom (real OllamaPairBackend -> fake server).
    head = {"v": "OK"}
    ro = PairRealizer(backend="ollama", ollama_url=fo.url, headroom=lambda: head["v"])
    case("ollama realizer speaks on OK", ro(_spec()) is not None and ro.backend_info()["device"] == "gpu")
    opts = fo.requests[-1]["options"]
    case("OK: num_gpu omitted (Ollama offloads)", "num_gpu" not in opts and "num_thread" not in opts)
    case("raw Qwen2.5 chat format", fo.requests[-1]["raw"] is True
         and fo.requests[-1]["prompt"].endswith("<|im_end|>\n<|im_start|>assistant\n"))
    head["v"] = "TIGHT"
    case("ollama realizer speaks on TIGHT", ro(_spec()) is not None and ro.backend_info()["device"] == "cpu")
    opts = fo.requests[-1]["options"]
    case("TIGHT: num_gpu 0, 2 threads", opts.get("num_gpu") == 0 and opts.get("num_thread") == CPU_THREADS)
    case("OK->TIGHT evicts the GPU copy (keep_alive 0)", fo.unloads == ["realizer-elah"])
    head["v"] = "ROOMY"
    ro(_spec(speaker="montaigne"))
    case("ROOMY: back on the GPU", "num_gpu" not in fo.requests[-1]["options"] and ro.backend_info()["device"] == "gpu"
         and fo.requests[-1]["model"] == "realizer-montaigne")
    case("TIGHT->ROOMY evicts the CPU copy", fo.unloads == ["realizer-elah", "realizer-elah"])

    # 13. Explicit ollama with the models missing: silence and a note, not an exception.
    fo2 = FakeOllama(models=set())
    rm = PairRealizer(backend="ollama", ollama_url=fo2.url, headroom=lambda: "OK")
    case("ollama w/o models: silence, note names the PREFERRED set",
         rm(_spec()) is None and "missing suitmk2-elah" in rm.backend_info()["note"])
    # 13b. Provisioned names (model_provision.py) are accepted and PREFERRED over the dev realizer-* set.
    fs = FakeOllama(models={"suitmk2-elah", "suitmk2-montaigne", "qwen2.5:1.5b"})
    case("auto: suitmk2-* only -> ollama", resolve_backend("auto", fs.url, hf_ok=False) == "ollama")
    sb = OllamaPairBackend(fs.url, device="cpu")
    sb.generate("montaigne", "x", 0.0)
    case("suitmk2-* only: generate hits suitmk2-montaigne", fs.requests[-1]["model"] == "suitmk2-montaigne")
    fs.stop()
    fb3 = FakeOllama(models={"suitmk2-elah", "suitmk2-montaigne", "realizer-elah", "realizer-montaigne"})
    case("both sets: suitmk2-* preferred", realizer_prefix(fb3.url) == "suitmk2-"
         and plan_for("ollama", HERE, fb3.url).meta.get("prefix") == "suitmk2-")
    fb3.stop()
    fh = FakeOllama(models={"suitmk2-elah", "realizer-montaigne"})
    case("no COMPLETE set (one of each) -> not ollama", resolve_backend("auto", fh.url, hf_ok=False) == "none"
         and missing_realizer_models(fh.url) == ["suitmk2-montaigne"])
    fh.stop()
    case("realizer-* only (dev machine) -> realizer- prefix, nothing missing",
         complete_prefix(BOTH) == "realizer-" and _missing(BOTH) == [])
    case("unreachable -> prefix None, missing None", realizer_prefix(dead) is None
         and missing_realizer_models(dead) is None)
    # ... and /reload picks the models up once merge_for_ollama.py has created them.
    fo2.models |= BOTH
    info = rm.reload()
    case("reload picks up newly created models", info["note"] == "" and rm(_spec()) is not None)

    # 14. No backend at all: silence, and the info says so.
    rn = PairRealizer(resolver=lambda: plan_for("none", HERE), headroom=lambda: "ROOMY")
    case("backend none -> silence", rn(_spec()) is None and rn.backend_info()["backend"] == "none")

    # 15. Reload swaps the backend mid-run without dropping a request.
    rng = random.Random(3)
    phase = {"v": "A"}
    slow_a = _FakeBackend([], device="cuda", delay=0.02)
    slow_a.script = [lambda: varied_line(rng)] * 10_000

    def resolver():
        if phase["v"] == "A":
            return BackendPlan("custom", lambda: slow_a, None)
        return plan_for("ollama", HERE, fo.url)
    rs = PairRealizer(resolver=resolver, headroom=lambda: "OK")
    outs, errs = [], []

    def worker(n):
        for i in range(n):
            try:
                outs.append(rs(_spec(speaker=SPEAKERS[i % 2])))
            except Exception as e:
                errs.append(e)
    before = len(fo.requests)
    threads = [threading.Thread(target=worker, args=(12,)) for _ in range(4)]
    for t in threads:
        t.start()
    time.sleep(0.15)
    phase["v"] = "B"
    info = rs.reload()
    for t in threads:
        t.join()
    case("reload mid-run: no exceptions", not errs)
    case("reload mid-run: every request answered", len(outs) == 48 and all(outs))
    case("reload mid-run: both backends served", len(slow_a.calls) > 0 and len(fo.requests) > before)
    case("reload never closes a backend mid-generation", slow_a.closed and not slow_a.closed_while_busy
         and not slow_a.used_after_close)
    a_calls = len(slow_a.calls)
    rs(_spec())
    case("after reload only the new backend serves", len(slow_a.calls) == a_calls and info["backend"] == "ollama"
         and slow_a.closed)
    fo.stop()
    fo2.stop()

    for name, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    bad = sum(not ok for _, ok in results)
    print(f"pair_realizer selftest: {len(results) - bad}/{len(results)} passed")
    return 1 if bad else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print(__doc__)
