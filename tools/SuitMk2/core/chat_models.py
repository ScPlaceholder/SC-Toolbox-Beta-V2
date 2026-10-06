"""chat_models.py - which local models may word free talk: the list, the pick, and the check before use (J 2026-10-05).

J: "I guarantee there will be some lonely user which swaps our 3B model for a 27B model and then wants Elah and
Montaigne to be their best friends. We should also have a drop down that ... auto-detects local models to make that
easy." And, the same evening: the hardware limit is a HARD limit. A 27B model beside Star Citizen on a 1080 Ti with
16 GB of RAM must not crash the game or overload the machine, and no setting may get past that.

So this module does three things, none of which loads a model or asks one anything:

  installed()   the models Ollama has on this PC, with their size on disk. ONE request to Ollama's tag list, short
                timeout. Ollama not running is not an error: the answer is None and the window says so.
  choose()      the pick in the drop-down. The model's need is set against the memory that is free right now
                (hardware_guard.fits). A model that does not fit is REFUSED: it is not written to the settings, so
                the talk path can never be handed it.
  UseCheck      the same check again when the talk path is about to use a model chosen earlier (the game may have
                started since). A model that no longer fits is "cannot be asked" to chat_talker, which then answers
                as with chat off. Cheap: the answer is kept for FIT_RECHECK_S.

Only gemma3:4b has been measured with the prompt chat_talker sends (elah-audio/_suit_chat_eval.md). Every other
model is listed and marked untested, not hidden.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Callable, Optional

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import hardware_guard as hg                       # noqa: E402

TESTED = ("gemma3:4b",)                 # measured with the current prompt; everything else is listed as untested
LINE_MODEL_PREFIXES = ("suitmk2-", "realizer-")   # the two speakers' own line models (pair_realizer.MODEL_PREFIXES)
TAGS_TIMEOUT_S = 1.5                    # the list is asked for when the panel opens: never a long wait
FIT_RECHECK_S = 30.0                    # how long one use-time answer is kept
NONE_FOUND = "No local models found: Ollama is not running, or has none installed."


def _manager(url: Optional[str]):
    import ollama_manager as om
    return om.OllamaManager(url) if url else om.OllamaManager()


def installed(url: Optional[str] = None, timeout: float = TAGS_TIMEOUT_S, get: Optional[Callable] = None) -> Optional[list]:
    """[{"name": "gemma3:4b", "size": bytes on disk or None}, ...] sorted by name, or None when Ollama does not
    answer. get(path, timeout) -> dict | None stands in for the request in tests; otherwise it is the existing
    ollama_manager helper. Never raises."""
    try:
        t = (get or _manager(url)._get)("/api/tags", timeout=timeout)
    except Exception:
        return None
    if not isinstance(t, dict):
        return None
    out = []
    for m in t.get("models") or []:
        if not isinstance(m, dict):
            continue
        name = str(m.get("name") or m.get("model") or "").strip()
        size = m.get("size")
        if name:
            out.append({"name": name, "size": int(size) if isinstance(size, (int, float)) and size > 0 else None})
    return sorted(out, key=lambda m: m["name"].lower())


def loaded(url: Optional[str] = None, timeout: float = TAGS_TIMEOUT_S, get: Optional[Callable] = None) -> Optional[set]:
    """Names of the models Ollama holds in memory right now (/api/ps), or None when it cannot be told."""
    try:
        t = (get or _manager(url)._get)("/api/ps", timeout=timeout)
    except Exception:
        return None
    if not isinstance(t, dict):
        return None
    return {str(m.get("name") or m.get("model") or "") for m in t.get("models") or [] if isinstance(m, dict)}


def size_of(name: str, models: Optional[list]) -> Optional[int]:
    """The size on disk of a listed model, or None: not listed, or listed without a size."""
    want = str(name or "").strip().lower()
    for m in models or []:
        n = m["name"].lower()
        if n == want or (n.endswith(":latest") and n[: -len(":latest")] == want):
            return m.get("size")
    return None


def is_installed(name: str, models: Optional[list]) -> bool:
    want = str(name or "").strip().lower()
    return bool(want) and any(m["name"].lower() in (want, want + ":latest") for m in models or [])


def entry_label(name: str) -> str:
    """What the drop-down shows for one model. Untested models are marked, never hidden."""
    if name.lower() in TESTED:
        return f"{name} (tested)"
    if name.lower().startswith(LINE_MODEL_PREFIXES):
        return f"{name} (a speaker's line model; untested for talk)"
    return f"{name} (untested)"


def choose(s: dict, name: str, models: Optional[list], free: Optional[hg.FreeMemory]) -> tuple:
    """The drop-down picked `name`. (True, why) and s["chat_model"] is set when it may be used; (False, why) and s
    is NOT touched when it may not. An empty name clears the choice (and chat with it: there is nothing to talk with).
    The caller saves s."""
    name = str(name or "").strip()
    if not name:
        s["chat_model"], s["chat"] = "", False
        return True, "No chat model chosen. Free talk is off."
    if models is None:
        return False, "Ollama is not reachable, so this model cannot be checked. Not chosen."
    if not is_installed(name, models):
        return False, f"{name} is not installed in Ollama. Not chosen."
    ok, why = hg.fits(size_of(name, models), free)
    if not ok:
        return False, f"{name}: {why} Not chosen."
    s["chat_model"] = name
    return True, f"{name}: {why}"


def why_chat_cannot_be_on(s: dict, models: Optional[list]) -> str:
    """"" when the chat checkbox may be ticked; otherwise the sentence the window shows instead of ticking it."""
    name = str(s.get("chat_model") or "").strip()
    if not name:
        return "Pick a chat model first."
    if models is None:
        return "Ollama is not reachable, so free talk cannot be turned on."
    if not is_installed(name, models):
        return f"{name} is not installed in Ollama any more. Pick another model."
    return ""


class UseCheck:
    """callable() -> (ok, why): may the talk path use this model NOW. Handed to chat_talker.Talker as `fit`.

    A model Ollama already holds in memory has its memory already; unloading it is Ollama's own doing and the
    headroom rule's, not this check's. Otherwise the model's size is set against the memory free at this moment.
    Anything that cannot be told (Ollama not answering, no size, no memory reading for a model above the small
    size) is a no. The three readers are parameters so tests hand in fakes."""

    def __init__(self, model: str, url: Optional[str] = None, now: Callable[[], float] = time.time,
                 tags: Optional[Callable[[], Optional[list]]] = None,
                 ps: Optional[Callable[[], Optional[set]]] = None,
                 free: Optional[Callable[[], Optional[hg.FreeMemory]]] = None, recheck_s: float = FIT_RECHECK_S):
        self.model, self._now, self.recheck_s = str(model).strip(), now, float(recheck_s)
        self._tags = tags or (lambda: installed(url))
        self._ps = ps or (lambda: loaded(url))
        self._free = free or hg.read_free_memory
        self._kept: Optional[tuple] = None           # (when, (ok, why))
        self.checks = 0

    def __call__(self) -> tuple:
        t = self._now()
        if self._kept is not None and 0 <= t - self._kept[0] < self.recheck_s:
            return self._kept[1]
        self.checks += 1
        try:
            held = self._ps()
            if held and any(h.lower() in (self.model.lower(), self.model.lower() + ":latest") for h in held):
                answer = (True, "already loaded")
            else:
                answer = hg.fits(size_of(self.model, self._tags()), self._free())
        except Exception as e:                        # a check that cannot run is a no, never a yes
            answer = (False, f"the fit check could not run ({type(e).__name__})")
        self._kept = (t, answer)
        return answer
