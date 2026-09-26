"""dev_facts.py - OPTIONAL "fun fact from the dev history" asides for the SuitMk2 companions (J 2026-09-25).

OFF BY DEFAULT (settings "dev_facts": False). A dev fact breaks the fourth wall, and some pilots want full immersion.
When the pilot turns it on:

  * WHAT (J 2026-09-25: "The fun facts should also be fun not just video titles"): a fact is ONE specific detail from
    INSIDE a dev video or comm-link (a number CIG gave, a design decision, something that changed), never the title.
    Facts come ONLY from a CURATED PACK, data/dev_facts_pack.json, built offline by tools/build_dev_fact_pack.py from
    the Star Citizen dev-history corpus (sc_dev_history.py: 1,368 dev-video transcripts + 5,152 comm-links). Each
    entry carries the VERBATIM excerpt it came from, the source title, date and url, so a human can audit it. The
    build rejects any sentence with a number, name or content word the excerpt and metadata do not have
    (fact_problems below); the selftest re-checks every entry. At runtime nothing is generated or fetched: the pack is
    a local file, so a fact is instant, works offline, and cannot invent anything. The line still goes through
    grounding_validator.ground() with the EXCERPT as its source (numbers and names are checked against it), which for
    a dev fact also demands the aside frame, the date, no URL and no claim of memory.
  * HOW IT IS SAID: always framed as its own aside ("Fun fact from the dev history: ..."), one sentence plus the date,
    never as something a character remembers. ONE companion only: Montaigne, the ship who "knows things only
    secondhand". Elah never says one. Nothing about it is written to the pilot's memory.
  * WHEN: only in a quiet moment, decided by the core (CompanionCore._dev_fact_unquiet): no combat, no injury or death
    on record, no event line in the last few minutes, not AFK, not shaken, PRESENCE mode, nothing queued, no
    not-now snooze. The normal gate, pacing and quiet budget still apply on top.
  * TOPICAL first: the current ship, then what the pilot is doing (mining, salvage, cargo, quantum travel), then
    where they are (pack entries carry topic tags). A random fact only when nothing topical is left. Never the same
    fact, nor two facts from the same source, in one session.
  * RATE: at most `dev_facts_max_per_hour` (default 2) per rolling hour.
  * OFF THE HOT PATH: nothing here runs on the game-event path (note_event is a deque append). The core's ambient tick
    asks poll(), which reads the bundled pack once (a few hundred KB, local) and picks from memory after that. No
    network, ever. A missing or unreadable pack = no aside at all, no error, and a quiet retry half an hour later.
  * VOICE TOGGLE (J 2026-09-25): "fun facts on" / "fun facts off" and close variants (voice_toggle below) flip it
    mid-session without the settings dialog; Montaigne acknowledges in one line and the choice is saved.

THE INTERRUPTED FACT (J 2026-09-25, Montaigne only): if a Tier 2 or 3 injury lands while a fact is still being said,
the fact is cut, the normal injury call is said unchanged, and once nothing urgent has happened for a short while
Montaigne picks the fact back up ("Since you are still among the living, back to that fun fact from the dev
history: ..."). Once per session at most. A Tier 1 injury, a death, combat, a second urgent event while it waits, or
the pilot turning fun facts off DROPS the fact for good. The core owns the timing; this module owns the text.

    python dev_facts.py --selftest [path\\to\\Game.log]     the real pack + fixtures + a real Game.log replay (no models)
    python dev_facts.py --examples                          asides from the real pack, for a few contexts
    python dev_facts.py --sample N                          N random pack facts, each with its source excerpt
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import random
import re
import sys
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
RETRY_UNAVAILABLE_S = 1800.0          # pack missing or unreadable: try again in half an hour, silently
PACK_PATH = HERE.parent / "data" / "dev_facts_pack.json"
REF = os.environ.get("SC_DEV_HISTORY_REF", "corpus")     # the build tool's corpus ref; "corpus" until the PR merges

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
ACTIVITIES = ("mining", "salvage", "cargo", "quantum")      # the pack's activity tags
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


# ---- the pack's checks (shared: the build tool refuses with these, the selftest re-checks every entry with them) ----
# A pack fact is one sentence rewritten from a verbatim excerpt. It may reword; it may not ADD. Three mechanical tests,
# each against the excerpt plus the document's own metadata (title, date):
#   numbers  every number it states, in digits or words, is a number the source states ("two hundred" == "200");
#   names    every capitalised word or code (C2, 890, Mk) is a word of the source;
#   content  at most MAX_NOVEL content words that the source lacks (framing like "developers said" excepted).
# Plus shape: third person (no "I"/"we"/"you": Montaigne is reporting, not remembering or addressing), one sentence,
# not a bare "X came out on DATE" title template. Stated limits: a check on WORDS cannot see a true-worded sentence
# with a wrong meaning ("the Prospector is not slow" from "the prospector is not fast"); that is what the human read
# at build time is for, and why every entry keeps its excerpt next to it.
_N_UNITS = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen "
                                       "fourteen fifteen sixteen seventeen eighteen nineteen".split())}
_N_UNITS.update({"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
                 "ninety": 90, "dozen": 12, "twice": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7,
                 "eighth": 8, "ninth": 9, "tenth": 10})
_N_SCALE = {"hundred": 100, "thousand": 1000, "million": 10 ** 6, "billion": 10 ** 9}
_N_TOK = re.compile(r"\d[\d,]*(?:\.\d+)?k?\b|[a-z]+")


def numbers_in(text: str) -> set:
    """Every number a text states, as values: '5,000' / 'five thousand' -> 5000, 'two hundred and fifty' -> 250,
    '10k' -> 10000, '1.5 million' -> 1500000, 'a hundred' -> 100. 'one' ALONE is ordinary English ('the one that'),
    so it only counts with a scale word after it; 'first'/'second' are not counted (time and ordinary English)."""
    toks = _N_TOK.findall(str(text or "").lower().replace("-", " "))
    out, total, cur, active, only_one = set(), 0, 0, False, True

    def flush():
        nonlocal total, cur, active, only_one
        if active and not (only_one and total + cur in (0, 1)):
            out.add(total + cur)
        total, cur, active, only_one = 0, 0, False, True
    i = 0
    while i < len(toks):
        t = toks[i]
        if t[0].isdigit():
            flush()
            v = float(t.rstrip("k").replace(",", "")) * (1000 if t.endswith("k") else 1)
            if i + 1 < len(toks) and toks[i + 1] in _N_SCALE:
                v *= _N_SCALE[toks[i + 1]]
                i += 1
            out.add(int(v) if v == int(v) else v)
        elif t in _N_UNITS:
            u = _N_UNITS[t]
            if active and ((cur % 10 == 0 and cur >= 20 and u < 10) or (cur % 100 == 0 and cur >= 100 and u < 100)
                           or (cur == 0 and total > 0)):
                cur += u                                    # "twenty five", "two hundred and fifty", "a thousand two"
            elif active and cur:
                flush()
                cur, active = _N_UNITS[t], True
            else:
                cur, active = cur + _N_UNITS[t], True
            if t != "one":
                only_one = False
        elif t in _N_SCALE:
            if not active and i > 0 and toks[i - 1] == "a":
                cur, active = 1, True
            if active:
                only_one = False
                if _N_SCALE[t] == 100:
                    cur = max(cur, 1) * 100
                else:
                    total, cur = total + max(cur, 1) * _N_SCALE[t], 0
        elif t == "and" and active and i + 1 < len(toks) and toks[i + 1] in _N_UNITS:
            pass
        else:
            flush()
        i += 1
    flush()
    return out


_STOPISH = set("a an the and or but of to in on at for with by from is are was were be been being it its it's this "
               "that these those as not no so if then than there their they them what which who when where while how "
               "all any some more most much many very just only also into over under about after before up down out "
               "can could would should will may might must has have had does did do each every both other such own "
               "same one ones".split())
# Reporting words Montaigne may add without adding a fact.
_FRAMING = set("developer developers devs dev team teams said says explained explain showed shown revealed noted "
               "mentioned according designers designer talked discussed described confirmed video videos show "
               "star citizen game players player pilots pilot ship ships spacecraft back".split())
_FIRST_OR_SECOND = re.compile(r"\b(?:i|i'm|i've|i'd|i'll|we|we're|we've|we'd|we'll|our|ours|us|my|me|mine|you|your|"
                              r"you're|you'll|you've|yours)\b")
TEMPLATE = re.compile(r"\b(?:came out on|came up in the (?:video|comm-link)|was released on|was published on)\b", re.I)
MAX_NOVEL = 2
FACT_WORDS = (7, 32)
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9'’.\-]*[A-Za-z0-9]|[A-Za-z0-9]")


def _norm(w: str) -> str:
    return w.lower().replace("’", "'").removesuffix("'s").strip(".'-")


def _stem(w: str) -> str:
    for suf in ("ing", "ed", "es", "s", "ly", "er"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def _vocab(text: str) -> set:
    out = set()
    for t in _TOKEN.findall(str(text or "")):
        n = _norm(t)
        out.add(n)
        out.update(p for p in re.split(r"[-.]", n) if p)
    return out


def excerpt_number_words(excerpt: str) -> str:
    """The digit form of every number in the excerpt (words included), so the gate's number-word check can see that
    'five' in a fact is the excerpt's 'five'. A claim value: it states nothing the excerpt does not."""
    return " ".join(str(n) for n in sorted(numbers_in(excerpt), key=float))


def fact_problems(entry: dict) -> list:
    """Why this pack entry must not be said ([] = fine). Used by the build (to refuse) and the selftest (to re-check)."""
    fact, ex = str(entry.get("fact") or "").strip(), str(entry.get("excerpt") or "").strip()
    meta = f"{entry.get('title', '')} {entry.get('date', '')} {_spoken_date(entry.get('date', '')) or ''}"
    probs = []
    if not ex:
        return ["no source excerpt"]
    if not fact:
        return ["no fact"]
    if not _spoken_date(entry.get("date", "")):
        probs.append("no usable date")
    n = len(fact.split())
    if not FACT_WORDS[0] <= n <= FACT_WORDS[1]:
        probs.append(f"length {n} words")
    if TEMPLATE.search(fact):
        probs.append("title template")
    title_words = _vocab(entry.get("title", "")) - _STOPISH
    if title_words and len(_vocab(fact) & title_words) >= max(3, int(0.8 * len(title_words))) \
            and not (_vocab(fact) - title_words - _STOPISH - _FRAMING) & _vocab(ex):
        probs.append("restates the title")
    if re.search(r'["“”]', fact):
        probs.append("quotation marks")
    if _FIRST_OR_SECOND.search(fact.lower().replace("’", "'")):
        probs.append("first or second person")
    if len(re.findall(r"[.!?](?:\s|$)", fact.rstrip(".!? ") + " ")) > 0:
        probs.append("more than one sentence")
    src_nums = numbers_in(ex) | numbers_in(meta)
    extra = sorted(numbers_in(fact) - src_nums, key=float)
    if extra:
        probs.append(f"numbers not in the source {extra}")
    src = _vocab(ex) | _vocab(meta) | {_norm(t) for t in (entry.get("topics") or [])}
    names = []
    spoken = _lower_first(fact, f"{ex} {entry.get('title', '')}", entry.get("topics") or ())
    for i, t in enumerate(_TOKEN.findall(spoken)):          # checked AS SPOKEN (first word de-capitalised)
        n_ = _norm(t)
        has_digit, has_alpha = any(c.isdigit() for c in t), any(c.isalpha() for c in t)
        if not (t[0].isupper() or (has_digit and has_alpha)):
            continue                                        # a plain number is the number check's job
        if n_ in _FRAMING or (i == 0 and n_ in _STOPISH):
            continue                                        # "Star Citizen", "The": not a claim about anything
        if n_ not in src and not all(p in src for p in re.split(r"[-.]", n_) if p) \
                and _stem(n_) not in {_stem(w) for w in src}:      # "Jump" from "890 jumps" is not invented
            names.append(t)
    if names:
        probs.append(f"names not in the source {names}")
    src_stems = {_stem(w) for w in src}
    novel = [w for w in (_norm(t) for t in _TOKEN.findall(fact))
             if len(w) >= 4 and w not in _STOPISH and w not in _FRAMING and w not in src
             and _stem(w) not in src_stems and not any(s.startswith(_stem(w)[:6]) for s in src_stems
                                                       if len(_stem(w)) >= 6)]
    if len(novel) > MAX_NOVEL:
        probs.append(f"content not in the source {novel}")
    return probs


def _lower_first(s: str, source: str = "", names=()) -> str:
    """The fact follows 'in a video from DATE,' so its first word loses its sentence capital, unless it is a name:
    a word the source itself writes capitalised ('Crusader', 'MISC'), one of the entry's topic tags (a ship or place
    the captions happen to write in lower case), a code (C8R, 890) or an acronym."""
    w = s.split()[0] if s.split() else ""
    if not w or w[0].islower():
        return s
    core = re.sub(r"(?:'s|’s)?[^\w]*$", "", w)
    if any(c.isdigit() for c in core) or (len(core) > 1 and core.isupper()):
        return s
    if _norm(core) in (_STOPISH | _FRAMING):
        return s[0].lower() + s[1:]
    tags = {_norm(n) for n in names} - set(ACTIVITIES)      # "mining" is a topic, not a name
    if _norm(core) in tags or re.search(rf"(?<![\w]){re.escape(core)}(?![\w])", source):
        return s
    return s[0].lower() + s[1:]


def fact_spec(entry: dict, frame: str = FRAME, scenario: str = SCENARIO) -> Optional[dict]:
    """One pack entry -> a Montaigne aside spec. The source (excerpt, title, date) goes in as the claims, so the gate
    checks the spoken numbers and names against the SOURCE, never against the sentence itself."""
    date = _spoken_date(entry.get("date", ""))
    fact = str(entry.get("fact") or "").strip().rstrip(".!? ")
    if not date or not fact or not entry.get("excerpt"):
        return None
    kind = "video" if entry.get("k") == "v" else "comm-link"
    src_text = f"{entry['excerpt']} {entry.get('title', '')}"
    body = f"in a {kind} from {date}, {_lower_first(fact, src_text, entry.get('topics') or ())}."
    text = f"{RESUME_LEAD if scenario == RESUME_SCENARIO else frame} {body}"
    n = len(text.split())
    claims = [{"id": "C1", "predicate": "devfact.date", "value": str(entry.get("date", ""))},
              {"id": "C2", "predicate": "devfact.date_spoken", "value": date},
              {"id": "C3", "predicate": "devfact.kind", "value": kind},
              {"id": "C4", "predicate": "devfact.source_title", "value": str(entry.get("title", ""))},
              {"id": "C5", "predicate": "devfact.excerpt", "value": str(entry["excerpt"])},
              {"id": "C6", "predicate": "devfact.excerpt_numbers", "value": excerpt_number_words(entry["excerpt"])}]
    return {"id": f"{scenario}:{entry.get('id')}", "scenario": scenario, "aside": "dev_fact", "speaker": SPEAKER,
            "fixed_text": text, "body": body, "date_spoken": date, "fact": dict(entry),
            "claims": claims, "required_values": [], "length_words": [max(1, n - 3), n],
            "allowed_names": [],          # name gate ON: a capitalised word must come from the excerpt/title/date
            "source": {"id": entry.get("id"), "doc": entry.get("doc"), "title": _short_title(entry.get("title", "")),
                       "title_raw": entry.get("title", ""), "date": entry.get("date", ""), "kind": kind,
                       "k": entry.get("k"), "url": entry.get("url")}}


def _short_title(t: str) -> str:
    return re.sub(r"\s+", " ", str(t)).strip()


def resume_spec(spec: dict) -> dict:
    """The same fact, picked back up after an injury (Montaigne only; the core decides WHEN)."""
    return fact_spec(spec["fact"], scenario=RESUME_SCENARIO)


def load_pack(path: Path = PACK_PATH) -> list:
    """The curated facts, [] if the file is missing. Entries without an excerpt are dropped here too."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return [f for f in data.get("facts", []) if f.get("excerpt") and f.get("fact") and f.get("id")]


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
    """The sidecar. enabled=False (the default) means poll() returns None and nothing is ever loaded."""

    def __init__(self, enabled: bool = False, max_per_hour: int = DEFAULT_MAX_PER_HOUR,
                 pack_factory: Optional[Callable[[], list]] = None, now: Callable[[], float] = time.time,
                 sync: bool = False, rng: Optional[random.Random] = None, ground: Optional[Callable] = None,
                 pack_path: Optional[Path] = None):
        self.enabled = bool(enabled)
        self.max_per_hour = int(max_per_hour or 0)
        self.now, self.sync = now, sync          # sync is kept for callers; the pack is local, nothing is threaded
        self._factory = pack_factory or (lambda: load_pack(pack_path or PACK_PATH))
        self._pack: Optional[list] = None
        self._rng = rng or random.Random()
        if ground is None:
            from grounding_validator import ground as _g
            ground = _g
        self._ground = ground
        self._unavailable_until = -1e18
        self.used: set = set()                       # fact ids SPOKEN this session: never twice
        self.used_docs: set = set()                  # ... and never two facts from one source in a session
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

    # -- inputs ------------------------------------------------------------------------------------------------------
    def note_event(self, et: str, data: Optional[dict] = None) -> None:
        """Cheap (a deque append): called from the core's event path. Never loads anything."""
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
        """[(kind, match words)] in order: ship, activity, place, system. Rotated a step each time a fact is spoken so
        one ship does not own every fact of the night. A pack entry matches when one of its topic tags is one of the
        match words ('Cutlass Black' -> {'cutlass', 'black'} matches the tag 'cutlass')."""
        out = []
        ship = clean_ship(st.get("ship"))
        if ship:
            out.append(("ship", {_norm(w) for w in ship.split()}))
        act = self.activity(st)
        if act:
            out.append(("activity", {act}))
        for key in ("location", "planetary_body", "system"):
            v = st.get(key)
            if v:
                words = {_norm(w) for w in re.split(r"[\s_\-]+", str(v)) if len(w) >= 3}
                words.add(_norm(str(v)).replace(" ", ""))
                if not any(words == o[1] for o in out):
                    out.append(("place", words))
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
        src = spec.get("source") or {}
        self.used.add(src.get("id"))
        if src.get("doc"):
            self.used_docs.add(src.get("doc"))
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

    # -- poll (ambient thread; a local file read once, then memory) --------------------------------------------------
    def pack(self) -> Optional[list]:
        if self._pack is None:
            if self.now() < self._unavailable_until:
                return None
            try:
                self._pack = list(self._factory() or [])
            except Exception as e:
                self._unavailable_until = self.now() + RETRY_UNAVAILABLE_S
                self.stats["unavailable"] += 1
                log.info("dev facts pack unavailable (%s: %s); silent for now", type(e).__name__, e)
                return None
        return self._pack

    def poll(self, st: dict) -> Optional[dict]:
        if not self.enabled:
            return None
        self.stats["polls"] += 1
        if not self.under_cap():
            return None
        pack = self.pack()
        if not pack:
            return None
        try:
            spec = self._pick(pack, st)
        except Exception:
            log.info("dev facts: pick failed", exc_info=True)
            spec = None
        if spec is None:
            self.stats["no_doc"] += 1
            return None
        self.stats["prepared"] += 1
        return spec

    def _usable(self, entry: dict) -> Optional[dict]:
        if entry.get("id") in self.used or (entry.get("doc") and entry.get("doc") in self.used_docs):
            return None
        spec = fact_spec(entry, self._rng.choice(FRAMES))
        if spec is None or self._ground(spec, spec["fixed_text"]):
            self.stats["ungrounded"] += 1
            return None
        return spec

    def _pick(self, pack: list, st: dict) -> Optional[dict]:
        for kind, words in self.topics(st):
            cands = [e for e in pack if {_norm(t) for t in (e.get("topics") or [])} & words]
            self._rng.shuffle(cands)
            for e in cands:
                s = self._usable(e)
                if s is not None:
                    return s
        # Nothing topical left: any fact, so the feature still does something on a bare log.
        order = list(pack)
        self._rng.shuffle(order)
        for e in order:
            s = self._usable(e)
            if s is not None:
                return s
        return None


# ======================================================================================================================
# selftest
# ======================================================================================================================
FIXTURE_FACTS = [
    {"id": "f1", "doc": "v1", "k": "v", "title": "Q&A: MISC Prospector - Part I", "date": "2016-04-27",
     "url": "https://www.youtube.com/watch?v=v1", "topics": ["prospector", "mining"],
     "excerpt": "the prospector carries thirty two scu of ore in the side pods and the arm folds under the nose "
                "when you are flying so it does not get knocked off",
     "fact": "The Prospector carries 32 SCU of ore in its side pods, and its arm folds under the nose in flight."},
    {"id": "f2", "doc": "v2", "k": "v", "title": "Inside Star Citizen: Salvage Operation", "date": "2023-12-20",
     "url": "https://www.youtube.com/watch?v=v2", "topics": ["salvage", "vulture"],
     "excerpt": "so hull scraping was the first step and now the vulture can also break a wreck into pieces with "
                "structural salvage which is the big new thing",
     "fact": "After hull scraping came structural salvage, which lets the Vulture break a wreck into pieces."},
    {"id": "f3", "doc": "v3", "k": "v", "title": "Around the Verse - The Evolution of Quantum Travel",
     "date": "2017-11-09", "url": "https://www.youtube.com/watch?v=v3", "topics": ["quantum"],
     "excerpt": "originally quantum travel was a straight line at a fixed speed and now the drive spools up and has "
                "to calibrate before the jump",
     "fact": "Quantum travel was originally a straight line at a fixed speed; now the drive spools up and calibrates."},
    {"id": "f4", "doc": "v5", "k": "v", "title": "Mining Gameplay Deep Dive", "date": "2019-02-05",
     "url": "https://www.youtube.com/watch?v=v5", "topics": ["mining"],
     "excerpt": "the rock has a resistance and an instability and if the energy goes into the red for too long the "
                "rock will explode and damage your ship",
     "fact": "A rock that stays in the red for too long explodes and damages the ship, the developers explained."},
    {"id": "f5", "doc": "v5", "k": "v", "title": "Mining Gameplay Deep Dive", "date": "2019-02-05",
     "url": "https://www.youtube.com/watch?v=v5", "topics": ["mining"],
     "excerpt": "we added modules so a mining head can take three consumable modules that change resistance",
     "fact": "A mining head can take three consumable modules that change the rock's resistance."},
    {"id": "f6", "doc": "c1", "k": "c", "title": "Portfolio: Hurston Dynamics", "date": "2013-07-23",
     "url": "https://r/1", "topics": ["hurston", "lorville"],
     "excerpt": "Hurston Dynamics bought an entire planet in 2865 and built its capital Lorville on it",
     "fact": "Hurston Dynamics bought an entire planet in 2865 and built its capital, Lorville, on it."},
    {"id": "f7", "doc": "v6", "k": "v", "title": "Hauling Cargo Across Stanton", "date": "2022-04-29",
     "url": "https://www.youtube.com/watch?v=v6", "topics": ["cargo", "hull"],
     "excerpt": "the hull c can carry four thousand six hundred scu when the spine is fully loaded",
     "fact": "A fully loaded Hull C spine carries 4,600 SCU of cargo."},
]


def _fixture_pack(cache: Optional[Path] = None, offline: bool = False):
    """A small hand-written pack (the shape build_dev_fact_pack.py writes). offline=True = the pack file is missing."""
    def load():
        if offline:
            raise FileNotFoundError("dev_facts_pack.json")
        return [dict(f) for f in FIXTURE_FACTS]
    return load


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
    dv = DevFacts(enabled=dev_on, pack_factory=_fixture_pack(cache), now=lambda: clock[0], sync=True,
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
    case("the sc_dev_history engine is found from SuitMk2 (the pack builder's engine)", load_engine() is not None)
    case("settings: dev_facts defaults OFF", settings_mod.DEFAULTS.get("dev_facts") is False)
    case("settings: dev_facts_max_per_hour defaults low (2)", settings_mod.DEFAULTS.get("dev_facts_max_per_hour") == 2)

    # -- the aside itself ---------------------------------------------------------------------------------------
    a = fact_spec(FIXTURE_FACTS[0])
    case("aside is framed, dated, one sentence, and says the FACT (not the title)",
         bool(a) and a["fixed_text"].startswith(FRAME_KEY) and "27 April 2016" in a["fixed_text"]
         and a["fixed_text"].rstrip(".").count(".") == 0 and "32 SCU" in a["fixed_text"]
         and "Q&A" not in a["fixed_text"])
    case("aside passes the real grounding gate", bool(a) and not ground(a, a["fixed_text"]))
    case("aside speaker is Montaigne, never Elah", bool(a) and a["speaker"] == "montaigne")
    case("aside carries no URL in its text", bool(a) and "http" not in a["fixed_text"])
    case("every fixture fact passes its own checks and the gate",
         all(not fact_problems(f) and not ground(fact_spec(f), fact_spec(f)["fixed_text"]) for f in FIXTURE_FACTS))
    case("a number said in digits that the excerpt says in words is the same number ('four thousand six hundred')",
         not fact_problems(FIXTURE_FACTS[6]))
    # the checks that stop a rewrite from adding a fact
    base = FIXTURE_FACTS[0]

    def probs(fact, **kw):
        return " | ".join(fact_problems(dict(base, fact=fact, **kw)))
    case("check: an invented number is refused",
         "numbers not in the source" in probs("The Prospector carries 40 SCU of ore in its side pods."))
    case("check: an invented name is refused",
         "names not in the source" in probs("Chris Roberts said the Prospector arm folds under the nose in flight."))
    case("check: invented content is refused",
         "content not in the source" in probs("The Prospector was modelled on deep-sea trawlers and tested in "
                                              "blizzards before launch."))
    case("check: first person is refused (Montaigne reports, he was not there)",
         "first or second person" in probs("We made the Prospector arm fold under the nose so it does not get "
                                           "knocked off."))
    case("check: the old 'X came out on DATE' template is refused",
         "title template" in probs("The video Q&A, MISC Prospector, Part I came out on 27 April 2016."))
    case("check: restating the title is refused", "restates the title" in probs("It was the Q&A on the MISC "
                                                                                 "Prospector, Part I."))
    case("check: an entry without its excerpt is refused", fact_problems(dict(base, excerpt="")) == ["no source excerpt"])
    case("check: two sentences are refused",
         "more than one sentence" in probs("The Prospector carries 32 SCU of ore. The arm folds under the nose."))
    # a rephrasing at speech time is held to the same source by the gate
    case("gate: a rephrase with an invented number is refused", any("unauthorized numbers" in f for f in ground(
        a, FRAME + " in a video from 27 April 2016, the Prospector carries 40 SCU of ore.")))
    case("gate: a rephrase with a wrong year is refused", any("unauthorized numbers" in f for f in ground(
        a, FRAME + " in a video from 27 April 2017, the Prospector carries 32 SCU of ore.")))
    case("gate: a rephrase with an invented name is refused", any("unauthorized names" in f for f in ground(
        a, FRAME + " in a video from 27 April 2016, Chris Roberts said the Prospector carries 32 SCU of ore.")))
    case("gate: a rephrase claiming memory is refused", any("memory" in f for f in ground(
        a, FRAME + " I remember, in a video from 27 April 2016, the Prospector carried 32 SCU of ore.")))
    case("gate: a rephrase without the aside frame is refused", any("frame" in f for f in ground(
        a, "In a video from 27 April 2016, the Prospector carries 32 SCU of ore in its side pods, pilot.")))
    case("gate: a rephrase without the date is refused", any("date" in f for f in ground(
        a, FRAME + " in an old video, the Prospector carries 32 SCU of ore in its side pods, pilot, truly.")))
    case("gate: a rephrase speaking the URL is refused", any("URL" in f for f in ground(
        a, FRAME + " in a video from 27 April 2016 on youtube.com, the Prospector carries 32 SCU.")))
    r = resume_spec(a)
    case("resume keeps the frame, the fact and the date and passes the gate",
         r["fixed_text"].startswith("Since you are still among the living") and "27 April 2016" in r["fixed_text"]
         and a["body"] in r["fixed_text"] and not ground(r, r["fixed_text"]))

    # -- THE REAL PACK (data/dev_facts_pack.json) --------------------------------------------------------------------
    import socket
    real_socket = socket.socket

    def _no_net(*a_, **k_):
        raise OSError("network disabled by the selftest")
    socket.socket = _no_net                      # the pack must load with no network at all
    try:
        pack_ok, pack = True, []
        try:
            pack = load_pack()
        except Exception as e:
            pack_ok = False
            print(f"  pack failed to load: {type(e).__name__}: {e}")
        t0 = time.perf_counter()
        dvp = DevFacts(enabled=True, max_per_hour=99, now=lambda: 0.0, rng=random.Random(4))
        sp0 = dvp.poll({"ship": "MISC Prospector : P"})
        first_ms = (time.perf_counter() - t0) * 1000
    finally:
        socket.socket = real_socket
    case(f"the bundled pack loads OFFLINE (network disabled) and a fact comes from it ({first_ms:.0f} ms)",
         pack_ok and bool(sp0) and sp0["source"]["id"] in {f["id"] for f in pack})
    raw = json.loads(PACK_PATH.read_text(encoding="utf-8")) if PACK_PATH.exists() else {"facts": []}
    case(f"the pack is a real size ({len(pack)} facts; aim 150-300)", 150 <= len(pack) <= 400)
    case("every entry in the pack FILE has its verbatim source excerpt, title, date and url",
         all(f.get("excerpt") and f.get("title") and f.get("date") and f.get("url") for f in raw["facts"])
         and len(raw["facts"]) == len(pack))
    bad_pack = [(f["id"], fact_problems(f)) for f in pack if fact_problems(f)]
    for fid, pr in bad_pack[:5]:
        print(f"  pack entry {fid}: {pr}")
    case("every pack fact: every number and name in it appears in its excerpt or metadata (fact_problems)",
         pack_ok and not bad_pack)
    ungrounded = [f["id"] for f in pack if ground(fact_spec(f), fact_spec(f)["fixed_text"])]
    case("every pack fact, as spoken, passes the real grounding gate", pack_ok and not ungrounded)
    case("no pack fact is a bare 'X came out on DATE' template, nor its title",
         all(not TEMPLATE.search(f["fact"]) and _norm(f["fact"]) != _norm(f["title"]) for f in pack))
    case("pack ids are unique", len({f["id"] for f in pack}) == len(pack))
    kinds = {k: sum(1 for f in pack if f.get("topic_kind") == k) for k in ("ship", "activity", "place")}
    case(f"the pack covers ships, activities and places {kinds}", all(v >= 10 for v in kinds.values()))
    t0 = time.perf_counter()
    for _ in range(20):
        dvp.poll({"ship": "Drake Cutlass Black : P", "location": "Lorville"})
    case("a poll with the pack loaded is instant (< 20 ms)", (time.perf_counter() - t0) / 20 * 1000 < 20)

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
    dfc = DevFacts(enabled=True, now=lambda: ct_[0], rng=random.Random(5), pack_factory=lambda: None)
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
    df = DevFacts(enabled=True, pack_factory=_fixture_pack(tmp / "a"), sync=True, now=lambda: 1000.0,
                  rng=random.Random(1))
    case("ship implies activity", df.activity({"ship": "Aegis Reclaimer : P"}) == "salvage")
    df.note_event("refinery_complete", {})
    case("a refinery event means mining", df.activity({}) == "mining")
    s1 = df.poll({"ship": "MISC Prospector : P"})
    case("topical: the current ship's fact first", bool(s1) and "Prospector" in s1["fixed_text"])
    df2 = DevFacts(enabled=True, pack_factory=_fixture_pack(tmp / "b"), sync=True, now=lambda: 1000.0,
                   rng=random.Random(2))
    s2 = df2.poll({"system": "Nyx"})     # nothing about Nyx in the fixture
    case("nothing topical: any pack fact, still framed and dated",
         bool(s2) and s2["fixed_text"].startswith(FRAME_KEY) and not ground(s2, s2["fixed_text"]))
    df3 = DevFacts(enabled=True, max_per_hour=99, pack_factory=_fixture_pack(), now=lambda: 1000.0,
                   rng=random.Random(6))
    said3 = []
    for _ in range(len(FIXTURE_FACTS) + 3):
        s3_ = df3.poll({"ship": "MISC Prospector : P"})
        if s3_:
            df3.spoken(s3_)
            said3.append(s3_["source"])
    case("never the same fact twice, nor two facts from one source, in a session (f4/f5 share a video)",
         len({x["id"] for x in said3}) == len(said3) and len({x["doc"] for x in said3}) == len(said3)
         and len(said3) == len({f["doc"] for f in FIXTURE_FACTS}))

    # -- offline with no cache: silent, no error --------------------------------------------------------------------
    warns = []

    class _H(logging.Handler):
        def emit(self, rec):
            if rec.levelno >= logging.WARNING:
                warns.append(rec.getMessage())
    h = _H()
    logging.getLogger().addHandler(h)
    try:
        off = DevFacts(enabled=True, pack_factory=_fixture_pack(tmp / "empty_cache", offline=True), sync=True,
                       now=lambda: 5000.0)
        outs = [off.poll({"ship": "MISC Prospector : P"}) for _ in range(5)]
        case("pack missing: no aside, no exception, no warning, one attempt then backoff",
             all(o is None for o in outs) and off.stats["unavailable"] == 1 and not warns)
        clock = [300_000.0]
        offc = DevFacts(enabled=True, pack_factory=_fixture_pack(tmp / "empty_core", offline=True), sync=True,
                        now=lambda: clock[0])
        core_off, sp_off = _core(clock, offc)
        core_off.state.set("ship", "MISC Prospector : P")
        for _ in range(10):
            clock[0] += 700
            core_off.ambient_tick()
            _drain(core_off)
        case("pack missing, through the core: zero dev lines, no warning",
             not any(_is_dev(x[3]) for x in sp_off.said) and not warns)
        ct2 = [0.0]
        calls2 = []
        off2 = DevFacts(enabled=True, pack_factory=lambda: calls2.append(1) or 1 / 0, now=lambda: ct2[0])
        o2 = [off2.poll({"ship": "x"}) for _ in range(3)]
        ct2[0] += RETRY_UNAVAILABLE_S + 1
        off2.poll({"ship": "x"})
        case("a broken pack is silent and retried only after the back-off", o2 == [None] * 3 and len(calls2) == 2)
        case("disabled: poll touches nothing", DevFacts(enabled=False, pack_factory=lambda: 1 / 0).poll({}) is None)
    finally:
        logging.getLogger().removeHandler(h)

    # -- the voice toggle through the core: flips both ways, persists, acknowledges --------------------------------
    clock = [400_000.0]
    saved = {}
    dvt = DevFacts(enabled=False, pack_factory=_fixture_pack(tmp / "tog"), now=lambda: clock[0], sync=True)
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
        dvs = DevFacts.from_settings(s, pack_factory=_fixture_pack(tmp / "tog2"), now=lambda: clock[0],
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
                                      pack_factory=lambda: touched.append(1) or 1 / 0)
        core, sp, events, t0 = replay(path, dflt, clock)
        n_dev = sum(1 for x in sp.said if _is_dev(x[3]))
        print(f"  default replay of {path.name}: {core.stats['events']} events, {(clock[0] - t0) / 3600:.1f} h, "
              f"{len(sp.said)} lines spoken, {n_dev} dev facts")
        case("DEFAULT settings: zero dev-fact lines over a replayed session", n_dev == 0 and len(sp.said) > 0)
        case("DEFAULT settings: the dev-history corpus is never loaded", not touched)

        clock = [0.0]
        facts = []
        on = DevFacts(enabled=True, max_per_hour=2, pack_factory=_fixture_pack(tmp / "replay"),
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
            return numbers_in(f"{s['fact']['excerpt']} {s['source']['title_raw']} {s['source']['date']} "
                              f"{s['date_spoken']}")
        case("no invented numbers: every number said is in the source excerpt, title or date",
             all(numbers_in(s["fixed_text"]) <= src_nums(s) for _, s in facts))
        hurt = [t for t, et, _ in events if et in ("injury", "incapacitated", "player_respawned")]
        case("none within 10 min after an injury or death in the log",
             all(not any(0 <= t - h < 600 for h in hurt) for t in times))
        spk = [t for t, et, _ in events if et in cc.EVENT_PRIORITY]
        case("none within the quiet window after any speaking event",
             all(not any(0 <= t - e < cc.CompanionCore.DEV_FACT_QUIET_AFTER_S for e in spk) for t in times))
        case("none in the session's first 10 minutes", all(t - t0 >= 600 for t in times))

    # -- synthetic: combat and injury windows are refused even when everything else is quiet ----------------------
    clock = [100_000.0]
    dv = DevFacts(enabled=True, pack_factory=_fixture_pack(tmp / "syn"), now=lambda: clock[0], sync=True)
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
    dvc = DevFacts(enabled=True, pack_factory=_fixture_pack(tmp / "co"), now=lambda: clock[0], sync=True)
    dvc._rng = type("R", (), {"random": lambda self: 0.0, "shuffle": lambda self, x: None,
                              "choice": lambda self, x: x[0]})()
    cco, sco = _core(clock, dvc)
    fspec = fact_spec(FIXTURE_FACTS[3])
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
              f"{bag.pop()}\n     source: {s['source']['title_raw']} | {s['source']['date']} | {s['source']['url']}"
              f"\n     excerpt: {s['fact']['excerpt']}")
    return 0


def _sample(n: int, seed: Optional[int] = None) -> int:
    """N random facts from the pack as Montaigne would say them, each with the verbatim excerpt it came from."""
    pack = load_pack()
    rng = random.Random(seed)
    for f in rng.sample(pack, min(n, len(pack))):
        s = fact_spec(f, rng.choice(FRAMES))
        print(f"[{f['id']}] ({', '.join(f['topics'])})\n  SAID:    {s['fixed_text']}\n  EXCERPT: {f['excerpt']}\n"
              f"  SOURCE:  {f['title']} | {f['date']} | {f['url']}\n")
    print(f"({len(pack)} facts in {PACK_PATH.name})")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    try:
        sys.stdout.reconfigure(encoding="utf-8")        # a bare cp1252 console must not crash on a curly quote
    except Exception:
        pass
    if "--selftest" in sys.argv:
        rest = [a for a in sys.argv[1:] if not a.startswith("--")]
        sys.exit(_selftest(rest[0] if rest else None))
    if "--examples" in sys.argv:
        sys.exit(_examples())
    if "--sample" in sys.argv:
        i = sys.argv.index("--sample")
        n_ = int(sys.argv[i + 1]) if len(sys.argv) > i + 1 and sys.argv[i + 1].isdigit() else 12
        seed_ = next((int(a.split("=", 1)[1]) for a in sys.argv if a.startswith("--seed=")), None)
        sys.exit(_sample(n_, seed_))
    print(__doc__)
