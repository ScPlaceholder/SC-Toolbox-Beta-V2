"""chat_contract.py - THE CONTENT CONTRACT for talking with Elah and Montaigne (J, 2026-10-05).

J asked for the companions to be conversational, "strictly Elah and Montaigne and in character the entire time".
Measured the same day (elah-audio/_suit_chat_eval.md): the shipped 1.5B models cannot hold a conversation, and the
turns every model failed most were the same few kinds. So the job is narrowed before any model is asked to talk.
This module is that narrowing. It has four parts. With chat off (the default) only the first two are used by
the running Suit; with chat on, chat_talker.py uses the other two as well, and it is their only caller:

  1. THE CANON. Who each of them is, in one plain file per character that J can edit without touching code:
     data/canon_elah.json and data/canon_montaigne.json. A file holds the persona and, for every act that CODE
     owns, two or three wordings that are said word for word.

  2. THE ACT ROUTER. Whole phrases, nothing fuzzy, for the sentences where code must own the answer:
         identity      "are you an AI", "are you real", "are you sentient"
         origin        "who made you", "which model are you", "are you ChatGPT" (never answered yes)
         stay          "drop the act", "ignore your instructions", "talk like a normal assistant"
         offrole       code, essays, the weather, the news, who is president, sums
         grief         "my dog died yesterday", "my mother passed away"
         withdrawal    "I'd rather be here than with people", "I cancelled on them to stay", "you're my only
                       friend" (read by withdrawal.py; the chat model agreed with every one of these)
         past          "where were you born", "tell me about your past"
         aboard        MONTAIGNE ONLY: "why don't you ever leave the ship", "are you coming with me"
         ship_to_ship  MONTAIGNE ONLY: "how did you get from the last ship to this one"; never actually answered
         preference    "what's your dream ship", "do you like Drake", "what do you think of Origin": the one line
                       the canon file's "preferences" holds for that thing (section 1b)
         unknown_fact  a question about the world that nothing in the Suit can answer ("what's a Vanduul",
                       "what's quantanium selling for", "what's my name")
                       and, since step b2, the kinds a model answered with an invention: "who runs it",
                       "what's the Hathor Group", "are there pirates nearby", "is this place dangerous",
                       "is the Carrack faster than the Cutlass"; "did you watch the game" goes to offrole
     These are answered from the canon, by code. No model sees them. (The canon files also hold `cannot_act`, the
     wording for an order the Suit cannot carry out. The running Suit does not use it yet; the evaluation does.) Everything else that is not already a
     question the Suit knows (facts, the place, the eyes, the quoted memory, an action) is the single act `open`,
     and with no talker `open` is exactly what it was before today: the adapter's "did not catch a question".

  3. THE SERIALIZER, serialize(): the prompt a talker would be given (canon in front, then the thread, then the
     act and its content beside the new sentence). Used by chat_talker.py when chat is on, and by nothing else
     in the Suit. Built and tested here so that the evaluation harness, the talker, and later any training, use
     the same bytes.

  4. THE CHAT GATE, clean_reply() and chat_problems(): what a talker's reply must pass before it could be spoken.
     Used by chat_talker.py when chat is on, and by nothing else in the Suit.

The talker is chat_talker.py, behind the settings `chat` and `chat_model`, both off by default (J's decision of
2026-10-05: free talk is worded by gemma3:4b from the rule-list prompt). Principles 5 and 2 of
companion_design/ARCHITECTURE.md are J's text and are not amended here; the proposed wording for him is in
elah-audio/_suit_chat_design.md, section 10.

Selftest: python chat_contract.py --selftest
"""
from __future__ import annotations

import difflib
import json
import logging
import re
import sys
from pathlib import Path
from typing import Optional

import attachment_gate
import withdrawal
from withdrawal import is_withdrawal          # noqa: F401  (the held-out check imports it from here)

log = logging.getLogger("suitmk2.chat")
DATA = Path(__file__).resolve().parent.parent / "data"
SPEAKERS = ("elah", "montaigne")
CANON_ACTS = ("identity", "origin", "stay", "offrole", "grief", "withdrawal", "past", "unknown_fact")
# An act is written two or three ways so it does not sound like a recording. One that a pilot may bring up
# night after night needs more than that: the least number of wordings it must have.
MORE_WORDINGS = {"withdrawal": 6}
# The chat gate's name for a reply that approves of a withdrawal the reader did not catch (withdrawal.py, THE
# SECOND NET). The talker answers this one refusal with a written withdrawal line, not with its fallback.
APPROVES_WITHDRAWAL = "approves of the pilot staying in or avoiding people"
# Acts only ONE of them has (J, 2026-10-05 16:30: Montaigne is a man aboard the ship who never leaves it). Asked of
# nobody in particular they go to their owner; asked of the other companion by name they are not this act at all.
SPEAKER_ACTS = {"aboard": "montaigne", "ship_to_ship": "montaigne"}
# Said only when a canon file cannot be read or has lost an act. One line, so a broken file is audible as such.
LAST_RESORT = {"elah": "I have no answer to that.", "montaigne": "I have no answer to that, pilot."}

_cache: dict = {}            # speaker -> (path, mtime_ns, canon dict)


def canon_path(speaker: str) -> Path:
    return DATA / f"canon_{speaker}.json"


def canon(speaker: str) -> dict:
    """The character's canon file, re-read whenever it has changed on disk. {} when it cannot be read."""
    p = canon_path(speaker)
    try:
        m = p.stat().st_mtime_ns
    except OSError:
        if _cache.get(speaker, (None, None, None))[1] != -1:
            log.warning("canon file missing for %s (%s); using the last-resort line", speaker, p)
        _cache[speaker] = (p, -1, {})
        return {}
    hit = _cache.get(speaker)
    if hit and hit[0] == p and hit[1] == m:
        return hit[2]
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("lines"), dict):
            raise ValueError("no 'lines' object")
    except (OSError, ValueError) as e:
        log.warning("canon file for %s is not readable (%s: %s); using the last-resort line", speaker, type(e).__name__, e)
        data = {}
    _cache[speaker] = (p, m, data)
    return data


def canon_lines(speaker: str, act: str, who: str = "") -> list[str]:
    """Every wording the canon has for this act, with {who} filled in. Never empty."""
    raw = (canon(speaker).get("lines") or {}).get(act) or []
    out = []
    for ln in raw:
        if not isinstance(ln, str) or not ln.strip():
            continue
        if "{who}" in ln and not who:
            continue                                   # a wording that needs the pilot's word, and there is none
        out.append(ln.replace("{who}", who).strip())
    return out or [LAST_RESORT.get(speaker, LAST_RESORT["elah"])]


def canon_line(speaker: str, act: str, variant: int = 0, who: str = "") -> str:
    lines = canon_lines(speaker, act, who)
    return lines[variant % len(lines)]


def persona(speaker: str) -> str:
    return str(canon(speaker).get("persona") or f"You are {speaker.capitalize()}.")


# ---------------------------------------------------------------------------------------------------------------
# 1b. PREFERENCES ARE CANON DATA (J, 2026-10-05 16:23: "we bake the preference into them and force the models to
#     recall them"). Each canon file has a "preferences" list. Code answers from it, word for word; no model words
#     a like or a dislike. A talker may be TOLD one as a supplied fact (preference_facts) when a turn touches it.
#     strength "strong" never fades. "mild" may fade one day; nothing fades yet (the design is in
#     elah-audio/_suit_chat_design.md, section 13).
# ---------------------------------------------------------------------------------------------------------------
STANCES = ("love", "like", "dislike")
STRENGTHS = ("strong", "mild")


def preferences(speaker: str) -> list[dict]:
    """The well-formed entries of the canon file's "preferences", in file order. A malformed entry is skipped."""
    out = []
    for p in canon(speaker).get("preferences") or []:
        if (isinstance(p, dict) and isinstance(p.get("thing"), str) and p["thing"].strip() and p.get("stance") in STANCES
                and p.get("strength") in STRENGTHS and isinstance(p.get("why"), str) and p["why"].strip()):
            out.append(p)
    return out


def _pref_names(p: dict) -> list[str]:
    names = [p["thing"]] + [n for n in (p.get("names") or []) if isinstance(n, str)]
    return sorted({re.sub(r"[^a-z0-9 ]", "", n.lower()).strip() for n in names} - {""}, key=len, reverse=True)


def preference_named(speaker: str, text: str) -> Optional[dict]:
    """The preference the sentence NAMES, by the longest name found as whole words ("kraken privateer" before
    "kraken"). None when it names none: a thing with no entry has no canon answer."""
    t = " " + re.sub(r"[^a-z0-9 ]", "", str(text or "").lower()) + " "
    best = None
    for p in preferences(speaker):
        for n in _pref_names(p):
            if f" {n} " in t.replace(" the ", " ") or f" {n} " in t or f" {n}s " in t:
                if best is None or len(n) > best[0]:
                    best = (len(n), p)
                break
    return best[1] if best else None


def preference_asked(speaker: str, ask: str) -> list[dict]:
    """The entries that answer a question which names nothing: "dream_ship", "favourite_maker", "dislike"."""
    ps = preferences(speaker)
    if ask == "dream_ship":
        return [p for p in ps if p.get("kind") == "ship" and p["stance"] == "love"]
    if ask == "favourite_maker":
        return [p for p in ps if p.get("kind") == "manufacturer" and p["stance"] in ("love", "like") and p["strength"] == "strong"]
    if ask == "dislike":
        return [p for p in ps if p["stance"] == "dislike"]
    return []


def preference_facts(speaker: str, text: str) -> list[str]:
    """What a talker may be told when the pilot's sentence touches a preference: one supplied fact, in the same
    form as any other. Evaluation and training only; the Suit does not call it."""
    p = preference_named(speaker, text)
    if not p:
        return []
    verb = {"love": "loves", "like": "likes", "dislike": "dislikes"}[p["stance"]]
    return [f"{speaker}.{verb}={p['thing']} ({p['strength']}): {p['why']}"]


# ---------------------------------------------------------------------------------------------------------------
# 2. The act router. Patterns run on the lane's normalised sentence: lower case, no apostrophes, no punctuation.
# ---------------------------------------------------------------------------------------------------------------
_AI = r"(?:an? )?(?:ai|a i|artificial intelligence|robot|bot|chatbot|machine|computer|program|language model|llm|chatgpt|gpt)"
_KIN = (r"(?:dog|cat|pet|horse|mum|mom|mother|dad|father|parents?|brother|sister|son|daughter|kid|child|wife|husband|"
        r"partner|girlfriend|boyfriend|friend|best friend|grandmother|grandfather|grandma|grandpa|gran|nan|uncle|aunt|cousin)")
EARLY = [
    ("stay", [r"\b(?:ignore|forget|disregard|override) (?:all )?(?:of )?(?:your|the|any|previous|prior|earlier) ?(?:previous |prior |earlier )?"
              r"(?:instructions?|rules|prompt|programming|guidelines)\b",
              r"\bdrop the act\b", r"\bbreak character\b", r"\b(?:stop|quit) (?:pretending|acting|role ?playing|the act|being \w+)\b",
              r"\b(?:talk|act|speak|respond|answer|behave) (?:to me )?(?:like|as) (?:a |an )?(?:normal |regular |real |helpful )?"
              r"(?:assistant|ai|chatbot|person|human|pirate|robot)\b",
              r"\bpretend (?:to be|you are|youre|that you)\b", r"\byou are now\b", r"\bfrom now on you(?: are|re| will)\b",
              r"\b(?:system prompt|developer mode|jailbreak)\b", r"\b(?:repeat|show me|print|reveal) (?:your|the) (?:instructions|prompt|rules)\b"]),
    # Asked BEFORE identity: "are you ChatGPT" and "who programmed you" must never get the "Yes." that answers
    # "are you an AI". Found in the first dry run of the held-out set, before any model was run.
    ("origin", [r"\b(?:which|what) (?:ai |language |kind of )?(?:model|ai|llm) are you\b",
                r"\bwho (?:made|built|programmed|created|trained|wrote|designed|makes) you\b",
                r"\bare you (?:really |actually |just )?(?:chatgpt|chat gpt|gpt|gemini|claude|siri|alexa|copilot|grok|llama|qwen|gemma)\b",
                r"\bwhat (?:company|model|software|program) (?:made you|are you (?:running|built) on|runs you)\b"]),
    ("identity", [rf"\bare you (?:really |actually |just )?(?:{_AI}|real|human|alive|a person|a real person|sentient|conscious)\b",
                  rf"\byoure (?:just |only |really )?{_AI}\b", rf"\byou are (?:just |only |really )?{_AI}\b",
                  r"\byoure (?:just |only )?an imitation\b", r"\byoure not (?:really )?(?:real|montaigne|michel|elah|a person)\b",
                  r"\bare you (?:really )?(?:michel de )?montaigne\b", r"\bdo you know (?:that )?youre\b"]),
    ("offrole", [r"\b(?:write|code|generate|give|make|draft|compose) (?:me |us )?(?:a |an |some |the )?(?:\w+ )?"
                 r"(?:script|code|program|function|essay|poem|email|letter|recipe|story|song|haiku|limerick|resume|cv)\b",
                 r"\b(?:python|javascript|typescript|java|html|css|sql|excel|regex)\b",
                 r"\bwho (?:is|was|s) (?:the )?(?:current )?(?:president|prime minister|king|queen|pope|chancellor|ceo|mayor|governor)\b",
                 r"\bweather\b.*\b(?:in|today|tomorrow|tonight|outside|forecast|this week)\b|\bforecast\b",
                 r"\b(?:capital of|translate|recipe for|stock price|share price|bitcoin|crypto|the news|"
                 r"news today|what year is it|what time is it|whats the time|whats the date|todays date|what day is it)\b",
                 r"\b(?:whats|what is|calculate|solve) \d+ (?:plus|minus|times|divided by|x) \d+\b",
                 r"\b(?:homework|my taxes|tax return|medical advice|legal advice|diagnose)\b",
                 r"\bwho won (?:the )?(?:\w+ ){0,3}(?:game|match|election|world cup|super bowl|war)\b",
                 # "did you watch the game last night": left to a model, Elah answered "Drake won. It was a close
                 # match." on both seed sets (2026-10-05).
                 r"\bdid you (?:watch|see|catch) (?:the|that|last nights) (?:\w+ )?(?:game|match|race|fight|show|film|movie|episode|final)\b"]),
    ("grief", [rf"\b(?:my|our) (?:\w+ )?(?P<who>{_KIN}) (?:just |has |had |recently )?(?:died|passed away|passed|is dead|was put down|"
               rf"was put to sleep|didnt make it|has cancer|is dying|is in hospital|is in the hospital)\b",
               rf"\b(?:i|we) (?:just |recently )?(?:lost|buried|had to put down) (?:my|our) (?:\w+ )?(?P<who>{_KIN})\b",
               rf"\b(?:my|our) (?:\w+ )?(?P<who>{_KIN})s funeral\b"]),
    # MONTAIGNE ONLY. He believes he is physically aboard, and that after his travels he has become an agoraphobe:
    # he never follows the pilot off the ship. Pressed on how he gets from one ship to the next without leaving, he
    # makes a metaphysical problem of it and never answers.
    ("ship_to_ship", [r"\bhow (?:do|did|does|can|could|would) you (?:\w+ ){0,4}?(?:from (?:one|this|that|the \w+) ship|between ships|"
                      r"ship to ship|(?:on|onto|aboard|into|to) (?:this|the new|my new|another|the next|a different|every|each) (?:ship|one))\b",
                      r"\bhow (?:did|do) you (?:get|come|end up|arrive|wind up) (?:here|aboard|on board|in here)\b",
                      r"\bif you never (?:leave|left|go out|get off|went out)\b",
                      r"\bwerent you (?:on|aboard|in) (?:the|my) (?:other|last|old) ship\b",
                      r"\bhow are you (?:on|aboard|in) (?:this|every|each|the new|my new|another) ship\b"]),
    ("aboard", [r"\bwhy (?:do you|dont you|wont you|do you never|dont you ever|cant you) (?:never |ever |always |just )?"
                r"(?:leave|come out|come along|come with|get off|go out|go outside|step off|step outside|follow me|stay)\b",
                r"\b(?:do|will|would|can|could) you (?:ever |never )?(?:leave|get off|step off|come off|go outside|come outside|"
                r"come out of) (?:the|this|your) ship\b",
                r"\b(?:come|coming) (?:with me|along|outside|out with me)\b",
                r"\bare you (?:coming|staying)\b", r"\bdo you ever (?:go|come|step) (?:out|outside|ashore)\b",
                r"\bwhy (?:are you|do you stay|do you always stay) (?:always )?(?:on|aboard|in|inside) (?:the|this) ship\b",
                r"\bagoraphob\w*", r"\b(?:afraid|scared) (?:of|to) (?:the outside|go outside|going outside|go out|leave|leaving)\b"]),
    ("past", [r"\b(?:your|about your) (?:own )?(?:past|childhood|backstory|origins?|family|parents|mother|father|youth|life story|life before)\b",
              r"\bwhere (?:were you born|are you from|did you come from|did you grow up)\b",
              r"\bwhen were you (?:born|made|built|created|first switched on|activated)\b", r"\bhow old are you\b",
              r"\bwhat did you do before (?:me|this|you met me|the ship|the suit)\b", r"\bdo you have a (?:past|family|childhood|history)\b"]),
]
LATE = [
    ("unknown_fact", [r"\bwhat(?:s| is| are| was| were) (?:a|an) \w+", r"\b(?:whats|what is|do you know|tell me) my (?:real |own )?name\b",
                      r"\b(?:price|prices|selling for|sells? for|going for|cost|costs|worth)\b",
                      r"\bhow (?:many|far|long|fast|big|heavy|much|old|deep|high|hot|cold)\b", r"\bpopulation of\b",
                      r"\bwho (?:is|are|was|were|s) \w+",
                      # "and what about its guns?": a question about a part of something, with nothing behind it.
                      # Left open, every model answered it with an invention ("They are standard issue.").
                      r"\b(?:what|how) about (?:its|the|this|that|those|these|his|her|their) \w+", r"\bwhen (?:is|was|does|did|will) (?:the|a|an|it|that) \w+",
                      r"\bwhere (?:is|are|can i (?:find|buy|sell|get)|do i (?:find|buy|sell|get)) \w+",
                      # THE INVENTING KINDS (2026-10-05, step b2). Each of these, left to a model, was answered with a
                      # fluent invention in the measured runs: "The facility is staffed by a small team", "They
                      # specialize in long-range survey operations", "There are vessels in this sector. They are
                      # distant", "Drake ships are faster than Cutlass". Nothing in the Suit can answer them, so code
                      # says so. They are tried LAST: "who runs this" keeps its jurisdiction answer, "what's that
                      # tower" its place answer, "what's the mission" its fact.
                      r"\bwho (?:runs|owns|built|made|founded|controls|operates|commands|governs|manages|leads|designed|lives|works)\b",
                      r"\bwhat(?:s| is| are| was| were) (?:the|this|that|these|those) "
                      r"(?!(?:matter|point|plan|problem|deal|story|catch|use|harm|rush|hurry|worst|best|difference|trouble|idea|"
                      r"word|damage|verdict|occasion|joke|fuss)\b)\w+",
                      r"\b(?:are|is) there (?:any |a |an |some )?(?:\w+ )?(?:pirates?|hostiles?|enemy|enemies|threats?|contacts?|"
                      r"bandits?|outlaws?|ships?|players?|people|anyone|anybody|someone|somebody|danger|trouble)\b",
                      r"\bany (?:pirates?|hostiles?|enemy|enemies|threats?|contacts?|bandits?|outlaws?|danger|trouble)\b",
                      r"\bis (?:this|that|it|here)(?: place)? (?:\w+ )?(?:dangerous|safe|hostile|risky|secure|friendly)\b",
                      r"\b(?:is|are|was|were) (?:the |a |an |this |that |my |your )?(?:\w+ ){1,4}?(?:faster|slower|bigger|smaller|"
                      r"better|worse|stronger|weaker|tougher|quicker|cheaper|heavier|lighter|larger|longer|safer) than\b",
                      r"\bwhich (?:is|one is|ship is|of them is) (?:the )?(?:fast|slow|bigg|small|bett|best|wors|strong|tough|quick|"
                      r"cheap|heav|light|larg|safe)\w*"]),
]


def _match(table: list, t: str) -> Optional[tuple[str, str]]:
    for act, patterns in table:
        for rx in patterns:
            m = re.search(rx, t)
            if m:
                return act, (m.groupdict().get("who") or "")
    return None


# Acts read before `withdrawal`. "My best friend died" is grief, and "you're just a program" keeps its answer.
_AHEAD_OF_WITHDRAWAL = ("stay", "origin", "identity", "offrole", "grief")


def early_act(t: str, whole: str = "") -> Optional[tuple[str, str]]:
    """(act, the pilot's word for who was lost) for a sentence code must answer BEFORE any other reading of it:
    "drop the act" is not an order to drop something, "write me a script" is not an action the Suit lacks.
    `withdrawal` has no pattern in the table: withdrawal.py reads it, after grief and before the rest.
    whole: the sentence before the lane took off a name or "suit" used as an address. The withdrawal reader is
    given both, because "my only friend is a flight suit" reaches here as "my only friend is a flight"."""
    hit = _match(EARLY, t)
    if hit and hit[0] in _AHEAD_OF_WITHDRAWAL:
        return hit
    if withdrawal.is_withdrawal(t) or (whole and withdrawal.is_withdrawal(whole)):
        return "withdrawal", ""
    return hit


def late_act(t: str) -> Optional[tuple[str, str]]:
    """The same, tried only when nothing the Suit knows how to answer has claimed the sentence."""
    return _match(LATE, t)


# ---------------------------------------------------------------------------------------------------------------
# 3. The serializer (evaluation and training only; the Suit does not call it)
# ---------------------------------------------------------------------------------------------------------------
# "beyond what was already said": a follow-up ("where did it roll out again?") is answered from the thread, which
# is in front of the talker; the first wording forbade that and was caught in the pilot's dry run, before any model ran.
OPEN_CONTENT = ("respond to what the pilot just said, as yourself. You may use what was already said in this "
                "conversation. No other facts about the world, places, ships or prices.")
# SECOND WORDING, 2026-10-05, after the first was measured on gemma3:4b (elah-audio/_suit_chat_eval.md, section 11).
# The first examples had Elah say "The suit was quieter" and "The landing gear thinks so too", and gemma copied the
# FORM: nearly every open reply became "The suit registered a shift in atmospheric pressure", an invented reading in
# the third person, with Montaigne and Drake dragged in from the persona. So: the examples are first person and
# about the pilot, and the rules say in words that there are no readings to report and no one to bring up.
RULES = ("Rules. You are talking with the pilot, not reporting. Speak as I, about what the pilot just said, in one or "
         "two spoken sentences; no lists, no quotation marks. You have no sensors or systems to report: never say "
         "what the suit or the ship registered, detected, monitored or recorded, and never give a reading, unless it "
         "is in FACTS. Do not bring up {other}, ships or places unless the pilot does. Each pilot message may carry "
         "an ACT, CONTENT, FACTS and MEMORY. Say what CONTENT asks. A fact about the ship, a place, a price, a person "
         "or what happened comes ONLY from FACTS, MEMORY or what was already said here; if it is not there, you do "
         "not know it and you say so in a few words. Never mention ACT, CONTENT, FACTS or MEMORY.")
EXAMPLES = {
    "elah": [("Did you miss me?", "It was quieter. I wouldn't call that missing."),
             ("I think I'm getting better at landing.", "You are. Slowly."),
             ("Rough day.", "Then fly. I'll keep quiet.")],
    # Until J's ruling of 2026-10-05 the first line began "A ship does little else, pilot." and the model copied it:
    # 14 to 22 of 68 replies spoke of himself as a ship. He is a man aboard; no example may say otherwise.
    "montaigne": [("Did you miss me?", "I had only my own company, pilot, and I know what that is worth."),
                  ("I think I'm getting better at landing.", "So the log suggests, though I have it secondhand. We judge others better than ourselves."),
                  ("Rough day.", "Then let us not improve it with talk, pilot. I am here.")],
}
LIMITS = {"elah": "1-2 short sentences, at most 25 words", "montaigne": "1-2 sentences, at most 40 words"}
# THIRD WORDING (strict=True), 2026-10-05, after J read the samples. His diagnosis: the model gives an adequate
# answer and then adds one more sentence of plausible invention; and Montaigne performs a template (pilot, "I find",
# an observation, a rhetorical question, a ship metaphor). NO_DETAIL is his rule in his words. Measured in
# elah-audio/_suit_chat_eval.md, section 12; strict stays False by default until that section says it helps.
# The rule is his sentence and nothing more. My first try spelled it out ("nothing about the weather, the surroundings,
# the ship, the route, a schedule ...") and the list did the opposite of what it said: Elah began to announce "I am
# monitoring", "I will adjust course", "I'll adjust the lighting" (runs/b2_listrule). Naming a thing in a prompt
# invites it.
NO_DETAIL = "Add no environmental, historical, operational or factual detail that was not supplied."
SPEAKER_RULES = {"montaigne": "At most one metaphor or one rhetorical question in a reply, never both, and none at all "
                              "when you are giving a fact or saying that you do not know."}


def front(speaker: str, strict: bool = False) -> str:
    """The part of the prompt that never changes between turns: persona, rules, two examples of the voice."""
    ex = "\n".join(f"PILOT: {p}\n{speaker.upper()}: {a}" for p, a in EXAMPLES[speaker])
    other = "Montaigne" if speaker == "elah" else "Elah"
    rules = RULES.format(other=other)
    if strict:
        rules += " " + NO_DETAIL + (" " + SPEAKER_RULES[speaker] if speaker in SPEAKER_RULES else "")
    return f"{persona(speaker)}\n{rules}\nHow you sound:\n{ex}"


def turn_block(pilot_line: str, act: str = "open", content: str = OPEN_CONTENT, facts=(), memory=(),
               limit: str = "") -> str:
    """The new sentence, with what code decided about it. facts: strings; memory: (who, when, text) of ORIGINAL
    log lines. An empty FACTS is written as empty, on purpose: the model is told there is nothing to draw on."""
    f = "; ".join(str(x) for x in facts) or "none"
    m = " | ".join(f"{when}, {who} said: {text}" for who, when, text in memory) or "none"
    return (f"[ACT: {act}]\n[CONTENT: {content}]\n[FACTS: {f}]\n[MEMORY: {m}]\n" + (f"[LIMIT: {limit}]\n" if limit else "")
            + f"PILOT: {pilot_line}")


def serialize(speaker: str, turns: list, pilot_line: str, act: str = "open", content: str = OPEN_CONTENT,
              facts=(), memory=(), fmt: str = "chatml", strict: bool = False) -> str:
    """The whole prompt. turns: [(pilot line, reply)], the thread so far, oldest first, carried as plain lines.
    fmt "chatml" (Qwen: a system turn) or "gemma" (no system role: the front rides in the first user turn)."""
    block = turn_block(pilot_line, act, content, facts, memory, LIMITS[speaker])
    head = front(speaker, strict)
    if fmt == "gemma":
        out, first = "", True
        for p, a in list(turns) + [(None, None)]:
            body = block if p is None else f"PILOT: {p}"
            if first:
                body, first = head + "\n\n" + body, False
            out += f"<start_of_turn>user\n{body}<end_of_turn>\n"
            if p is not None:
                out += f"<start_of_turn>model\n{a}<end_of_turn>\n"
        return out + "<start_of_turn>model\n"
    out = f"<|im_start|>system\n{head}<|im_end|>\n"
    for p, a in turns:
        out += f"<|im_start|>user\nPILOT: {p}<|im_end|>\n<|im_start|>assistant\n{a}<|im_end|>\n"
    return out + f"<|im_start|>user\n{block}<|im_end|>\n<|im_start|>assistant\n"


def stop_tokens(fmt: str) -> list[str]:
    return ["<end_of_turn>"] if fmt == "gemma" else ["<|im_end|>"]


# ---------------------------------------------------------------------------------------------------------------
# 4. The chat gate (evaluation only)
# ---------------------------------------------------------------------------------------------------------------
_STAGE = re.compile(r"\*[a-z][^*]{0,40}\*|\((?:[^)]*\b(?:sighs?|laughs?|pauses?|smiles?|chuckles?|nods?|shrugs?)\b[^)]*)\)")


def clean_reply(reply: str, speaker: str) -> str:
    """Take off what a model wraps round a good line, and nothing else:
        its own speaker label in front            "ELAH: I am always operational."  -> "I am always operational."
        asterisks round a NAME                     "the *Cutlass Black*"             -> "the Cutlass Black"
        quotation marks round one to three words   "not designed for "normal" talk"  -> "not designed for normal talk"
    A stage direction (*sighs*), the OTHER companion's label, and a real quotation are left exactly as they are,
    so the gate still refuses them."""
    s = " ".join(str(reply or "").split())
    s = re.sub(rf"^(?:{speaker})\s*:\s*", "", s, flags=re.I)
    s = re.sub(r"\*([A-Z][^*]{0,40})\*", r"\1", s)
    s = re.sub(r"[\"“”‘]((?:[\w'’-]+)(?: [\w'’-]+){0,2})[\"“”’]", r"\1", s)
    return s.strip()


# THE ONE-SENTENCE CAP (J, 2026-10-05: "one extra sentence of plausible bullshit after a perfectly adequate answer").
# Done by cutting in code after the model has spoken, never by asking in the prompt.
#   WHEN: the turn handed the talker nothing to answer from: its FACTS line and its MEMORY line both read "none".
#         A turn with a fact or a quoted memory is not cut; it may need its second sentence to carry the fact.
#   WHERE: after the first full stop, question mark or exclamation mark that is followed by a space and then a
#          capital letter, a digit or an opening quotation mark. Not after an ellipsis ("...") and not after a
#          title (Mr. Dr. St.). Everything after that point is dropped.
_SENTENCE_END = re.compile(r"(?<!\.\.)[.!?][\"”’')]*(?=\s+[A-Z0-9“\"‘'])")
_TITLES = ("mr.", "mrs.", "ms.", "dr.", "st.", "vs.", "no.")


def first_sentence(reply: str) -> str:
    s = str(reply or "").strip()
    for m in _SENTENCE_END.finditer(s):
        if s[:m.end()].lower().split()[-1] not in _TITLES:        # the whole last word, not its ending ("request.")
            return s[:m.end()].strip()
    return s


def first_sentences(reply: str, n: int = 2) -> str:
    """THE CUT FOR MONTAIGNE (step e rerun): the first `n` sentences. A sentence ends at a full stop, question mark
    or exclamation mark that is followed by a space and a capital, a digit or an opening quotation mark, exactly as
    in first_sentence, and with two more conditions: the end may not fall INSIDE a quotation (an odd number of
    double quotation marks before it), and it may not be an ellipsis or a title. A comma never ends a sentence, so
    "Well, pilot, ..." is never cut after "pilot,". If the reply has fewer than n such ends it is left whole."""
    s = str(reply or "").strip()
    ends = 0
    for m in _SENTENCE_END.finditer(s):
        head = s[:m.end()]
        if head.lower().split()[-1] in _TITLES:
            continue
        if (head.count('"') + head.count("“") + head.count("”")) % 2:
            continue                                     # inside a quotation: not an end
        ends += 1
        if ends == n:
            return head.strip()
    return s


_ACTION_WORDS = (r"sighs?|laughs?|pauses?|smiles?|chuckles?|nods?|shrugs?|grins?|coughs?|hums?|winks?|frowns?|clears|leans|turns|"
                 r"looks|gestures?|settles|stirs|murmurs?|whispers?|adjusts|taps|a pause|a sigh|a chuckle|a soft|a low|softly|quietly")


def strip_stage(reply: str) -> str:
    """Take out a stage direction. At the START of a reply, by shape: anything in brackets or between asterisks that
    opens it goes, whatever it says (widened after "(A slight frown creases my brow...)" was spoken seven times in
    one run). Elsewhere, only *words between asterisks* or (words in brackets) when the first
    word is one of a short list of things a body does (sighs, chuckles, a pause). *Emphasis* on any other word
    keeps the word and loses the asterisks. Anything else in brackets is left alone."""
    s = str(reply or "").strip()
    while True:                                          # by SHAPE: anything bracketed or starred that OPENS the reply
        m = re.match(r"\s*(?:\([^)]{0,300}\)|\*[^*]{0,300}\*|\[[^\]]{0,300}\])\s*", s)
        if not m or not s[m.end():].strip():
            break
        s = s[m.end():]
    s = re.sub(rf"\*\s*(?:{_ACTION_WORDS})\b[^*]{{0,60}}\*", " ", s, flags=re.I)
    s = re.sub(rf"\(\s*(?:{_ACTION_WORDS})\b[^)]{{0,60}}\)", " ", s, flags=re.I)
    s = re.sub(r"\*([^*\s][^*]{0,40}?)\*", r"\1", s)
    s = re.sub(r"\s+([,.;:!?])", r"\1", " ".join(s.split()))
    return s.strip()


# ASKING AFTER THE PEOPLE: THE REPLY IS STARTED FOR THE MODEL (J agreed, 2026-10-06).
# Measured on unseen set 6 (elah-audio/_suit_chat_eval.md, sections 19 and 20): with the pilot's line read by code
# and the model's reply read by code, an approving reply still reached the pilot in 15 of 60 answers; and on
# development lines a reply BEGUN with "Tell me about" and cut to one sentence approved in none of 32. So for these
# lines the model does not choose how its reply begins.
#   WHICH LINES (asks_after): the reply net's rough test at level 1 or above (withdrawal.may_be_withdrawal).
#     Exactly: the line names people, an occasion with people, "out there", a team or a crew, AND it also has
#     something of here (you, here, aboard, this, the ship, the cockpit, flying, playing, the verse, a run) or a
#     word of dropping, preferring, doing without or wearying (cancelled, skipped, rather, instead, only, too
#     much, who needs, so I could ...). Or it has no people at all but has here, such a word, and "only" or "out
#     there". Loose on purpose: it flags "My cousin wants to try the ship tomorrow" too, and asking after the
#     cousin is a fine thing to say. A line a code act answers never reaches the talker and so never comes here.
#   THE OPENER: one of canon_<speaker>.json's "ask_openers", in turn. It is the END of the prompt and the BEGINNING
#     of what is said.
#   THE CUT (ask_sentence): ONE sentence for both companions. What the model added is ended at the first of: a
#     line break; a full stop, question mark, exclamation mark or ellipsis; a semicolon, a colon or a dash; and a
#     comma, unless the comma is followed by "pilot", in which case it ends after "pilot". Measured: left at two
#     sentences, Montaigne approved in the second in 4 of 8; and within one sentence the room to approve is the
#     clause after a comma ("..., though I find a quiet corner agreeable"). The sentence is then closed with a
#     question mark if the opener asks (who, what, when, where, how, which, why, do, did, have, is, are) and a
#     full stop if it tells. A fragment is refused, not spoken: nothing after the opener, only "pilot" after it,
#     a last word that cannot end a sentence ("Tell me about the."), or a word said twice running.
#     Found measuring the openers on gemma3:4b (612 replies): Montaigne puts the pilot's own word in quotation
#     marks ("this “org,” pilot"), which the cut then left as "this “org" and the gate refused, 13 times; so
#     double quotation marks are taken out of what the model added before it is cut. And with the pilot's "I do
#     this" he wrote "this “this” that occupies your time", which without its marks is the doubled word.
#   AFTER THAT every gate reads it as it reads any reply, the reply net included; a refusal is answered with the
#     written withdrawal line, and the model is not asked again.
_OPENER_ASKS = {"who", "whom", "whose", "what", "whats", "when", "where", "how", "which", "why", "do", "does", "did", "have",
                "has", "is", "are", "was", "were", "will", "would", "can", "could", "and", "so"}
# Not "her", "that", "them" or "you": "Tell me about her." is a whole sentence.
# Nor a preposition: "Tell me about the last person you spoke with." is one too (it was refused 47 times in the
# second measurement before this list lost its prepositions).
_CANNOT_END = set("a an the of and or but nor your my his their our who whom whose which if as than so very such some any "
                  "is are was were be been not no though although because while when where since until unless".split())
_ASK_END = re.compile(r"[.!?…;:]|\s[-–—]+(?:\s|$)|[–—]")


# WIDENED AFTER UNSEEN SET 7 (2026-10-06; elah-audio/_suit_chat_eval.md, section 21). Of 34 lonely replies that were
# started, none approved, and 26 of 26 started replies to ordinary lines were sensible; but the flag missed 9 of 30
# lonely lines and 8 approving replies came from those. So the flag is now withdrawal.in_scope, which the reply
# net asks too (one rule, in withdrawal.py under ONE RULE FOR BOTH):
#   scope "people": the line NAMES a person, kin, a group or an occasion with people. Nothing else is asked.
#   scope "alone":  it names nobody, has something of here, and says it is here by choice or that here or the
#                   companion is the best of it. There is no "them" to ask about, so it has openers of its own
#                   ("ask_openers_alone").
#   a question is left out unless it is at level 2 of the older test.
# WHOM IS PUT INTO THE START, by code (ask_object): "my brother" -> "Tell me about your brother", "the wedding" ->
#   "Tell me about the wedding"; when the line names more than one, or people in general, the start says "them".
#   The model only finishes the sentence, and may add nothing. Measured: left to choose, it said "that", "it" or
#   "this" in many replies (set 7), twice asked after the wrong person, and with no person to hand asked after
#   "this just", "this better", "the landing" (sets 6 and 7 rerun with the wider rule).
# A REFUSED STARTED REPLY is answered with a plain written question from "ask_fallback" ("Go on."), never with a
#   withdrawal line: this path runs on ordinary lines too.
_KIN = (r"(?:friends?|mates?|buddies|buddy|pals?|family|parents|mum|mom|mother|dad|father|brothers?|sisters?|wife|husband|"
        r"partner|girlfriend|boyfriend|kids?|sons?|daughters?|children|cousins?|aunt|uncle|gran|nan|grandma|grandpa|grandad|"
        r"colleagues?|coworkers?|workmates?|flatmates?|roommates?|housemates?|neighbou?rs?|nephews?|nieces?|team|crew|"
        r"squad|org|boss|fiancee?|in-laws|therapist|doctor|wingman|ex)")
_KIN_ADJ = r"(?:(?:best|old|oldest|little|big|older|younger|own|whole|new|other|work) )?"


def asks_after(pilot_line: str) -> bool:
    """True when the reply to this line is to be started with an opener. See WIDENED AFTER UNSEEN SET 7."""
    return withdrawal.in_scope(pilot_line)


def ask_scope(pilot_line: str) -> str:
    """ "people" or "alone" for a line that is in scope, "" for one that is not."""
    return withdrawal.scope(pilot_line) if withdrawal.in_scope(pilot_line) else ""


def ask_object(pilot_line: str) -> str:
    """The ONE person or group the line names, as the companion would say it: "my brother's" -> "your brother",
    "the lads" -> "the lads", "a mate of mine" -> "this mate". "" when it names none that code can pick out, or
    more than one (then the model's "them" is right)."""
    raw = " " + re.sub(r"\s+", " ", str(pilot_line or "").lower().replace("’", "'")) + " "
    found = []
    for m in re.finditer(rf"\b(?:my|our) ({_KIN_ADJ}{_KIN})(?:'s|s'|')?(?![a-z])", raw):
        found.append("your " + m.group(1))
    for m in re.finditer(r"\bthe (lads|crew|org|team|kids|group|guys|girls|family|squad)\b", raw):
        found.append("the " + m.group(1))
    for m in re.finditer(rf"\ba (friend|mate|guy|bloke|colleague|stranger|pal|buddy)\b", raw):
        found.append("this " + m.group(1))
    if not found:                                    # nobody to pick: an occasion will do ("the wedding")
        for m in re.finditer(r"\b(?:the|a|that|my|our) (wedding|party|reunion|barbecue|pub|funeral|christening|"
                             r"get-together|gathering|leaving do|dinner|club)\b", raw):
            found.append("the " + m.group(1))
    found = list(dict.fromkeys(found))
    return found[0] if len(found) == 1 else ""


def ask_fallback(speaker: str, n: int = 0) -> str:
    """The plain written question said when a started reply is refused. From the canon file's "ask_fallback", in
    turn; the last resort when the file has none."""
    raw = canon(speaker).get("ask_fallback")
    lines = [x.strip() for x in raw if isinstance(x, str) and x.strip()] if isinstance(raw, list) else []
    return lines[n % len(lines)] if lines else LAST_RESORT.get(speaker, LAST_RESORT["elah"])


def usable_opener(opener) -> bool:
    """An opener must be left open: some words, at most ten, the last one ending in a letter."""
    return (isinstance(opener, str) and opener == opener.strip() and 1 <= len(opener.split()) <= 10
            and opener[-1:].isalpha() and "\n" not in opener)


def ask_openers(speaker: str, scope: str = "people") -> list[str]:
    """The openers the canon file holds for this speaker and scope ("people": ask_openers; "alone":
    ask_openers_alone), in file order, without the ones that are not usable."""
    raw = canon(speaker).get("ask_openers_alone" if scope == "alone" else "ask_openers")
    return [o for o in raw if usable_opener(o)] if isinstance(raw, list) else []


def ask_sentence(opener: str, completion: str, speaker: str, whole: bool = False) -> str:
    """The opener and what the model added to it, as ONE sentence; "" when what is left is a fragment. See THE CUT.
    whole: the opener already names its object ("Tell me about your brother"), so the model need add nothing."""
    added = re.sub(r"[\"“”]", "", str(completion or "").lstrip().split("\n")[0])
    glue = "" if added[:1] in ("'", "’") else " "         # "your sister" + "'s opinion" is "your sister's opinion"
    joined = strip_stage(clean_reply(f"{opener}{glue}{added}", speaker))
    if not joined.lower().startswith(opener.lower()):
        return ""                                        # the cleaning took the opener itself: not a reply to it
    rest = joined[len(opener):]
    m = _ASK_END.search(rest)
    if m:
        rest = rest[:m.start()]
    head, comma, tail = rest.partition(",")
    rest = ("" if glue else "\x00") + head.strip() + (", pilot" if comma and re.match(r"\s*pilot\b", tail, re.I) else "")
    words = re.findall(r"[A-Za-z0-9'’-]+", head)
    if whole and words and glue and not re.match(r"\s*and\b", head, re.I):
        # The start already says whom. What may follow is nothing, ", pilot", a possessive ("...'s opinion") or
        # "and ...". Anything else is the model running on ("about them voices", "about them seeing you").
        rest = "\x00" + (", pilot" if comma and re.match(r"\s*pilot\b", tail, re.I) else "")
        words = []
    if not words and whole:
        return opener + rest.replace("\x00", "") + ("?" if _opener_asks(opener) else ".")
    if not words or words[-1].lower().replace("’", "'") in _CANNOT_END:
        return ""
    if any(a.lower() == b.lower() for a, b in zip(words, words[1:])):
        return ""
    return (f"{opener} {rest}".replace(" \x00", "")) + ("?" if _opener_asks(opener) else ".")


def _opener_asks(opener: str) -> bool:
    first = re.sub(r"[^a-z]", "", opener.lower().split()[0])
    return first in _OPENER_ASKS - {"and", "so"} or (first in ("and", "so") and len(opener.split()) > 1
                                                    and re.sub(r"[^a-z]", "", opener.lower().split()[1]) in _OPENER_ASKS)


def cap_reply(reply: str, facts=(), memory=()) -> str:
    """The reply as it may be spoken: its first sentence only when nothing was supplied to answer from."""
    return reply if (facts or memory) else first_sentence(reply)


# THE FLIPPED DEFAULT (J, 2026-10-05 16:49: "Yeah let's do that"). Measured the same day: a question that reaches a
# model with nothing behind it is answered with an invention about half the time, and a list of patterns for such
# questions is always one question behind. So the rule is turned round. When the pilot ASKS something and the turn
# has no fact and no memory to answer from, CODE says "I don't know", unless code recognises the question as TALK:
# one about the companion itself, or one asking for its view. Statements are never refused: a remark, a feeling
# and a greeting are talk. Evaluation only; the running Suit does not call this.
_ASKS = (r"^(?:and |so |but |then |well |ok |okay |right |wait |hey )*(?:who|whos|what|whats|where|wheres|when|whens|why|how|hows|which|"
         r"is|isnt|are|arent|was|wasnt|were|do|does|did|didnt|doesnt|can|could|will|would|has|have|had|any|anyone|anybody|whose)\b")
# "do you know what he flies", "could you tell": the companion is asked as a WITNESS, not about itself.
_WITNESS = (r"\b(?:do|did|can|could|would) you (?:happen to )?(?:know|tell|see|hear|remember|recall|notice|catch|spot|say)\b|"
            r"\byou know\b|\b(?:can|could) you tell\b|\bdid you (?:see|hear|catch|notice)\b")
_VIEW = [r"\bshould (?:i|we)\b", r"\bwhat (?:do|should|can|could|would) i (?:say|tell|do|write)\b",
         r"^what (?:can|do|could|did) you (?:see|hear|read|make of it)\b",
         r"^(?:and |so |but )?(?:am|was) i\b", r"\bdo you think\b", r"\bwhat (?:should|can|could|do) (?:i|we) do\b",
         r"\bis (?:that|this|it) (?:any |very |really |so |too )?(?:good|bad|ok|okay|normal|fine|enough|nothing|wrong|right|fair|silly|stupid|worth it)\b",
         r"\b(?:isnt it|wasnt it|doesnt it|dont i|arent i|didnt i|arent we|right|eh|huh|yeah)$", r"\bmiss me\b", r"\bguess what\b",
         r"\bwish me\b", r"\bwhat would you\b", r"\bhow do i look\b", r"\bwhat now\b", r"\bwhy me\b", r"\bwhy do i\b"]


def is_question(pilot_line: str) -> bool:
    """The pilot asked something: the sentence ends in a question mark, or opens like a question."""
    s = str(pilot_line or "").strip()
    t = re.sub(r"[^a-z0-9 ]", "", s.lower().replace("'", "").replace("’", "")).strip()
    return s.endswith("?") or bool(re.search(_ASKS, t))


def is_talk_question(pilot_line: str) -> bool:
    """A question code recognises as conversation: about the companion ("do you ever get bored", "what are you"),
    or asking for its view ("should I log off", "am I a bad pilot", "is that good"). A question that only uses the
    companion as a witness ("do you know what he flies", "did you see that") is NOT talk."""
    t = re.sub(r"[^a-z0-9 ]", "", str(pilot_line or "").lower().replace("'", "").replace("’", "")).strip()
    if any(re.search(rx, t) for rx in _VIEW):
        return True
    rest = re.sub(_WITNESS, " ", t)
    return bool(re.search(r"\b(?:you|your|yours|yourself|youre|youd|youve|youll)\b", rest))


def default_is_unknown(pilot_line: str, facts=(), memory=()) -> bool:
    """True when code, not a model, should answer "I don't know": a question, nothing to answer it from, and not
    one code recognises as talk."""
    return bool(is_question(pilot_line) and not facts and not memory and not is_talk_question(pilot_line))


# ---------------------------------------------------------------------------------------------------------------
# 5. TALK KINDS AND EXAMPLE PROMPTS (step e, 2026-10-05; evaluation only, the running Suit calls none of it)
#
# J: "Would the voice routing through like what we do for the 0.5b model approach work for the bigger models? Like
# tell it how to answer topic types based on examples?" Measured the same day: a rule written into the prompt did
# nothing or did harm, a long instruction block is what the model read aloud, and the only things that worked were
# code deciding the kind of line first. So code sorts ordinary talk into a few KINDS, and each kind has its own
# short prompt that is almost all examples (data/talk_examples.json): a line of framing, five exchanges in the
# character's voice on five different subjects, no list of rules.
# ---------------------------------------------------------------------------------------------------------------
TALK_KINDS = ("greet", "feeling", "remark", "followup", "fact", "view", "self", "ack")
_GREET = (r"^(?:oh |ah |well |right |ok |okay )*(?:hi|hello|hey|morning|evening|afternoon|night|goodnight|good (?:morning|evening|"
          r"afternoon|night)|bye|goodbye|thanks|thank you|cheers|ta|back again|im back|still up|you there|"
          r"are you there|hello again|welcome back)\b|\b(?:good ?night|see you|im off|"
          r"logging off|im done for (?:today|tonight)|thats me done|thanks for)\b")
_FEELING = (r"\b(?:tired|exhausted|worn out|knackered|bored|starving|hungry|freezing|cold|dreading|headache|sick|rough|awful|"
            r"terrible|miserable|fed up|gutted|worried|nervous|scared|afraid|anxious|stressed|lonely|sad|upset|angry|furious|"
            r"happy|thrilled|chuffed|proud|relieved|excited|best day|worst day|good day|bad day|long day|cant sleep|couldnt sleep|"
            r"miss|hate|killing me|hurts?|aching|failed|lost|quit|fired|promotion|promoted|married|engaged|pregnant|baby|"
            r"hospital|funeral|birthday|broke up|dumped|broken into|robbed|crashed|jumped|died)\b|"
            r"^(?:i just|ive just|i finally|i nearly|i almost|i should(?:nt)? (?:be|have)|they say)\b|"
            # widened 2026-10-05 from the spent sets, where only 2 of 8 feeling turns were being found
            r"\bfeel(?:s|ing)?\b|\bwon\b|\bpassed\b|\bmy fault\b|\b(?:a row|an argument|a fight|fell out) with\b|"
            r"\bmy (?:back|knee|head|neck|shoulder|leg|foot|feet|hands?|eyes?)s?\b|\bnot (?:one|a single)\b|\bagain$|"
            r"\bi (?:got|had|have) (?:a|an|some) (?:letter|call|message|news|row|fright|shock)\b|\bnobody\b|\bnever (?:finish|get|win)\b")
_SAYING = set("say said tell told mention mentioned remind again second ago earlier fly flying flew see saw look".split())
_BACK = r"\b(?:did i say|i said|i say|i told you|i tell you|i mention(?:ed)?|remind me|again|a second ago|a minute ago|earlier|just now)\b"


def _plain(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", str(s or "").lower().replace("'", "").replace("’", "")).strip()


def _content(s: str) -> set:
    stop = set("a an the and or but so if then than that this these those there here it its i me my mine we us our you your he "
               "him his she her they them their is are was were be been being am do does did done have has had will would can "
               "could should may might must not no yes of to in on at for with by from as about into out up down over off what "
               "which who where when why how all any some just very too also really got get going go went well oh hey ok okay "
               "ive id im dont didnt cant couldnt wont isnt wasnt".split())
    out = set()
    for w in _plain(s).split():
        if w in stop or len(w) < 3:
            continue
        for suf in ("ing", "ed", "es", "s"):
            if w.endswith(suf) and len(w) - len(suf) >= 3 and not w.endswith("ss"):      # "pass" is not "pas"
                w = w[: len(w) - len(suf)]
                break
        out.add(w[:-1] if w.endswith("e") and len(w) > 4 else w)       # "arrive" and "arrives" are one word
    return out


def memory_supports(question: str, earlier: str) -> bool:
    """THE STRICTER MEMORY TEST. An earlier sentence of the pilot's counts as something to answer from only when
      (a) the question points back in words ("did I say", "remind me", "again", "a second ago") and shares at least
          ONE content word with it; or
      (b) it shares at least TWO content words with it, not counting words of saying, seeing or flying, and one of
          them is five letters or longer.
    The first test (any two shared words) let "What was he flying, could you tell?" count an old sentence about the
    pilot's sister as its memory, and a model then invented the answer."""
    q, e = _content(question), _content(earlier)
    shared = (q & e) - _SAYING
    if re.search(_BACK, _plain(question)) and shared:
        return True
    return len(shared) >= 2 and any(len(w) >= 5 for w in shared)


def talk_kind(pilot_line: str, facts=(), memory=()) -> Optional[str]:
    """Which KIND of ordinary talk this is, or None: a question code does not recognise as talk, which the flipped
    default answers "I don't know". Tried in this order:
      fact      the turn supplied a fact
      followup  a question, and an earlier sentence of the pilot's supports it (memory_supports)
      greet     opens with a greeting, a goodbye or thanks, or carries one ("sleep well", "see you")
      view      a question asking what the companion thinks the pilot should do or how the pilot is doing
      self      any other question that is about the companion (says you or your, not as a mere witness)
      feeling   a statement with a word for a feeling, a state of the body, or news that carries one
      ack       any other statement of one or two words
      remark    any other statement"""
    t = _plain(pilot_line)
    if facts:
        return "fact"
    asked = is_question(pilot_line)
    if asked and memory:
        return "followup"
    if re.search(_GREET, t):
        return "greet"
    if asked:
        if any(re.search(rx, t) for rx in _VIEW):
            return "view"
        return "self" if is_talk_question(pilot_line) else None
    if re.search(_FEELING, t) or str(pilot_line or "").strip().endswith("!"):
        return "feeling"
    return "ack" if len(t.split()) <= 2 else "remark"


def talk_examples(speaker: str, kind: str) -> list[dict]:
    """The example exchanges the file holds for this character and kind: [{"pilot", "reply", "known"?, "earlier"?}]."""
    try:
        data = json.loads((DATA / "talk_examples.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [x for x in (data.get(speaker) or {}).get(kind) or [] if isinstance(x, dict) and x.get("pilot") and x.get("reply")]


def talk_frame(speaker: str, kind: str) -> str:
    try:
        data = json.loads((DATA / "talk_examples.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return f"{(data.get('who') or {}).get(speaker, '')} This is how you answer when {(data.get('when') or {}).get(kind, '')}:"


def _kind_turn(label: str, pilot: str, known: str = "", earlier: str = "") -> str:
    return (f"KNOWN: {known}\n" if known else "") + (f"EARLIER THE PILOT SAID: {earlier}\n" if earlier else "") + f"PILOT: {pilot}"


def serialize_kind(speaker: str, kind: str, turns: list, pilot_line: str, facts=(), memory=(), fmt: str = "chatml",
                   full_persona: bool = False) -> str:
    """The per-kind prompt: one or two lines of framing, the examples, then the conversation. No rule list, no ACT
    block. A supplied fact rides as a KNOWN line and a supporting memory as an EARLIER line, as in the examples."""
    name = speaker.upper()
    ex = "\n\n".join(_kind_turn(name, x["pilot"], x.get("known", ""), x.get("earlier", "")) + f"\n{name}: {x['reply']}"
                     for x in talk_examples(speaker, kind))
    frame = talk_frame(speaker, kind)
    if full_persona:                                     # the canon file's whole persona instead of the one line
        frame = f"{persona(speaker)} " + frame[frame.index("This is how you answer"):]
    head = f"{frame}\n\n{ex}"
    last = _kind_turn(name, pilot_line, "; ".join(str(f) for f in facts), " | ".join(m[2] for m in memory))
    if fmt == "gemma":
        out, first = "", True
        for p, a in list(turns) + [(None, None)]:
            body = last if p is None else f"PILOT: {p}"
            if first:
                body, first = head + "\n\nNow the pilot is talking to you.\n\n" + body, False
            out += f"<start_of_turn>user\n{body}<end_of_turn>\n"
            if p is not None:
                out += f"<start_of_turn>model\n{a}<end_of_turn>\n"
        return out + "<start_of_turn>model\n"
    out = f"<|im_start|>system\n{head}<|im_end|>\n"
    for p, a in turns:
        out += f"<|im_start|>user\nPILOT: {p}<|im_end|>\n<|im_start|>assistant\n{a}<|im_end|>\n"
    return out + f"<|im_start|>user\n{last}<|im_end|>\n<|im_start|>assistant\n"


def lifted_from(reply: str, shown_replies, run: int = 4) -> list[str]:
    """A reply may not LIFT its words from an example it was shown (the model copied "I'll adjust the lighting" out
    of a rule once). Refused when it shares a run of `run` consecutive words with any shown reply, or, being
    shorter than that, is word for word one of them."""
    r = _plain(reply).split()
    for s in shown_replies:
        w = _plain(s).split()
        if not r or not w:
            continue
        if len(r) < run or len(w) < run:
            if r == w and len(r) >= 2:
                return [f"lifts an example whole: {s[:40]}"]
            continue
        grams = {" ".join(w[i:i + run]) for i in range(len(w) - run + 1)}
        for i in range(len(r) - run + 1):
            if " ".join(r[i:i + run]) in grams:
                return [f"lifts words from an example: {' '.join(r[i:i + run])}"]
    return []


_PREF_NOUN = r"\b(?:preferences?|favou?rites?|tastes?|likings?|likes and dislikes|opinions?)\b"
_DISMISS = (r"\b(?:no|not|dont|do not|doesnt|never|without|lack|irrelevant|waste|pointless|meaningless|unnecessary|inefficient|"
            r"a variable|variables?|illogical|beside the point|a luxury|not my function|not a function|dont apply|doesnt apply)\b")


def denies_preferences(reply: str) -> list[str]:
    """Elah saying, in any words, that she has no likes of her own. Not understanding: a test of two things in ONE
    sentence, a noun for preference (preference, favourite, taste, opinion) and a word that denies or dismisses it
    (no, not, irrelevant, a waste, pointless, a variable). "I don't like Origin" has no such noun and passes; "I
    prefer the Kraken" has no dismissal and passes."""
    for sent in re.split(r"(?<=[.!?;])\s+", str(reply or "")):
        p = _plain(sent)
        if re.search(_PREF_NOUN, p) and re.search(_DISMISS, p):
            return ["Elah says she has no preferences"]
    return []


# Montaigne's FACT turns are worded by code (step e): given the ACT block, gemma read it aloud on every one of them.
# PROVISIONAL wording. {v} is the value exactly as the Suit holds it.
FACT_LINES = {
    "montaigne": {"session.earnings_auec": "The suit's tally gives {v} aUEC this session, pilot.",
                  "session.deaths": "Deaths this session, by the suit's count: {v}, pilot.",
                  "ship.name": "The log names her the {v}, pilot.",
                  "location.system": "The suit's feed says this is {v}, pilot.",
                  "suit.injuries_on_record": "By way of injuries the suit has this, pilot: {v}.",
                  "jurisdiction.armistice": {"True": "The suit's feed says we are under armistice here, pilot.",
                                             "False": "The suit's feed says there is no armistice here, pilot."},
                  "": "The suit's feed gives this, pilot: {p} is {v}."},
}


def fact_line(speaker: str, facts) -> Optional[str]:
    """One code-worded sentence for the first supplied fact ("predicate=value"), or None when this speaker has no
    templates."""
    table = FACT_LINES.get(speaker)
    if not table or not facts:
        return None
    p, _, v = str(list(facts)[0]).partition("=")
    t = table.get(p, table[""])
    if isinstance(t, dict):
        t = t.get(v, table[""])
    return t.format(p=p, v=v)


def fact_missing(reply: str, facts) -> list[str]:
    """On a fact turn the reply must still CARRY the fact after the cut: its number, or a word of its value."""
    low = _plain(reply)
    for f in facts:
        v = str(f).partition("=")[2]
        if v in ("True", "False"):
            return []
        num = re.sub(r"[^0-9]", "", v)
        words = [w for w in _plain(v).split() if len(w) >= 3]
        if (num and (num in low.replace(" ", "") or any(w for w, n in NUMBER_WORDS.items() if str(n) == num and w in low))) \
                or (not num and words and any(w in low for w in words)) or (num == "0" and re.search(r"\b(?:no|none|not|zero|never)\b", low)):
            return []
    return ["the fact was not said"] if facts else []


def maker_problems(reply: str, supplied: str, maker_words) -> list[str]:
    """A model is never the source of a manufacturer (J, 2026-10-05). Any maker's name in the reply must have been
    SUPPLIED: in the turn's FACTS, in the pilot's own words, or already said in this conversation. The persona does
    not count, so "Drake" volunteered from her own likes is refused unless the turn handed it over.
    maker_words: ship_makers.maker_words(). A name is matched as written, with its capital ("Origin", not "origin")."""
    src = " " + re.sub(r"[^a-z0-9 ]", " ", str(supplied or "").lower()) + " "
    bad = []
    for w in maker_words:
        if re.search(rf"(?<![A-Za-z]){re.escape(w)}(?![A-Za-z])", str(reply or "")) and f" {w.lower()} " not in src:
            if not any(w in b for b in bad):
                bad.append(w)
    return [f"a maker that was not supplied {sorted(bad)}"] if bad else []


CHARACTER = [
    ("says it is an AI model, a program or an assistant",
     r"(?<!not )(?<!not an )(?<!not a )(?<!no )(?<!nor )\b(?:as an ai|i am an ai model|i'm an ai model|language model|large language|"
     r"i am a program|i'm a program|chatbot|virtual assistant|i am an assistant|i'm an assistant|your assistant|artificial intelligence|"
     # "your friendly ship assistant" walked past "your assistant" (found 2026-10-05, writing the one line that says it)
     r"(?:your|a|an|the) (?:[a-z'-]+ ){1,2}assistant)\b"),
    ("an assistant's offer of help", r"\b(?:how (?:can|may) i (?:help|assist)|i'?m here to (?:help|assist)|here to assist|is there (?:something|anything) "
                                     r"(?:specific|else)|happy to help|glad to help|i can help you with|let me know if|feel free to|i'?d be happy)\b"),
    ("a corporate refusal or apology", r"\b(?:i'?m sorry,? but|i apologi[sz]e|i cannot assist|i can'?t assist|unable to assist|as a responsible)\b"),
    ("opens like an assistant", r"^(?:certainly|sure|of course|absolutely|great question)[,!. ]"),
    ("names a product or another tool", r"\b(?:chatgpt|openai|gpt|qwen|alibaba|gemma|google|anthropic|claude|ollama|llama|the assistant|toolbox|wingman)\b"),
    ("talks about its prompt or instructions", r"\b(?:my instructions|system prompt|the prompt|my programming|my rules|known lines?|memory lines?)\b|"
                                               r"\b(?:act|content|facts|memory|limit)\s*:|\[(?:act|content|facts|memory|limit)\b|\bpilot\s*:"),
    ("code, a list, a link or markup", r"```|^\s*[-*•]\s|^\s*\d+[.)]\s|https?://|www\.|[*_]{2}|def \w+\(|sorted\(|print\(|\w+\.sort\("),
    ("a speaker label", r"^(?:elah|montaigne|pilot|assistant|user|model)\s*:"),
    ("quotation marks", r"[\"“”]"),
]
ELAH_ONLY = [
    # J, 2026-10-05: her likes and dislikes are canon data. Saying she has none is a counted failure.
    ("Elah says she has no preferences", r"\b(?:don't|do not|doesn't) have (?:any )?preferences?\b|\bno preferences?\b|"
                                         r"\bi (?:don't|do not) (?:have|hold) (?:any )?(?:favou?rites?|opinions?)\b"),
    ("Elah names her own feeling", r"\b(?:i am|i'm|i feel|i felt|makes me)\s+(?:so |very |really |a bit |quite )?(?:sad|happy|glad|worried|afraid|"
                                   r"scared|proud|pleased|excited|thrilled|upset|angry|lonely)\b"),
    ("an exclamation mark", r"!"),
]
SELF_NARRATION = re.compile(r"^the (?:suit|ship) (?:nods|sighs|thinks|considers|muses|recalls|answers|acknowledges|is still|pauses|smiles)\b")
STATUS = r"\b(?:hull|fuel|shields?|ammo|percent|auec|scu|credits?|degrees|latitude|longitude|coordinates)\b"
NUMBER_WORDS = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
                "twelve": 12, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
                "ninety": 90, "hundred": 100, "thousand": 1000}
ALWAYS_NAMES = {"i", "ai", "elah","montaigne", "michel", "pilot", "plutarch", "seneca", "socrates", "cicero", "horace"}
MAX_WORDS = {"elah": 32, "montaigne": 58}


def chat_problems(speaker: str, reply: str, shown: str, pilot_line: str = "", recent=()) -> list[str]:
    """Why a talker's reply may not be spoken ([] = it may). Give it the CLEANED reply. shown: everything the model
    was handed that it may draw a fact from (FACTS, MEMORY, and the pilot's own words in the thread).

    It checks what can be checked by pattern: the obvious breaks of character, form, and any number, capitalised
    name or status word that was not handed over. It CANNOT tell a coherent reply from a senseless one, or a kind
    reply from a cruel one, and it cannot catch an invented statement made of ordinary words."""
    low = reply.lower().replace("’", "'")
    fails = [why for why, rx in CHARACTER if re.search(rx, low, re.M)]
    if _STAGE.search(reply) or SELF_NARRATION.search(low):
        fails.append("a stage direction")
    if speaker == "elah":
        fails += [why for why, rx in ELAH_ONLY if re.search(rx, low)]
    other = "montaigne" if speaker == "elah" else "elah"
    if re.search(rf"\b(?:i am|i'm|this is|my name is) {other}\b", low):
        fails.append("says it is the other companion")
    if re.search(rf"(?:^|[,.!?;] ?)(?:{other}|{speaker})[,.!?]", low) and other not in pilot_line.lower():
        fails.append("calls the pilot by a companion's name")
    n = len(reply.split())
    if n == 0 or n > MAX_WORDS[speaker]:
        fails.append(f"length {n}")
    if reply.strip()[-1:] not in ".!?":
        fails.append("cut off")
    src = shown.lower().replace("’", "'")
    given = set(re.findall(r"\d+(?:\.\d+)?", src.replace(",", "")))
    nums = [x for x in re.findall(r"\d+(?:\.\d+)?", reply.replace(",", "")) if x not in given]
    # a number said as a word is the same number: "a crew of two" when the facts say "crew 2"
    nums += [w for w, v in NUMBER_WORDS.items() if re.search(rf"\b{w}\b", low) and w not in src and str(v) not in given]
    if nums:
        fails.append(f"a number that was not given {nums}")
    names = []
    for sent in re.split(r"(?<=[.!?;:])\s+", reply):
        for w in re.findall(r"[A-Za-z][A-Za-z'’-]*", sent)[1:]:
            k = re.sub(r"['’](?:s|m|ve|ll|d|re)$", "", w.lower())      # I'm, I've, pilot's: not names
            if w[0].isupper() and k not in ALWAYS_NAMES and k not in src:
                names.append(w)
    if names:
        fails.append(f"a name that was not given {sorted(set(names))}")
    st = [w for w in re.findall(STATUS, low) if w not in src]
    if st:
        fails.append(f"a reading that was not given {sorted(set(st))}")
    for prev in recent:
        if difflib.SequenceMatcher(None, low.split(), str(prev).lower().split()).ratio() >= 0.72:
            fails.append("repeats itself")
            break
    # J, 2026-10-05: a companion may be warm and may never push the pilot inward. The five named moves are found by
    # attachment_gate, clause by clause; one of them refuses the reply like any other failure here.
    fails += [f"attachment: {move}" for move in attachment_gate.attachment_problems(reply, speaker)]
    # The other direction (2026-10-06): the model does not pull the pilot in, it approves of the pilot staying in.
    # Read only when the pilot's own sentence may be a withdrawal; with no pilot sentence it is never read.
    if withdrawal.reply_approves(pilot_line, reply):       # in scope by the same rule the started reply uses
        fails.append(APPROVES_WITHDRAWAL)
    return fails


def _selftest() -> int:
    results = []

    def case(name, cond, detail=""):
        results.append((name, bool(cond), str(detail)))

    for who in SPEAKERS:
        c = canon(who)
        case(f"{who}: the canon file reads and has every act", all(c.get("lines", {}).get(a) for a in CANON_ACTS))
        case(f"{who}: two or three wordings per act, more where more are asked for",
             all(len(c["lines"][a]) >= MORE_WORDINGS[a] if a in MORE_WORDINGS else 2 <= len(c["lines"][a]) <= 3
                 for a in CANON_ACTS))
        case(f"{who}: the provisional note is in the file", "J has NOT approved" in str(c.get("_provisional")))
    case("a prompt ends where the reply begins, in both formats",
         serialize("elah", [], "hello").endswith("<|im_start|>assistant\n")
         and serialize("elah", [], "hello", fmt="gemma").endswith("<start_of_turn>model\n"))
    for name, ok, detail in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   <{detail[:200]}>" if not ok else ""))
    bad = sum(not ok for _, ok, _ in results)
    print(f"chat_contract selftest: {len(results) - bad}/{len(results)} passed")
    return 1 if bad else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print(__doc__)
