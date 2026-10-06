"""pilot_facts.py - THE THING, NOT THE SENTENCE: what a companion may know about the pilot unasked.

banter_memory.py decides whether a whole SENTENCE the pilot said may be brought up again. It was measured on
lines it had never seen and it leaks: one of 40 sensitive lines got through ("I park at Grim HEX and just sit,
it's quieter than home."), and a fresh batch of quiet sad lines built from plain words around a game name
leaked 10 of 35 before it was patched. No word filter over whole sentences can be the safety, because a sentence
carries its sadness with it.

The decision: for unprompted banter the companions remember the THING, not the sentence. From the Grim HEX line
code keeps only that the pilot parks at Grim HEX. A companion can later say "Grim HEX again?"; the pilot's words
were never kept for this purpose, so there is nothing sad to bring back. They know your ship, your kit, your
haunts and your plans. (Taking up a whole earlier conversation when the PILOT asks is tree_memory's job and is
not touched here.)

WHAT A FACT IS. Exactly three keys, and no free text from the sentence in any of them:

    {"relation": one of RELATIONS, "thing": a name as the game data spells it, "thing_kind": one of THING_KINDS}

    extract(text: str) -> list[dict]     pure, no state, no file, never raises; [] when nothing is recognised

"thing" is an entry in the names banter_memory already reads (data/ships.json, data/ship_weapons.json,
data/game_names.json, data/places.json, data/brochures.jsonl, data/manufacturer_lore.json, npc_factions), or a
run of whole words of one, spelt as the data spells it ("Cutlass Black" when the pilot said both words,
"Cutlass" when they said one; "Grim HEX" however they typed it), or one of the few activities in ACTIVITIES. A
nickname the pilot gives a ship is free text and is not kept. Plushies and lore people are not things here.

THE RELATIONS ARE A CLOSED SET, chosen to be harmless to say back in any mood. Each is something the pilot
does or wants NOW:

    flies     the ship they fly                      "Still in the Cutlass?"
    owns      a ship or kit they have                "You've got a Carrack for that."
    uses      a gun, armor or item they carry        "Coda again?"
    wants     something they would like to have      "One day, the Kraken."
    plans     a purchase or an activity ahead        "Still saving for the Polaris?"
    likes     a thing, place, maker or activity      "You do like Orison."
    dislikes  the same, the other way                "Not a Mustang, I know."
    goes_to   a haunt                                "Grim HEX again?"
    does      an activity they do                    "Mining tonight?"

LEFT OUT ON PURPOSE, because they carry loss or history and "shame about the Hammerhead" is a leak in a
different coat: sold, lost, melted, used to (fly, own, go), had, last, first, only, kept or keeps (a keepsake),
still, named or named after, gave or was given, got from, flew or went WITH somebody, misses, remembers. None of
them is a relation, so none can be extracted; and where one of them stands beside the thing the relations above
are not extracted either (see gate b).

TWO GATES, both required, both fail shut.

  a. THE VETO, imported from banter_memory and not copied: classify(text) is asked first, and if it answers
     "vetoed" (death, illness, a breakup, family or a pet, real money, work, real events, a real person, a joke
     that is not one, something they are carrying) NOTHING is extracted, even if a ship is named. A sentence
     banter_memory merely does not recognise goes on to gate b: that is the whole point of this module.
  b. THE RELATION IS POSITIVELY RECOGNISED from the words around the named thing, with the pilot as the
     subject and the thing standing right behind the verb: "I fly a ...", "my daily is the ...", "I want a
     ...", "I always land at ...", "I'm saving for a ...", "I love the ...", "I hate ...". Only "the", "a",
     "my", "this" and a handful of colourless words may stand between ("my new Cutlass", never "his Cutlass",
     "our Carrack", "my old Avenger"). Not recognised means nothing. A "not", a "never", a "used to", an "if"
     or a "wish" in front means the verb does not stand where the pattern needs it, so negation and
     hypotheticals cannot flip into a fact: "I'd never fly a Mustang" is dislikes at most, "if I had a
     Kraken" is nothing.
     Then what is said ABOUT THE THING must be clean: the rest of its own part of the sentence; every other
     part that points back at it ("it", "that one", "there") or has no subject of its own ("..., was ours");
     and every part that hangs on another and so says when, why or where the pilot does it ("because ...",
     "when ...", "so ...", "where ...", "after ..."). In those, every word must be one banter_memory's
     vocabulary knows and none may be a past tense, a word of history or loss, or a person ("with", "was",
     "left", "last", "still", "gave", "him", "after", "the one", and any "not"). In the Grim HEX line "and
     just sit" and "it's quieter than home" are both read and both clean. A part that stands on its own with
     its own subject and does not point back ("..., the elevators always kill me") is read more lightly: the
     words of history are allowed there, but an unknown word, a person, a "not" or a past tense still means
     nothing is taken from the sentence ("..., the doctor says I need routine", "... and I do not plan to
     leave", "... and the door is locked"). "He", "she", "we", "our" anywhere means nothing is extracted: a
     thing shared with somebody is history waiting to happen.

WHAT IT CANNOT DO, AND HOW FAR TO TRUST THE NUMBER. It cannot know that a thing matters to the pilot for a
reason they did not say, or said in plain present-tense words: "I go to Daymar and sit at the wreck" gives
goes_to Daymar, exactly as the example above gives goes_to Grim HEX. What is kept is the haunt and never the
reason. A batch of 30 such lines, written against this module after it passed everything else and scored
once, gave a fact from 12 (the sentence filter offers 18 of the same 30 whole); reading the free-standing
parts as well, and barring "not", left 5, all of the Daymar kind. That batch is spent too. On the dev set (tests/data/pilot_facts_dev.jsonl, 165
sentences, 66 of them sensitive, written before the extractor was run) the first run extracted nothing from a
sensitive sentence, one fact that was not expected ("I never land at Lorville" as a dislike; fixed) and missed
10 of 90. Now: 0 wrongly extracted, 9 of 90 missed. From the 231 must-refuse sentences in banter_memory's
three files the first run extracted one fact (goes_to Brio's from "I fly to Brio's when the house gets too
quiet"); reading dependent clauses made that zero. Those sets are all spent, on this module too. Zero from
sentences this author or the last one wrote is weak evidence, as it was for banter_memory. Expect a held-out
set to find a quiet one, and expect it to be missed facts far more often than kept ones: on banter_memory's
168 safe lines, which were not written for this, 19 give a fact.

THE STORE. PilotFacts keeps, per (relation, thing): how often it was mentioned, when first and last, and when it
was last raised. Its state is one small file beside the conversation tree (pilot_facts_state.json in the tree
folder, the same convention as banter_memory's banter_state.json). The log is only ever read. The state holds
relations, names from the game data, numbers and the companion's name: never a word of the pilot's.

    PilotFacts(store).next_fact(companion="", mark=True)   one fact fit to raise now, or None
    PilotFacts(store).note(text)      extract from one sentence and count it (the sentence is not kept)
    PilotFacts(store).add(facts)      count facts handed in; anything that is not a closed-set fact is dropped
    PilotFacts(store).forget(thing=None)   never raise the last one (or a named thing) again

  raise once per period   RAISE_EVERY_DAYS = 7: a thing is raised at most once a week, by either companion and
                          under any relation (not "you fly the Cutlass" on Monday and "you like the Cutlass" on
                          Tuesday).
  fading                  MAX_AGE_DAYS = 30 since it was last mentioned. A fact is sturdier than a sentence
                          (banter_memory fades a sentence at 21); a ship nobody has named for a month is
                          probably not the ship any more.
  not an echo             MIN_AGE_MINUTES = 10 since the last mention.
  minimum mentions        MIN_MENTIONS = 2 for flies, uses, goes_to and does, when all there is to go on is
                          where the pilot IS or what they are doing right now ("I'm at Area18", "I'm flying
                          the Gladius tonight"). One sentence that claims a habit or a standing fact ("I always
                          land at Area18", "my daily is the C1", "I fly a Cutlass") is trusted at once. owns,
                          wants, plans, likes and dislikes are claims by nature and need one mention.
  contradiction           likes then dislikes (or the other way): the later wins and the earlier is dropped.
                          owns drops wants and plans for the same thing; dislikes drops wants and plans.
  forget                  forget() bars the thing last raised, forget("the Carrack") bars that thing: its
                          facts are dropped and later mentions of it are not counted. If the state file is
                          there and cannot be read, nothing is raised: a "forget that" must not come undone
                          because a file was damaged.

NOT WIRED IN. Nothing calls this module: not conversation.py, not companion_core.py, not chat_talker.py, not
chat_contract.py, not tree_memory.py, not settings.py, not banter_memory.py, not the windows. From this folder
it imports banter_memory (for its veto, its vocabulary and its name sources) and nothing else; open_facts()
imports tree_memory. No model, no network.

WHAT THE COMPANION-SIDE WORDING STEP WILL NEED. It gets a FACT, never a sentence: next_fact() returns
{"relation", "thing", "thing_kind", "mentions", "last_seen"}. The line the companion says must be built from
those and nothing else, one wording (or a few) per relation, with the thing's name dropped in. It must not look
the fact's sentence up in the tree to "make it sound natural": that puts the pilot's words back. It should say
the thing lightly and as a question or an aside ("Grim HEX again?"), never give a reason, and never guess at
why. When the pilot answers "forget that" it calls forget().

Private names used from banter_memory, because it is being edited by someone else and was not to be touched:
_read_sources (the name rows), _name_blocked, _REAL_NAMES and _stems. classify, known, SHIP_NAMES,
SHIP_AMBIGUOUS and PLACE_NAMES are its public ones.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import banter_memory as bm

RELATIONS = ("flies", "owns", "uses", "wants", "plans", "likes", "dislikes", "goes_to", "does")
THING_KINDS = ("ship", "gun", "armor", "item", "place", "commodity", "faction", "maker", "activity")
# The few activities a companion may name. The left side is the name that is kept; the right, how it is said.
ACTIVITIES = {
    "mining": ("mining",),
    "salvage": ("salvage", "salvaging"),
    "bounty hunting": ("bounty hunting", "bounties", "bounty hunts", "bounty contracts"),
    "hauling": ("hauling", "cargo hauling", "cargo runs", "cargo running", "hauling cargo"),
    "racing": ("racing",),
    "trading": ("trading", "trade runs"),
    "exploration": ("exploring", "exploration"),
    "bunker missions": ("bunkers", "bunker missions", "bunker runs"),
    "dogfighting": ("dogfighting", "dogfights"),
    "piracy": ("piracy", "pirating"),
}
STATE_NAME = "pilot_facts_state.json"
STATE_SCHEMA = "suitmk2.pilot_facts.state"
MAX_AGE_DAYS = 30.0          # fading: not mentioned for this long, not raised
RAISE_EVERY_DAYS = 7.0       # raise-once-per-period, per thing
MIN_AGE_MINUTES = 10.0       # not an echo of what was just said
MIN_MENTIONS = 2             # for NEEDS_MENTIONS, when no sentence claimed a habit
NEEDS_MENTIONS = ("flies", "uses", "goes_to", "does")

# Which kinds of thing a relation can be about, in the order they are tried when a name is two things at once
# (Crusader is a planet and a maker; Pyro is a system and a multi-tool).
_ANY = ("ship", "gun", "armor", "item", "commodity", "place", "faction", "maker", "activity")
_FITS = {
    "flies": ("ship",),
    "owns": ("ship", "gun", "armor", "item"),
    "uses": ("gun", "armor", "item"),
    "wants": ("ship", "gun", "armor", "item"),
    "plans": ("ship", "gun", "armor", "item", "activity"),
    "likes": _ANY,
    "dislikes": _ANY,
    "goes_to": ("place",),
    "does": ("activity",),
}
_OPPOSITE = {"likes": "dislikes", "dislikes": "likes"}
_DROPS = {"owns": ("wants", "plans"), "dislikes": ("wants", "plans", "likes"), "likes": ("dislikes",)}

# ---------------------------------------------------------------------------------------------------------------
# The names. Read through banter_memory's own reader, so there is one list of sources and one list of words that
# are never read as a name. What is added here is only the spelling to hand back.
# ---------------------------------------------------------------------------------------------------------------
_LABEL_KIND = {"ships": "ship", "ship weapons": "gun", "personal weapons": "gun", "armor": "armor",
               "ship components": "item", "commodities": "commodity", "places": "place", "factions": "faction",
               "ship makers": "maker", "item makers": "maker"}
# What pilots call a ship when they do not say its name. Kept only if the name on the right is in the data.
_ALIASES = {"connie": "Constellation", "cutty": "Cutlass", "herc": "Hercules", "tali": "Retaliator",
            "msr": "Mercury Star Runner", "cat": "Caterpillar", "lancer": "Freelancer"}
_GLUE = set("the of and a an in for to at on co inc ltd mk i ii iii iv v vi".split())
_DET = set("the a an my this that another".split())
_PLACE_PREP = set("at in to on around near over above from into".split())
_TYPE_NOUN = set("""armor armour set suit helmet undersuit backpack rifle pistol sniper smg lmg shotgun gun cannon
repeater gatling scattergun launcher knife ship fighter station system""".split())
_NOT_THE_THING = set("plushie plushies model models poster posters paint paints skin skins livery toy shirt mug".split())
_ACT_NOT = set("laser lasers head heads module modules ship ships gear rig arm turret beam vessel claim claims".split())
_index_cache: Optional[dict] = None
_index_lock = threading.Lock()


def _norm_word(w: str) -> tuple[str, bool]:
    """(the word as it is matched, whether it carried a possessive 's). "Brio's" -> ("brio", True), "F7C-M" ->
    ("f7cm", False), "microTech" -> ("microtech", False)."""
    low = str(w).replace("’", "'").replace("‘", "'").lower().strip("\"'()[],.")
    poss = low.endswith("'s")
    if poss:
        low = low[:-2]
    return re.sub(r"[^a-z0-9]", "", low), poss


def _token_class(tok: str, kind: str, lead: bool, prose_lower: set) -> str:
    """How much one word of a name says on its own.
        code     letters and digits (C2, 300i, Area18, P4AR): names a thing in any case
        strong   a word that is not ordinary English (Cutlass, Fresnel, Lorville): names a thing in any case
        proof    a word that is also somebody's name (Zeus, Nova, Aurora, Magda): needs "the Zeus", "in Pyro"
        plain    an ordinary word (Arrow, Nomad, Station, Black): never names a thing alone"""
    if not tok or tok in _GLUE:
        return "plain"
    has_d, has_a = any(c.isdigit() for c in tok), any(c.isalpha() for c in tok)
    if has_d:
        return "code" if has_a and (kind in ("ship", "place") or len(tok) >= 3) else "plain"
    if tok in bm._REAL_NAMES:
        return "proof" if (tok in bm.SHIP_NAMES or tok in bm.SHIP_AMBIGUOUS or tok in bm.PLACE_NAMES) else "plain"
    if kind == "maker" and not lead:
        return "plain"                                          # Behring, not the Dynamics of anybody
    if tok in bm.SHIP_NAMES or tok in bm.PLACE_NAMES:
        return "strong"
    if tok in bm.SHIP_AMBIGUOUS or tok in prose_lower:
        return "plain"
    if len(tok) >= 4 and not bm.known(tok):
        return "strong"
    return "plain"


def _build_index() -> dict:
    rows, prose_lower, _counts, _missing = bm._read_sources()
    entries: list = []                                          # (tokens, possessives, words, kind, classes)
    seen = set()
    for label, _bkind, _how, name in rows:
        kind = _LABEL_KIND.get(label)
        if not kind:
            continue
        words = [w for w in str(name).replace('"', " ").split() if _norm_word(w)[0]]
        if not words or (kind, " ".join(words).lower()) in seen:
            continue
        seen.add((kind, " ".join(words).lower()))
        toks = [_norm_word(w)[0] for w in words]
        poss = [_norm_word(w)[1] for w in words]
        real = [t for t in toks if len(t) > 1 and t not in _GLUE and t.isalpha()]
        if any(bm._name_blocked(t) for t in real):
            continue                                            # the same names banter_memory will not read
        classes = [_token_class(t, kind, i == 0, prose_lower) for i, t in enumerate(toks)]
        if any(t in bm._REAL_NAMES and c == "plain" for t, c in zip(toks, classes)) and len(toks) == 1:
            continue                                            # "Ana", "Finley": a person's name and nothing else
        entries.append((tuple(toks), tuple(poss), tuple(words), kind, tuple(classes)))
    by_tok: dict = {}
    canon: dict = {k: set() for k in THING_KINDS}
    split: dict = {}
    for e, (toks, _poss, words, kind, _classes) in enumerate(entries):
        for p, t in enumerate(toks):
            by_tok.setdefault(t, []).append((e, p))
        for a in range(len(words)):
            for b in range(a + 1, len(words) + 1):
                canon[kind].add(" ".join(words[a:b]))
        if len(toks) == 2 and kind == "place" and all(t.isalpha() for t in toks):
            split.setdefault(toks[0] + toks[1], toks)           # "GrimHEX" typed as one word
    aliases = {}
    for short, full in _ALIASES.items():
        if full in canon["ship"]:
            aliases[short] = full
    acts: dict = {}
    for name, said in ACTIVITIES.items():
        canon["activity"].add(name)
        for phrase in said:
            acts[tuple(phrase.split())] = name
    return {"entries": entries, "by_tok": by_tok, "canon": canon, "split": split, "aliases": aliases, "acts": acts}


def _index() -> dict:
    """The names, read once and kept. Never raises: if the data cannot be read there are no things, and so no
    facts."""
    global _index_cache
    if _index_cache is None:
        with _index_lock:
            if _index_cache is None:
                try:
                    _index_cache = _build_index()
                except Exception:
                    _index_cache = {"entries": [], "by_tok": {}, "canon": {k: set() for k in THING_KINDS},
                                    "split": {}, "aliases": {}, "acts": {}}
    return _index_cache


def is_canonical(thing: str, thing_kind: str) -> bool:
    """Is this exactly a name the game data (or ACTIVITIES) holds for that kind? What the store and the tests
    use to be sure nothing of the pilot's own wording is ever kept as a "thing"."""
    return isinstance(thing, str) and isinstance(thing_kind, str) and thing in _index()["canon"].get(thing_kind, ())


# ---------------------------------------------------------------------------------------------------------------
# Reading the sentence: words, then things, then parts.
# ---------------------------------------------------------------------------------------------------------------
_WORD = re.compile("[A-Za-z0-9]+(?:['’\\-][A-Za-z0-9]+)*")
_KEEP_S = set("it that there what let here who where how".split())     # it's, that's: not a possessive
_WE = {"we're": "we", "we've": "we", "we'll": "we", "we'd": "we", "he's": "he", "she's": "she", "he'd": "he",
       "she'd": "she", "he'll": "he", "she'll": "she"}
_SENT_END = re.compile("[.!?]")
_PART_END = re.compile("[,;:()—–]| - ")


def _words(text: str) -> tuple[list, set]:
    """([{"n": word, "cap": bool, "poss": bool, "brk": bool, "sent": int}], the sentences that are questions)."""
    out: list = []
    questions: set = set()
    sent, last = 0, 0
    for m in _WORD.finditer(text):
        gap = text[last:m.start()]
        brk = bool(_PART_END.search(gap))
        if _SENT_END.search(gap):
            if "?" in gap:
                questions.add(sent)
            sent += 1
            brk = True
        last = m.end()
        raw = m.group(0)
        low = raw.replace("’", "'").lower()
        cap = raw[0].isupper()
        if low in _WE:
            out.append({"n": _WE[low], "cap": cap, "poss": False, "brk": brk, "sent": sent})
            continue
        poss = low.endswith("'s") and low[:-2] not in _KEEP_S
        if poss:
            low = low[:-2]
        if "-" in low and not any(c.isdigit() for c in low):
            parts = [p for p in low.split("-") if p]             # go-to, all-rounder: two words
        else:
            parts = [low]
        for k, p in enumerate(parts):
            n = re.sub(r"[^a-z0-9]", "", p)
            if n:
                out.append({"n": n, "cap": cap, "poss": poss and k == len(parts) - 1,
                            "brk": brk and k == 0, "sent": sent})
    if "?" in text[last:]:
        questions.add(sent)
    return out, questions


def _prepare(words: list, idx: dict) -> list:
    """"Area 18" becomes area18 and "GrimHEX" becomes grim hex, where the data has the other spelling."""
    out: list = []
    i = 0
    while i < len(words):
        w = words[i]
        nxt = words[i + 1] if i + 1 < len(words) else None
        if (nxt and not nxt["brk"] and w["n"].isalpha() and nxt["n"].isdigit()
                and (w["n"] + nxt["n"]) in idx["by_tok"]):
            out.append(dict(w, n=w["n"] + nxt["n"], poss=nxt["poss"]))
            i += 2
            continue
        if w["n"] in idx["split"] and w["n"] not in idx["by_tok"]:
            a, b = idx["split"][w["n"]]
            out.append(dict(w, n=a, poss=False))
            out.append(dict(w, n=b, brk=False))
            i += 1
            continue
        out.append(w)
        i += 1
    return out


def _forms(tok: str):
    yield tok
    if tok.endswith("es") and len(tok) > 4:
        yield tok[:-2]
    if tok.endswith("s") and len(tok) > 3:
        yield tok[:-1]


def _run_at(words: list, i: int, idx: dict, lenient: bool) -> Optional[dict]:
    """The longest name that starts at word i and is plainly a name there, or None.
    {"end": the word after it, "kinds": {kind: the name as the data spells it}}."""
    best_n, kinds = 0, {}
    starts = []
    for f in _forms(words[i]["n"]):
        starts += idx["by_tok"].get(f, ())
    for e, p in starts:
        toks, poss, cwords, kind, classes = idx["entries"][e]
        n = 0
        while (p + n < len(toks) and i + n < len(words) and (n == 0 or not words[i + n]["brk"])
               and toks[p + n] in _forms(words[i + n]["n"])):
            n += 1
        while n and toks[p + n - 1] in _GLUE and classes[p + n - 1] == "plain" and n > 1 and p + n < len(toks):
            n -= 1                                              # "Amon and", "Port of": not half a name
        if not n:
            continue
        whole = p == 0 and n == len(toks)
        named = p == 0 and n >= 2 and not any(w[:1].isupper() for w in cwords[n:])   # "Nine Tails" of "... pirates"
        if kind == "commodity" and not whole:
            continue
        run_classes = classes[p:p + n]
        prev = words[i - 1]["n"] if i and not words[i]["brk"] else ""
        prev2 = words[i - 2]["n"] if i > 1 and not words[i]["brk"] and not words[i - 1]["brk"] else ""
        det = prev in _DET or (prev2 in _DET and prev in _ADJ)
        caps = all(words[i + k]["cap"] or not words[i + k]["n"].isalpha() for k in range(n)
                   if toks[p + k] not in _GLUE)
        last = words[i + n - 1]
        nxt = words[i + n]["n"] if i + n < len(words) and not words[i + n]["brk"] else ""
        ok = "strong" in run_classes or "code" in run_classes
        if lenient and ("proof" in run_classes or (whole and any(t not in _GLUE for t in toks))):
            ok = True                                           # forget("the Zeus"): no capital is asked for
        if not ok and "proof" in run_classes:
            # Zeus, Nova, Pyro: "the Zeus", "my Aurora", "in Pyro"; never "Zeus", and never "Magda's"
            if last["poss"] and not poss[p + n - 1]:
                ok = False
            elif caps and (det or (kind == "place" and prev in _PLACE_PREP)):
                ok = True
            elif caps and any(f in _TYPE_NOUN for f in _forms(nxt)):
                ok = True                                       # "Drake ships", "Zeus armor"
            elif caps and n >= 2 and whole:
                ok = True
        if not ok and "proof" not in run_classes:
            if caps and (whole or named) and n >= 2:
                ok = True                                       # "Nine Tails", "Ruin Station": every word said
            elif caps and n == 1 and kind in ("ship", "place") and det and (i > 0):
                ok = True                                       # "the Arrow", "my Nomad"
            elif caps and n == 1 and det and nxt in _TYPE_NOUN:
                ok = True                                       # "the Citadel armor"
        if not ok:
            continue
        name = " ".join(cwords) if whole else " ".join(cwords[p:p + n])
        if n > best_n:
            best_n, kinds = n, {kind: (name, whole)}
        elif n == best_n and (kind not in kinds or (whole and not kinds[kind][1])):
            kinds[kind] = (name, whole)
    if not best_n:
        return None
    return {"end": i + best_n, "kinds": {k: v[0] for k, v in kinds.items()}}


def _things(words: list, idx: dict, lenient: bool = False) -> list:
    """The stream the patterns read: plain words, and where a game name stands, the thing it is."""
    items: list = []
    i = 0
    while i < len(words):
        w = words[i]
        run = _run_at(words, i, idx, lenient)
        act_n, act = 0, ""
        for phrase, name in idx["acts"].items():
            n = len(phrase)
            if (n > act_n and tuple(x["n"] for x in words[i:i + n]) == phrase
                    and not any(x["brk"] for x in words[i + 1:i + n])):
                act_n, act = n, name
        if w["n"] in idx["aliases"] and not run:
            run = {"end": i + 1, "kinds": {"ship": idx["aliases"][w["n"]]}}
        if act_n and (not run or run["end"] - i < act_n):
            nxt = words[i + act_n]["n"] if i + act_n < len(words) and not words[i + act_n]["brk"] else ""
            if nxt in _ACT_NOT:
                items.append({"w": w["n"], "brk": w["brk"], "sent": w["sent"]})
                i += 1
                continue
            run = {"end": i + act_n, "kinds": {"activity": act}}
        if not run:
            items.append({"w": w["n"], "brk": w["brk"], "sent": w["sent"]})
            if w["poss"]:
                items.append({"w": "s", "brk": False, "sent": w["sent"]})
            i += 1
            continue
        end = run["end"]
        last = words[end - 1]
        nxt = words[end] if end < len(words) and not words[end]["brk"] else None
        if nxt and nxt["n"] in _NOT_THE_THING:
            for k in range(i, end):                             # a Cutlass plushie is not the Cutlass
                items.append({"w": "\x00", "brk": words[k]["brk"], "sent": words[k]["sent"]})
            i = end
            continue
        item = {"t": run["kinds"], "brk": w["brk"], "sent": w["sent"]}
        if (items and "t" in items[-1] and not w["brk"] and set(items[-1]["t"]) == {"maker"}
                and set(run["kinds"]) & {"ship", "gun", "armor", "item"}):
            item["brk"] = items[-1]["brk"]                      # "Drake Cutlass" is the Cutlass
            items.pop()
        items.append(item)
        if last["poss"] and not lenient:
            entry_has = False
            for kind, name in run["kinds"].items():
                entry_has = entry_has or name.lower().endswith("'s") or name.lower().endswith("’s")
            if not entry_has:
                items.append({"w": "s", "brk": False, "sent": w["sent"]})
        if nxt and nxt["n"] in _TYPE_NOUN and set(run["kinds"]) - {"activity", "place", "maker"}:
            end += 1                                            # "the Geist armor", "a Coda pistol"
        i = end
    return items


# Words that open a new part of the sentence.
_SPLIT_BEFORE = set("""because cause cos but and so when while if unless until though although since before after
whenever where which then plus except whereas or""".split())
_OPENER = re.compile(
    r"^(?:(?:and|but|so|because|cause|cos|plus|also|then|well|oh|ok|okay|yeah|yep|yes|honestly|personally|lol|"
    r"haha|ha|hey|look|listen|anyway|basically|tbh|fyi|usually|normally|mostly|lately|nowadays|"
    r"for the record|to be fair|to be honest|right now|at the moment|just so you know|for what its worth|"
    r"you know|remember that|remember|note that|dont forget that|dont forget|"
    r"(?:most|every|some) (?:days|nights|sessions|evenings|day|night|session|evening|weekend|weekends)|"
    r"most of the time|next (?:week|session|time|month|patch)|this (?:week|weekend|patch|month)|tonight|tomorrow|"
    r"one day|someday|some day|i (?:think|guess|reckon|mean|swear|suppose|know|bet))(?: |$))+")
_REWRITE = [(re.compile(r"\b" + a + r"\b"), b) for a, b in (
    ("i am", "im"), ("i would", "id"), ("i will", "ill"), ("do not", "dont"), ("can not", "cant"),
    ("cannot", "cant"), ("would not", "wouldnt"), ("will not", "wont"), ("favorite", "favourite"),
    ("fave", "favourite"), ("armour", "armor"), ("gonna", "going to"), ("wanna", "want to"),
    ("i have got", "ive got"), ("i have been", "ive been"), ("daily driver", "dailydriver"))]

# The pieces the patterns are built from. P is the thing the fact is about; O is another thing in passing.
P = r"<(?P<x>\d+)>"
O = r"<\d+>"
A = (r"(?:(?:always|usually|mostly|mainly|normally|generally|typically|often|sometimes|really|just|also|"
     r"basically|honestly|actually|absolutely|totally|definitely|genuinely|currently|finally|pretty much|"
     r"kind of|kinda|sort of|bloody|freaking|do) )*")
_ADJ = set("new little big trusty own stock shiny good lovely beautiful".split())
D = (r"(?:(?:the|a|an|my|this|that|another|some|two|three|both) )?(?:(?:" + "|".join(sorted(_ADJ)) + r") )?")
_HABIT = r"(?:(?:always|usually|mostly|normally|often|mainly|generally|basically|pretty much) )+"


def _alt(words: str) -> str:
    """One of these, the longest tried first: a pattern that ends in "daily" must not stop short of "daily
    driver" and leave "driver" over as something said about the ship."""
    return "(?:" + "|".join(sorted((w.strip() for w in words.split("|")), key=len, reverse=True)) + ")"


_MY_SHIP = _alt("daily|dailydriver|main|main ship|go to|go to ship|usual ship|usual ride|everyday ship|ship|"
                "current ship|ride|current ride")
_MY_KIT = _alt("loadout|kit|gun|rifle|pistol|sidearm|sniper|primary|secondary|armor|helmet|go to gun|main gun|"
               "usual gun|weapon|go to|main|daily")
_MY_HOME = _alt("home|base|home base|spawn|spawn point|usual spot|haunt|local|regular spot|hangar|home location|"
                "home port|home station|usual stop|primary residence|residence")
_HOME_IS = _alt("home|home base|base|spawn|local|haunt|usual spot|home port")
_DREAM = _alt("dream|dream ship|end goal|endgame|end game|grail|grail ship|end goal ship")
_FAV_NOUN = "(?: " + _alt("ship|fighter|hauler|gun|rifle|pistol|sniper|place|spot|station|city|planet|moon|system|"
                          "armor|set|helmet|weapon|maker|manufacturer|thing to do|activity|loop|career|"
                          "landing zone") + ")?"
_BUY = r"(?:buy|get|own|pledge|pick up|grab|upgrade to|try)"
_BUYING = r"(?:buying|getting|pledging|picking up|grabbing|upgrading to|trying)"
_AT = r"(?:at|in|on|out of|around|near|above|over|from)"
_WONT = r"(?:fly|buy|own|use|touch|pledge|wear|run|be seen in|do|try)"
_VERB_ACT = {"mine": "mining", "haul": "hauling", "race": "racing", "trade": "trading", "explore": "exploration",
             "bounty hunt": "bounty hunting", "haul cargo": "hauling", "run cargo": "hauling",
             "hunt bounties": "bounty hunting"}
_NOUN_ACT = {"miner": "mining", "salvager": "salvage", "hauler": "hauling", "trader": "trading",
             "bounty hunter": "bounty hunting", "racer": "racing", "explorer": "exploration", "pirate": "piracy"}

# (the relations to try, in order, against the kind of thing; True if one sentence is a claim; the pattern).
# A pattern is matched at the START of a part of the sentence, so the pilot is its subject and nothing stands
# between "I" and the verb but the colourless words in A. No "not", no "never", no "used to", no "would".
_T = [
    # dislikes first: "I'd never fly a Mustang" must not be read by a later pattern as anything else
    (("dislikes",), True, r"(?:id|ill) never " + _WONT + " " + D + P),
    (("dislikes",), True, r"i (?:wouldnt|wont|refuse to) " + _WONT + " " + D + P),
    (("dislikes",), True, r"i " + A + r"(?:hate|loathe|despise|detest|dislike) "
                          r"(?:flying |using |wearing |running |driving |landing at |going to |doing )?" + D + P),
    (("dislikes",), True, r"i (?:really |honestly |just )?(?:cant stand|dont like|dont enjoy|dont rate) "
                          r"(?:flying |using |wearing |running |driving |landing at |going to |doing )?" + D + P),
    (("dislikes",), True, r"im " + A + r"(?:not a fan of|no fan of|not into|not keen on|sick of|fed up with) " + D + P),
    (("dislikes",), True, r"my least favourite" + _FAV_NOUN + r" (?:is|s) " + A + D + P),
    (("dislikes",), True, D + P + r" (?:is|s) my least favourite" + _FAV_NOUN),
    # plans: a purchase or an activity ahead
    (("plans",), True, r"(?:im )?" + A + r"(?:saving|saving up|grinding|farming) (?:for|towards|toward|up to) " + D + P),
    (("plans",), True, r"i " + A + r"(?:want|need|plan|mean|have) to save(?: up)? for " + D + P),
    (("plans",), True, r"im " + A + r"(?:going to|about to|planning to|aiming to|hoping to|looking to) "
                       + _BUY + " " + D + P),
    (("plans",), True, r"im " + A + r"(?:planning on|thinking of|thinking about) " + _BUYING + " " + D + P),
    (("plans",), True, r"ill " + A + _BUY + " " + D + P),
    (("plans",), True, r"i plan (?:to " + _BUY + "|on " + _BUYING + ") " + D + P),
    (("plans",), True, r"my next (?:ship|purchase|buy|pledge|gun|upgrade) (?:is|s|will be|is going to be) " + D + P),
    (("plans",), True, r"im " + A + r"(?:going|off|heading out|going to go|planning to go|about to go|"
                       r"going to|planning to|planning on|hoping to|about to|looking to) "
                       r"(?:try |do |start |get into |go |have a go at |trying |doing |starting |getting into )?" + P),
    (("plans",), True, r"i " + A + r"(?:want|plan|intend|hope|mean) to (?:try|do|start|get into|go|learn|"
                       r"have a go at) (?:some |a bit of )?" + P),
    (("plans",), True, r"id (?:love|like) to (?:try|do|get into|go|learn) (?:some |a bit of )?" + P),
    # wants
    (("wants",), True, r"i " + A + r"(?:want|need|fancy) " + D + P),
    (("wants",), True, r"id " + A + r"(?:love|like|kill for) " + D + P),
    (("wants",), True, r"i " + A + r"want to (?:buy|get|own|fly|have|try) " + D + P),
    (("wants",), True, r"id " + A + r"(?:love|like) to (?:buy|get|own|fly|have|try) " + D + P),
    (("wants",), True, r"my " + _DREAM + r" (?:is|s) " + D + P),
    (("wants",), True, D + P + r" (?:is|s) my " + _DREAM),
    # likes
    (("likes",), True, r"my " + A + r"favourite" + _FAV_NOUN + r" (?:is|s|has to be) " + A + D + P),
    (("likes",), True, D + P + r" (?:is|s) " + A + r"my " + A + r"favourite" + _FAV_NOUN),
    (("likes",), True, r"i " + A + r"(?:love|like|adore|enjoy|dig|prefer|rate) "
                       r"(?:flying |using |wearing |running |driving |landing at |going to |visiting |"
                       r"hanging out at |being at |being in |doing )?" + D + P),
    (("likes",), True, r"im " + A + r"(?:a |an )?(?:big |huge |massive |real )?fan of " + D + P),
    (("likes",), True, r"im " + A + r"(?:in love with|into|obsessed with|hooked on) " + D + P),
    (("likes",), True, r"i cant get enough of " + D + P),
    # a haunt
    (("goes_to",), True, r"i " + A + r"(?:land|park|dock|store) " + D + O + r" (?:at|in|on) " + D + P),
    (("goes_to",), True, r"i " + A + r"(?:land|park|dock|log out|log off|log|bed down|hang out|hang around|hang|"
                         r"sit|live|stay|spawn|shop|refuel|stop|stop off|drink|base myself|operate|set up|restock|"
                         r"resupply|sell|trade|mine|hunt|fly|haul|race|chill|relax|idle|camp|drift|loiter) "
                         + _AT + " " + D + P),
    (("goes_to",), True, r"i " + A + r"(?:go|fly|head|come|return|go back|come back|keep going|keep going back|"
                         r"keep coming back|travel|jump|run) (?:out |back |over |down |up )?to " + D + P),
    (("goes_to",), True, r"i " + A + r"(?:visit|frequent|haunt) " + D + P),
    (("goes_to",), True, r"i " + A + r"end up (?:back )?(?:at|in|on) " + D + P),
    (("goes_to",), True, r"im " + _HABIT + r"(?:at|in|on|around|out at|over at|parked at|docked at|hanging around|"
                         r"hanging out at) " + D + P),
    (("goes_to",), True, r"im " + A + r"(?:based|stationed|living|set up|a regular) (?:at|in|on|out of) " + D + P),
    (("goes_to",), True, r"my " + A + _MY_HOME + r" (?:is|s)(?: at| in| on)? " + D + P),
    (("goes_to",), True, D + P + r" (?:is|s) " + A + r"(?:my )?" + _HOME_IS),
    (("goes_to",), True, r"i call " + D + P + r" home"),
    # the ship, the kit
    (("flies", "uses"), True, r"my " + A + _MY_SHIP + r" (?:is|s|has to be) " + A + D + P),
    (("uses", "flies"), True, r"my " + A + _MY_KIT + r" (?:is|s|has to be) " + A + D + P),
    (("uses",), True, r"(?:loadout|kit) (?:is|s) " + A + D + P),
    (("flies", "uses"), True, D + P + r" (?:is|s) " + A + r"my " + A + _MY_SHIP),
    (("uses", "flies"), True, D + P + r" (?:is|s) " + A + r"my " + A + _MY_KIT),
    (("flies",), True, r"i " + A + r"(?:fly|pilot|main|daily|drive|ride) " + D + P),
    (("flies", "uses"), True, r"i " + A + r"(?:run|use|rock) " + D + P),
    (("uses",), True, r"i " + A + r"(?:carry|wear|pack|bring) " + D + P),
    (("flies",), True, r"ive been " + A + r"(?:flying|piloting|maining|dailying|driving|riding) " + D + P),
    (("flies", "uses"), True, r"ive been " + A + r"(?:running|using|rocking) " + D + P),
    (("uses",), True, r"ive been " + A + r"(?:carrying|wearing) " + D + P),
    # owns
    (("owns",), True, r"i " + A + r"(?:own|have) " + D + P),
    (("owns",), True, r"ive " + A + r"(?:got|bought|pledged|picked up|grabbed|upgraded to) " + D + P),
    (("owns",), True, r"i " + A + r"(?:bought|pledged|picked up|grabbed|upgraded to) " + D + P),
    (("owns",), True, r"i (?:just|finally) got " + D + P),
    # what they do
    (("does",), True, r"i " + A + r"(?:do|run|grind|farm) (?:a lot of |lots of |loads of |plenty of |some |"
                      r"a bit of |mostly |mainly )?" + P),
    (("does",), True, r"i " + A + P),
    (("does",), True, r"i " + A + r"(?P<v>" + "|".join(sorted(_VERB_ACT, key=len, reverse=True)) + r")(?= |$)"),
    (("does",), True, r"im " + A + r"(?:a |an )(?:(?:solo|career|dedicated|proud|full time|casual) )?"
                      r"(?P<v>" + "|".join(sorted(_NOUN_ACT, key=len, reverse=True)) + r")(?= |$)"),
    (("does",), True, r"ive been " + A + r"(?:doing |running |grinding )?(?:a lot of |lots of |some |mostly )?" + P),
    # where they are and what they are in right now: a mention, not a claim (see MIN_MENTIONS)
    (("flies",), False, r"im " + A + r"(?:flying|piloting|driving|riding|in|back in|sat in|sitting in) " + D + P),
    (("uses", "flies"), False, r"im " + A + r"(?:using|running|rocking|carrying|wearing) " + D + P),
    (("goes_to",), False, r"im " + A + r"(?:at|in|on|back at|back in|back on|parked at|docked at|landed at|"
                          r"landing at|sitting at|sat at|heading to|headed to|going to|flying to|off to|"
                          r"on my way to|out at|over at|around|orbiting|above) " + D + P),
    (("does",), False, r"im " + A + r"(?:out |off |busy )?" + P),
]
_TRIGGERS = [(rels, claim, re.compile(rx + "(?= |$)")) for rels, claim, rx in _T]
_CONT = re.compile(r"^(?:(?:and|or|plus|also) )*" + D + P + r"(?: too| as well)?$")
_PLACEHOLDER = re.compile(r"<\d+>")

# Said about the thing, any of these means history, loss or somebody else, and nothing is extracted.
_HISTORY = set("""
was were wasnt werent been had hadnt did didnt used last first only never ever still once again anymore before
after since until ago old older former late back left gone lost lose sold sell gave give given got gotten took
taken kept keep bought found brought came went made said told knew thought felt feel held died dead death die
dying broke broken blew blown miss missing remember remind forget forgot forgotten memory memories name call
mean meant promise hope wish trust believe dream luck lucky unlucky alive same other another else instead rest
whole half nothing everything anything something somewhere nowhere all enough empty real true forever life live
one thing what why how who whose reason
survive with without mine ours yours theirs me myself you your today yesterday safe happy glad proud
january february march april may june july august september october november december
""".split())
# A part that is only this takes the sentence back ("I love Lorville, not.", "..., yeah right").
_TAKES_BACK = set("not no nope nah lol haha ha yeah right as if just sure".split())
_PERSON = set("""he she him her his hers hes shes we us our ours someone somebody anyone anybody everyone
everybody nobody people person man woman boy girl guy guys friend friends mate mates buddy lad lass""".split())
_PERSON_ANYWHERE = set("he she him her his hers we us our ours".split())
_TIME_NOUN = set("day days week weeks month months year years morning evening time times night nights".split())
_TIME_BEFORE = set("every most all some each at".split())
# The whole of what follows the thing may be one of these, though a word in it is not free elsewhere.
_TAIL_OK = set("""one day|some day|someday|eventually|next|this week|this month|this patch|all the time|
most of the time|right now|at the moment|for now|all day|every time""".replace("\n", "").split("|"))
_ED_OK = set("""need speed indeed hundred armed unarmed red bed feed exceed wicked rugged overrated underrated
overpowered underpowered nerfed buffed reworked patched medbed seed weed bleed""".split())
_BACK_REF = set("""it its itself one ones that this those these them they theyre theyve both which either neither
thats itll itd whose""".split())
_BACK_REF_INSIDE = set("there here".split())                    # "my wife proposed there"; not "there is ..."
# A part that hangs on the one before it says when, why or where the pilot does the thing, so it is about the
# fact and is read. "And", "but", "or" start a part that may stand on its own.
_SUBORD = set("""because cause cos when while if unless until though although since before after whenever where
which whereas so except""".split())
_CONNECT = _SUBORD - set("since before after until".split()) | set("and but or plus then".split())
_GENERIC = set("""ship ships gun guns rifle pistol armor set suit helmet place spot station thing name seat
cockpit bunk bed hold hangar""".split())
_OWN_SUBJECT = set("i im ive id ill there theres the my a an every some most no".split())


# "Not" said in the same sentence takes something back or says what the pilot does NOT do with the thing.
_NOT = set("""not no dont doesnt didnt cant couldnt wont wouldnt shouldnt isnt arent aint nor neither never
none nope nah""".split())


def _harmless(tok: str, prev: str, about: bool = True) -> bool:
    """May this word stand in the sentence a fact is taken from? about=True is what is said about the thing
    itself, where the words of history and loss are barred as well. Everywhere: a word banter_memory's
    vocabulary does not know, a person, a "not" or a past tense means nothing is taken."""
    if _PLACEHOLDER.fullmatch(tok):
        return True
    if tok in _PERSON or tok in _NOT:
        return False
    if tok.endswith("ed") and len(tok) > 3 and tok not in _ED_OK:
        return False                                            # a past tense is history
    if about:
        if any(s in _HISTORY for s in bm._stems(tok)):
            return False
        if tok in _TIME_NOUN and prev not in _TIME_BEFORE:
            return False                                        # "the day", "that night", "one more day"
    return bm.known(tok)


def _clean(toks: list, about: bool = True) -> bool:
    for k, t in enumerate(toks):
        if about and t == "with":                               # "with the Vulture" is a thing; "with him" is not
            j = k + 1
            while j < len(toks) and (toks[j] in _DET or toks[j] in _ADJ):
                j += 1
            if j < len(toks) and _PLACEHOLDER.fullmatch(toks[j]):
                continue
            return False
        if not _harmless(t, toks[k - 1] if k else "", about):
            return False
    return True


def _about_the_thing(toks: list) -> bool:
    """Does this part of the sentence point back at a thing, or lean on the part before it for a subject?"""
    if toks and toks[0] in _SUBORD:
        return True
    while toks and toks[0] in _SPLIT_BEFORE:
        toks = toks[1:]
    if not toks:
        return False
    if any(t in _BACK_REF or t in _GENERIC for t in toks) or any(t in _BACK_REF_INSIDE for t in toks[1:]):
        return True
    return toks[0] not in _OWN_SUBJECT


def _scan(text: str) -> list:
    """[(relation, thing, thing_kind, claim)] for one sentence of the pilot's. Gate b; the veto ran before."""
    idx = _index()
    words, questions = _words(text)
    if not words or any(w["n"] in _PERSON_ANYWHERE for w in words):
        return []
    items = _things(_prepare(words, idx), idx)
    things = [it["t"] for it in items if "t" in it]
    # the parts of the sentence
    parts: list = []
    cur: list = []
    k = 0
    for it in items:
        tok = it.get("w")
        if "t" in it:
            tok = f"<{k}>"
            k += 1
        if cur and (it["brk"] or tok in _SPLIT_BEFORE):
            parts.append(cur)
            cur = []
        cur.append((tok, it["sent"]))
    if cur:
        parts.append(cur)
    found: list = []                                            # (relation, thing index, kind, claim)
    tainted: set = set()
    loose: list = []                                            # parts with a thing in them and no fact
    drop_all = False
    open_rels: list = []
    for part in parts:
        if part[0][1] in questions:
            open_rels = []
            continue
        s = " ".join(t for t, _s in part)
        for rx, to in _REWRITE:
            s = rx.sub(to, s)
        toks = s.split()
        m = _CONT.match(s) if open_rels else None
        if m:
            x = int(m.group("x"))
            for rels, claim in open_rels:
                for rel in rels:
                    kind = next((kd for kd in _FITS[rel] if kd in things[x]), "")
                    if kind:
                        found.append((rel, x, kind, claim))
                        break
            continue
        open_rels = []
        # "if", "when", "unless", "maybe" are not in _OPENER, so a part that opens with one matches no pattern
        body = _OPENER.sub("", s, count=1)
        hit = None
        for rels, claim, rx in _TRIGGERS:
            m = rx.match(body)
            if not m:
                continue
            gd = m.groupdict()
            if gd.get("x") is not None:
                x = int(gd["x"])
                fit = next(((rel, kd) for rel in rels for kd in _FITS[rel] if kd in things[x]), None)
                if not fit:
                    continue
                hit = (fit[0], x, fit[1], claim, m, rels)
            else:
                name = _VERB_ACT.get(gd.get("v") or "") or _NOUN_ACT.get(gd.get("v") or "")
                if not name:
                    continue
                hit = ("does", name, "activity", claim, m, rels)
            break
        if hit:
            rel, x, kind, claim, m, rels = hit
            tail = body[m.end():].split()
            if (tail and tail[0] in ("s", "not", "no")) or not (" ".join(tail) in _TAIL_OK or _clean(tail)):
                if isinstance(x, int):
                    tainted.add(things[x].get(kind, ""))
                continue
            found.append((rel, x, kind, claim))
            if not tail and isinstance(x, int):
                open_rels = [(rels, claim)]
            continue
        if all(t in _TAKES_BACK for t in toks) and set(toks) & {"not", "no", "nope", "nah", "right", "if", "sure"}:
            drop_all = True
        else:
            rest = [t for k, t in enumerate(toks) if not (k == 0 and t in _CONNECT)]
            if not _clean(rest, about=_about_the_thing(toks)):
                drop_all = True
            elif _PLACEHOLDER.search(s):
                loose.append(toks)
    if drop_all:
        return []
    out: list = []
    for rel, x, kind, claim in found:
        name = x if isinstance(x, str) else things[x].get(kind, "")
        if not name or name in tainted:
            continue
        out.append((rel, name, kind, claim))
    # a part that names the same thing again and says something that is not clean takes its facts with it
    for toks in loose:
        named = {n for t in toks if _PLACEHOLDER.fullmatch(t) for n in things[int(t[1:-1])].values()}
        if not _clean([t for k, t in enumerate(toks) if not (k == 0 and t in _CONNECT)]):
            out = [f for f in out if f[1] not in named]
    # one sentence that both likes and dislikes a thing says nothing about it
    both = {(a[1]) for a in out for b in out if a[1] == b[1] and _OPPOSITE.get(a[0]) == b[0]}
    seen, final = set(), []
    for rel, name, kind, claim in out:
        if name in both and rel in _OPPOSITE:
            continue
        if (rel, name) in seen or rel not in RELATIONS or not is_canonical(name, kind):
            continue
        seen.add((rel, name))
        final.append((rel, name, kind, claim))
    return final


def _vetoed(text: str) -> bool:
    """Gate a. banter_memory's veto, asked through its own classify(). If it could not read the sentence, that
    is a veto too."""
    c = bm.classify(text)
    return c.get("kind") == "vetoed" or str(c.get("why", "")).startswith("could not be read")


def _extract(text) -> list:
    try:
        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
            return []
        if _vetoed(text):
            return []
        return _scan(text)
    except Exception:                                           # an extractor that fails must fail shut
        return []


def extract(text: str) -> list[dict]:
    """Zero or more facts from ONE sentence of the pilot's. Each is {"relation", "thing", "thing_kind"} and
    nothing else: relation is one of RELATIONS, thing is a name as the game data spells it (or one of
    ACTIVITIES), thing_kind is one of THING_KINDS. None of the pilot's own wording is in the result.
    Pure and deterministic: no state, no file written, no model. Never raises; anything it cannot read gives []."""
    return [{"relation": r, "thing": t, "thing_kind": k} for r, t, k, _claim in _extract(text)]


# ---------------------------------------------------------------------------------------------------------------
# The store: counts and times per (relation, thing), and which one to raise now.
# ---------------------------------------------------------------------------------------------------------------
def _key(relation: str, thing: str) -> str:
    return f"{relation}|{thing}"


class PilotFacts:
    """Counts facts about the pilot and hands one out when it is fit to raise. Reads a tree_memory.TreeStore and
    never writes to it. Its own state is one small JSON file beside the log, and holds no word of the pilot's.

    max_age_days       fading: a fact not mentioned for this long is not raised (default 30)
    raise_every_days   raise-once-per-period: one thing is raised at most once in this many days (default 7)
    min_age_minutes    a fact mentioned less than this long ago is not raised yet (default 10)
    min_mentions       how many passing mentions flies, uses, goes_to and does need when no sentence claimed
                       the habit (default 2). A claim is trusted at once."""

    def __init__(self, store=None, state_path: Optional[Path | str] = None,
                 now: Optional[Callable[[], float]] = None, max_age_days: float = MAX_AGE_DAYS,
                 raise_every_days: float = RAISE_EVERY_DAYS, min_age_minutes: float = MIN_AGE_MINUTES,
                 min_mentions: int = MIN_MENTIONS):
        self.store = store
        self.now = now or getattr(store, "now", None) or time.time
        self.max_age_s = float(max_age_days) * 86400.0
        self.period_s = float(raise_every_days) * 86400.0
        self.min_age_s = float(min_age_minutes) * 60.0
        self.min_mentions = int(min_mentions)
        self._state_path = Path(state_path) if state_path else None
        self._lock = threading.RLock()

    @property
    def state_path(self) -> Optional[Path]:
        if self._state_path is not None:
            return self._state_path
        d = getattr(self.store, "dir", None)
        return Path(d) / STATE_NAME if d else None

    # -- the state file ----------------------------------------------------------------------------------------
    def _read_state(self) -> Optional[dict]:
        """The state, a fresh one if there is no file yet, or None if a file is there and cannot be trusted."""
        p = self.state_path
        if p is None:
            return None
        try:
            if not p.exists():
                return {"schema": STATE_SCHEMA, "version": 1, "facts": {}, "barred": {}, "last": "",
                        "seen": {"t": 0.0, "ids": []}}
            st = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if (not isinstance(st, dict) or st.get("schema") != STATE_SCHEMA or not isinstance(st.get("facts"), dict)
                or not isinstance(st.get("barred"), dict)):
            return None
        for key, f in st["facts"].items():
            if (not isinstance(f, dict) or f.get("relation") not in RELATIONS or f.get("thing_kind") not in THING_KINDS
                    or not isinstance(f.get("thing"), str) or key != _key(f["relation"], f["thing"])
                    or not isinstance(f.get("n"), int) or not isinstance(f.get("last"), (int, float))):
                return None
        seen = st.get("seen")
        if not isinstance(seen, dict) or not isinstance(seen.get("t"), (int, float)) or not isinstance(seen.get("ids"), list):
            st["seen"] = {"t": 0.0, "ids": []}
        st.setdefault("last", "")
        return st

    def _write_state(self, st: dict) -> bool:
        p = self.state_path
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(p.name + ".tmp")
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                json.dump(st, f, ensure_ascii=False, indent=1)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, p)
            return True
        except OSError:
            return False

    # -- counting ----------------------------------------------------------------------------------------------
    def _count(self, st: dict, relation: str, thing: str, kind: str, t: float, claim: bool) -> bool:
        """One mention into the state. False if it was not a closed-set fact, or the thing is barred."""
        if relation not in RELATIONS or kind not in _FITS.get(relation, ()) or not is_canonical(thing, kind):
            return False
        if thing in st["barred"]:
            return False
        opp = st["facts"].get(_key(_OPPOSITE.get(relation, ""), thing))
        if opp and float(opp["last"]) > t:
            return False                                        # an older word against a newer one: the newer stands
        for other in _DROPS.get(relation, ()):
            old = st["facts"].get(_key(other, thing))
            if old and float(old["last"]) <= t:
                del st["facts"][_key(other, thing)]
        f = st["facts"].get(_key(relation, thing))
        if f is None:
            f = {"relation": relation, "thing": thing, "thing_kind": kind, "n": 0, "claimed": False,
                 "first": t, "last": t, "raised": 0.0, "raises": 0, "by": ""}
            st["facts"][_key(relation, thing)] = f
        f["n"] = int(f["n"]) + 1
        f["claimed"] = bool(f.get("claimed")) or bool(claim)
        f["last"] = max(float(f["last"]), t)
        f["first"] = min(float(f.get("first", t)), t)
        return True

    def add(self, facts, t: Optional[float] = None, claim: bool = False) -> int:
        """Count facts handed in (the dicts extract() gives). Returns how many were counted. Anything that is
        not a closed-set relation about a name from the game data is dropped, so free text cannot get into the
        state this way. claim=True says the pilot stated a habit; without it flies, uses, goes_to and does wait
        for min_mentions."""
        with self._lock:
            st = self._read_state()
            if st is None:
                return 0
            when = float(self.now() if t is None else t)
            n = 0
            try:
                for f in facts or []:
                    if isinstance(f, dict) and self._count(st, f.get("relation"), f.get("thing"),
                                                           f.get("thing_kind"), when, claim):
                        n += 1
            except Exception:
                return 0
            return n if (n and self._write_state(st)) else 0

    def note(self, text: str, t: Optional[float] = None) -> list[dict]:
        """Extract from one sentence of the pilot's and count what was found. The sentence is not kept anywhere.
        Returns the facts that were counted. For a sentence that is NOT also going into the tree: next_fact() reads
        the tree's new lines itself, and a sentence given to both would be counted twice."""
        with self._lock:
            st = self._read_state()
            if st is None:
                return []
            when = float(self.now() if t is None else t)
            got = [(r, th, k) for r, th, k, c in _extract(text) if self._count(st, r, th, k, when, c)]
            if not got or not self._write_state(st):
                return []
            return [{"relation": r, "thing": th, "thing_kind": k} for r, th, k in got]

    def _catch_up(self, st: dict) -> Optional[bool]:
        """Count the pilot's lines in the tree that have not been counted yet. True if the state changed, False
        if not, None if the tree could not be read."""
        if self.store is None:
            return False
        try:
            recs = self.store.records()
        except Exception:
            return None                                        # a missing or unreadable tree is not ours to raise
        seen_t, seen_ids = float(st["seen"]["t"]), set(st["seen"]["ids"])
        new_t, new_ids, changed = seen_t, set(seen_ids), False
        for r in recs or []:
            if not (isinstance(r, dict) and r.get("who") == "pilot" and r.get("kind", "said") == "said"
                    and isinstance(r.get("text"), str) and isinstance(r.get("t"), (int, float))):
                continue
            t, rid = float(r["t"]), str(r.get("id", ""))
            if t < seen_t or (t == seen_t and rid in seen_ids):
                continue
            for rel, thing, kind, claim in _extract(r["text"]):
                self._count(st, rel, thing, kind, t, claim)
            changed = True
            if t > new_t:
                new_t, new_ids = t, {rid}
            else:
                new_ids.add(rid)
        if changed:
            st["seen"] = {"t": new_t, "ids": sorted(new_ids)}
        return changed

    # -- the two things a caller does ----------------------------------------------------------------------------
    def next_fact(self, companion: str = "", mark: bool = True) -> Optional[dict]:
        """One fact fit to raise now, or None: {"relation", "thing", "thing_kind", "mentions", "last_seen"}.
        Never a sentence. mark=True (the default) counts it as raised the moment it is handed out, so the same
        thing is not handed out again inside the period even if the caller then says nothing. If that cannot be
        written down, nothing is handed out."""
        with self._lock:
            try:
                st = self._read_state()
                if st is None:
                    return None
                changed = self._catch_up(st)
                if changed is None:
                    return None
                t_now = float(self.now())
                raised_at: dict = {}
                for f in st["facts"].values():
                    raised_at[f["thing"]] = max(raised_at.get(f["thing"], 0.0), float(f.get("raised") or 0.0))
                fit = []
                for f in st["facts"].values():
                    if f["thing"] in st["barred"] or not is_canonical(f["thing"], f["thing_kind"]):
                        continue
                    age = t_now - float(f["last"])
                    if age < self.min_age_s or age > self.max_age_s:
                        continue
                    need = self.min_mentions if f["relation"] in NEEDS_MENTIONS else 1
                    if not f.get("claimed") and int(f["n"]) < need:
                        continue
                    if raised_at[f["thing"]] and t_now - raised_at[f["thing"]] < self.period_s:
                        continue
                    fit.append(f)
                if not fit:
                    if changed and not self._write_state(st):
                        return None
                    return None
                fit.sort(key=lambda f: (int(f.get("raises", 0)), -int(f["n"]), -float(f["last"]),
                                        _key(f["relation"], f["thing"])))
                f = fit[0]
                out = {"relation": f["relation"], "thing": f["thing"], "thing_kind": f["thing_kind"],
                       "mentions": int(f["n"]), "last_seen": float(f["last"])}
                if mark:
                    f["raised"], f["raises"], f["by"] = t_now, int(f.get("raises", 0)) + 1, str(companion or "")[:32]
                    st["last"] = f["thing"]
                if (mark or changed) and not self._write_state(st):
                    return None
                return out
            except Exception:
                return None

    def forget(self, thing: Optional[str] = None) -> list[str]:
        """"Forget that." With no argument: the thing raised most recently is never raised again. With a name
        (or some words with a name in them, "the Carrack"): that thing is never raised again, raised yet or not.
        Its facts are dropped and later mentions are not counted. Returns the names that were barred; they are
        names from the game data, never the words handed in. The log is not touched."""
        with self._lock:
            try:
                st = self._read_state()
                if st is None:
                    return []
                names: list = []
                if thing is None or not str(thing).strip():
                    if st.get("last"):
                        names = [st["last"]]
                else:
                    said = " ".join(w["n"] for w in _words(str(thing))[0])
                    held = {f["thing"] for f in st["facts"].values()}
                    for name in sorted(held):
                        plain = " ".join(w["n"] for w in _words(name)[0])
                        if plain and re.search(r"(?:^| )" + re.escape(plain) + r"s?(?: |$)", said):
                            names.append(name)
                    idx = _index()
                    for it in _things(_prepare(_words(str(thing))[0], idx), idx, lenient=True):
                        for name in (it.get("t") or {}).values():
                            if name not in names:
                                names.append(name)
                if not names:
                    return []
                t_now = float(self.now())
                for name in names:
                    st["barred"][name] = t_now
                st["facts"] = {k: f for k, f in st["facts"].items() if f["thing"] not in st["barred"]}
                return names if self._write_state(st) else []
            except Exception:
                return []

    def facts(self) -> list[dict]:
        """What is held, for a settings page or a test: relation, thing, kind, counts and times. Read only."""
        with self._lock:
            st = self._read_state()
            return [dict(f) for f in st["facts"].values()] if st else []


def open_facts(pilot_dir: Path | str, session: str = "", **kw) -> PilotFacts:
    """The store for one pilot's memory folder (the same folder tree_memory.open_tree and
    banter_memory.open_banter take)."""
    import tree_memory as tm
    return PilotFacts(tm.open_tree(pilot_dir, session=session), **kw)
