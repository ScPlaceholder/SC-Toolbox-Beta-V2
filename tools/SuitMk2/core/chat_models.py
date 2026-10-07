"""chat_models.py - which local models may word free talk: the list, the pick, and the check before use.

Some lonely user will swap the 3B model for a 27B model and then want Elah and
Montaigne to be their best friends. So there is a drop down that auto-detects local models to make that
easy. And the hardware limit is a HARD limit. A 27B model beside Star Citizen on a 1080 Ti with
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

Only gemma3:4b has been measured with the prompt chat_talker sends. Every other
model is listed and marked untested, not hidden.

THE OFFER (the last part of this file). Free talk needs a heavier model than the two speakers' own line models, and
nothing downloads it unless the player says yes. offer() words the question for the pop-up (ui/chat_offer.py) from
what is installed and what memory is free; fetch() downloads it through the same pull the first-run setup uses;
take() chooses it as the chat model through choose() above, so the fit check is the same one; remove() deletes it.
None of them turns free talk on: that stays the checkbox.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from dataclasses import dataclass
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


# ---- the offer: Gemma, for talking with the companions ---------------------------------------------------------------
OFFER = TESTED[0]                       # the one model measured with the prompt in use
OFFER_NAME = "Gemma"
# The download: the sum of the layers in the registry manifest for gemma3:4b (read 2026-10-06), which is also the
# size Ollama lists once it is installed. 3.3 GB as a download is quoted; the setup panel says the same figure.
OFFER_DOWNLOAD_BYTES = 3_338_801_804
OFFER_TITLE = "Talk with Elah and Montaigne?"
NOT_NOW = "Not now"
CLOSE = "Close"
FETCH_LABEL = "Downloading Gemma"
SWITCH_LABEL = "Free talk (chat model)"            # the checkbox in the Suit Mk2 tab; ui/suit_window.py uses this
GET_BUTTON = "Get Gemma for talking..."
USE_BUTTON = "Use Gemma for talking..."
REMOVE_BUTTON = "Remove Gemma..."
STOP_BUTTON = "Stop the download"
REMOVE_TITLE = "Remove Gemma?"
REMOVE_YES = "Remove Gemma"


def download_text() -> str:
    """"3.3 GB": the download as the player is told it."""
    return f"{OFFER_DOWNLOAD_BYTES / 1e9:.1f} GB"


def serves_eyes() -> bool:
    """The optional closer look at the screen uses this same model, so it is one download for both."""
    try:
        import model_provision as mp
        return mp.VISION_TAG == OFFER
    except Exception:
        return False


@dataclass
class Offer:
    """What the pop-up shows. kind: "download" (not on this PC; yes downloads it), "select" (already here; yes only
    chooses it), "tight" (this PC has no room for it now: nothing is offered), "unreachable" (Ollama does not
    answer, so nothing can be downloaded from here). yes is the label of the yes button, "" when there is none."""
    kind: str
    headline: str
    body: str
    yes: str = ""
    no: str = NOT_NOW

    @property
    def text(self) -> str:
        return self.headline + "\n\n" + self.body


def _memory_lines() -> tuple:
    need = hg.need_bytes(OFFER_DOWNLOAD_BYTES)
    reserve = hg.VRAM_RESERVE_GB * hg.GB
    return hg._gb(need), hg._gb(reserve), hg._gb(need + reserve)


def offer(models: Optional[list], free: Optional[hg.FreeMemory], setup: bool = False) -> Offer:
    """The question, worded from what is installed (models: installed(), None = Ollama did not answer) and what
    memory is free. setup=True is the first-run button: Ollama may not be installed yet, and the setup itself
    installs it, so "does not answer" is not a reason to withhold the offer there. Pure: asks nothing."""
    need, reserve, both = _memory_lines()
    here = is_installed(OFFER, models)
    size = size_of(OFFER, models) if here else None
    ok, why = hg.fits(size or OFFER_DOWNLOAD_BYTES, free)
    without = "Elah and Montaigne's normal comments work without it."
    if not ok:
        return Offer("tight", f"{OFFER_NAME} is not offered on this PC right now",
                     f"Talking back and forth with Elah and Montaigne needs a heavier model, {OFFER_NAME} ({OFFER}), "
                     f"and right now this PC does not have the room for it: {why}\n\n"
                     f"Nothing was downloaded and nothing was changed. {without}\n\n"
                     "You can try again later from the Suit Mk2 tab, with more memory free (for example with the "
                     "game closed).", no=CLOSE)
    if models is None and not setup:
        return Offer("unreachable", f"{OFFER_NAME} cannot be set up right now",
                     f"Ollama is not running, so {OFFER_NAME} can be neither checked nor downloaded. Nothing was "
                     f"changed. {without}\n\nRun the setup at the top of the Suit Mk2 tab, or start Ollama, and "
                     "try again.", no=CLOSE)
    memory = (f"Video memory: about {need} while it is in use. The Suit only uses it when about {both} is free, "
              f"so that {reserve} stays free for the game, and it stands down by itself when the PC is busy.")
    later = "You can add it later from the Suit Mk2 tab, and remove it again there."
    if here:
        return Offer("select", f"{OFFER_NAME} is already on this PC",
                     f"{OFFER_NAME} ({OFFER}) is the heavier model that lets you talk back and forth with Elah and "
                     "Montaigne. It is already on this PC, so there is nothing to download.\n\n"
                     f"It is only worth using if you plan to talk with the companions. {without}\n\n"
                     f"{memory}\n\nChoose it as the model for talking? You can change this later in the Suit Mk2 "
                     "tab.", yes=f"Use {OFFER_NAME} for talking")
    one = (" It is one download that serves both: the same model also gives the companions their optional closer "
           "look at the screen.") if serves_eyes() else ""
    return Offer("download", f"Also get {OFFER_NAME}, for talking with the companions?",
                 f"{without} Nothing more is needed for those.\n\n"
                 f"Talking back and forth with them needs a heavier model, {OFFER_NAME} ({OFFER}). It is only worth "
                 "getting if you plan to talk with the companions.\n\n"
                 f"Download: about {download_text()}.{one}\n"
                 f"{memory}\n\n{later}",
                 yes=f"Download {OFFER_NAME} (about {download_text()})")


def fetch(url: Optional[str] = None, progress: Optional[Callable] = None, cancel=None) -> tuple:
    """Download the offered model through the pull the first-run setup uses (model_provision.Provisioner.pull:
    streamed progress, and Ollama resumes a partial download itself). (True, "pulled" | "present") or
    (False, one plain sentence). Changes no setting and never raises."""
    try:
        import model_provision as mp
    except Exception as e:
        return False, f"{OFFER_NAME} could not be downloaded: the downloader did not load ({type(e).__name__})."
    try:
        return True, mp.Provisioner(url, progress=progress, cancel=cancel).pull(OFFER, FETCH_LABEL)
    except mp.Cancelled:
        return False, (f"The {OFFER_NAME} download was stopped. Nothing was changed; starting it again carries on "
                       "from where it stopped.")
    except Exception as e:
        reason = " ".join(str(e).split())[:160] or type(e).__name__
        return False, (f"{OFFER_NAME} could not be downloaded ({reason}). Nothing was changed, and Elah and "
                       "Montaigne's normal comments work without it. You can try again from the Suit Mk2 tab.")


def take(s: dict, models: Optional[list], free: Optional[hg.FreeMemory]) -> tuple:
    """Choose the offered model as the chat model, through choose() and so through the same fit check. (ok, what
    the window says). s["chat"] is NOT touched: free talk stays whatever the checkbox was. The caller saves s."""
    ok, why = choose(s, OFFER, models, free)
    if not ok:
        return False, f"{OFFER_NAME} is on this PC but was not chosen for talking. {why}"
    if s.get("chat") is True:
        return True, f"{OFFER_NAME} is now the model for talking, and free talk is on."
    return True, (f"{OFFER_NAME} is chosen for talking. Free talk is still off: tick \"{SWITCH_LABEL}\" to start "
                  "talking.")


def remove_question() -> str:
    eyes = (" The companions' optional closer look at the screen uses the same model and stops working too."
            if serves_eyes() else "")
    return (f"This deletes {OFFER_NAME} ({OFFER}) from this PC and frees about {download_text()} of disk. Free talk "
            f"is switched off.{eyes}\n\nElah and Montaigne's normal comments are not affected. You can download it "
            "again later.")


def remove(s: dict, url: Optional[str] = None, timeout: float = 30.0) -> tuple:
    """Delete the offered model from Ollama. (ok, sentence). On success the choice is cleared if it was this model
    (and free talk with it: there is nothing left to talk with). The caller saves s. Never raises."""
    base = (url or _manager(None).url).rstrip("/")
    req = urllib.request.Request(base + "/api/delete", data=json.dumps({"model": OFFER}).encode(), method="DELETE",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            pass
    except Exception as e:
        reason = " ".join(str(e).split())[:160] or type(e).__name__
        return False, f"{OFFER_NAME} could not be removed ({reason}). Nothing was changed."
    if str(s.get("chat_model") or "").strip().lower() in (OFFER, OFFER + ":latest"):
        choose(s, "", None, None)
    return True, f"{OFFER_NAME} was removed from this PC. Free talk is off."
