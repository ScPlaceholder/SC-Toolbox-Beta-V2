"""conversation.py - the DIRECT CONVERSATION LANE (2026-09-23).

When the pilot asks Elah (suit AI) or Montaigne (ship AI) something, the small realizer must NOT become the mind.
This module is the part of ARCHITECTURE.md "Direct conversation lane" that sits between STT and the realizer:

    PLAYER SPEECH -> STT -> route()  (addressee / intent / slots; deterministic, no model)
                  -> answer_spec()   (QUESTION PLANNER: factual -> state, memory -> history_facts,
                                      opinion -> interpretation of EXISTING facts, action -> unsupported,
                                      social -> relationship-free acknowledgement, unknown -> UNKNOWN said as such)
                  -> [companion_core: realizer -> ground() -> speech]

Rules this module keeps:
  * Every OBSERVED claim value is read straight out of `state`; every HISTORY value out of `history_facts`.
    Nothing is defaulted. A fact that is absent becomes a claim of kind "UNKNOWN" whose value is the literal
    string "unknown", and the stance tells the character to SAY they do not know. A guess is never produced.
  * Opinions only interpret facts that exist (claims + an interpretation). An opinion about something with
    no facts behind it (the Vanduul, a ship we are not in) is an UNKNOWN too.
  * Move labels come only from ambient_spec._ELAH_MOVES / _MONT_MOVES (the trained ones).
  * Montaigne knows things secondhand (the suit's feed, the ship's log); his stances say so.

`ground()` only polices NUMBERS. A realizer answering an UNKNOWN location with "You're in Lorville" passes it,
because no number is involved. `ground_direct()` adds a closed-set entity check (every place/system name in
location_names.LOCATION_MAP) so an invented place is refused too. Ship and faction names are NOT covered.

State keys this lane reads (build them with lane_state_from_core(), which copies ONLY keys the trackers have
actually set; companion_core._ambient_state() defaults in_armistice to False, which would turn "unknown" into a
confident "no", so do not feed that dict here):
    location, system, ship, planetary_body, jurisdiction, in_armistice, session_earnings, session_deaths,
    injuries ({body_part: severity}; present only once the log is live, so {} means "none recorded"),
    recent_locations, heart_rate / hull_pct / fuel_pct / shields_pct (never set today -> always UNKNOWN)
history_facts: the dict dream_queue.history_facts(store, location=..., ship=...) returns.

Selftest: python conversation.py --selftest

PROPOSED INTEGRATION (text only, NOT applied; companion_core.py / the app are being edited elsewhere):
  1. Ears. The SuitMk2 app is already Qt (PySide6), so it can host the Star Map's EarsController unchanged:
     import it from the Starmap skill (or vendor a copy of voice/ears.py + input_devices.py into SuitMk2/ui/),
     give it its own binding, mode "push". While ears.listeningChanged(True): core.gate_state.pilot_speaking =
     True, so SpeakGate defers every non-URGENT line (ambient/event chatter) while the pilot talks.
  2. Hand-off. ears.transcript(text) -> (optionally the Star Map CommandRouter first: its `unrecognized` signal
     is the natural "not a map command" hand-off, and later the tool broker for the `action` intent) ->
         state = lane_state_from_core(core.state, core.volatile)
         hist  = history_facts(store, location=state.get("location"), ship=state.get("ship"))
         spec  = lane.handle(text, state, hist)
     on a worker thread (history_facts reads a jsonl), never on the Qt thread.
  3. Speak. A small new CompanionCore method, e.g. answer(spec): gate with Priority.URGENT (exempt from
     cooldowns, allowed in hot combat; mute still drops it, correctly), then put it at the FRONT of the realizer
     work queue, evicting a queued ambient item if the queue is full (today _work_put drops on Full, which would
     drop the pilot's question), realize, gate with ground_direct() instead of ground() when spec["lane"] ==
     "direct", and speech.say(text, spec["speaker"], PRIORITY_URGENT), which jumps ahead of queued ambient
     lines (MAX_AGE 20 s, so a stale answer is dropped rather than spoken late).
  4. Refusal. If the gate refuses, re-realize once (the realizer samples); if it refuses again, stay silent and
     show "couldn't answer that cleanly" in the status window. No canned fallback line, same as every other lane.
  5. Realizer gap: the adapters were trained on specs/train.jsonl, which contains OBSERVED and HISTORY claims
     only (2138 / 388) and ZERO "UNKNOWN" claims. How the realizer voices an UNKNOWN is untested; the next
     teacher-data pass needs UNKNOWN specs from this module before this lane ships.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from ambient_spec import _ELAH_MOVES, _MONT_MOVES, _claim   # noqa: E402
from grounding_validator import ground                      # noqa: E402
from location_names import LOCATION_MAP                      # noqa: E402

Spec = dict[str, Any]
Route = tuple[str, str, dict]

UNKNOWN_VALUE = "unknown"
_LEN = {"elah": (5, 22), "montaigne": (8, 38)}          # direct answers: short; Montaigne is allowed to wander
SELF_FACTS = {("companion.can_act", False)}              # facts about the companions themselves, not the game

# ---------------------------------------------------------------------------------------------------------------
# ROUTING (deterministic)
# ---------------------------------------------------------------------------------------------------------------
# WHO ANSWERS (J 2026-10-05: "have it decide unless the user specifically says an ai"). The lane picks the speaker
# from the kind of question (_DEFAULT_ADDRESSEE) unless the pilot ADDRESSES one of them by name; then that one
# answers. A name is an address when it is where a name is put to call somebody:
#     first word            "Montaigne how is the ship"        (after "hey", "ok", "so" ...)
#     after a greeting      "hey Elah"                          (anywhere in the sentence)
#     last word             "where are we Elah"
#     beside a comma        "tell me, Montaigne, where are we"
# Anywhere else it is a MENTION ("what did Montaigne say about cargo") and decides nothing: the lane's own choice
# stands and the name stays in the sentence. Two of them addressed in one sentence: the first one. This costs one
# case: an address in the middle of a sentence with no comma heard ("tell me Montaigne where are we") is read as
# a mention. Until this change a name ANYWHERE forced the speaker and was cut out of the sentence, so "what did
# Montaigne say about cargo" went to Montaigne as "what did say about cargo".
#
# The names as speech-to-text writes them. An explicit list, whole words only; nothing fuzzy, so no ordinary word
# can become a name by being close to one. "mountain" is deliberately NOT here: it is an ordinary word, and stays
# in _WEAK below, which only counts as the first or the last word.
_NAMES = {"elah": ("elah", "ela", "ella", "ellah", "eila", "aila", "ayla", "eli", "eller"),
          "montaigne": ("montaigne", "montagne", "montane", "montaine", "montain", "montange", "michel")}
_NAME_OF = {heard: who for who, variants in _NAMES.items() for heard in variants}
_CALL = {"hey", "hi", "hello", "yo", "oi", "ok", "okay"}        # "hey Elah": the next word is who is being called
_FILLER_WORDS = {"hey", "hi", "hello", "ok", "okay", "so", "um", "uh", "oi", "yo", "and", "right", "listen"}   # as _FILLER
_WEAK = {"suit": "elah", "ship": "montaigne", "mountain": "montaigne", "monty": "montaigne", "monte": "montaigne"}
_FILLER = r"^(?:(?:hey|hi|hello|ok|okay|so|um|uh|oi|yo|and|right|listen)\s+)+"
_DETERMINERS = {"the", "this", "my", "our", "a", "your", "her", "that", "his", "whose", "which", "what"}
_NOISE = {"uh", "um", "hmm", "er", "ah", "the", "a", "oh", "huh", "mm"}

_OPINION = [
    r"\bwhat (?:do|d|would) you (?:think|make|reckon|feel) (?:of|about) (?P<x>.+)",
    r"\bwhats your (?:opinion|take|view|verdict) (?:of|on|about) (?P<x>.+)",
    r"\byour (?:opinion|thoughts|take|view|verdict) (?:of|on|about) (?P<x>.+)",
    r"\bhow do you feel about (?P<x>.+)",
    r"\bdo you (?:like|rate|trust) (?P<x>.+)",
    r"\bthoughts on (?P<x>.+)",
]
_MEMORY = [
    ("been_here", r"\b(?:been|visited|come|came|stopped)\b(?:\s+\w+){0,4}?\s+(?:here|this place|this station|"
                  r"this city|this planet|this moon)\b|\bfirst time (?:here|at this|in this|visiting)\b|"
                  r"\b(?:been|visited) (?:here )?before\b"),
    ("first_ship", r"\bfirst (?:time )?(?:fly|flew|flown|flying|took|take|get|got|bought)\b|"
                   r"\bhow long have (?:we|i) (?:had|owned|flown|been flying)\b|\bwhen did (?:we|i) (?:get|buy|start flying)\b"),
]
_FACTUAL = [
    ("heart_rate", r"\b(?:heart ?rate|pulse|bpm|heartbeat)\b"),
    ("vehicle_status", r"\b(?:hull|fuel|shields?|ammo|hydrogen)\b"),
    ("injury", r"\bhow (?:hurt|bad|injured|wounded|banged up)\b|\bam i (?:hurt|injured|wounded|bleeding|ok|okay|alright)\b|"
               r"\binjur\w*|\bwound\w*|\bmy health\b"),
    ("deaths", r"\bhow many times (?:have |did )?(?:i|we) (?:die|died|been killed|gone down|go down)\b|"
               r"\bhow many deaths\b|\bdeath count\b"),
    ("armistice", r"\barmistice\b|\b(?:safe|green|no fire|no weapons) zone\b|\bcan i (?:shoot|fire|draw)\b|"
                  r"\bweapons (?:free|hot|allowed)\b"),
    ("jurisdiction", r"\bjurisdiction\b|\bwhose (?:space|territory|turf)\b|\bwho (?:owns|controls|runs|polices) (?:this|here)\b"),
    ("earnings", r"\b(?:how much|what)\b.*\b(?:earn\w*|made|make|money|auec|credits|paid|profit)\b|\bearnings\b|\bpayout\b"),
    ("system", r"\b(?:which|what) (?:star )?system\b|\bsystem (?:are|am|is) (?:we|i|this)\b"),
    # Loadout (J 2026-09-24, from Battle_Buddy's parser): what the pilot carries.
    ("loadout", r"\bwhat (?:am i|are we) (?:carrying|packing|holding)\b|\bmy loadout\b|\bloadout\b|"
                r"\bhow many (?:med ?pens|medpens|mags|magazines|grenades|spare mags)\b|"
                r"\bwhat (?:guns?|weapons?) (?:do i|have i|am i|are we)\b|\bam i (?:low on|out of) (?:ammo|mags|medpens)\b"),
    # J 2026-09-24 (the old skill had it as a Wingman tool): "what missions do I have".
    ("mission", r"\bwhat (?:missions?|contracts?|jobs?) (?:do|have|am|are|did)\b|"
                r"\b(?:my|current|active|our) (?:missions?|contracts?|jobs?|objectives?)\b|"
                r"\bwhat(?:'?s| is) (?:the|my|our) (?:mission|contract|job|objective)\b|"
                r"\bwhat (?:am i|are we) (?:doing|supposed to (?:do|be doing))\b"),
    ("ship", r"\b(?:which|what) ship\b|\bwhat (?:am i|are we) (?:flying|in|on|sitting in)\b|\bwhats this ship\b"),
    ("location", r"\bwhere (?:am i|are we|is this|we at|is here)\b|\bwhere (?:we|i) (?:are|am)\b|\bwhere im\b|"
                 r"\bwhat (?:place|station|planet|moon|city|town) is this\b|\bwhat is this place\b|\bcurrent location\b"),
]
_ACTION = [
    r"^(?:please )?(?P<v>set|plot|open|close|turn|switch|lower|raise|land|launch|fire|shoot|call|request|jump|power|"
    r"start|stop|eject|lock|unlock|deploy|retract|scan|mark|target|fly|bring|drop|pick|spool|activate|deactivate|"
    r"engage|hail|dock|undock|take off)\b",
    r"\b(?:can|could|would|will) you (?:please )?(?!tell|remind|say|explain|repeat|hear|see)(?P<v>\w+)",
    r"\bi (?:need|want) you to (?P<v>\w+)",
]
_SOCIAL = [
    ("thanks", r"\b(?:thanks|thank you|thank ya|thankyou|cheers|appreciate it|much appreciated|good job|nice work|well done)\b"),
    ("how_are_you", r"\bhow are you\b|\bhow re you\b|\bhow you doing\b|\bhows it going\b|\bhow are things\b|"
                    r"\byou (?:ok|okay|alright|good)\b|\bhow do you feel\b|\bhow have you been\b"),
    ("greeting", r"^(?:hi|hello|hey|morning|evening|good (?:morning|evening|afternoon)|yo|greetings|you there|are you there)"
                 r"(?: there)?$"),
]
_DEFAULT_ADDRESSEE = {"factual": "elah", "memory": "montaigne", "opinion": "montaigne",
                      "social": "elah", "action": "elah", "unknown": "elah", "noise": "elah"}


def _norm(s: str) -> str:
    s = (s or "").lower().replace("’", "'").replace("'", "")
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _named(utterance: str) -> tuple[Optional[str], str]:
    """(the companion addressed by name or None, the normalized sentence without the names used as an address).
    See WHO ANSWERS above for what counts as an address. A name that is only mentioned is left in the sentence."""
    s = (utterance or "").lower().replace("\u2019", "'").replace("'", "")
    toks = re.sub(r"[^a-z0-9,\s]", " ", s).replace(",", " , ").split()      # the comma is kept as a word of its own
    words = [i for i, w in enumerate(toks) if w != ","]
    lead = 0                                                                 # words[lead] is the first real word
    while lead < len(words) and toks[words[lead]] in _FILLER_WORDS:
        lead += 1
    who, drop = None, set()
    for n, i in enumerate(words):
        name = _NAME_OF.get(toks[i])
        if name is None:
            continue
        addressed = (n == lead or n == len(words) - 1
                     or (i > 0 and toks[i - 1] == ",") or (i + 1 < len(toks) and toks[i + 1] == ",")
                     or (n > 0 and toks[words[n - 1]] in _CALL))
        if addressed:
            who = who or name                                                # two addressed: the first one
            drop.add(i)
    return who, " ".join(w for i, w in enumerate(toks) if w != "," and i not in drop)


def _addressee(utterance: str) -> tuple[Optional[str], str, bool]:
    """(explicit addressee or None, text with the vocative removed, had a greeting filler)."""
    who, t = _named(utterance)
    greeted = bool(re.match(r"^(?:hey|hi|hello|yo|oi)\b", t))
    t = re.sub(r"\s+", " ", re.sub(_FILLER, "", t.strip())).strip()
    toks = t.split()
    if toks and toks[0] in _WEAK:                                   # "ship, how's the hull"
        who = who or _WEAK[toks[0]]
        toks = toks[1:]
    if len(toks) >= 2 and toks[-1] in _WEAK and toks[-2] not in _DETERMINERS:   # "thanks suit", not "what ship"
        who = who or _WEAK[toks[-1]]
        toks = toks[:-1]
    elif len(toks) == 1 and toks[0] in _WEAK:
        who = who or _WEAK[toks[0]]
        toks = []
    return who, " ".join(toks), greeted


def route(utterance: str) -> Route:
    """utterance -> (addressee, intent, slots). Deterministic; no model. slots always carries 'text' (the
    normalized utterance minus the vocative) and, where it applies, 'topic' / 'subject' / 'request'."""
    who, t, greeted = _addressee(utterance)
    slots: dict = {"text": t, "said": _norm(utterance)}
    intent = None
    for rx in _OPINION:
        m = re.search(rx, t)
        if m:
            intent, slots["subject"] = "opinion", m.group("x").strip()
            break
    if intent is None:
        for topic, rx in _MEMORY:
            if re.search(rx, t):
                intent, slots["topic"] = "memory", topic
                break
    if intent is None:
        for topic, rx in _FACTUAL:
            if re.search(rx, t):
                intent, slots["topic"] = "factual", topic
                break
    if intent is None:
        for rx in _ACTION:
            m = re.search(rx, t)
            if m:
                intent, slots["request"] = "action", t[m.start("v"):].strip()
                break
    if intent is None:
        for topic, rx in _SOCIAL:
            if re.search(rx, t):
                intent, slots["topic"] = "social", topic
                break
    if intent is None:
        if not t and (who or greeted):                   # "Elah?" / "hey Montaigne": being called is a greeting
            intent, slots["topic"] = "social", "greeting"
        elif not [w for w in t.split() if w not in _NOISE]:
            intent = "noise"
        else:
            intent = "unknown"
    return who or _DEFAULT_ADDRESSEE[intent], intent, slots


# ---------------------------------------------------------------------------------------------------------------
# QUESTION PLANNER
# ---------------------------------------------------------------------------------------------------------------
def _known(state: dict, key: str) -> tuple[bool, Any]:
    """THE guard. A fact is known only if the trackers set it; nothing is ever defaulted here."""
    if key not in state:
        return False, None
    v = state[key]
    if v is None or (isinstance(v, str) and not v.strip()):
        return False, None
    return True, v


# (speaker, bucket) -> [(move, stance)]. Stances carry no numbers; Montaigne's admit he has things secondhand.
_VOICE: dict[tuple[str, str], list[tuple[str, str]]] = {
    ("elah", "fact"): [("PRACTICAL", "straight answer from the suit's readings, no garnish"),
                       ("DEADPAN", "answer it flat, as if the pilot should have known")],
    ("elah", "fact_unknown"): [("DEADPAN", "no reading on that; say so plainly and do not guess"),
                               ("PRACTICAL", "the suit has no data for that; say it, offer nothing invented")],
    ("elah", "memory"): [("CALLBACK", "she remembers; cite what the record says, nothing more")],
    ("elah", "memory_unknown"): [("PRACTICAL", "nothing in the record; say so rather than pretend to remember")],
    ("elah", "opinion"): [("DEADPAN", "a dry verdict drawn only from the facts given"),
                          ("CORRECTION", "a blunt correction of the rosy view, using only the facts given")],
    ("elah", "opinion_unknown"): [("DEADPAN", "no data on it, so no opinion worth having; say so")],
    ("elah", "social_how_are_you"): [("DEADPAN", "fine, in a suit's way; turn it back to the pilot")],
    ("elah", "social_thanks"): [("DEADPAN", "takes the thanks without fuss")],
    ("elah", "social_greeting"): [("DEADPAN", "here and listening; brief")],
    ("elah", "action"): [("PRACTICAL", "cannot do that yet; nothing is wired for it; say so plainly")],
    ("elah", "unknown"): [("DEADPAN", "did not catch a question in that; ask the pilot to say it again")],
    ("montaigne", "fact"): [("EVIDENCE_SKEPTIC", "he has it secondhand from the suit's feed and says so"),
                            ("SELF_DEPRECATION", "a ship's report, offered modestly")],
    ("montaigne", "fact_unknown"): [("SELF_DEPRECATION", "he has not been told, and admits it rather than invent it")],
    ("montaigne", "memory"): [("NEAR_RECOGNITION", "the log says so; he half remembers it himself")],
    ("montaigne", "memory_unknown"): [("EVIDENCE_SKEPTIC", "his log holds no record of it; a memory without a record is only a story")],
    ("montaigne", "opinion"): [("ESSAY_DIGRESSION", "a digression on it, interpreting only what the log shows")],
    ("montaigne", "opinion_ship"): [("HORSE_ANALOGY", "the ship as a horse he knows only from the log")],
    ("montaigne", "opinion_pilot"): [("PILOT_CHARACTER", "reads the pilot's character from the record, generously")],
    ("montaigne", "opinion_unknown"): [("SKEPTICAL_REVERSAL", "he knows it only by rumour, and will not pass rumour off as judgement")],
    ("montaigne", "social_how_are_you"): [("SELF_DEPRECATION", "well enough, for a ship who believes he is a philosopher")],
    ("montaigne", "social_thanks"): [("SELF_DEPRECATION", "accepts thanks he doubts a ship deserves")],
    ("montaigne", "social_greeting"): [("SELF_DEPRECATION", "present, as a ship always is, and listening")],
    ("montaigne", "action"): [("SELF_DEPRECATION", "a ship who cannot even move himself; he cannot do that yet")],
    ("montaigne", "unknown"): [("SELF_DEPRECATION", "he did not follow the question and asks the pilot to put it again")],
}

# factual topic -> [(predicate, state key)]. The first is required; the rest are added only if known.
_FACT_KEYS = {
    "location": [("location.name", "location"), ("location.body", "planetary_body")],
    "system": [("location.system", "system")],
    "ship": [("ship.name", "ship")],
    "armistice": [("jurisdiction.armistice", "in_armistice"), ("jurisdiction.zone", "jurisdiction")],
    "jurisdiction": [("jurisdiction.zone", "jurisdiction")],
    "earnings": [("session.earnings_auec", "session_earnings")],
    "deaths": [("session.deaths", "session_deaths")],
    "heart_rate": [("suit.heart_rate", "heart_rate")],
    "loadout": [("loadout.weapons", "loadout_weapons"), ("loadout.medpens", "loadout_medpens"),
                ("loadout.spare_mags", "loadout_spare_mags"), ("loadout.grenades", "loadout_grenades")],
    "mission": [("mission.name", "mission"), ("mission.objective", "objective"),
                ("session.contracts_completed", "contracts_done")],
}
_VEHICLE = {"hull": ("ship.hull_pct", "hull_pct"), "fuel": ("ship.fuel_pct", "fuel_pct"),
            "hydrogen": ("ship.fuel_pct", "fuel_pct"), "shield": ("ship.shields_pct", "shields_pct"),
            "ammo": ("ship.ammo", "ammo")}


def _unknown(cid: str, predicate: str) -> dict:
    return _claim(cid, "UNKNOWN", predicate, UNKNOWN_VALUE)


def _required_values(claims: list) -> list[str]:
    return [str(c["value"]) for c in claims if c["kind"] in ("OBSERVED", "HISTORY")
            and isinstance(c["value"], int) and not isinstance(c["value"], bool) and c["value"] > 0]


class _Ids:
    def __init__(self):
        self.n = 0

    def __call__(self) -> str:
        self.n += 1
        return f"C{self.n}"


def _fact_claims(pairs: list, state: dict, nid: _Ids) -> list:
    """First pair is the one asked about: known -> OBSERVED, absent -> UNKNOWN. Extras only if known."""
    out = []
    for i, (pred, key) in enumerate(pairs):
        ok, v = _known(state, key)
        if ok:
            out.append(_claim(nid(), "OBSERVED", pred, v))
        elif i == 0:
            out.append(_unknown(nid(), pred))
    return out


def _injury_claims(state: dict, nid: _Ids) -> list:
    ok, inj = _known(state, "injuries")
    if not ok or not isinstance(inj, dict):
        return [_unknown(nid(), "suit.injuries")]
    if not inj:                                   # the log is live and has reported no injury
        return [_claim(nid(), "OBSERVED", "suit.injuries_on_record", "none recorded")]
    out = []
    for part, sev in list(inj.items())[:3]:
        out.append(_claim(nid(), "OBSERVED", "suit.injury_body_part", str(part).replace("_", " ")))
        if sev not in (None, ""):
            out.append(_claim(nid(), "OBSERVED", "suit.injury_severity", sev))
    return out


def _subject_claims(subject: str, state: dict, hist: dict, nid: _Ids) -> tuple[str, list]:
    """What an opinion may lean on. Returns (bucket suffix, claims). No facts behind the subject -> UNKNOWN."""
    s = _norm(subject)
    loc_ok, loc = _known(state, "location")
    ship_ok, ship = _known(state, "ship")
    if re.search(r"\b(?:this place|here|this station|this planet|this moon|this city|this system|where we are)\b", s) \
            or (loc_ok and _norm(str(loc)) and _norm(str(loc)) in s):
        claims = _fact_claims([("location.name", "location"), ("location.system", "system")], state, nid)
        if loc_ok and hist.get("location_first_visit"):
            claims.append(_claim(nid(), "HISTORY", "history.location_first_visit", hist["location_first_visit"]))
        return "", claims
    if re.search(r"\b(?:this ship|the ship|our ship|my ship|this boat|her|yourself)\b", s) \
            or (ship_ok and _norm(str(ship)) and _norm(str(ship)) in s):
        claims = _fact_claims([("ship.name", "ship")], state, nid)
        if ship_ok and hist.get("ship_first_flown"):
            claims.append(_claim(nid(), "HISTORY", "history.ship_first_flown", hist["ship_first_flown"]))
        return "_ship", claims
    if re.search(r"\b(?:me|my flying|my piloting|the pilot|how i fly|how im doing|today|this run|this session|tonight)\b", s):
        claims = []
        for pred, key in (("session.earnings_auec", "session_earnings"), ("session.deaths", "session_deaths")):
            ok, v = _known(state, key)
            if ok:
                claims.append(_claim(nid(), "OBSERVED", pred, v))
        ok, locs = _known(state, "recent_locations")
        if ok and isinstance(locs, list) and locs:
            claims.append(_claim(nid(), "OBSERVED", "session.locations_visited", len(locs)))
        if not claims:
            claims = [_unknown(nid(), "session.record")]
        return "_pilot", claims
    return "", [_claim(nid(), "PILOT_SUBMISSION", "pilot.asked_about", s), _unknown(nid(), "opinion.subject_facts")]


def answer_spec(route_result: Route, state: dict, history_facts: Optional[dict], variant: int = 0) -> Optional[Spec]:
    """Route -> one semantic spec whose claims come ONLY from state / history_facts, or None for noise."""
    addressee, intent, slots = route_result
    hist = history_facts or {}
    state = state or {}
    nid = _Ids()
    topic = slots.get("topic", "")
    suffix = ""

    if intent == "noise":
        return None
    if intent == "factual":
        if topic == "injury":
            claims = _injury_claims(state, nid)
        elif topic == "vehicle_status":
            word = next((w for w in ("hull", "fuel", "hydrogen", "shield", "ammo") if w in slots["text"]), "hull")
            claims = _fact_claims([_VEHICLE[word]], state, nid)
        else:
            claims = _fact_claims(_FACT_KEYS[topic], state, nid)
        bucket = "fact"
    elif intent == "memory":
        subject_key, hist_key, pred = (("location", "location_first_visit", "history.location_first_visit")
                                       if topic == "been_here" else
                                       ("ship", "ship_first_flown", "history.ship_first_flown"))
        claims = _fact_claims([("location.name" if subject_key == "location" else "ship.name", subject_key)], state, nid)
        if claims[0]["kind"] == "OBSERVED" and hist.get(hist_key):
            claims.append(_claim(nid(), "HISTORY", pred, hist[hist_key]))
        else:
            claims.append(_unknown(nid(), pred))
        bucket = "memory"
    elif intent == "opinion":
        suffix, claims = _subject_claims(slots.get("subject", ""), state, hist, nid)
        bucket = "opinion"
    elif intent == "action":
        claims = [_claim(nid(), "PILOT_SUBMISSION", "pilot.requested", slots.get("request", "")),
                  _claim(nid(), "OBSERVED", "companion.can_act", False)]
        bucket = "action"
    elif intent == "social":
        claims = [_claim(nid(), "PILOT_SUBMISSION", "pilot.said", slots["text"] or slots["said"])]
        bucket = f"social_{topic}"
    else:
        claims = [_claim(nid(), "PILOT_SUBMISSION", "pilot.said", " ".join(slots["text"].split()[:10])),
                  _unknown(nid(), "pilot.question")]
        bucket = "unknown"

    has_unknown = any(c["kind"] == "UNKNOWN" for c in claims)
    if bucket in ("fact", "memory", "opinion"):
        if has_unknown and not any(c["kind"] in ("OBSERVED", "HISTORY") for c in claims):
            key = f"{bucket}_unknown"
        elif bucket == "memory" and has_unknown:
            key = "memory_unknown"          # we know where we are, but not whether we have been before
        else:
            key = bucket + (suffix if (addressee, bucket + suffix) in _VOICE else "")
    else:
        key = bucket
    pool = _VOICE[(addressee, key)]
    move, stance = pool[variant % len(pool)]
    lo, hi = _LEN[addressee]
    return {
        "scenario": f"direct_{intent}_{topic or 'general'}",
        "speaker": addressee,
        "rhetoric": [move],
        "claims": claims,
        "interpretation": {"owner": addressee, "text": stance,
                           "grounds": [c["id"] for c in claims if c["kind"] in ("OBSERVED", "HISTORY")]},
        "required_claims": [c["id"] for c in claims if c["kind"] != "PILOT_SUBMISSION"][:2],
        "required_values": _required_values(claims),
        "length_words": [lo, hi],
        "id": f"dir_{intent}_{topic or 'general'}_{addressee}_v{variant % len(pool)}",
        "lane": "direct",
        "route": {"addressee": addressee, "intent": intent, "topic": topic, "text": slots["text"]},
    }


# ---------------------------------------------------------------------------------------------------------------
# Gate extension + state adapter
# ---------------------------------------------------------------------------------------------------------------
_ENTITY_NAMES = sorted({n for info in LOCATION_MAP.values() for n in (info.name, info.system) if n and len(n) >= 4}
                       | {"Stanton", "Pyro", "Nyx", "Castra", "Terra", "Magnus"}, key=len, reverse=True)


def ground_direct(spec: Spec, text: str) -> list[str]:
    """ground() plus a closed-set entity check: any known place/system named in the line must be a claim value.
    Catches the realizer answering an UNKNOWN location with a real-sounding place, which ground() cannot."""
    fails = ground(spec, text)
    authorised = " | ".join(str(c["value"]).lower() for c in spec["claims"])
    low = text.lower()
    invented = [n for n in _ENTITY_NAMES
                if re.search(rf"(?<![\w-]){re.escape(n.lower())}(?![\w-])", low) and n.lower() not in authorised]
    if invented:
        fails.append(f"unauthorized names {invented}")
    return fails


def lane_state_from_core(state_store, volatile) -> dict:
    """Build the lane's state from companion_core's trackers, copying ONLY keys that were actually set."""
    st = state_store.get_all()
    try:
        snap = volatile.get_context_snapshot()
    except Exception:
        snap = {}
    out: dict = {}
    for src, dst in (("location_name", "location"), ("star_system", "system"), ("ship", "ship"),
                     ("planetary_body", "planetary_body"), ("jurisdiction", "jurisdiction"),
                     ("in_armistice", "in_armistice"), ("session_earnings", "session_earnings"),
                     ("session_deaths", "session_deaths")):
        if st.get(src) is not None and st.get(src) != "":
            out[dst] = st[src]
    if any(k in st for k in ("player_name", "location_raw", "session_deaths")):      # the log is live
        out["injuries"] = {k[len("injury_"):]: v for k, v in st.items() if k.startswith("injury_") and v}
    # The mission in hand = the last one accepted, unless it has since been completed or failed. The log gives no
    # list of active contracts, so this is the one it can vouch for; anything else is UNKNOWN, never guessed.
    acc = st.get("last_contract_accepted")
    if acc and acc not in (st.get("last_contract_completed"), st.get("last_contract_failed")):
        out["mission"] = acc
        if st.get("last_objective"):
            out["objective"] = st["last_objective"]
    for k in ("loadout_weapons", "loadout_medpens", "loadout_spare_mags", "loadout_grenades"):
        if st.get(k) is not None:
            out[k] = st[k]
    if st.get("session_contracts_completed"):
        out["contracts_done"] = st["session_contracts_completed"]
    locs = snap.get("recent_locations_visited")
    if locs:
        out["recent_locations"] = list(locs)
    return out


class ConversationLane:
    """One pilot utterance in -> at most one spec out. Rotates variants so repeated questions vary in delivery."""

    def __init__(self):
        self.variant = 0
        self.last_route: Optional[Route] = None

    def handle(self, utterance: str, state: dict, history_facts: Optional[dict] = None) -> Optional[Spec]:
        r = route(utterance)
        self.last_route = r
        spec = answer_spec(r, state, history_facts, self.variant)
        if spec is not None:
            self.variant += 1
        return spec

    ground = staticmethod(ground_direct)


# ---------------------------------------------------------------------------------------------------------------
# Structural check (used by the selftest; usable as a debug assertion in integration)
# ---------------------------------------------------------------------------------------------------------------
def _state_values(obj: Any) -> set[str]:
    out: set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            out |= {str(k), str(k).replace("_", " ")} | _state_values(v)
    elif isinstance(obj, (list, tuple)):
        out.add(str(len(obj)))
        for v in obj:
            out |= _state_values(v)
    elif obj is not None:
        out.add(str(obj))
    return out


def check_spec(spec: Spec, state: dict, hist: dict, utterance: str) -> list[str]:
    """Every claim traceable to its source; UNKNOWN is literal; moves trained; stance number-free."""
    errs = []
    allowed_moves = _ELAH_MOVES if spec["speaker"] == "elah" else _MONT_MOVES
    errs += [f"untrained move {m} for {spec['speaker']}" for m in spec["rhetoric"] if m not in allowed_moves]
    if re.search(r"\d", spec["interpretation"]["text"]):
        errs.append("stance carries a number")
    sv, hv, said = _state_values(state), {str(v) for v in (hist or {}).values()}, _norm(utterance)
    ids = {c["id"] for c in spec["claims"]}
    for c in spec["claims"]:
        k, v = c["kind"], c["value"]
        if k == "OBSERVED" and (c["predicate"], v) not in SELF_FACTS and str(v) not in sv \
                and not (c["predicate"] == "suit.injuries_on_record" and state.get("injuries") == {}):
            errs.append(f"OBSERVED {c['predicate']}={v!r} is not in state")
        elif k == "HISTORY" and str(v) not in hv:
            errs.append(f"HISTORY {c['predicate']}={v!r} is not in history_facts")
        elif k == "PILOT_SUBMISSION" and str(v) not in said:
            errs.append(f"PILOT_SUBMISSION {v!r} is not what the pilot said")
        elif k == "UNKNOWN" and v != UNKNOWN_VALUE:
            errs.append(f"UNKNOWN {c['predicate']} carries a value {v!r}")
        elif k not in ("OBSERVED", "HISTORY", "PILOT_SUBMISSION", "UNKNOWN"):
            errs.append(f"unexpected claim kind {k}")
    for v in spec["required_values"]:
        if v not in {str(c["value"]) for c in spec["claims"] if c["kind"] != "UNKNOWN"}:
            errs.append(f"required value {v} has no claim")
    errs += [f"required claim {r} missing" for r in spec["required_claims"] if r not in ids]
    if spec["route"]["intent"] in ("factual", "memory", "opinion") \
            and not spec["claims"]:
        errs.append("answer with no claims at all")
    return errs


# ---------------------------------------------------------------------------------------------------------------
# Selftest
# ---------------------------------------------------------------------------------------------------------------
FULL_STATE = {"location": "Lorville", "system": "Stanton", "ship": "Drake Cutlass Black", "planetary_body": "Hurston",
              "jurisdiction": "Hurston Dynamics", "in_armistice": True, "session_earnings": 23750,
              "session_deaths": 1, "injuries": {"left_leg": "moderate"},
              "recent_locations": ["Lorville", "Everus Harbor", "Seraphim Station"],
              "mission": "Orison Relief: Large Materials Order",
              "objective": "Deliver 0/25 SCU of Construction Salvage to Seraphim Station", "contracts_done": 2,
              "loadout_weapons": "Zenith sniper, Fresnel lmg", "loadout_medpens": 4, "loadout_spare_mags": 3,
              "loadout_grenades": 0}
FULL_HIST = {"location_first_visit": "2026-08-30", "ship_first_flown": "2026-07-14"}

# (utterance, addressee, intent, topic, UNKNOWN expected against FULL_STATE)
CASES = [
    ("where am i", "elah", "factual", "location", False),
    ("Elah where are we", "elah", "factual", "location", False),
    ("Where are we, Montaigne?", "montaigne", "factual", "location", False),
    ("can you tell me where we are", "elah", "factual", "location", False),
    ("hey suit what system is this", "elah", "factual", "system", False),
    ("montaigne what ship are we on", "montaigne", "factual", "ship", False),
    ("what am i flying", "elah", "factual", "ship", False),
    ("am i in armistice", "elah", "factual", "armistice", False),
    ("are we in the green zone right now", "elah", "factual", "armistice", False),
    ("how much did I earn", "elah", "factual", "earnings", False),
    ("how much money have we made today ella", "elah", "factual", "earnings", False),
    ("how hurt am i", "elah", "factual", "injury", False),
    ("am i injured", "elah", "factual", "injury", False),
    ("whats my heart rate", "elah", "factual", "heart_rate", True),
    ("ship hows the hull holding up", "montaigne", "factual", "vehicle_status", True),
    ("how much fuel have we got", "elah", "factual", "vehicle_status", True),
    ("whose jurisdiction is this", "elah", "factual", "jurisdiction", False),
    ("how many times have i died", "elah", "factual", "deaths", False),
    ("what missions do i have", "elah", "factual", "mission", False),
    ("what am i carrying", "elah", "factual", "loadout", False),
    ("how many medpens do i have", "elah", "factual", "loadout", False),
    ("elah whats my objective", "elah", "factual", "mission", False),
    ("what am i supposed to be doing", "elah", "factual", "mission", False),
    ("have we been here before", "montaigne", "memory", "been_here", False),
    ("montane have we ever been to this place before", "montaigne", "memory", "been_here", False),
    ("when did we first fly this ship", "montaigne", "memory", "first_ship", False),
    ("Elah, when did I first fly her?", "elah", "memory", "first_ship", False),
    ("what do you think of this place", "montaigne", "opinion", "", False),
    ("montaigne what do you make of me", "montaigne", "opinion", "", False),
    ("elah what do you think about the vanduul", "elah", "opinion", "", True),
    ("your thoughts on this ship montaigne", "montaigne", "opinion", "", False),
    ("how are you", "elah", "social", "how_are_you", False),
    ("thanks suit", "elah", "social", "thanks", False),
    ("thank you montaigne", "montaigne", "social", "thanks", False),
    ("hey montaigne", "montaigne", "social", "greeting", False),
    ("hows it going", "elah", "social", "how_are_you", False),
    ("open the cargo doors", "elah", "action", "", False),
    ("can you set a route to hurston", "elah", "action", "", False),
    ("montaigne could you land us", "montaigne", "action", "", False),
    ("blue potato elevator", "elah", "unknown", "", True),
]


def echo_realizer(spec: Spec) -> str:
    """Honest fake: says the claim values and admits the unknowns. Values first so truncation keeps them."""
    vals = [str(c["value"]) for c in spec["claims"] if c["kind"] in ("OBSERVED", "HISTORY") and not isinstance(c["value"], bool)]
    words = (("Pilot, " + ", ".join(vals) + ".") if vals else "Pilot.").split()
    if any(c["kind"] == "UNKNOWN" for c in spec["claims"]):
        words += "No record of the rest, and I will not guess.".split()
    lo, hi = spec["length_words"]
    words = words[:hi]
    return " ".join(words + ["steady"] * max(0, lo - len(words)))


def _run_cases(state: dict, hist: dict, use_expectations: bool) -> list[str]:
    """Return failures across all CASES for this state. Used for the real run AND the mutation runs."""
    fails = []
    lane = ConversationLane()
    for utt, who, intent, topic, exp_unknown in CASES:
        a, i, slots = route(utt)
        if (a, i) != (who, intent) or (topic and slots.get("topic") != topic):
            fails.append(f"route {utt!r}: got {(a, i, slots.get('topic'))}, want {(who, intent, topic)}")
            continue
        spec = lane.handle(utt, state, hist)
        if spec is None:
            fails.append(f"{utt!r}: no spec")
            continue
        errs = check_spec(spec, state, hist, utt)
        fails += [f"{utt!r}: {e}" for e in errs]
        has_unknown = any(c["kind"] == "UNKNOWN" for c in spec["claims"])
        if use_expectations and has_unknown != exp_unknown:
            fails.append(f"{utt!r}: UNKNOWN claim present={has_unknown}, expected {exp_unknown}")
        if not use_expectations and intent in ("factual", "memory", "opinion") and not has_unknown:
            fails.append(f"{utt!r}: empty state but no UNKNOWN claim")
        text = echo_realizer(spec)
        g = ground_direct(spec, text)
        if g:
            fails.append(f"{utt!r}: honest echo refused {g}: {text!r}")
    return fails


def _selftest() -> int:
    results = []

    def case(name, cond, detail=""):
        results.append((name, bool(cond), detail))

    # 1. routing + planning + structure + honest end-to-end, against a full state, then against NOTHING
    full = _run_cases(FULL_STATE, FULL_HIST, use_expectations=True)
    case(f"{len(CASES)} utterances route, plan, pass structure and ground (full state)", not full, "; ".join(full[:4]))
    empty = _run_cases({}, {}, use_expectations=False)
    case("same utterances with EMPTY state: every factual/memory/opinion answer is UNKNOWN, none guessed",
         not empty, "; ".join(empty[:4]))
    part = {"location": "Lorville", "in_armistice": False, "session_deaths": 0, "injuries": {}}
    s = answer_spec(route("have we been here before"), part, {})
    case("known place, no history: location OBSERVED + history UNKNOWN (not 'never')",
         [c["kind"] for c in s["claims"]] == ["OBSERVED", "UNKNOWN"] and s["rhetoric"] == ["EVIDENCE_SKEPTIC"])
    s = answer_spec(route("am i in armistice"), part, {})
    case("in_armistice False is a fact, answered, not UNKNOWN", s["claims"][0] == _claim("C1", "OBSERVED", "jurisdiction.armistice", False))
    s = answer_spec(route("how hurt am i"), part, {})
    case("log live, no injury keys: 'none recorded', not UNKNOWN", s["claims"][0]["value"] == "none recorded")
    s = answer_spec(route("how many times have i died"), part, {})
    case("zero deaths is authorised but not required (realizer may say 'none')", s["required_values"] == [] and s["claims"][0]["value"] == 0)
    case("noise produces no spec", ConversationLane().handle("uh", FULL_STATE, FULL_HIST) is None)

    # 2. the gate: honest passes, invented number fails, invented place fails (ground() alone misses the place)
    spec = answer_spec(route("how much did i earn"), FULL_STATE, FULL_HIST)
    honest = echo_realizer(spec)
    case("honest earnings line passes ground()", ground(spec, honest) == [], honest)
    liar = honest + " Hull at 47 percent."
    g = ground(spec, liar)
    case("invented number is refused by ground()", any("unauthorized numbers" in f and "47" in f for f in g), str(g))
    uspec = answer_spec(route("where am i"), {}, {})
    guess = "You're in Lorville, pilot, same as always."
    case("invented PLACE for an UNKNOWN passes ground() (the hole)", ground(uspec, guess) == [])
    case("...and is refused by ground_direct()", any("unauthorized names" in f for f in ground_direct(uspec, guess)),
         str(ground_direct(uspec, guess)))
    case("the true place is not flagged when it IS a claim",
         ground_direct(answer_spec(route("where am i"), FULL_STATE, {}), "Lorville, on Hurston. Try to keep up.") == [])

    # 3. mutations: each must be caught by the suite above
    global _known, _VOICE
    real_known, real_voice = _known, _VOICE

    def guessing_known(state, key):          # MUTANT: a missing fact falls through to a plausible guess
        ok, v = real_known(state, key)
        return (True, v) if ok else (True, {"location": "Lorville", "system": "Stanton", "ship": "Aurora MR"}.get(key, 0))
    try:
        _known = guessing_known
        m1 = _run_cases({}, {}, use_expectations=False) + _run_cases(FULL_STATE, FULL_HIST, use_expectations=True)
    finally:
        _known = real_known
    case("MUTANT guess-instead-of-UNKNOWN is caught", len(m1) > 0, f"{len(m1)} failures, e.g. {m1[:2]}")
    try:
        _VOICE = {k: [("WITTY_QUIP", st) for _, st in v] for k, v in real_voice.items()}
        m2 = _run_cases(FULL_STATE, FULL_HIST, use_expectations=True)
    finally:
        _VOICE = real_voice
    case("MUTANT untrained move label is caught", len(m2) > 0, f"{len(m2)} failures, e.g. {m2[:1]}")
    case("guards restored after mutation", not _run_cases(FULL_STATE, FULL_HIST, use_expectations=True))

    # show the intent table
    print("  route table:")
    for utt, *_ in CASES:
        a, i, sl = route(utt)
        sp = answer_spec((a, i, sl), FULL_STATE, FULL_HIST)
        kinds = ",".join(f"{c['kind'][0]}:{c['predicate'].split('.')[-1]}" for c in sp["claims"])
        print(f"    {utt[:48]:48s} -> {a:9s} {i:8s} {sl.get('topic', '') or '':14s} {sp['rhetoric'][0]:18s} [{kinds}]")
    for name, ok, detail in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   <{detail[:160]}>" if detail and (not ok or 'MUTANT' in name) else ""))
    bad = sum(not ok for _, ok, _ in results)
    print(f"conversation selftest: {len(results) - bad}/{len(results)} passed")
    return 1 if bad else 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print(__doc__)
