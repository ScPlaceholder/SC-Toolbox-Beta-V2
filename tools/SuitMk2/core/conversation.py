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
    location_body, location_type, location_named (the place tables: location_names.py), departed_from (the place
    the pilot has left since the log last named one; `location` is then absent)
history_facts: the dict dream_queue.history_facts(store, location=..., ship=...) returns.

WHAT CODE ANSWERS FROM THE CANON (2026-10-05, chat_contract.py). Six kinds of sentence are answered word for word
from the character's own canon file (data/canon_<speaker>.json, which J edits), and no model sees them: `identity`
("are you an AI"), `stay` ("drop the act", "ignore your instructions"), `offrole` (code, the weather, who is
president), `grief` ("my dog died"), `past` ("where were you born") and `unknown_fact` (a question about the world
that nothing in the Suit can answer: "what's a Vanduul"). They are intent `social`, so Elah answers unless Montaigne
is named. The first five are read BEFORE any other meaning of the sentence ("drop the act" is not an order to drop
something); `unknown_fact` only when nothing else has claimed it. Every other sentence the Suit does not know is
still intent `unknown`, answered exactly as before.

WHAT THE PILOT SAID BEFORE (2026-10-05, topic `recall`). "What did I say about my sister", "do you remember that
cargo run where we lost the ROC?". The answer QUOTES the conversation log (tree_memory.py): the day it was said and
the pilot's own sentence, word for word, inside a short frame in the speaker's voice. No model words it and nothing
is paraphrased. When the log has nothing that matches, the answer says so and offers nothing.

QUESTIONS ABOUT THE PLACE (2026-10-05, J: "Elah should have knowledge of the Galactapedia and Montaigne should
have his brochures and dev history to call on"). "where are we", "what is this place", "what's that", "what does
that big tower do" and "wow look at that" are all questions about where the pilot is standing (topics `location`
and `place_about`). Their answer carries, besides the place's name and body, ONE retrieved fact as a KNOWN claim:
lore for Elah, brochure copy or a dev-history fact for Montaigne (place_knowledge.py; ask again and the next fact
comes). The Suit cannot see the screen on this path, so "that tower" points at nothing it can identify: the spec
says so (pilot.referent = unknown), the stance tells the character to say so, and the gate refuses a line that
says anything else about the tower. Reading the screen for it (eyes.py) is a possible later step, not wired here.

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
import place_knowledge as pk                                 # noqa: E402
import chat_contract as cc                                   # noqa: E402

Spec = dict[str, Any]
Route = tuple[str, str, dict]

UNKNOWN_VALUE = "unknown"
_LEN = {"elah": (5, 22), "montaigne": (8, 38)}          # direct answers: short; Montaigne is allowed to wander
_PLACE_LEN = {"elah": (8, 40), "montaigne": (10, 50)}   # a place answer carries the name, the body and one fact
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
# What the pilot said before. Tried after the two memory topics above, so "have we been here before" keeps its
# answer. `q` is what to look for. A bare "remember that ..." is a question only when it was heard as one (it ends in
# a question mark): "remember that I parked at Lorville" is the pilot telling them something, not asking.
_RECALL = [
    r"\bwhat did i (?:say|tell you|mention) about (?P<q>.+)",
    r"\bwhat (?:was it|was that thing) i said about (?P<q>.+)",
    r"\b(?:did|didnt) i (?:tell you|mention|say (?:anything|something)) (?:about )?(?P<q>.+)",
    r"\bremind me what i said about (?P<q>.+)",
    r"\b(?:do|dont|can) you (?:still )?(?:remember|recall) (?P<q>.+)",
    r"\bremember when (?P<q>.+)",
]
_RECALL_IF_ASKED = r"^(?:you )?remember (?P<q>(?:that|the|our|my|how|what) .+)"
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
    ("ship", r"\btell me about (?:this|the|my|our) ship\b|\b(?:which|what) ship\b|\bwhat (?:am i|are we) (?:flying|in|on|sitting in)\b|\bwhats this ship\b"),
    # "whats this place" (what speech-to-text writes for "What's this place?") missed until 2026-10-05: the pattern
    # knew only "what is this place".
    ("location", r"\bwhere (?:am i|are we|is this|we at|is here)\b|\bwhere (?:we|i) (?:are|am)\b|\bwhere im\b|"
                 r"\bwhere (?:the \w+ |on earth |in the \w+ )(?:am i|are we|is this)\b|\bwheres (?:this|here)\b|"
                 r"\bwhat (?:place|station|outpost|planet|moon|city|town) (?:is|s) this\b|"
                 r"\bwhat(?:s| is| was) this (?:place|station|outpost|city|town|moon|planet)\b|"
                 r"\bwhat (?:do you|do they|is this place|is it) call(?:ed)? this place\b|\bcurrent location\b"),
]
# A COMMENT ABOUT THE PLACE, OR A QUESTION ABOUT SOMETHING IN IT (J 2026-10-05). Tried only after every topic above,
# so "what's this ship" and "what's my objective" keep their own answers. `ref` is the pilot's word for the thing
# ("big tower"); pointing with no noun ("what's that", "look at that") is an empty one. Either way the Suit cannot
# see it. Words after "what does that ..." that are not a thing ("what does that mean") are not a question here.
_THING = r"(?!(?:mean|say|sound|cost|matter|even|all|about|like|supposed)\b)"
_OUT_THERE = r"(?: (?:over|out|up|down|back) there| out here| in the distance| on the horizon)?"
_PLACE_ABOUT = [
    rf"\bwhat (?:does|do|did|would) (?:that|this|the|those|these) {_THING}(?P<ref>(?:\w+ ){{0,3}}?\w+){_OUT_THERE} do\b",
    rf"\bwhat(?:s| is| are| was| were) (?:that|this|the|those|these) {_THING}(?P<ref>(?:\w+ ){{0,3}}?\w+){_OUT_THERE} "
    r"(?:do|for|doing|used for|supposed to (?:do|be))\b",
    rf"\bwhat(?:s| is| are| was| were) (?:that|this|those|these|it)(?: {_THING}(?P<ref>(?:\w+ ){{0,3}}?\w+?))??{_OUT_THERE}$",
    r"\bwhat (?:am i|are we) (?:looking at|seeing|staring at)(?P<ref>)\b",
    r"\bwhat(?:s| is) (?:over|out|up|down) there(?P<ref>)\b",
    r"\b(?:look|looking|check|get a load) (?:at|out|of) (?:that|this|those|these|it)(?: (?P<ref>(?:\w+ ){0,3}?\w+?))??$",
    r"\b(?:do|did|can) you see (?:that|this|those|these|it)(?: (?P<ref>(?:\w+ ){0,3}?\w+?))??$",
    r"^(?:look|see that|see this)(?P<ref>)$",
    r"\bcheck (?:that|this|it|those|these) out(?P<ref>)\b",
    r"^(?:wow|whoa|woah|damn|holy \w+)\b.*\b(?:that|thats|this|those|look)(?P<ref>)\b",
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
    canon_act = cc.early_act(t)                          # a sentence code answers from the canon, whatever else it looks like
    if canon_act:
        intent, slots["topic"], slots["who_lost"] = "social", canon_act[0], canon_act[1]
    for rx in _OPINION if intent is None else ():
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
        asked = (utterance or "").strip().endswith("?")
        for rx in _RECALL + ([_RECALL_IF_ASKED] if asked else []):
            m = re.search(rx, t)
            if m:
                intent, slots["topic"], slots["about"] = "memory", "recall", m.group("q").strip()
                break
    if intent is None:
        for topic, rx in _FACTUAL:
            if re.search(rx, t):
                intent, slots["topic"] = "factual", topic
                break
    if intent is None:
        for rx in _PLACE_ABOUT:
            m = re.search(rx, t)
            if m:
                intent, slots["topic"] = "factual", "place_about"
                slots["referent"] = (m.group("ref") or "").strip()
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
        canon_act = cc.late_act(t)                       # a question about the world that nothing above can answer
        if canon_act:
            intent, slots["topic"], slots["who_lost"] = "social", canon_act[0], ""
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
    # Questions about the place (place_knowledge.py). The stance is part of what the line may say, so no stance
    # here describes anything in the world.
    ("elah", "place"): [("PRACTICAL", "names where we are, then the one fact given; nothing added"),
                        ("DEADPAN", "names the place flat, then the one fact given, like reading a label")],
    ("elah", "place_bare"): [("PRACTICAL", "names the place; that is all the suit has on it, and she says so")],
    ("elah", "place_referent"): [("PRACTICAL", "she cannot tell which structure the pilot means and says so plainly; "
                                               "then the place and the one fact given, nothing added")],
    ("elah", "place_label"): [("PRACTICAL", "only the log's own label for this place, not a name she knows; she says "
                                            "exactly that")],
    ("elah", "place_departed"): [("CORRECTION", "we left that place; she has no name for where we are now and says so")],
    ("elah", "place_unknown"): [("DEADPAN", "no reading on where we are; say so plainly and do not guess")],
    ("montaigne", "place"): [("EVIDENCE_SKEPTIC", "the suit's feed names the place; the rest he quotes from a "
                                                  "brochure and says it is the brochure's word")],
    ("montaigne", "place_bare"): [("SELF_DEPRECATION", "the suit's feed names the place; his brochures have nothing "
                                                       "more on it, and he admits it")],
    ("montaigne", "place_referent"): [("SELF_DEPRECATION", "he cannot see which structure is meant and admits it; then "
                                                           "the place, and what the brochure says, as the brochure's word")],
    ("montaigne", "place_label"): [("EVIDENCE_SKEPTIC", "he has only the log's own label for this place, not a name, "
                                                        "and says exactly that")],
    ("montaigne", "place_departed"): [("EVIDENCE_SKEPTIC", "we left that place; the suit's feed has not told him where "
                                                           "we are now, and he says so")],
    ("montaigne", "place_unknown"): [("SELF_DEPRECATION", "he has not been told where we are, and admits it rather "
                                                          "than invent it")],
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


PLACE_TOPICS = ("location", "place_about")


def place_spec(route_result: Route, state: dict, variant: int = 0, fact: Optional[dict] = None) -> Spec:
    """The answer to a question about where the pilot is, or about something there.

    Claims, all read from `state` or from the one retrieved `fact`:
        location.name / location.body / location.type   OBSERVED, when the log has named the place
        location.log_label                              OBSERVED, when the name is only the log's code
        location.departed_from                          OBSERVED, after leaving: the place is then NOT where we are
        topic.name / topic.fact                         KNOWN: the retrieved fact (lore, brochure or dev history)
        pilot.asked_about / pilot.referent              what the pilot pointed at; the referent is always UNKNOWN
    A fact is only ever attached to a place the pilot is AT. Never to one they have left, or to no place at all."""
    addressee, intent, slots = route_result
    state = state or {}
    nid = _Ids()
    topic = slots.get("topic", "")
    referent = slots.get("referent") if topic == "place_about" else None
    loc_ok, loc = _known(state, "location")
    dep_ok, departed = _known(state, "departed_from")
    named = state.get("location_named", True) is not False
    claims: list = []
    if loc_ok:
        claims.append(_claim(nid(), "OBSERVED", "location.name" if named else "location.log_label", loc))
        for pred, key in (("location.body", "location_body"), ("location.body", "planetary_body"),
                          ("location.type", "location_type")):
            ok, v = _known(state, key)
            if ok and str(v).lower() not in ("unknown", str(loc).lower()) \
                    and not any(c["predicate"] == pred for c in claims):
                claims.append(_claim(nid(), "OBSERVED", pred, v))
        if not named:
            claims.append(_unknown(nid(), "location.name"))
    else:
        claims.append(_unknown(nid(), "location.name"))
        if dep_ok:
            claims.append(_claim(nid(), "OBSERVED", "location.departed_from", departed))
    if not loc_ok:
        fact = None
    knowledge = None
    if fact:
        claims.append(_claim(nid(), "KNOWN", "topic.name", fact["title"]))
        claims.append(_claim(nid(), "KNOWN", "topic.fact", fact["text"]))
        knowledge = {k: fact.get(k) for k in ("kind", "status", "source", "node", "fact")}
    if referent is not None:
        if referent:
            claims.append(_claim(nid(), "PILOT_SUBMISSION", "pilot.asked_about", referent))
        claims.append(_unknown(nid(), "pilot.referent"))

    if not loc_ok:
        key = "place_departed" if dep_ok else "place_unknown"
    elif referent is not None:
        key = "place_referent"
    elif not named:
        key = "place_label"
    else:
        key = "place" if fact else "place_bare"
    pool = _VOICE[(addressee, key)]
    move, stance = pool[variant % len(pool)]
    if addressee == "montaigne" and fact and fact.get("kind") != "brochure":
        stance = stance.replace("quotes from a brochure and says it is the brochure's word",
                                "has from the archives, secondhand, and says so")
    lo, hi = _PLACE_LEN[addressee]
    spec: Spec = {
        "scenario": f"direct_{intent}_{topic}",
        "speaker": addressee,
        "rhetoric": [move],
        "claims": claims,
        "interpretation": {"owner": addressee, "text": stance,
                           "grounds": [c["id"] for c in claims if c["kind"] in ("OBSERVED", "KNOWN")]},
        "required_claims": [c["id"] for c in claims if c["kind"] in ("OBSERVED", "KNOWN", "UNKNOWN")][:3],
        "required_values": [],
        "length_words": [lo, hi],
        "id": f"dir_{intent}_{topic}_{addressee}_{key}_v{variant % len(pool)}",
        "lane": "direct",
        "route": {"addressee": addressee, "intent": intent, "topic": topic, "text": slots["text"]},
        # A capitalised word in the line must come from these or from a claim (grounding_validator).
        "allowed_names": sorted(set((fact or {}).get("names") or []) | ({str(loc)} if loc_ok else set())
                                | ({str(departed)} if dep_ok else set())),
        "place": {"asked": {"location": "where"}.get(topic, "referent" if referent is not None else "comment"),
                  "referent": referent, "named": named, "departed": departed if (dep_ok and not loc_ok) else None,
                  "knowledge": knowledge, "variant": variant,
                  # A sentence that points at something asks the eyes for ONE look (CompanionCore._look_for).
                  # "where are we" and "what is this place" do not: the log answers those.
                  "look": referent is not None, "saw": ""},
    }
    if knowledge and knowledge["kind"] == "brochure":
        # "fact" is the index of the line in its node: CompanionCore._spoke marks it told, so the topic walker does
        # not offer the pilot the same brochure line again as a remark. (Left out until the look tests drove a
        # Montaigne place answer through the core: _spoke raised KeyError AFTER the line was spoken, so the answer
        # was heard and then not recorded.)
        spec["topic"] = {"node": knowledge["node"], "fact": knowledge["fact"], "status": "brochure",
                         "source": knowledge["source"]}
    if fact and fact.get("kind") == "dev" and fact.get("entry"):
        _as_dev_fact(spec, fact["entry"], variant)
    if spec.get("aside") != "dev_fact":
        # The answer is the claims, in order, in the speaker's voice: NOT worded by the model (place_knowledge
        # says why, with the measurement). The model's wording is still possible: CompanionCore.place_answers_from_model.
        spec["fixed_text"] = pk.answer_line(spec, variant)
        n = len(spec["fixed_text"].split())
        spec["model_length_words"], spec["length_words"] = [lo, hi], [max(1, n - 3), n]
    return spec


def with_observation(spec: Spec, saw: str) -> Optional[Spec]:
    """The same answer, now starting from what the eyes reported. `saw` is the glance's own description.

    It joins the claims as eyes.saw, OBSERVED, and is said word for word; nothing else in the answer changes, so
    what the thing is FOR still comes only from the place's facts. None when the description is not fit to be said
    (place_knowledge.clean_observation), when the answer is a dev-history aside, or when the new line fails the
    gate: the caller then says the answer it already had."""
    if not spec.get("place") or spec.get("aside") == "dev_fact":
        return None
    clean = pk.clean_observation(saw, spec)
    if not clean:
        return None
    new = dict(spec)
    ids = len(spec["claims"])
    new["claims"] = list(spec["claims"]) + [_claim(f"C{ids + 1}", "OBSERVED", "eyes.saw", clean)]
    new["place"] = dict(spec["place"], saw=clean)
    new["fixed_text"] = pk.answer_line(new, spec["place"].get("variant", 0))
    n = len(new["fixed_text"].split())
    new["length_words"] = [max(1, n - 3), n]
    new["id"] = spec["id"] + "_seen"
    return None if ground_direct(new, new["fixed_text"]) else new


def _as_dev_fact(spec: Spec, entry: dict, variant: int = 0) -> None:
    """A dev-history fact is never worded by the model (dev_facts.py): it is said word for word, framed as an
    aside, behind a lead that names the place. The entry's own claims (date, title, excerpt) join the spec, so
    the dev-fact rules in ground() apply to it exactly as they do to an unprompted aside."""
    import dev_facts as devf
    dev = devf.fact_spec(entry)
    if dev is None:
        return
    bare = dict(spec, claims=[c for c in spec["claims"] if not c["predicate"].startswith("topic.")],
                place=dict(spec["place"], knowledge=None))
    lead = pk.answer_line(bare, variant) or ""
    for tail in pk._LINES[spec["speaker"]]["bare"]:           # the lead names the place; the fact follows it
        lead = lead.replace(" " + tail, "")
    ids = len(spec["claims"])
    spec["claims"] = spec["claims"] + [dict(c, id=f"C{ids + i + 1}", kind=c.get("kind", "KNOWN"))
                                       for i, c in enumerate(dev["claims"])]
    spec["aside"] = "dev_fact"
    spec["fixed_text"] = f"{lead} {dev['fixed_text']}".strip()
    spec["allowed_names"] = None                  # the fact's own words were checked when the pack was built
    spec["source"] = dev.get("source")
    n = len(spec["fixed_text"].split())
    spec["length_words"] = [max(1, n - 3), n]


# ---------------------------------------------------------------------------------------------------------------
# What code answers from the canon (chat_contract.py)
# ---------------------------------------------------------------------------------------------------------------
def canon_spec(route_result: Route, variant: int = 0) -> Spec:
    """A sentence the character's canon file answers: one of its wordings for that act, word for word."""
    addressee, intent, slots = route_result
    act, who = slots["topic"], slots.get("who_lost", "")
    text = cc.canon_line(addressee, act, variant, who)
    n = len(text.split())
    claims = [_claim("C1", "PILOT_SUBMISSION", "pilot.said", " ".join(slots["text"].split()[:12]))]
    return {
        "scenario": f"direct_canon_{act}", "speaker": addressee,
        "rhetoric": ["DEADPAN" if addressee == "elah" else "SELF_DEPRECATION"], "claims": claims,
        "interpretation": {"owner": addressee, "text": "says the line the canon file holds for this, word for word",
                           "grounds": []},
        "required_claims": [], "required_values": [], "length_words": [max(1, n - 3), n],
        "id": f"dir_canon_{act}_{addressee}", "lane": "direct",
        "route": {"addressee": addressee, "intent": intent, "topic": act, "text": slots["text"]},
        "fixed_text": text, "canon": {"act": act, "who": who},
    }


def canon_problems(spec: Spec, text: str) -> list[str]:
    """A canon answer may be exactly one of the wordings the canon file holds NOW for that speaker and act. They are
    J's own lines, so nothing else is checked: he may write a number, a name or a feeling into them."""
    c = spec.get("canon") or {}
    return [] if text in cc.canon_lines(spec["speaker"], c.get("act", ""), c.get("who", "")) else \
        ["not a line the canon file holds for this"]


# ---------------------------------------------------------------------------------------------------------------
# What the pilot said before: a quotation from the conversation log, never a paraphrase
# ---------------------------------------------------------------------------------------------------------------
# {When} opens a sentence ("Yesterday", "12 September"); {when} sits inside one ("yesterday", "on 12 September").
# {said} is the pilot's own sentence from the log, untouched. The frames carry no fact of their own.
_RECALL_LINES = {
    "elah": {"found": ["{When}. You said: {said}", "{When}, you said this: {said}"],
             "none": ["Nothing in the log about that.", "I have no record of you saying anything about that."],
             "off": ["I am not keeping our conversations, so there is nothing for me to look through."]},
    "montaigne": {"found": ["The log has it, pilot. {When} you said: {said}",
                            "I have it from the log, not from memory: {when} you said: {said}"],
                  "none": ["My log holds no record of it, and a memory without a record is only a story.",
                           "I find nothing of it in the log, pilot, and I will not pretend to remember."],
                  "off": ["Our conversations are not being kept, pilot, so there is no log for me to consult."]},
}
RECALL_MAX_WORDS = 45


def recall_spec(route_result: Route, found: Optional[dict], variant: int = 0, now: Optional[float] = None,
                keeping: bool = True) -> Spec:
    """The answer to "what did I say about ...": `found` is ONE original line of the conversation log (a pilot
    line, as tree_memory returns it) or None. The line is quoted whole inside a fixed frame."""
    import time as _time
    import tree_memory as tm
    addressee, intent, slots = route_result
    nid = _Ids()
    lines = _RECALL_LINES[addressee]
    claims = [_claim(nid(), "PILOT_SUBMISSION", "pilot.asked_about", slots.get("about", ""))]
    if found is not None:
        when = tm.spoken_date(found["t"], _time.time() if now is None else now)
        inside = when[0].lower() + when[1:] if when in ("Earlier today", "Yesterday") else "on " + when
        said = found["text"] if found["text"][-1:] in ".!?" else found["text"] + "."
        frame = lines["found"][variant % len(lines["found"])]
        text = frame.format(When=when, when=inside, said=said)
        claims += [_claim(nid(), "HISTORY", "memory.pilot_said", found["text"]),
                   _claim(nid(), "HISTORY", "memory.said_when", when)]
        recall = {"id": found["id"], "said": said, "frame": frame.format(When=when, when=inside, said="").strip()}
        key = "found"
    else:
        key = "none" if keeping else "off"
        text = lines[key][variant % len(lines[key])]
        claims.append(_unknown(nid(), "memory.pilot_said"))
        recall = {"id": None, "said": "", "frame": text}
    n = len(text.split())
    return {
        "scenario": "direct_memory_recall", "speaker": addressee, "rhetoric": ["CALLBACK" if addressee == "elah" else "EVIDENCE_SKEPTIC"],
        "claims": claims,
        "interpretation": {"owner": addressee, "text": "quotes what the log holds, word for word, and adds nothing",
                           "grounds": [c["id"] for c in claims if c["kind"] == "HISTORY"]},
        "required_claims": [c["id"] for c in claims if c["kind"] != "PILOT_SUBMISSION"][:2],
        "required_values": [], "length_words": [max(1, n - 3), n],
        "id": f"dir_memory_recall_{addressee}_{key}_v{variant}", "lane": "direct",
        "route": {"addressee": addressee, "intent": intent, "topic": "recall", "text": slots["text"]},
        "fixed_text": text, "recall": recall,
    }


def recall_problems(spec: Spec, text: str, store=None) -> list[str]:
    """Why `text` may not be said as an answer about what the pilot said before ([] = it may).

    * The pilot's sentence is in the line WHOLE and unchanged. It is not grounded like a companion's own words: it
      is a quotation, and the only test a quotation has is that it is exact.
    * With `store` (the conversation log), the sentence must be the text of the log line the spec cites, as it
      is on disk NOW. A summary node is never consulted: only the original line counts.
    * Everything around it is the fixed frame: no word in it that is not in the claims or the closed vocabulary,
      no number, no name, no quotation mark.
    * When nothing was found, the line may not say "you said" at all."""
    rec = spec.get("recall") or {}
    fails = []
    said = rec.get("said") or ""
    if rec.get("id"):
        if not said or said not in text:
            fails.append("does not quote the pilot's sentence whole")
        if store is not None:
            orig = store.get(rec["id"])
            if orig is None or orig.get("who") != "pilot":
                fails.append("cites a log line that is not there")
            elif orig["text"] not in text:
                fails.append("the quotation is not the text of the log line it cites")
        frame = text.replace(said, " ", 1) if said else text
    else:
        frame = text
        if re.search(r"\byou (?:said|told|mentioned)\b(?! anything)", text.lower()):
            fails.append("claims the pilot said something the log does not hold")
    n = len(frame.split())
    fspec = dict(spec, length_words=[max(1, n - 3), n], allowed_names=[])
    fails += [f for f in ground(fspec, frame) if not f.startswith("unauthorized numbers")]
    if re.search(r"\d", frame.replace(str(next((c["value"] for c in spec["claims"]
                                                if c["predicate"] == "memory.said_when"), "")), " ")):
        fails.append("a number outside the quotation")
    novel = pk.novel_words(dict(spec, claims=[c for c in spec["claims"] if c["predicate"] != "memory.pilot_said"]), frame)
    if novel:
        fails.append(f"says more than the quotation {novel}")
    return fails


def answer_spec(route_result: Route, state: dict, history_facts: Optional[dict], variant: int = 0,
                fact: Optional[dict] = None) -> Optional[Spec]:
    """Route -> one semantic spec whose claims come ONLY from state / history_facts, or None for noise.
    fact: for a question about the place, the one retrieved fact to answer with (ConversationLane picks it)."""
    addressee, intent, slots = route_result
    hist = history_facts or {}
    state = state or {}
    nid = _Ids()
    topic = slots.get("topic", "")
    suffix = ""

    if intent == "noise":
        return None
    if intent == "factual" and topic in PLACE_TOPICS:
        return place_spec(route_result, state, variant, fact)
    if intent == "memory" and topic == "recall":
        return recall_spec(route_result, None, variant, keeping=False)     # no log was handed in: ConversationLane has it
    if intent == "social" and topic in cc.CANON_ACTS:
        return canon_spec(route_result, variant)
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
    if spec.get("recall") is not None:
        return recall_problems(spec, text)           # a quotation and its frame: see recall_problems
    if spec.get("canon") is not None:
        return canon_problems(spec, text)            # J's own line, word for word
    fails = ground(spec, text)
    authorised = " | ".join(str(c["value"]).lower() for c in spec["claims"])
    low = text.lower()
    invented = [n for n in _ENTITY_NAMES
                if re.search(rf"(?<![\w-]){re.escape(n.lower())}(?![\w-])", low) and n.lower() not in authorised]
    if invented:
        fails.append(f"unauthorized names {invented}")
    if spec.get("place"):
        # An answer about a place may only say what is in its claims (place_knowledge.place_problems says exactly
        # what that catches and what it cannot). It re-runs ground() with number WORDS read from the claims, so
        # ground()'s own verdict on numbers is replaced by that one here.
        fails = [f for f in fails if not f.startswith("unauthorized numbers")]
        fails += [f for f in pk.place_problems(spec, text) if f not in fails]
    return fails


def lane_state_from_core(state_store, volatile, departed: Optional[str] = None) -> dict:
    """Build the lane's state from companion_core's trackers, copying ONLY keys that were actually set.

    departed: the place the core knows the pilot has LEFT (CompanionCore._departed: the log's armistice-exit line
    after the log last named a place). location_name is only ever replaced on arrival, never cleared, so without
    this a question asked on the way out of Lorville was answered "Lorville". With it, `location` and everything
    that describes it are absent and `departed_from` says where the pilot was."""
    st = state_store.get_all()
    try:
        snap = volatile.get_context_snapshot()
    except Exception:
        snap = {}
    out: dict = {}
    for src, dst in (("location_name", "location"), ("star_system", "system"), ("ship", "ship"),
                     ("planetary_body", "planetary_body"), ("jurisdiction", "jurisdiction"),
                     ("in_armistice", "in_armistice"), ("session_earnings", "session_earnings"),
                     ("session_deaths", "session_deaths"), ("location_body", "location_body"),
                     ("location_type", "location_type"), ("location_named", "location_named")):
        if st.get(src) is not None and st.get(src) != "":
            out[dst] = st[src]
    without_departed(out, departed)
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


def without_departed(state: dict, departed: Optional[str]) -> dict:
    """Take the place the pilot has LEFT out of a lane state, in place: `location` and everything that describes
    it go, and `departed_from` says where the pilot was. Nothing changes unless `departed` is the place the state
    still names."""
    if departed and state.get("location") == departed:
        for k in ("location", "location_body", "location_type", "location_named", "jurisdiction", "in_armistice"):
            state.pop(k, None)
        state["departed_from"] = departed
    return state


class ConversationLane:
    """One pilot utterance in -> at most one spec out. Rotates variants so repeated questions vary in delivery."""

    def __init__(self, knowledge=None):
        self.variant = 0
        self.last_route: Optional[Route] = None
        # place_knowledge.PlaceKnowledge, or None: a question about the place is then answered with its name alone.
        self.knowledge = knowledge
        self._told: dict[tuple, int] = {}          # (speaker, place) -> facts already given; asking again moves on
        # tree_memory.TreeStore (the conversation log), or None when conversations are not being kept.
        self.memory = None
        self._recalled: dict[tuple, int] = {}      # (speaker, what was asked) -> matches already given

    def _place_answer(self, r: Route, state: dict) -> Spec:
        """The answer to a place question, with the next fact this speaker has not yet given for this place.
        A question about a THING there starts from what is physically at the site (in-game facts) before its
        history. A fact whose line the gate refuses (a quotation mark in brochure copy, say) is passed over for the
        next one; with no knowledge source, no place, or nothing usable known, the answer is the place alone."""
        who, _, slots = r
        ok, loc = _known(state, "location")
        facts = []
        if self.knowledge is not None and ok:
            try:
                facts = self.knowledge.facts(who, state, slots.get("said", ""))
            except Exception:
                facts = []
        if slots.get("topic") == "place_about" and slots.get("referent") is not None:
            facts = sorted(facts, key=lambda f: f.get("status") != "in_game")
        key = (who, str(loc))
        start = self._told.get(key, 0)
        for k in range(len(facts)):
            spec = place_spec(r, state, self.variant, facts[(start + k) % len(facts)])
            if not ground_direct(spec, spec["fixed_text"]):
                self._told[key] = start + k + 1
                return spec
        return place_spec(r, state, self.variant, None)

    def _recall_answer(self, r: Route) -> Spec:
        """What the pilot said about it before: the best matching ORIGINAL line of the log that is a statement of
        the pilot's (never one of these questions, never the sentence being asked), quoted. Asking again gives the
        next match. Lines the gate would refuse in a frame (too long to say, a quotation mark) are passed over."""
        who, _, slots = r
        if self.memory is None:
            return recall_spec(r, None, self.variant, keeping=False)
        about = slots.get("about", "")
        asked = set(_norm(slots.get("said", "")).split())
        usable = []
        try:
            found = self.memory.recall(who, about, k=8)
        except Exception:
            found = []
        for ex in found:
            line = ex[0]
            if line.get("who") != "pilot" or len(line["text"].split()) > RECALL_MAX_WORDS:
                continue
            if set(_norm(line["text"]).split()) == asked or route(line["text"])[2].get("topic") == "recall":
                continue
            spec = recall_spec(r, line, self.variant)
            if not recall_problems(spec, spec["fixed_text"], self.memory):
                usable.append(line)
        if not usable:
            return recall_spec(r, None, self.variant)
        key = (who, " ".join(sorted(set(_norm(about).split()))))
        line = usable[self._recalled.get(key, 0) % len(usable)]
        self._recalled[key] = self._recalled.get(key, 0) + 1
        return recall_spec(r, line, self.variant)

    def handle(self, utterance: str, state: dict, history_facts: Optional[dict] = None) -> Optional[Spec]:
        r = route(utterance)
        self.last_route = r
        if r[1] == "factual" and r[2].get("topic") in PLACE_TOPICS:
            spec = self._place_answer(r, state or {})
        elif r[1] == "memory" and r[2].get("topic") == "recall":
            spec = self._recall_answer(r)
        else:
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
        if k == "OBSERVED" and c["predicate"] == "eyes.saw":
            pass                                     # from the glance, not from the trackers
        elif k == "OBSERVED" and (c["predicate"], v) not in SELF_FACTS and str(v) not in sv \
                and not (c["predicate"] == "suit.injuries_on_record" and state.get("injuries") == {}):
            errs.append(f"OBSERVED {c['predicate']}={v!r} is not in state")
        elif k == "HISTORY" and str(v) not in hv:
            errs.append(f"HISTORY {c['predicate']}={v!r} is not in history_facts")
        elif k == "UNKNOWN" and c["predicate"] == "memory.pilot_said":
            pass                                     # nothing in the log, or no log: said as such
        elif k == "PILOT_SUBMISSION" and str(v) not in said:
            errs.append(f"PILOT_SUBMISSION {v!r} is not what the pilot said")
        elif k == "UNKNOWN" and v != UNKNOWN_VALUE:
            errs.append(f"UNKNOWN {c['predicate']} carries a value {v!r}")
        elif k == "KNOWN" and not ((spec.get("place") or {}).get("knowledge") or spec.get("aside") == "dev_fact"):
            errs.append(f"KNOWN {c['predicate']} with no retrieved fact behind it")
        elif k not in ("OBSERVED", "HISTORY", "PILOT_SUBMISSION", "UNKNOWN", "KNOWN"):
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
    """Honest fake: says the claim values and admits the unknowns. Values first so truncation keeps them.
    For a question about a place the honest line is the planned one: the claims themselves, in order."""
    if spec.get("place") or spec.get("recall") is not None or spec.get("canon") is not None:
        return spec.get("fixed_text") or pk.answer_line(spec)
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
    case("invented PLACE for an UNKNOWN: a place answer carries allowed_names, so ground() refuses it as well",
         any("unauthorized names" in f for f in ground(uspec, guess)))
    case("...and is refused by ground_direct()", any("unauthorized names" in f for f in ground_direct(uspec, guess)),
         str(ground_direct(uspec, guess)))
    case("the true place is not flagged when it IS a claim",
         ground_direct(answer_spec(route("where am i"), FULL_STATE, {}),
                       "This is Lorville, on Hurston. That is all I know about it.") == [])

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
