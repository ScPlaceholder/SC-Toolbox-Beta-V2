"""dev_facts.py - OPTIONAL "fun fact from the dev history" asides for the SuitMk2 companions (J 2026-09-25).

OFF BY DEFAULT (settings "dev_facts": False). A dev fact breaks the fourth wall, and some pilots want full immersion.
When the pilot turns it on:

  * WHAT: a fact comes ONLY from the Star Citizen dev-history corpus (sc_dev_history.py: 1,368 dev-video transcripts +
    5,152 comm-links). The line is a TEMPLATE filled from one document's title, kind and date. No model words it, so
    no model can invent it. Every template still goes through grounding_validator.ground(), which for a dev fact also
    demands the aside frame, the date, no URL and no claim of memory (the dev-fact block there), so a model rephrasing
    one later is held to the same source.
  * HOW IT IS SAID: always framed as its own aside ("Fun fact from the dev history: ..."), one sentence plus the date,
    never as something a character remembers. ONE companion only: Montaigne, the ship who "knows things only
    secondhand". Elah never says one. Nothing about it is written to the pilot's memory.
  * WHEN: only in a quiet moment, decided by the core (CompanionCore._dev_fact_unquiet): no combat, no injury or death
    on record, no event line in the last few minutes, not AFK, not shaken, PRESENCE mode, nothing queued, no
    not-now snooze. The normal gate, pacing and quiet budget still apply on top.
  * TOPICAL first: the current ship, then what the pilot is doing (mining, salvage, cargo, quantum travel), then
    where they are. A random fact only when nothing topical is found. Never the same document twice in a session.
  * RATE: at most `dev_facts_max_per_hour` (default 2) per rolling hour.
  * OFF THE HOT PATH: nothing here runs on the game-event path (note_event is a deque append). The core's ambient tick
    asks poll(); poll never blocks: it hands back an aside prepared earlier, or starts ONE background worker that
    loads the cached index (or fetches it with a short socket timeout) and prepares the next. Offline with no cache =
    no aside at all, no error, and a quiet retry half an hour later.
  * VOICE TOGGLE (J 2026-09-25): "fun facts on" / "fun facts off" and close variants (voice_toggle below) flip it
    mid-session without the settings dialog; Montaigne acknowledges in one line and the choice is saved.

THE INTERRUPTED FACT (J 2026-09-25, Montaigne only): if a Tier 2 or 3 injury lands while a fact is still being said,
the fact is cut, the normal injury call is said unchanged, and once nothing urgent has happened for a short while
Montaigne picks the fact back up ("Since you are still among the living, back to that fun fact from the dev
history: ..."). Once per session at most. A Tier 1 injury, a death, combat, a second urgent event while it waits, or
the pilot turning fun facts off DROPS the fact for good. The core owns the timing; this module owns the text.

    python dev_facts.py --selftest [path\\to\\Game.log]     fixture corpus + a real Game.log replay (no models)
    python dev_facts.py --examples                          asides from the REAL cached index (offline if cached)
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import random
import re
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Optional

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
log = logging.getLogger("suitmk2.dev_facts")

SCENARIO = "dev_fact"
RESUME_SCENARIO = "dev_fact_resume"
TOGGLE_SCENARIO = "dev_fact_toggle"
FRAME = "Fun fact from the dev history:"
FRAME_KEY = "Fun fact from the dev history"          # every frame starts with it; the gate checks for it
# Montaigne may insist on his sources (J 2026-09-25): technically right in the real world, hallucinating in the lore.
FRAMES = (FRAME, "Fun fact from the dev history, straight from the archives:",
          "Fun fact from the dev history, and yes, it is in the archives:")
RESUME_LEAD = "Since you are still among the living, back to that fun fact from the dev history:"
SPEAKER = "montaigne"                 # ONE companion only. Elah never says a dev fact.
DEFAULT_MAX_PER_HOUR = 2
FETCH_TIMEOUT_S = 5.0                 # per socket operation; the fetch never runs on the game-event path anyway
RETRY_UNAVAILABLE_S = 1800.0          # offline and uncached: try again in half an hour, silently
MAX_TITLE_WORDS = 14
REF = os.environ.get("SC_DEV_HISTORY_REF", "corpus")     # "corpus" until the corpus PR merges

# Montaigne acknowledges a toggle. Fixed lines: no facts, no numbers, nothing for a model to get wrong.
ACK_ON = ("Splendid. When it is quiet, I shall share what the archives say.",
          "Very well, the dev history it is, in the quiet moments.")
ACK_OFF = ("Very well, I shall keep my erudition to myself.",
           "As you wish. The archives stay closed.")

# ELAH'S CALLOUT (J 2026-09-25). Elah is fully in-universe: she does not know she is in a game, so to her Montaigne
# talking about CIG, patches and builds is Montaigne glitching. Sometimes (CALLOUT_P, and never twice inside
# CALLOUT_MIN_GAP_S) she follows his fact with one of these. In-universe, no facts of their own, no numbers, no named
# feelings (grounding's ELAH_NAMES_FEELING applies), never the words "dev history" (she never delivers a dev fact).
CALLOUT_P = 1 / 3
CALLOUT_MIN_GAP_S = 1800.0
ELAH_CALLOUTS = ("You're glitching again. There's no CIG.",
                 "Run a diagnostic, Montaigne.",
                 "Patch notes? We're in a ship, not a spreadsheet.",
                 "Archives. Right. Your memory core is leaking again.",
                 "He's hallucinating again. Ignore him.",
                 "There's no such thing as a build, Montaigne. There's the ship.",
                 "Those aren't archives, Montaigne. That's static.",
                 "Nobody made this place on a schedule. Recalibrate.",
                 "Again with the archives. Check your sensors.",
                 "Whatever you read, it wasn't in any archive I can find.",
                 "Montaigne, the stars were not released. They were here.",
                 "Somebody reseat his logic board.")
CALLOUT_NAMES = ["CIG"]


def callout_spec(text: str) -> dict:
    n = len(text.split())
    return {"id": "dev_fact_callout", "scenario": "dev_fact_callout", "speaker": "elah", "fixed_text": text,
            "claims": [], "required_values": [], "length_words": [max(1, n - 3), n], "allowed_names": CALLOUT_NAMES}


_MONTHS = ("January February March April May June July August September October November December").split()
# Titles that are real but make a dull "fun fact" (store promotions, schedules). Skipped, never rewritten.
_DULL = re.compile(r"\b(?:promotions?|subscriber|giveaway|free fly|patch notes|known issues|schedule|"
                   r"roadmap roundup|this week in star citizen|sale)\b", re.I)

# ---- the voice toggle -----------------------------------------------------------------------------------------------
_N = r"(?:(?:fun|dev|development|dev history|developer)\s+facts?|trivia)"
_ADDR = r"^(?:(?:hey|ok|okay|so|um|uh|right|elah|montaigne|ship|suit|monty)[, ]+)*"
_ON = [rf"^{_N} (?:back )?on$", rf"^(?:turn|switch|put) (?:the )?{_N} (?:back )?on$",
       rf"^(?:turn|switch) on (?:the )?{_N}$", rf"^(?:enable|start|resume|restart) (?:the )?{_N}$",
       rf"^(?:some )?more {_N}$", rf"^(?:i want|give me|let's have|lets have|bring back) (?:some |the )?(?:more )?{_N}$"]
_OFF = [rf"^{_N} off$", rf"^(?:turn|switch) (?:the )?{_N} off$", rf"^(?:turn|switch) off (?:the )?{_N}$",
        rf"^(?:disable|stop|pause|end|cancel|kill) (?:the |with the )?{_N}$",
        rf"^(?:no more|enough|enough with the|enough of the|that's enough|thats enough) {_N}$"]
_ON_RX = [re.compile(p) for p in _ON]
_OFF_RX = [re.compile(p) for p in _OFF]
VOICE_ON_EXAMPLES = ("fun facts on", "turn on dev facts", "turn the fun facts on", "more fun facts",
                     "enable fun facts", "start the fun facts", "give me some fun facts", "dev history facts on",
                     "Montaigne, fun facts on please", "trivia on")
VOICE_OFF_EXAMPLES = ("fun facts off", "stop the fun facts", "turn off the fun facts", "turn the dev facts off",
                      "no more fun facts", "enough with the fun facts", "disable fun facts", "trivia off",
                      "Elah, fun facts off.")


def voice_toggle(text: str) -> Optional[bool]:
    """True = turn dev facts on, False = off, None = not a toggle. Short utterances only (a question that merely
    mentions fun facts goes to the conversation lane)."""
    t = " ".join(str(text or "").lower().replace(",", ", ").split()).strip(" .!?")
    t = re.sub(r"[.!?]", "", t)
    t = re.sub(r"\s*,\s*", " ", t)
    t = re.sub(r"\b(?:please|now|thanks|thank you)\b", " ", t)
    t = re.sub(_ADDR, "", " ".join(t.split())).strip()
    if not t or len(t.split()) > 8:
        return None
    if any(r.match(t) for r in _OFF_RX):
        return False
    if any(r.match(t) for r in _ON_RX):
        return True
    return None


def toggle_ack_spec(on: bool, variant: int = 0) -> dict:
    lines = ACK_ON if on else ACK_OFF
    text = lines[variant % len(lines)]
    n = len(text.split())
    return {"id": f"{TOGGLE_SCENARIO}:{'on' if on else 'off'}", "scenario": TOGGLE_SCENARIO, "speaker": SPEAKER,
            "fixed_text": text, "claims": [], "required_values": [], "length_words": [max(1, n - 3), n],
            "allowed_names": []}


# ---- what the pilot is doing ------------------------------------------------------------------------------------
_MFR = {"aegis", "aegs", "anvil", "anvl", "argo", "banu", "consolidated", "outland", "cnou", "crusader", "crus",
        "drake", "drak", "esperia", "espr", "gatac", "gama", "greycat", "grin", "kruger", "krig", "misc", "origin",
        "orig", "rsi", "tumbril", "tmbl", "aopoa", "xian", "mirai", "mrai", "vehicle", "name"}
SHIP_ACTIVITY = {"prospector": "mining", "mole": "mining", "golem": "mining", "arrastra": "mining", "orion": "mining",
                 "roc": "mining", "vulture": "salvage", "reclaimer": "salvage", "fortune": "salvage",
                 "hull": "cargo", "caterpillar": "cargo", "raft": "cargo", "starlifter": "cargo"}
EVENT_ACTIVITY = {"refinery_complete": "mining", "platform_moving": "cargo", "qt_arrived": "quantum",
                  "qt_route_calculated": "quantum", "qt_target_selected": "quantum"}
CONTRACT_ACTIVITY = (("mining", re.compile(r"\b(?:mining|mine|ore|quantanium|refin\w*)\b", re.I)),
                     ("salvage", re.compile(r"\bsalvag\w*\b", re.I)),
                     ("cargo", re.compile(r"\b(?:cargo|haul\w*|deliver\w*|freight)\b", re.I)))
# activity -> (search query, the words said aloud). Spoken words are only ever the query's own words.
ACTIVITY_QUERY = {"mining": ("mining", "mining"), "salvage": ("salvage", "salvage"),
                  "cargo": ("cargo hauling", "cargo hauling"), "quantum": ("quantum travel", "quantum travel")}
ACTIVITY_WINDOW_S = 1800.0

_SHIP_NAMES: Optional[list] = None


def _ship_names() -> list:
    global _SHIP_NAMES
    if _SHIP_NAMES is None:
        try:
            rows = json.loads((HERE.parent / "data" / "ships.json").read_text(encoding="utf-8")).get("ships", [])
            _SHIP_NAMES = sorted({str(r["name"]) for r in rows if r.get("name")}, key=len, reverse=True)
        except Exception:
            _SHIP_NAMES = []
    return _SHIP_NAMES


def clean_ship(raw) -> Optional[str]:
    """'MISC Prospector : PilotName' / '@vehicle_NameDRAK_Golem_OX' / 'AEGS_Reclaimer_123' -> 'Prospector' etc."""
    if not raw:
        return None
    s = str(raw).split(" : ")[0]
    s = re.sub(r"^@?vehicle_?name", "", s, flags=re.I)
    s = re.sub(r"^(?:AEGS|ANVL|ARGO|BANU|CNOU|CRUS|DRAK|ESPR|GAMA|GRIN|KRIG|MISC|ORIG|RSI|TMBL|XIAN|MRAI)(?=[_ ])", "",
               s)
    norm = " ".join(w for w in s.replace("_", " ").replace("-", " ").split() if not w.isdigit())
    low = f" {norm.lower()} "
    for name in _ship_names():
        if len(name) >= 3 and f" {name.lower()} " in low:
            return name
    words = [w for w in norm.split() if w.lower() not in _MFR]
    return " ".join(words[:3]) or None


def _spoken_date(d: str) -> Optional[str]:
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", str(d or ""))
    if not m:
        return None
    y, mo, da = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (1 <= mo <= 12 and 1 <= da <= 31):
        return None
    return f"{da} {_MONTHS[mo - 1]} {y}"


def _spoken_title(t: str) -> str:
    t = re.sub(r"\(\s*[\d.\-/ ]+\s*\)", " ", str(t))           # "(2014.03.17)" is a date stamp, not a title
    t = re.sub(r"[\"“”‘’`]", "", t)                           # quotation marks are refused by the gate
    t = re.sub(r"\s*[|:]\s*", ", ", t)
    t = re.sub(r"\s+", " ", t).strip(" ,.-")
    return t


def build_aside(doc: dict, topic: Optional[str], in_title: bool, frame: str = FRAME) -> Optional[dict]:
    """One dev-history document -> a Montaigne aside spec with its fixed text. None when it cannot be said cleanly."""
    title, date = _spoken_title(doc.get("t", "")), _spoken_date(doc.get("d", ""))
    if not title or not date or len(title.split()) > MAX_TITLE_WORDS:
        return None
    kind = "video" if doc.get("k") == "v" else "comm-link"
    if topic and not in_title:
        body = f"{topic} came up in the {kind} {title}, on {date}."
    else:
        body = f"the {kind} {title} came out on {date}."
    return _spec(doc, f"{frame} {body}", body, title, date, kind, topic, SCENARIO)


def resume_spec(spec: dict) -> dict:
    """The same fact, picked back up after an injury (Montaigne only; the core decides WHEN)."""
    src = spec["source"]
    doc = {"t": src["title_raw"], "d": src["date"], "k": src["k"], "id": src["id"], "url": src.get("url")}
    return _spec(doc, f"{RESUME_LEAD} {spec['body']}", spec["body"], src["title"], spec["date_spoken"], src["kind"],
                 spec.get("topic_words"), RESUME_SCENARIO)


def _spec(doc, text, body, title, date, kind, topic, scenario) -> dict:
    n = len(text.split())
    claims = [{"id": "C1", "predicate": "devfact.title", "value": title},
              {"id": "C2", "predicate": "devfact.date", "value": str(doc.get("d", ""))},
              {"id": "C3", "predicate": "devfact.date_spoken", "value": date},
              {"id": "C4", "predicate": "devfact.kind", "value": kind}]
    if topic:
        claims.append({"id": "C5", "predicate": "devfact.topic", "value": topic})
    return {"id": f"{scenario}:{doc.get('id')}", "scenario": scenario, "aside": "dev_fact", "speaker": SPEAKER,
            "fixed_text": text, "body": body, "date_spoken": date, "topic_words": topic,
            "claims": claims, "required_values": [], "length_words": [max(1, n - 3), n],
            "allowed_names": [],          # name gate ON: only words from the title/date/topic may be capitalised
            "source": {"id": doc.get("id"), "title": title, "title_raw": doc.get("t", ""), "date": doc.get("d", ""),
                       "kind": kind, "k": doc.get("k"), "url": doc.get("url") or doc.get("u")}}


# ---- the engine (sc_dev_history.py), found without copying it --------------------------------------------------
def load_engine():
    """sc_dev_history, imported from wherever it lives: already importable; next to this file or the tool; a deployed
    Dev_History tool beside SuitMk2; the elah-audio staging layout (toolbox_port/dev_history); or env
    SC_DEV_HISTORY_ENGINE_DIR. None if nowhere (then dev facts are simply silent)."""
    if "sc_dev_history" in sys.modules:
        return sys.modules["sc_dev_history"]
    cands = [HERE, HERE.parent, HERE.parent.parent / "Dev_History", HERE.parents[2] / "dev_history"]
    if os.environ.get("SC_DEV_HISTORY_ENGINE_DIR"):
        cands.insert(0, Path(os.environ["SC_DEV_HISTORY_ENGINE_DIR"]))
    for d in cands:
        f = d / "sc_dev_history.py"
        if f.is_file():
            try:
                spec = importlib.util.spec_from_file_location("sc_dev_history", f)
                m = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(m)          # type: ignore
                sys.modules["sc_dev_history"] = m
                return m
            except Exception:
                log.info("dev facts: could not load %s", f)
    return None


class DevFacts:
    """The sidecar. enabled=False (the default) means poll() returns None and nothing is ever loaded or fetched."""

    def __init__(self, enabled: bool = False, max_per_hour: int = DEFAULT_MAX_PER_HOUR,
                 history_factory: Optional[Callable[[], object]] = None, now: Callable[[], float] = time.time,
                 sync: bool = False, rng: Optional[random.Random] = None, ground: Optional[Callable] = None):
        self.enabled = bool(enabled)
        self.max_per_hour = int(max_per_hour or 0)
        self.now, self.sync = now, sync
        self._factory = history_factory or self._default_history
        self._dh = None
        self._rng = rng or random.Random()
        if ground is None:
            from grounding_validator import ground as _g
            ground = _g
        self._ground = ground
        self._lock = threading.Lock()
        self._worker: Optional[threading.Thread] = None
        self._ready: Optional[tuple] = None          # (context key, spec)
        self._unavailable_until = -1e18
        self.used: set = set()                       # doc ids SPOKEN this session: never twice
        self._spoken_t: deque = deque()
        self._events: deque = deque(maxlen=64)       # (t, activity) from the game events that imply one
        self._rot = 0
        self._last_callout = -1e18
        self._callout_bag: list = []
        self.stats = {"polls": 0, "prepared": 0, "spoken": 0, "unavailable": 0, "no_doc": 0, "ungrounded": 0,
                      "callouts": 0}

    @classmethod
    def from_settings(cls, s: dict, **kw) -> "DevFacts":
        return cls(enabled=bool(s.get("dev_facts", False)),
                   max_per_hour=int(s.get("dev_facts_max_per_hour", DEFAULT_MAX_PER_HOUR) or 0), **kw)

    @staticmethod
    def _default_history():
        eng = load_engine()
        if eng is None:
            raise RuntimeError("sc_dev_history engine not found")

        def fetch(path: str) -> bytes:
            return eng._http_get(eng.raw_url(path, REF), timeout=FETCH_TIMEOUT_S)
        return eng.DevHistory(fetch=fetch, ref=REF)

    # -- inputs ------------------------------------------------------------------------------------------------------
    def note_event(self, et: str, data: Optional[dict] = None) -> None:
        """Cheap (a deque append): called from the core's event path. Never loads or fetches anything."""
        act = EVENT_ACTIVITY.get(et)
        if act is None and et in ("contract_accepted", "objective_new"):
            txt = str((data or {}).get("mission_name") or (data or {}).get("objective") or "")
            act = next((a for a, rx in CONTRACT_ACTIVITY if rx.search(txt)), None)
        if act:
            self._events.append((self.now(), act))

    def activity(self, st: dict) -> Optional[str]:
        now = self.now()
        for t, act in reversed(self._events):
            if now - t <= ACTIVITY_WINDOW_S:
                return act
        ship = (clean_ship(st.get("ship")) or "").lower()
        return next((a for k, a in SHIP_ACTIVITY.items() if k in ship.split()), None)

    def topics(self, st: dict) -> list:
        """[(query, spoken topic words)] in order: ship, activity, place, system. Rotated a step each time a fact is
        spoken so one ship does not own every fact of the night."""
        out = []
        ship = clean_ship(st.get("ship"))
        if ship:
            out.append((ship, f"the {ship}"))
        act = self.activity(st)
        if act:
            out.append(ACTIVITY_QUERY[act])
        for key in ("location", "planetary_body", "system"):
            v = st.get(key)
            if v and not any(str(v) == o[0] for o in out):
                out.append((str(v), str(v)))
        if out:
            r = self._rot % len(out)
            out = out[r:] + out[:r]
        return out

    # -- the rate cap --------------------------------------------------------------------------------------------
    def under_cap(self) -> bool:
        now = self.now()
        while self._spoken_t and now - self._spoken_t[0] >= 3600.0:
            self._spoken_t.popleft()
        return self.max_per_hour > 0 and len(self._spoken_t) < self.max_per_hour

    def spoken(self, spec: dict) -> None:
        """The core calls this when a dev fact was actually SAID. A resume is the same fact: not counted twice."""
        if spec.get("scenario") != SCENARIO:
            return
        self.used.add((spec.get("source") or {}).get("id"))
        self._spoken_t.append(self.now())
        self._rot += 1
        self.stats["spoken"] += 1

    # -- Elah's in-universe callout ------------------------------------------------------------------------------
    def callout_for(self, fact_spec: dict) -> Optional[dict]:
        """After a spoken fact: sometimes (CALLOUT_P) an Elah callout, never inside CALLOUT_MIN_GAP_S of the last one.
        Drawn from a shuffled bag so no line repeats until every line has been used. The CORE decides whether the
        moment still allows it (no urgency) and records it with callout_spoken()."""
        if fact_spec.get("scenario") != SCENARIO or self.now() - self._last_callout < CALLOUT_MIN_GAP_S:
            return None
        if self._rng.random() >= CALLOUT_P:
            return None
        if not self._callout_bag:
            self._callout_bag = list(ELAH_CALLOUTS)
            self._rng.shuffle(self._callout_bag)
        return callout_spec(self._callout_bag.pop())

    def callout_spoken(self) -> None:
        self._last_callout = self.now()
        self.stats["callouts"] += 1

    # -- poll (ambient thread, never blocks) ---------------------------------------------------------------------
    @staticmethod
    def context_key(st: dict) -> tuple:
        return (clean_ship(st.get("ship")), st.get("location"), st.get("system"))

    def poll(self, st: dict) -> Optional[dict]:
        if not self.enabled:
            return None
        self.stats["polls"] += 1
        if not self.under_cap() or self.now() < self._unavailable_until:
            return None
        key = self.context_key(st)
        with self._lock:
            ready, self._ready = self._ready, None
        if ready is not None:
            k, spec = ready
            if k == key and spec["source"]["id"] not in self.used:
                return spec
        self._kick(dict(st), key)
        if self.sync:
            with self._lock:
                ready, self._ready = self._ready, None
            if ready is not None:
                return ready[1]
        return None

    def _kick(self, st: dict, key: tuple) -> None:
        if self.sync:
            self._prepare(st, key)
            return
        if self._worker is not None and self._worker.is_alive():
            return
        self._worker = threading.Thread(target=self._prepare, args=(st, key), name="suitmk2_dev_facts", daemon=True)
        self._worker.start()

    def _history(self):
        if self._dh is None:
            self._dh = self._factory()
        return self._dh

    def _prepare(self, st: dict, key: tuple) -> None:
        try:
            dh = self._history()
            dh.index()                       # cached, or ONE fetch with a short timeout; raises offline+uncached
        except Exception as e:
            self._dh = None
            self._unavailable_until = self.now() + RETRY_UNAVAILABLE_S
            self.stats["unavailable"] += 1
            log.info("dev facts unavailable (%s: %s); silent for now", type(e).__name__, e)
            return
        try:
            spec = self._pick(dh, st)
        except Exception:
            log.info("dev facts: pick failed", exc_info=True)
            spec = None
        if spec is None:
            self.stats["no_doc"] += 1
            return
        self.stats["prepared"] += 1
        with self._lock:
            self._ready = (key, spec)

    def _usable(self, spec: Optional[dict]) -> bool:
        if spec is None or spec["source"]["id"] in self.used:
            return False
        if self._ground(spec, spec["fixed_text"]):
            self.stats["ungrounded"] += 1    # e.g. a title with a number word the gate cannot tie to a digit
            return False
        return True

    def _pick(self, dh, st: dict) -> Optional[dict]:
        for query, words in self.topics(st):
            try:
                docs = dh.search(query, 25)
            except Exception:
                continue
            qterms = set(re.findall(r"[a-z0-9][a-z0-9'\-]{2,}", query.lower()))
            titled, body = [], []
            for d in docs:
                if _DULL.search(d.get("t", "")):
                    continue
                if query.lower() in d.get("t", "").lower():
                    titled.append(d)
                elif qterms and d.get("matched", 0) >= len(qterms):
                    body.append(d)
            for group, in_title in ((titled, True), (body, False)):
                cands = [s for s in (build_aside(d, words, in_title, self._rng.choice(FRAMES)) for d in group[:8])
                         if self._usable(s)]
                if cands:
                    return self._rng.choice(cands[:3])
        # Nothing topical: a random document, so the feature still does something on a bare log.
        docs = dh.index().get("docs") or []
        for _ in range(60):
            if not docs:
                break
            d = self._rng.choice(docs)
            if _DULL.search(d.get("t", "")):
                continue
            s = build_aside(dict(d, url=d.get("u")), None, True, self._rng.choice(FRAMES))
            if self._usable(s):
                return s
        return None


# ======================================================================================================================
# selftest
# ======================================================================================================================
def _fixture_history(cache: Path, offline: bool = False):
    """A tiny fake corpus served through the REAL sc_dev_history client (so search and caching are the real code)."""
    import gzip
    eng = load_engine()
    docs = [{"id": "v1", "k": "v", "t": "Q&A: MISC Prospector - Part I", "d": "2016-04-27", "p": "x"},
            {"id": "v2", "k": "v", "t": "Inside Star Citizen: Salvage Operation", "d": "2023-12-20", "p": "x"},
            {"id": "v3", "k": "v", "t": "Around the Verse - The Evolution of Quantum Travel", "d": "2017-11-09",
             "p": "x"},
            {"id": "c1", "k": "c", "t": "Portfolio: Hurston Dynamics", "d": "2013-07-23", "s": "..", "u": "https://r/1"},
            {"id": "c2", "k": "c", "t": "The Observist: Area18, ArcCorp, Stanton", "d": "2014-06-18", "s": "..",
             "u": "https://r/2"},
            {"id": "v4", "k": "v", "t": "Ten for the Chairman: Episode 12 (2014.03.17)", "d": "2014-03-17", "p": "x"},
            {"id": "v5", "k": "v", "t": "Mining Gameplay Deep Dive", "d": "2019-02-05", "p": "x"},
            {"id": "c3", "k": "c", "t": "May 2025 Subscriber Promotions", "d": "2025-05-01", "s": "..", "u": "u"},
            {"id": "v6", "k": "v", "t": "Hauling Cargo Across Stanton", "d": "2022-04-29", "p": "x"},
            {"id": "c4", "k": "c", "t": "2120: Give These People Air", "d": "2012-09-12", "s": "..", "u": "https://r/4"},
            {"id": "v7", "k": "v", "t": "Calling All Devs: Prospector Mining Heads", "d": "2018-06-11", "p": "x"},
            {"id": "v8", "k": "v", "t": "Inside Star Citizen: Refinery Decks", "d": "2021-03-04", "p": "x"}]
    words = {"prospector": [0, 10], "misc": [0], "salvage": [1], "operation": [1], "quantum": [2], "travel": [2, 3],
             "hurston": [3], "dynamics": [3], "area18": [4], "arccorp": [4], "stanton": [4, 8], "chairman": [5],
             "mining": [6, 0, 10, 11], "gameplay": [6], "promotions": [7], "hauling": [8], "cargo": [8],
             "air": [9], "people": [9], "heads": [10], "refinery": [11]}
    postings = {w: {str(i): 2 for i in ix} for w, ix in words.items()}
    blob = gzip.compress(json.dumps({"version": 1, "n_docs": len(docs), "docs": docs, "postings": postings}).encode())

    def fetch(path):
        if offline:
            raise OSError("offline")
        if path == eng.INDEX_PATH:
            return blob
        raise OSError("no transcript in the fixture")
    return lambda: eng.DevHistory(fetch=fetch, cache=cache)


class _Speech:
    muted = False

    def __init__(self, clock):
        self.clock, self.said, self.cuts = clock, [], []

    def say(self, text, speaker, priority):
        self.said.append((self.clock[0], speaker, priority, text))
        return True

    def pending(self):
        return 0

    def mute(self, on=True):
        if on:
            self.cuts.append(self.clock[0])


def _fake_realizer(spec):
    """companion_core's selftest realizer: echoes every claim value, so the REAL grounding gate passes honest output."""
    vals = [str(c["value"]) for c in spec["claims"] if not isinstance(c["value"], bool)]
    words = ("Noted " + " and ".join(vals) + " for the record, pilot, as the suit reports it.").split()
    lo, hi = spec["length_words"]
    return " ".join(words[:hi]) if len(words) >= lo else " ".join(words + ["steady"] * (lo - len(words)))


def _core(clock, dev, **kw):
    import companion_core as cc
    sp = _Speech(clock)
    core = cc.CompanionCore(sp, realizer=_fake_realizer, now=lambda: clock[0], dev_facts=dev, **kw)
    return core, sp


def _drain(core):
    import queue as _q
    while True:
        try:
            item = core._work.get_nowait()
        except _q.Empty:
            return
        core._realize_one(item)


def _ev(et, **data):
    e = type("Ev", (), {})()
    e.event_type, e.data = et, data
    return e


def _is_dev(text: str) -> bool:
    return "dev history" in text.lower()


def _pick_log(game_log: Optional[str]) -> Optional[Path]:
    if game_log and Path(game_log).exists():
        return Path(game_log)
    import companion_core as cc
    live = cc.find_game_log()
    root = live.parent / "logbackups" if live else Path(r"C:\Star Citizen\StarCitizen\LIVE\logbackups")
    # The LONGEST recent session with a mining or salvage ship in it (a long replay is what bounds the rates), small
    # enough to replay in seconds. A busy 2-hour session sits in PRESENT mode almost throughout and gets no quiet at all.
    best, best_span = None, -1.0
    from datetime import datetime
    try:
        for p in sorted(root.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)[:150]:
            if not 2_000_000 < p.stat().st_size < 15_000_000:
                continue
            raw = p.read_bytes()
            if not any(k in raw for k in (b"MISC Prospector", b"Drake Golem", b"Aegis Reclaimer", b"Drake Vulture")):
                continue
            stamps = re.findall(rb"^<(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", raw[:20000] + b"\n" + raw[-20000:],
                                flags=re.M)
            if len(stamps) >= 2:
                span = (datetime.fromisoformat(stamps[-1].decode()) - datetime.fromisoformat(stamps[0].decode())).total_seconds()
                if span > best_span:
                    best, best_span = p, span
    except Exception:
        pass
    return best or live


TS = re.compile(r"^<(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?)Z>")


def replay(path: Path, dev: Optional[DevFacts], clock: list):
    """companion_design/dry_run.py's loop with a fake realizer: the log's own clock, ambient ticks at the core's cadence,
    and the realize queue drained synchronously so nothing depends on thread timing."""
    from datetime import datetime
    core, sp = _core(clock, dev)
    events = []
    orig = core.on_event

    def on_event(ev):
        events.append((clock[0], ev.event_type, dict(ev.data or {})))
        orig(ev)
    subs = core.classifier._subscribers
    subs[subs.index(orig)] = on_event
    t0, nxt = None, None
    with open(path, encoding="utf-8", errors="replace") as f:
        for ln in f:
            m = TS.match(ln)
            if m:
                ts = datetime.fromisoformat(m.group(1)).timestamp()
                if t0 is None:
                    t0, nxt = ts, ts + core.ambient_every_s
                    core._born_t = ts               # the app starts the core when the session starts
                while nxt is not None and nxt <= ts:
                    clock[0] = nxt
                    core.ambient_tick()
                    _drain(core)
                    nxt += core.ambient_every_s
                clock[0] = ts
            if t0 is None:
                continue
            core.feed_line(ln.rstrip("\n"))
            _drain(core)
    _drain(core)
    return core, sp, events, t0 or 0.0


def _interrupt_run(cache: Path, tier: int, second_urgent: bool = False, dev_on: bool = True,
                   off_mid_fact: bool = False, pre_tick: bool = True) -> dict:
    """A fact starts; `clock+3 s` a Tier-`tier` injury lands. Returns the ordered sequence of what was said/cut."""
    clock = [200_000.0]
    dv = DevFacts(enabled=dev_on, history_factory=_fixture_history(cache), now=lambda: clock[0], sync=True,
                  rng=random.Random(3))
    core, sp = _core(clock, dv)
    core.state.set("ship", "MISC Prospector : P")
    clock[0] += 700                                 # past the session warm-up
    order, t_start = [], clock[0]
    out = {"order": order, "fact_body": "", "plain_injury_text": "", "second_resumed": None, "core": core}
    if pre_tick:
        core.ambient_tick()
        _drain(core)
    fact = [x for x in sp.said if _is_dev(x[3])]
    if fact:
        order.append(("fact", fact[0][0] - t_start, fact[0][1], fact[0][3]))
        out["fact_body"] = fact[0][3].split(":", 1)[-1].strip()
    clock[0] += 3                                   # mid-fact
    if off_mid_fact:
        n = len(sp.said)
        core.set_dev_facts(False, "voice")
        for x in sp.said[n:]:
            order.append(("ack", x[0] - t_start, x[1], x[3]))
    n0, c0 = len(sp.said), len(sp.cuts)
    core.on_event(_ev("injury", body_part="left arm", tier=tier))
    for c in sp.cuts[c0:]:
        order.append(("cut", c - t_start, "-", "(fact cut mid-line)"))
    _drain(core)
    for x in sp.said[n0:]:
        order.append(("injury", x[0] - t_start, x[1], x[3]))
    from event_spec import build_event_spec
    ref = build_event_spec("injury", {"body_part": "left arm", "tier": tier}, core._ambient_state(), 0)
    out["plain_injury_text"] = _fake_realizer(ref) if ref else ""
    if second_urgent:
        clock[0] += 20
        core.on_event(_ev("incapacitated", source="log"))
        _drain(core)
    n1 = len(sp.said)
    for _ in range(8):                              # the next 12 minutes of ambient ticks
        clock[0] += 90
        core.ambient_tick()
        _drain(core)
    for x in sp.said[n1:]:
        if x[3].startswith("Since you are still among the living"):
            order.append(("resume", x[0] - t_start, x[1], x[3]))
    if dev_on and tier == 2 and not second_urgent and not off_mid_fact:
        # once per session: another fact, another Tier 3 injury -> cut and dropped, never resumed
        dv.max_per_hour = 99
        core._dev_fact_try = -1e9
        clock[0] += 4000
        core.volatile = type(core.volatile)()       # the first injury has aged out of the relevance window
        core.ambient_tick()
        _drain(core)
        n2 = len(sp.said)
        out["second_fact"] = any(_is_dev(x[3]) for x in sp.said[n2 - 1:])
        clock[0] += 3
        core.on_event(_ev("injury", body_part="right leg", tier=3))
        _drain(core)
        for _ in range(8):
            clock[0] += 90
            core.ambient_tick()
            _drain(core)
        out["second_resumed"] = any(x[3].startswith("Since you are still") for x in sp.said[n2:])
    return out


def _selftest(game_log: Optional[str]) -> int:
    import tempfile
    from grounding_validator import ground, NUM
    import settings as settings_mod
    import companion_core as cc
    results = []

    def case(name, cond):
        results.append((name, bool(cond)))

    tmp = Path(tempfile.mkdtemp(prefix="devfacts_"))
    case("the sc_dev_history engine is found from SuitMk2", load_engine() is not None)
    case("settings: dev_facts defaults OFF", settings_mod.DEFAULTS.get("dev_facts") is False)
    case("settings: dev_facts_max_per_hour defaults low (2)", settings_mod.DEFAULTS.get("dev_facts_max_per_hour") == 2)

    # -- the aside itself ---------------------------------------------------------------------------------------
    a = build_aside({"id": "v1", "k": "v", "t": "Q&A: MISC Prospector - Part I", "d": "2016-04-27"}, "the Prospector",
                    True)
    case("aside is framed, dated, one sentence", bool(a) and a["fixed_text"].startswith(FRAME_KEY)
         and "27 April 2016" in a["fixed_text"] and a["fixed_text"].count(".") == 1)
    case("aside passes the real grounding gate", bool(a) and not ground(a, a["fixed_text"]))
    case("aside speaker is Montaigne, never Elah", bool(a) and a["speaker"] == "montaigne")
    case("aside carries no URL in its text", bool(a) and "http" not in a["fixed_text"])
    b = build_aside({"id": "v2", "k": "v", "t": "Inside Star Citizen: Salvage Operation", "d": "2023-12-20"},
                    "salvage", False)
    case("a body match says the topic came up", bool(b) and "salvage came up in the video" in b["fixed_text"])
    z = build_aside({"id": "z", "k": "v", "t": "Ten for the Chairman: Episode 12 (2014.03.17)", "d": "2014-03-17"},
                    None, True)
    case("a title whose number word has no digit in the source is refused by the gate (silence, not a guess)",
         z is not None and bool(ground(z, z["fixed_text"])))
    # a model rephrasing is held to the same source
    case("rephrase with an invented number is refused", any("unauthorized numbers" in f for f in ground(
        a, FRAME + " the Prospector Q&A had 40 questions, on 27 April 2016.")))
    case("rephrase with a wrong year is refused", any("unauthorized numbers" in f for f in ground(
        a, FRAME + " the video Q&A, MISC Prospector, Part I came out on 27 April 2017.")))
    case("rephrase claiming memory is refused", any("memory" in f for f in ground(
        a, FRAME + " I remember the video Q&A, MISC Prospector, Part I from 27 April 2016.")))
    case("rephrase without the aside frame is refused", any("frame" in f for f in ground(
        a, "The video Q&A, MISC Prospector, Part I came out on 27 April 2016, a fine day.")))
    case("rephrase without the date is refused", any("date" in f for f in ground(
        a, FRAME + " the video Q&A, MISC Prospector, Part I came out a while back, pilot.")))
    case("rephrase speaking the URL is refused", any("URL" in f for f in ground(
        a, FRAME + " the video Q&A, MISC Prospector, Part I, 27 April 2016, youtube.com.")))
    r = resume_spec(a)
    case("resume keeps the frame and the date and passes the gate",
         r["fixed_text"].startswith("Since you are still among the living") and "27 April 2016" in r["fixed_text"]
         and not ground(r, r["fixed_text"]))

    # -- voice toggle ---------------------------------------------------------------------------------------------
    case("voice: every ON phrase is recognised", all(voice_toggle(p) is True for p in VOICE_ON_EXAMPLES))
    case("voice: every OFF phrase is recognised", all(voice_toggle(p) is False for p in VOICE_OFF_EXAMPLES))
    not_toggles = ("what missions do i have", "tell me a fun fact about the prospector and why it matters to mining",
                   "shut up", "stop", "turn on the lights", "fun", "facts", "good one", "what is a fun fact")
    case("voice: questions and other commands are NOT toggles", all(voice_toggle(p) is None for p in not_toggles))
    for on in (True, False):
        ack = toggle_ack_spec(on)
        case(f"toggle ack ({'on' if on else 'off'}) is Montaigne and passes the gate",
             ack["speaker"] == "montaigne" and not ground(ack, ack["fixed_text"]))

    # -- Elah's callout ---------------------------------------------------------------------------------------------
    case("every callout passes the gate as Elah (in-universe: no numbers, no named feeling, no invented names)",
         all(not ground(callout_spec(t), t) for t in ELAH_CALLOUTS))
    case("no callout delivers a dev fact (never says 'dev history')",
         all("dev history" not in t.lower() for t in ELAH_CALLOUTS))
    case("the ELAH_NAMES_FEELING guard still applies to a callout",
         bool(ground(callout_spec("You're glitching again. I'm worried."), "You're glitching again. I'm worried.")))
    ct_ = [0.0]
    dfc = DevFacts(enabled=True, now=lambda: ct_[0], rng=random.Random(5), history_factory=lambda: None)
    fake_fact = {"scenario": SCENARIO}
    hits, texts = 0, []
    for _ in range(3000):
        ct_[0] += CALLOUT_MIN_GAP_S + 1
        co = dfc.callout_for(fake_fact)
        if co:
            hits += 1
            texts.append(co["fixed_text"])
            dfc.callout_spoken()
    case(f"callout rate ~1 in 3 when the gap allows ({hits}/3000)", 0.28 < hits / 3000 < 0.39)
    case("callouts come from a varied pool: no repeat until the pool is used up",
         len(set(texts[:len(ELAH_CALLOUTS)])) == len(ELAH_CALLOUTS))
    ct_[0] += CALLOUT_MIN_GAP_S + 1
    dfc._rng = type("R", (), {"random": lambda self: 0.0, "shuffle": lambda self, x: None})()
    first = dfc.callout_for(fake_fact)
    dfc.callout_spoken()
    ct_[0] += CALLOUT_MIN_GAP_S - 1
    case("never two callouts inside the minimum gap", first is not None and dfc.callout_for(fake_fact) is None)
    case("no callout after a resume or anything that is not a fresh fact",
         dfc.callout_for({"scenario": RESUME_SCENARIO}) is None)

    # -- context --------------------------------------------------------------------------------------------------
    case("ship channel name cleaned", clean_ship("MISC Prospector : ProjectGegnome") == "Prospector")
    case("vehicle code cleaned", (clean_ship("@vehicle_NameDRAK_Golem_OX : X") or "").startswith("Golem"))
    df = DevFacts(enabled=True, history_factory=_fixture_history(tmp / "a"), sync=True, now=lambda: 1000.0,
                  rng=random.Random(1))
    case("ship implies activity", df.activity({"ship": "Aegis Reclaimer : P"}) == "salvage")
    df.note_event("refinery_complete", {})
    case("a refinery event means mining", df.activity({}) == "mining")
    s1 = df.poll({"ship": "MISC Prospector : P"})
    case("topical: the current ship's fact first", bool(s1) and "Prospector" in s1["fixed_text"])
    df2 = DevFacts(enabled=True, history_factory=_fixture_history(tmp / "b"), sync=True, now=lambda: 1000.0,
                   rng=random.Random(2))
    s2 = df2.poll({"system": "Nyx"})     # nothing about Nyx in the fixture
    case("nothing topical: a random real document, still framed and dated",
         bool(s2) and s2["fixed_text"].startswith(FRAME_KEY) and not ground(s2, s2["fixed_text"]))
    case("dull titles (store promotions) are never picked",
         all("Promotions" not in ((df2._pick(df2._history(), {}) or {}).get("fixed_text") or "") for _ in range(20)))

    # -- offline with no cache: silent, no error --------------------------------------------------------------------
    warns = []

    class _H(logging.Handler):
        def emit(self, rec):
            if rec.levelno >= logging.WARNING:
                warns.append(rec.getMessage())
    h = _H()
    logging.getLogger().addHandler(h)
    try:
        off = DevFacts(enabled=True, history_factory=_fixture_history(tmp / "empty_cache", offline=True), sync=True,
                       now=lambda: 5000.0)
        outs = [off.poll({"ship": "MISC Prospector : P"}) for _ in range(5)]
        case("offline + no cache: no aside, no exception, no warning, one attempt then backoff",
             all(o is None for o in outs) and off.stats["unavailable"] == 1 and not warns)
        clock = [300_000.0]
        offc = DevFacts(enabled=True, history_factory=_fixture_history(tmp / "empty_core", offline=True), sync=True,
                        now=lambda: clock[0])
        core_off, sp_off = _core(clock, offc)
        core_off.state.set("ship", "MISC Prospector : P")
        for _ in range(10):
            clock[0] += 700
            core_off.ambient_tick()
            _drain(core_off)
        case("offline + no cache through the core: zero dev lines, no warning",
             not any(_is_dev(x[3]) for x in sp_off.said) and not warns)
        off2 = DevFacts(enabled=True, history_factory=lambda: (time.sleep(0.5), 1 / 0)[1], now=lambda: 0.0)
        t0 = time.perf_counter()
        o2 = off2.poll({"ship": "x"})
        case("poll never blocks on a slow corpus load (background prepare)",
             o2 is None and time.perf_counter() - t0 < 0.1)
        if off2._worker:
            off2._worker.join(5)
        case("disabled: poll touches nothing", DevFacts(enabled=False, history_factory=lambda: 1 / 0).poll({}) is None)
    finally:
        logging.getLogger().removeHandler(h)

    # -- the voice toggle through the core: flips both ways, persists, acknowledges --------------------------------
    clock = [400_000.0]
    saved = {}
    dvt = DevFacts(enabled=False, history_factory=_fixture_history(tmp / "tog"), now=lambda: clock[0], sync=True)
    ct, spt = _core(clock, dvt)
    ct.dev_facts_persist = lambda on: saved.__setitem__("dev_facts", on)
    case("toggle: 'fun facts on' turns it on, persists, Montaigne acknowledges",
         ct.voice_command("Montaigne, fun facts on") and dvt.enabled and saved.get("dev_facts") is True
         and spt.said and spt.said[-1][1] == "montaigne" and spt.said[-1][3] in ACK_ON)
    clock[0] += 30
    case("toggle: 'stop the fun facts' turns it off, persists, acknowledges",
         ct.voice_command("stop the fun facts") and not dvt.enabled and saved.get("dev_facts") is False
         and spt.said[-1][3] in ACK_OFF)
    case("toggle: an ordinary question is not consumed", not ct.voice_command("what missions do I have"))
    # real persistence: settings.save() to a temp settings.json, and load() sees it after a "restart"
    old = (settings_mod.DIR, settings_mod.PATH)
    try:
        settings_mod.DIR = tmp / "settings"
        settings_mod.PATH = settings_mod.DIR / "settings.json"
        s = settings_mod.load()
        case("fresh settings file: dev_facts is off", s["dev_facts"] is False)
        dvs = DevFacts.from_settings(s, history_factory=_fixture_history(tmp / "tog2"), now=lambda: clock[0],
                                     sync=True)
        cs, _ = _core(clock, dvs)
        cs.dev_facts_persist = lambda on: (s.__setitem__("dev_facts", on), settings_mod.save(s))
        cs.voice_command("more fun facts")
        case("voice ON survives a restart (settings.json)", settings_mod.load()["dev_facts"] is True)
        clock[0] += 30
        cs.voice_command("fun facts off")
        case("voice OFF survives a restart (settings.json)", settings_mod.load()["dev_facts"] is False)
    finally:
        settings_mod.DIR, settings_mod.PATH = old

    # -- replays through the real core --------------------------------------------------------------------------------
    path = _pick_log(game_log)
    case(f"a real Game.log to replay ({path.name if path else 'none'})", path is not None)
    if path is not None:
        touched = []
        clock = [0.0]
        dflt = DevFacts.from_settings(dict(settings_mod.DEFAULTS), now=lambda: clock[0], sync=True,
                                      history_factory=lambda: touched.append(1) or 1 / 0)
        core, sp, events, t0 = replay(path, dflt, clock)
        n_dev = sum(1 for x in sp.said if _is_dev(x[3]))
        print(f"  default replay of {path.name}: {core.stats['events']} events, {(clock[0] - t0) / 3600:.1f} h, "
              f"{len(sp.said)} lines spoken, {n_dev} dev facts")
        case("DEFAULT settings: zero dev-fact lines over a replayed session", n_dev == 0 and len(sp.said) > 0)
        case("DEFAULT settings: the dev-history corpus is never loaded", not touched)

        clock = [0.0]
        facts = []
        on = DevFacts(enabled=True, max_per_hour=2, history_factory=_fixture_history(tmp / "replay"),
                      now=lambda: clock[0], sync=True, rng=random.Random(7))
        _orig_spoken = on.spoken

        def _rec(spec):
            if spec.get("scenario") == SCENARIO:
                facts.append((clock[0], spec))
            _orig_spoken(spec)
        on.spoken = _rec
        core, sp, events, t0 = replay(path, on, clock)
        print(f"  dev_facts ON replay: {len(facts)} asides over {(clock[0] - t0) / 3600:.1f} h, {len(sp.said)} lines "
              f"total; dev stats {on.stats}; core {{'dev_fact_unquiet': {core.stats.get('dev_fact_unquiet', 0)}}}")
        for t, s in facts[:6]:
            print(f"    +{(t - t0) / 60:6.1f} min  {s['fixed_text']}   [source: {s['source']['title_raw']} | "
                  f"{s['source']['date']}]")
        times = [t for t, _ in facts]
        case("dev_facts ON: asides appear in the replay", len(facts) >= 1)
        case("rate cap: never more than 2 in any rolling hour",
             all(sum(1 for u in times if 0 <= u - t < 3600) <= 2 for t in times))
        case("every aside framed as an aside, with its date, by Montaigne",
             all(s["fixed_text"].startswith(FRAME_KEY) and s["date_spoken"] in s["fixed_text"]
                 and s["speaker"] == "montaigne" for _, s in facts))
        case("no dev fact is ever attributed to Elah", all(x[1] == "montaigne" for x in sp.said if _is_dev(x[3])))
        calls = [(i, x) for i, x in enumerate(sp.said) if x[3] in ELAH_CALLOUTS]
        print(f"  callouts in the long replay: {len(calls)} after {len(facts)} facts")
        for i, x in calls[:3]:
            print(f"    {sp.said[i - 1][1]}: {sp.said[i - 1][3]}\n    {x[1]}: {x[3]}")
        case("every callout is Elah's and follows a Montaigne fact at the same moment",
             all(x[1] == "elah" and i > 0 and sp.said[i - 1][1] == "montaigne" and _is_dev(sp.said[i - 1][3])
                 and sp.said[i - 1][0] == x[0] for i, x in calls))
        ctimes = [x[0] for _, x in calls]
        case("callout rate within bound over the long replay (<= facts, >= 30 min apart)",
             len(calls) <= len(facts) and all(b - a >= CALLOUT_MIN_GAP_S for a, b in zip(ctimes, ctimes[1:])))
        case("no callout within 10 min of an injury or death",
             all(not any(0 <= t - h < 600 for h in [e[0] for e in events
                                                   if e[1] in ("injury", "incapacitated", "player_respawned")])
                 for t in ctimes))
        case("no repeated fact in the session", len({s["source"]["id"] for _, s in facts}) == len(facts))

        def src_nums(s):
            return set(NUM.findall(f"{s['source']['title_raw']} {s['source']['date']} {s['date_spoken']}"))
        case("no invented numbers: every number said is in the source title or date",
             all(set(NUM.findall(s["fixed_text"])) <= src_nums(s) for _, s in facts))
        hurt = [t for t, et, _ in events if et in ("injury", "incapacitated", "player_respawned")]
        case("none within 10 min after an injury or death in the log",
             all(not any(0 <= t - h < 600 for h in hurt) for t in times))
        spk = [t for t, et, _ in events if et in cc.EVENT_PRIORITY]
        case("none within the quiet window after any speaking event",
             all(not any(0 <= t - e < cc.CompanionCore.DEV_FACT_QUIET_AFTER_S for e in spk) for t in times))
        case("none in the session's first 10 minutes", all(t - t0 >= 600 for t in times))

    # -- synthetic: combat and injury windows are refused even when everything else is quiet ----------------------
    clock = [100_000.0]
    dv = DevFacts(enabled=True, history_factory=_fixture_history(tmp / "syn"), now=lambda: clock[0], sync=True)
    c3, s3 = _core(clock, dv)
    c3.state.set("ship", "MISC Prospector : P")
    clock[0] += 700                             # past the session warm-up
    c3.combat.state = "on"                      # CombatWatch says ON (ambient_tick re-reads it)
    c3._combat_edge("on", "test")
    _drain(c3)
    c3.ambient_tick()
    _drain(c3)
    case("combat: no aside", not any(_is_dev(x[3]) for x in s3.said))
    c3.combat.state = "quiet"
    c3._combat_edge("off", "test")
    _drain(c3)
    c3.on_event(_ev("injury", body_part="left leg", tier=3))
    _drain(c3)
    clock[0] += 120
    c3.ambient_tick()
    _drain(c3)
    case("injury 2 min ago: no aside", not any(_is_dev(x[3]) for x in s3.said))
    c3.state.set("injury_left_leg", "minor")
    clock[0] += 900
    c3.volatile = type(c3.volatile)()           # the injury has aged out of the 10-minute relevance window
    c3.ambient_tick()
    _drain(c3)
    case("an injury still on the suit's record: no aside", not any(_is_dev(x[3]) for x in s3.said))
    c3.state.set("injury_left_leg", None)
    clock[0] += 700
    c3.ambient_tick()
    _drain(c3)
    case("healed and quiet: the aside comes", any(_is_dev(x[3]) for x in s3.said))

    # -- the callout's own urgency guard: forced to fire, it still stays quiet in an urgent moment -------------------
    clock = [600_000.0]
    dvc = DevFacts(enabled=True, history_factory=_fixture_history(tmp / "co"), now=lambda: clock[0], sync=True)
    dvc._rng = type("R", (), {"random": lambda self: 0.0, "shuffle": lambda self, x: None,
                              "choice": lambda self, x: x[0]})()
    cco, sco = _core(clock, dvc)
    fspec = build_aside({"id": "v9", "k": "v", "t": "Mining Gameplay Deep Dive", "d": "2019-02-05"}, None, True)
    for label, setup in (("combat", lambda: setattr(cco.gate_state, "in_combat", True)),
                         ("not now", lambda: cco.not_now.snooze(5))):
        setup()
        n = len(sco.said)
        cco._dev_fact_said(fspec, fspec["fixed_text"], None)
        case(f"no callout in an urgent moment ({label})", len(sco.said) == n)
        cco.gate_state.in_combat = False
        cco.not_now.cancel()
        clock[0] += 10
    cco._dev_fact_said(fspec, fspec["fixed_text"], None)
    case("...and the same callout does come in a quiet one", sco.said and sco.said[-1][1] == "elah")

    # -- the interrupted fact (Montaigne only) --------------------------------------------------------------------
    seq = _interrupt_run(tmp / "int", tier=2)
    kinds = [k for k, *_ in seq["order"]]
    print("  interrupted-fact sequence (Tier 2):")
    for k, t, who, text in seq["order"]:
        print(f"    +{t:5.0f}s {k:7s} {who:9s} {text}")
    case("interrupt: fact, cut, injury call, resume, in that order", kinds == ["fact", "cut", "injury", "resume"])
    # The control: the same injury at the same instant, dev facts off and nothing said before it.
    control = _interrupt_run(tmp / "ctl", tier=2, dev_on=False, pre_tick=False)
    ctl_injury = [x for x in control["order"] if x[0] == "injury"]
    case("interrupt: the injury call is exactly the one said with dev facts off (unchanged, not delayed)",
         len(seq["order"]) > 2 and ctl_injury and seq["order"][2][3] == ctl_injury[0][3]
         and seq["order"][2][1] == ctl_injury[0][1])
    case("interrupt: resume is Montaigne, framed, same fact and date",
         len(seq["order"]) > 3 and seq["order"][3][2] == "montaigne"
         and "fun fact from the dev history" in seq["order"][3][3].lower() and seq["fact_body"] in seq["order"][3][3])
    case("interrupt: once per session (a second interrupted fact is dropped, not resumed)",
         seq.get("second_fact") and seq["second_resumed"] is False)
    t1 = _interrupt_run(tmp / "t1", tier=1)
    case("Tier 1 injury: the fact is cut, the injury call lands, and it NEVER resumes",
         [k for k, *_ in t1["order"]] == ["fact", "cut", "injury"])
    t2 = _interrupt_run(tmp / "t2", tier=2, second_urgent=True)
    case("a second urgent event while waiting drops the fact", "resume" not in [k for k, *_ in t2["order"]])
    tv = _interrupt_run(tmp / "tv", tier=2, off_mid_fact=True)
    case("'fun facts off' mid-fact: fact cut, acknowledged, and NO resume after the injury",
         "ack" in [k for k, *_ in tv["order"]] and "resume" not in [k for k, *_ in tv["order"]])
    case("dev facts OFF: no fact, no bit", not any(k in ("fact", "resume") for k, *_ in control["order"]))
    case("nothing about a dev fact reaches character memory (no topic/callback on the spec)",
         all("topic" not in s for s in (a, r)))

    bad = [n for n, ok in results if not ok]
    for n, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {n}")
    print(f"dev_facts selftest: {len(results) - len(bad)}/{len(results)} passed")
    return 1 if bad else 0


def _examples() -> int:
    """Asides from the REAL cached corpus (network only if the cache is missing), for a few real contexts."""
    df = DevFacts(enabled=True, max_per_hour=99, sync=True, rng=random.Random(11))
    bag = list(ELAH_CALLOUTS)
    random.Random(4).shuffle(bag)
    ctxs = [({"ship": "MISC Prospector : P"}, None), ({"ship": "Aegis Reclaimer : P"}, None),
            ({"location": "Area18"}, "qt_arrived"), ({"system": "Nyx"}, None)]
    for st, ev in ctxs:
        if ev:
            df.note_event(ev, {})
        s = df.poll(st)
        if s is None:
            print(f"{st}: (none) {df.stats}")
            continue
        df.spoken(s)
        print(f"{st}\n  montaigne: {s['fixed_text']}\n  elah (callout, forced for display; live ~1 in 3): "
              f"{bag.pop()}\n     source: {s['source']['title_raw']} | {s['source']['date']} | {s['source']['url']}")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    if "--selftest" in sys.argv:
        rest = [a for a in sys.argv[1:] if not a.startswith("--")]
        sys.exit(_selftest(rest[0] if rest else None))
    if "--examples" in sys.argv:
        sys.exit(_examples())
    print(__doc__)
