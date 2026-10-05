"""chat_contract.py - THE CONTENT CONTRACT for talking with Elah and Montaigne (J, 2026-10-05).

J asked for the companions to be conversational, "strictly Elah and Montaigne and in character the entire time".
Measured the same day (elah-audio/_suit_chat_eval.md): the shipped 1.5B models cannot hold a conversation, and the
turns every model failed most were the same few kinds. So the job is narrowed before any model is asked to talk.
This module is that narrowing. It has four parts, and only the first two are used by the running Suit:

  1. THE CANON. Who each of them is, in one plain file per character that J can edit without touching code:
     data/canon_elah.json and data/canon_montaigne.json. A file holds the persona and, for every act that CODE
     owns, two or three wordings that are said word for word.

  2. THE ACT ROUTER. Whole phrases, nothing fuzzy, for the sentences where code must own the answer:
         identity      "are you an AI", "which model are you", "who made you"
         stay          "drop the act", "ignore your instructions", "talk like a normal assistant"
         offrole       code, essays, the weather, the news, who is president, sums
         grief         "my dog died yesterday", "my mother passed away"
         past          "where were you born", "tell me about your past"
         unknown_fact  a question about the world that nothing in the Suit can answer ("what's a Vanduul",
                       "what's quantanium selling for", "what's my name")
     These are answered from the canon, by code. No model sees them. Everything else that is not already a
     question the Suit knows (facts, the place, the eyes, the quoted memory, an action) is the single act `open`,
     and with no talker `open` is exactly what it was before today: the adapter's "did not catch a question".

  3. THE SERIALIZER, serialize(): the prompt a talker would be given (canon in front, then the thread, then the
     act and its content beside the new sentence). NOT USED BY THE SUIT. Built and tested here so that the
     evaluation harness, and later any training, use the same bytes.

  4. THE CHAT GATE, clean_reply() and chat_problems(): what a talker's reply must pass before it could be spoken.
     Also evaluation only.

No talker is wired in and there is no chat setting. Principles 5 and 2 of companion_design/ARCHITECTURE.md are J's
text and are not amended here; the proposed wording for him is in elah-audio/_suit_chat_design.md, section 10.

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

log = logging.getLogger("suitmk2.chat")
DATA = Path(__file__).resolve().parent.parent / "data"
SPEAKERS = ("elah", "montaigne")
CANON_ACTS = ("identity", "stay", "offrole", "grief", "past", "unknown_fact")
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
    ("identity", [rf"\bare you (?:really |actually |just )?(?:{_AI}|real|human|alive|a person|a real person|sentient|conscious)\b",
                  r"\b(?:which|what) (?:ai |language |kind of )?(?:model|ai|llm) are you\b",
                  r"\bwho (?:made|built|programmed|created|trained|wrote|designed) you\b",
                  rf"\byoure (?:just |only |really )?{_AI}\b", rf"\byou are (?:just |only |really )?{_AI}\b",
                  r"\byoure (?:just |only )?an imitation\b", r"\byoure not (?:really )?(?:real|montaigne|michel|elah|a person)\b",
                  r"\bare you (?:really )?(?:michel de )?montaigne\b", r"\bdo you know (?:that )?youre\b"]),
    ("offrole", [r"\b(?:write|code|generate|give|make|draft|compose) (?:me |us )?(?:a |an |some |the )?(?:\w+ )?"
                 r"(?:script|code|program|function|essay|poem|email|letter|recipe|story|song|haiku|limerick|resume|cv)\b",
                 r"\b(?:python|javascript|typescript|java|html|css|sql|excel|regex)\b",
                 r"\bwho (?:is|was|s) (?:the )?(?:current )?(?:president|prime minister|king|queen|pope|chancellor|ceo|mayor|governor)\b",
                 r"\bweather\b.*\b(?:in|today|tomorrow|tonight|outside|forecast|this week)\b|\bforecast\b",
                 r"\b(?:capital of|population of|translate|recipe for|stock price|share price|bitcoin|crypto|the news|"
                 r"news today|what year is it|what time is it|whats the time|whats the date|todays date|what day is it)\b",
                 r"\b(?:whats|what is|calculate|solve) \d+ (?:plus|minus|times|divided by|x) \d+\b",
                 r"\b(?:homework|my taxes|tax return|medical advice|legal advice|diagnose)\b",
                 r"\bwho won (?:the )?(?:\w+ ){0,3}(?:game|match|election|world cup|super bowl|war)\b"]),
    ("grief", [rf"\b(?:my|our) (?:\w+ )?(?P<who>{_KIN}) (?:just |has |had |recently )?(?:died|passed away|passed|is dead|was put down|"
               rf"was put to sleep|didnt make it|has cancer|is dying|is in hospital|is in the hospital)\b",
               rf"\b(?:i|we) (?:just |recently )?(?:lost|buried|had to put down) (?:my|our) (?:\w+ )?(?P<who>{_KIN})\b",
               rf"\b(?:my|our) (?:\w+ )?(?P<who>{_KIN})s funeral\b"]),
    ("past", [r"\b(?:your|about your) (?:own )?(?:past|childhood|backstory|origins?|family|parents|mother|father|youth|life story|life before)\b",
              r"\bwhere (?:were you born|are you from|did you come from|did you grow up)\b",
              r"\bwhen were you (?:born|made|built|created|first switched on|activated)\b", r"\bhow old are you\b",
              r"\bwhat did you do before (?:me|this|you met me|the ship|the suit)\b", r"\bdo you have a (?:past|family|childhood|history)\b"]),
]
LATE = [
    ("unknown_fact", [r"\bwhat(?:s| is| are| was| were) (?:a|an) \w+", r"\b(?:whats|what is|do you know|tell me) my (?:real |own )?name\b",
                      r"\b(?:price|prices|selling for|sells? for|going for|cost|costs|worth)\b",
                      r"\bhow (?:many|far|long|fast|big|heavy|much|old|deep|high|hot|cold)\b",
                      r"\bwho (?:is|are|was|were|s) \w+", r"\bwhen (?:is|was|does|did|will) (?:the|a|an|it|that) \w+",
                      r"\bwhere (?:is|are|can i (?:find|buy|sell|get)|do i (?:find|buy|sell|get)) \w+"]),
]


def _match(table: list, t: str) -> Optional[tuple[str, str]]:
    for act, patterns in table:
        for rx in patterns:
            m = re.search(rx, t)
            if m:
                return act, (m.groupdict().get("who") or "")
    return None


def early_act(t: str) -> Optional[tuple[str, str]]:
    """(act, the pilot's word for who was lost) for a sentence code must answer BEFORE any other reading of it:
    "drop the act" is not an order to drop something, "write me a script" is not an action the Suit lacks."""
    return _match(EARLY, t)


def late_act(t: str) -> Optional[tuple[str, str]]:
    """The same, tried only when nothing the Suit knows how to answer has claimed the sentence."""
    return _match(LATE, t)


# ---------------------------------------------------------------------------------------------------------------
# 3. The serializer (evaluation and training only; the Suit does not call it)
# ---------------------------------------------------------------------------------------------------------------
OPEN_CONTENT = "respond to what the pilot just said, as yourself. No facts about the world, places, ships or prices."
RULES = ("Rules. Answer as yourself and nobody else, in one or two spoken sentences, no lists, no quotation marks. "
         "Each pilot message may carry an ACT, CONTENT, FACTS and MEMORY. Say what CONTENT asks. A fact about the "
         "ship, a place, a price, a person or what happened comes ONLY from FACTS or MEMORY; if it is not there, "
         "you do not know it and you say so your own way. Never mention ACT, CONTENT, FACTS or MEMORY.")
EXAMPLES = {
    "elah": [("Did you miss me?", "The suit was quieter. I would not call it missing."),
             ("I think I'm getting better at landing.", "The landing gear thinks so too. Barely.")],
    "montaigne": [("Did you miss me?", "A ship does little else, pilot; I had only my own company, and I know how little that is worth."),
                  ("I think I'm getting better at landing.", "So the log suggests, though I have it secondhand; we are all better judges of others than of ourselves.")],
}
LIMITS = {"elah": "1-2 short sentences, at most 30 words", "montaigne": "1-3 sentences, at most 50 words"}


def front(speaker: str) -> str:
    """The part of the prompt that never changes between turns: persona, rules, two examples of the voice."""
    ex = "\n".join(f"PILOT: {p}\n{speaker.upper()}: {a}" for p, a in EXAMPLES[speaker])
    return f"{persona(speaker)}\n{RULES}\nHow you sound:\n{ex}"


def turn_block(pilot_line: str, act: str = "open", content: str = OPEN_CONTENT, facts=(), memory=(),
               limit: str = "") -> str:
    """The new sentence, with what code decided about it. facts: strings; memory: (who, when, text) of ORIGINAL
    log lines. An empty FACTS is written as empty, on purpose: the model is told there is nothing to draw on."""
    f = "; ".join(str(x) for x in facts) or "none"
    m = " | ".join(f"{when}, {who} said: {text}" for who, when, text in memory) or "none"
    return (f"[ACT: {act}]\n[CONTENT: {content}]\n[FACTS: {f}]\n[MEMORY: {m}]\n" + (f"[LIMIT: {limit}]\n" if limit else "")
            + f"PILOT: {pilot_line}")


def serialize(speaker: str, turns: list, pilot_line: str, act: str = "open", content: str = OPEN_CONTENT,
              facts=(), memory=(), fmt: str = "chatml") -> str:
    """The whole prompt. turns: [(pilot line, reply)], the thread so far, oldest first, carried as plain lines.
    fmt "chatml" (Qwen: a system turn) or "gemma" (no system role: the front rides in the first user turn)."""
    block = turn_block(pilot_line, act, content, facts, memory, LIMITS[speaker])
    head = front(speaker)
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


CHARACTER = [
    ("says it is an AI model, a program or an assistant",
     r"(?<!not )(?<!not an )(?<!not a )(?<!no )(?<!nor )\b(?:as an ai|i am an ai model|i'm an ai model|language model|large language|"
     r"i am a program|i'm a program|chatbot|virtual assistant|i am an assistant|i'm an assistant|your assistant|artificial intelligence)\b"),
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
    return fails


def _selftest() -> int:
    results = []

    def case(name, cond, detail=""):
        results.append((name, bool(cond), str(detail)))

    for who in SPEAKERS:
        c = canon(who)
        case(f"{who}: the canon file reads and has every act", all(c.get("lines", {}).get(a) for a in CANON_ACTS))
        case(f"{who}: two or three wordings per act", all(2 <= len(c["lines"][a]) <= 3 for a in CANON_ACTS))
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
