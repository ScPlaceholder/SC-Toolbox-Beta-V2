"""place_knowledge.py - what each companion KNOWS about a place, for the question path (J, 2026-10-05).

J: "Elah should have knowledge of the Galactapedia and Montaigne should have his brochures and dev history to
call on". Until today the lore, the brochures and the dev-history pack were only ever used for remarks nobody
asked for (the topic walker, dev_facts). A question about the place the pilot is standing in was answered with
the place's name and nothing else.

Three things live here:

  1. SOURCES. Who draws on what:
         Elah        lore (data/topics_lore.json: sourced lore and in-game facts), then what the catalogue says
                     the place has (its amenities). GALACTAPEDIA IS NOT HERE YET: galactapedia_source() is the
                     seam, registered for Elah and returning nothing. Nothing in this module fetches anything.
         Montaigne   his brochures (data/brochures.jsonl, data/brochures/), then the dev-history pack
                     (data/dev_facts_pack.json through dev_facts.py).
     Every source reads the topic graph the core has ALREADY loaded (TopicGraph: the same nodes the topic walker
     talks from) or the dev pack DevFacts already holds. There is no second index.

  2. THE GATE for an answer about a place, place_problems(). A retrieved fact enters the spec as a claim, and
     the line that answers must stay inside its claims. See the function for exactly what it checks and what it
     cannot.

  2b. WHAT THE EYES SAW (J 2026-10-05: "'Wow look at that!' should summon the eyes on the word look"). A sentence
     that points at something makes the Suit take ONE look at the screen (eyes.py, the local vision glance, under
     every rule eyes.py already has). What comes back is a short description, and it enters the answer as an
     OBSERVATION, the claim eyes.saw, said word for word: "What I see: A tall lattice tower on a ridge." It is never a
     fact about what the thing is FOR. Purpose still comes only from the place's own facts, and the closed
     vocabulary still refuses an invented one. clean_observation() drops a description that carries a number,
     a name the claims do not have, or a word for where on the screen something is (eyes.py: "The eyes may say
     WHAT is on screen, never WHERE").

  3. THE ANSWER ITSELF, answer_line(): the claims, in order, in the speaker's voice. A question about a place
     is NOT worded by the model. Measured 2026-10-05 on the two shipped 1.5B adapters, 120 lines over ten place
     questions: the gate passed 20 of 60 of Elah's lines and 5 of 60 of Montaigne's, and the ones it passed
     were still wrong in ways no word check can see ("You're at Aberdeen's outpost in Vivere OLP", "Lorville
     isn't who you left", "Your spaceport is somewhere in Lorville. I don't know where."). The model keeps
     every word and loses the relation between them. So the line is put together here, from the spec, and
     is held to the same gate. CompanionCore.place_answers_from_model turns the model back on for these.

Selftest: python place_knowledge.py --selftest
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Callable, Optional

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from grounding_validator import CARDINAL_WORDS, ground      # noqa: E402

Fact = dict[str, Any]       # text, names, source, status, node, title, kind (lore | amenity | brochure | dev)

LORE_STATUSES = ("lore", "in_game")


# ---------------------------------------------------------------------------------------------------------------
# 1. Retrieval
# ---------------------------------------------------------------------------------------------------------------
class Query:
    """One question: where the pilot is, what was said, and the data already in memory."""

    def __init__(self, graph, state: dict, text: str = "", dev_pack: Optional[Callable[[], Optional[list]]] = None):
        self.graph, self.state, self.text, self.dev_pack = graph, state or {}, text or "", dev_pack
        self.location = str(self.state.get("location") or "").strip()
        self.body = str(self.state.get("location_body") or self.state.get("planetary_body") or "").strip()

    # -- which nodes are about this place ----------------------------------------------------------------------
    def site_nodes(self) -> list[str]:
        """Nodes about the place itself, the most exact first: a node titled exactly as the place, then nodes
        anchored to it by a name the place's name contains ("Vivere" in "Vivere OLP" -> the Hathor Group), the
        longest such name first. A brochure node of ANOTHER place is never one of them: "Ruptura PAF-I" is inside
        "Ruptura PAF-III" as text, and is a different site."""
        loc = self.location.lower()
        if not loc or self.graph is None:
            return []
        exact, anchored = [], []
        for nid, n in self.graph.nodes.items():
            if n["title"].lower() == loc:
                exact.append(nid)
                continue
            if n.get("brochure"):
                continue
            hits = [a for a in (n.get("anchors") or {}).get("location_contains", [])
                    if a and re.search(rf"(?<![\w-]){re.escape(a.lower())}(?![\w-])", loc)]
            if hits:
                anchored.append((-max(len(a) for a in hits), nid))
        exact.sort(key=lambda nid: bool(self.graph.nodes[nid].get("brochure")))       # the lore node first
        return exact + [nid for _, nid in sorted(anchored)]

    def body_nodes(self) -> list[str]:
        body = self.body.lower()
        if not body or self.graph is None or body == self.location.lower():
            return []
        nodes = [nid for nid, n in self.graph.nodes.items() if n["title"].lower() == body]
        return sorted(nodes, key=lambda nid: bool(self.graph.nodes[nid].get("brochure")))

    def named_nodes(self) -> list[str]:
        """Nodes for things NAMED in the sentence ("what is this Hathor place"): a whole-word title or alias of at
        least four letters. The place the pilot is at is found by site_nodes, not here."""
        if self.graph is None:
            return []
        low = re.sub(r"[^a-z0-9\s-]", " ", self.text.lower())
        out = []
        for nid, n in self.graph.nodes.items():
            for name in [n["title"], *n.get("aliases", [])]:
                nm = name.lower()
                if len(nm) >= 4 and re.search(rf"(?<![\w-]){re.escape(nm)}(?![\w-])", low):
                    out.append((-len(nm), nid))
                    break
        return [nid for _, nid in sorted(out)]

    def facts_of(self, nids: list[str], statuses: tuple, kind: str, amenities: Optional[bool] = None) -> list[Fact]:
        """The facts of these nodes with one of `statuses`. amenities: True = only the catalogue's "X has ..."
        facts on a brochure node, False = everything but those, None = no filter."""
        out, seen = [], set()
        for nid in nids:
            n = self.graph.nodes[nid]
            for i, f in enumerate(n["facts"]):
                if f.get("withdrawn") or f["status"] not in statuses or f["text"] in seen:
                    continue
                is_amenity = bool(n.get("brochure")) and f["status"] == "in_game"
                if amenities is not None and is_amenity != amenities:
                    continue
                seen.add(f["text"])
                out.append({"text": f["text"], "names": list(f.get("names") or []), "source": f.get("source", ""),
                            "status": f["status"], "node": nid, "fact": i, "title": n["title"],
                            "kind": "amenity" if is_amenity else kind})
        return out


def lore_source(q: Query) -> list[Fact]:
    """Elah: sourced lore and in-game facts about the site, then the things named in the sentence, then what the
    catalogue says the place has, then the body it stands on."""
    site, body, named = q.site_nodes(), q.body_nodes(), q.named_nodes()
    return (q.facts_of(site, LORE_STATUSES, "lore", amenities=False)
            + q.facts_of([n for n in named if n not in site], LORE_STATUSES, "lore", amenities=False)
            + q.facts_of(site, LORE_STATUSES, "lore", amenities=True)
            + q.facts_of(body, LORE_STATUSES, "lore", amenities=False))


def galactapedia_source(q: Query) -> list[Fact]:
    """THE SEAM for Elah's Galactapedia (J 2026-10-05). There is no Galactapedia data in the toolbox, so this
    returns nothing, and nothing here fetches any. When a file exists, return its entries for q.location, q.body
    and q.named_nodes() in the Fact shape, with kind "lore" and status "lore", and each entry's own `source`. The
    gate and answer_line() need nothing else: they work from the claims."""
    return []


def brochure_source(q: Query) -> list[Fact]:
    """Montaigne: brochure copy for the site, then for the body it stands on. A brochure line rarely names its
    subject ("Where some guests never want to leave."), so answer_line() says whose brochure it is whenever it
    is not the site's own."""
    site, body, named = q.site_nodes(), q.body_nodes(), q.named_nodes()
    return q.facts_of(site + [n for n in named if n not in site] + body, ("brochure",), "brochure")


def dev_history_source(q: Query) -> list[Fact]:
    """Montaigne: curated dev-history facts about this place or the body it stands on (never the whole system).
    Each keeps its pack entry, so the answer can carry the entry's date and excerpt exactly as an aside does."""
    pack = None
    if q.dev_pack is not None:
        try:
            pack = q.dev_pack()
        except Exception:
            pack = None
    if not pack:
        return []
    keys = set()
    for name in (q.location, q.body):
        words = re.findall(r"[a-z0-9]+", name.lower())
        keys |= {w for w in words if len(w) >= 4} | ({"".join(words)} if words else set())
    out = []
    for e in pack:
        if e.get("topic_kind") != "place" or not keys & {str(t).lower() for t in e.get("topics") or []}:
            continue
        out.append({"text": str(e.get("fact") or ""), "names": [], "source": str(e.get("url") or ""),
                    "status": "dev", "node": "dev:" + str(e.get("id")), "fact": 0,
                    "title": str(e.get("title") or ""), "kind": "dev", "entry": e})
    return out


# Who draws on what. A new source (the Galactapedia, when there is one) is one more function in the list.
SOURCES: dict[str, list[Callable[[Query], list[Fact]]]] = {
    "elah": [lore_source, galactapedia_source],
    "montaigne": [brochure_source, dev_history_source],
}


class PlaceKnowledge:
    """facts(speaker, state, text) -> what that companion may say about where the pilot is, best first.
    graph: the core's TopicGraph (core.walker.g). dev_pack: a callable returning the dev-history pack or None
    (DevFacts.pack)."""

    def __init__(self, graph, dev_pack: Optional[Callable[[], Optional[list]]] = None,
                 sources: Optional[dict] = None):
        self.graph, self.dev_pack = graph, dev_pack
        self.sources = sources if sources is not None else SOURCES

    def facts(self, speaker: str, state: dict, text: str = "") -> list[Fact]:
        """Best first. The speaker's first source leads, and every later source gets one turn in three, so
        Montaigne reaches his dev history without first reading out a whole brochure (Lorville's has 20 lines)."""
        q = Query(self.graph, state, text, self.dev_pack)
        lists, seen = [], set()
        for src in self.sources.get(speaker, []):
            try:
                found = src(q)
            except Exception:
                found = []                       # a broken source is a source with nothing to say
            mine = []
            for f in found:
                if f.get("text") and f["text"] not in seen:
                    seen.add(f["text"])
                    mine.append(f)
            lists.append(mine)
        out: list[Fact] = []
        first, rest = (lists[0] if lists else []), [x for x in lists[1:] if x]
        while first or rest:
            out += first[:2]
            first = first[2:]
            for x in rest:
                out.append(x.pop(0))
            rest = [x for x in rest if x]
        return out


# ---------------------------------------------------------------------------------------------------------------
# 2. The gate
# ---------------------------------------------------------------------------------------------------------------
# Words that say nothing about a place: grammar, the speaker, the pilot, knowing and not knowing, and where a fact
# came from. A word about the WORLD (old, abandoned, relay, reactor, guards) is deliberately not here: it has to be
# in the claims, in what the pilot said, or in the stance.
_GRAMMAR = set("""
a an the and or but nor so yet if then than that this these those there here it its itself
i me my mine myself we us our ours you your yours yourself he him his she her they them their
is are was were be been being am do does did done doing have has had having will would shall should can could
may might must cannot not no none never ever always only just also too very quite rather still even again
of to in on at for with by from as about into onto over under between among through during before after
above below up down out off near around across behind beyond within without against toward towards upon
what which who whom whose where when why how whether while because since until though although unless
all any some each every both either neither one ones other another such same own much many more most less least
few little lot enough else
t s re ll ve d m isn aren wasn weren don doesn didn haven hasn hadn won wouldn couldn shouldn ain
yes yeah well now today currently already once here's there's
""".split())
_VOICE = set("""
pilot suit ship sensors sensor readings reading feed log logs record records recorded label labels name names
named call calls called data reference information fact facts detail details note notes word words
know knows knew known knowing tell tells told telling say says said saying mean means meant meaning
ask asks asked asking question questions answer answers answered
see sees saw seen seeing look looks looked looking sight eye eyes view visible identify identified identifying
point pointing spot make made tell apart
sure certain certainty uncertain guess guessing guessed idea clue think thinks thought believe suppose imagine
honestly frankly plainly simply truly really exactly precisely specifically particular
afraid sorry forgive pardon admit confess alas regret humble modest merely mere
brochure brochures pamphlet read reads reading heard hear secondhand hearsay account rumour rumor source sources
quote quotes quoting quoted repeat repeating repeats according claims claim promises promise says
trust doubt doubts verify verified vouch confirm confirmed
structure structures building buildings thing things place places site sites spot location position
somewhere anywhere nowhere something anything nothing everything someone anyone
left leave leaving departed depart behind gone last since arrived arrive
got get gets give gives giving given offer offers offering have help helps tell
can't cannot unable able way from here
part rest piece bit kind sort matter case
myself yourself am philosopher
stand stands standing sit sits sitting worth mine nobody
memory remember story pretend keeping kept conversation conversations consult find finds earlier yesterday through
hold holds report reports shown show shows pick
""".split())
VOCABULARY = _GRAMMAR | _VOICE

# The pilot's word for the thing they are pointing at, when the Suit cannot see it.
# Where on the screen. eyes.py bans positions from everything the eyes expose (its POSITION_WORDS guards field
# names); this is the same rule for the one free-text field, before any of it can be spoken. "ahead", "in the
# distance" and "on a ridge" describe the scene and are fine; these describe the frame.
SCREEN_POSITION = re.compile(
    r"\b(?:left|right|top|bottom|centre|center|middle|corner|upper|lower|foreground|background|"
    r"(?:of|on) (?:the )?(?:screen|frame|image|picture|hud)|pixels?|coordinates?|degrees?|bearing|crosshair|reticle)\b")


def clean_observation(saw: str, spec: dict) -> Optional[str]:
    """What the eyes reported, made fit to be said, or None when it is not.

    The glance is asked for a few words with no numbers, names or HUD text, and a model does not always do as it
    is asked. So the description is refused whole (the answer then says what it would have said with no eyes)
    if it has: a digit or a number word; a capitalised word after its first that is not in the answer's own claims
    (a place, a ship, a maker the eyes cannot know); any word for a position on the screen; a quotation mark; or
    more than 20 words. Never repaired: a description with the name cut out of it would be a different sentence
    from the one the eyes gave."""
    s = " ".join(str(saw or "").replace("\u2019", "'").split()).strip().rstrip(".!")
    if not s or len(s.split()) > 20 or re.search(r'["\u201c\u201d]', s):
        return None
    low = s.lower()
    if re.search(r"\d", s) or any(re.search(rf"\b{w}\b", low) for w in CARDINAL_WORDS):
        return None
    if SCREEN_POSITION.search(low):
        return None
    known = {w.lower() for c in spec.get("claims", []) for w in re.findall(r"[A-Za-z][A-Za-z'-]*", str(c.get("value", "")))}
    for w in re.findall(r"[A-Za-z][A-Za-z'-]*", s)[1:]:
        if w[0].isupper() and w.lower() not in known and w != "I":
            return None
    return s


NOT_KNOWING = re.compile(
    r"\b(?:can(?:'|no)?t|cannot|could ?n(?:'|o)t|unable|do(?:es)? ?n(?:'|o)t know|do not know|no way|not sure|"
    r"no idea|no eyes|which (?:one|structure|building|tower|thing)|whichever|without (?:seeing|eyes)|"
    r"not (?:able|visible|certain)|have not been told|no (?:reading|view|sight|record|data))\b")
DEPARTED = re.compile(r"\b(?:left|leaving|departed|behind|gone|no longer|last|were|was|since)\b")


def _stem(w: str) -> str:
    w = w.lower()
    for suf in ("'s", "ies", "ing", "ed", "es", "ly", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            return w[: len(w) - len(suf)] + ("y" if suf == "ies" else "")
    return w


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z]+", str(text).lower().replace("’", "'").replace("'", " "))


def support(spec: dict) -> set:
    """Every word the answer may use: the claims (values and predicates), what the pilot said, the stance, the
    allowed names, and the closed vocabulary above. Stems, so "pairs" supports "paired"."""
    src = [str(c.get("value", "")) for c in spec.get("claims", [])]
    src += [str(c.get("predicate", "")).replace(".", " ").replace("_", " ") for c in spec.get("claims", [])]
    src += [str((spec.get("interpretation") or {}).get("text", "")), str((spec.get("route") or {}).get("text", ""))]
    src += [str(n) for n in spec.get("allowed_names") or []]
    return {_stem(w) for s in src for w in _tokens(s)} | {_stem(w) for w in VOCABULARY} | VOCABULARY


def novel_words(spec: dict, text: str) -> list[str]:
    ok = support(spec)
    return [w for w in dict.fromkeys(_tokens(text)) if w not in ok and _stem(w) not in ok]


def place_problems(spec: dict, text: str) -> list[str]:
    """Why `text` may not be said as the answer to a question about a place ([] = it may).

    WHAT IT CHECKS, on top of ground() (numbers, quotation marks, length, capitalised names, brochure attribution):
      * CLOSED VOCABULARY. Every word of the line is in the claims, in what the pilot said, in the stance, or in a
        fixed list of words that say nothing about a place (grammar, the speaker, the pilot, knowing and not
        knowing, sources). A purpose invented for a tower needs a word that is in none of those: "relay",
        "reactor", "defence", "storage". This is the check that closes most of the gap.
      * THE THING POINTED AT. When the pilot points at something the Suit cannot see, the line must say it cannot
        tell which one, and any sentence that uses the pilot's own word for it ("tower") must be that admission.
      * THE NAME. The answer names the place, when there is a name. After leaving a place the line must say it
        was left, in the sentence that names it.

    WHAT IT CANNOT CATCH. It checks words, not meaning:
      * a false sentence made only of permitted words. "Aberdeen was dissolved" and "the laser platform orbits
        Hathor" use nothing but claim words. Dropping or adding "not" is invisible too.
      * a wrong relation between two true names, for the same reason.
      * a vague invention in the closed vocabulary ("there is nothing here", "it is the last one").
    answer_line() has none of these: it is the claims, in order. That is why it, and not the model, answers.
    """
    place = spec.get("place") or {}
    low = text.lower().replace("’", "'")
    if spec.get("fixed_text") and text == spec["fixed_text"] and spec.get("aside") == "dev_fact":
        # A dev-history fact is said word for word from its pack entry behind a fixed lead, never worded by a model,
        # and ground() already holds it to the dev-fact rules (framed as an aside, date and excerpt on the spec).
        return ground(spec, text)
    fails = [f for f in ground(spec, text) if not f.startswith("unauthorized numbers")]
    # ground() takes any number WORD not written as a digit in the claims for an invented number, so "two moons",
    # quoted from a fact that says "two moons", is refused. Here a number word that is in a claim is authorised.
    claim_words = {w for c in spec.get("claims", []) for w in _tokens(c.get("value", ""))}
    claim_nums = {n for c in spec.get("claims", []) for n in re.findall(r"\d+(?:\.\d+)?", str(c.get("value", "")))}
    bad_nums = [n for n in re.findall(r"\d+(?:\.\d+)?", text.replace(",", "")) if n not in claim_nums]
    bad_nums += [w for w in CARDINAL_WORDS if re.search(rf"\b{w}\b", low) and w not in claim_words
                 and str(CARDINAL_WORDS[w]) not in claim_nums]
    if bad_nums:
        fails.append(f"unauthorized numbers {bad_nums}")
    novel = novel_words(spec, text)
    if novel:
        fails.append(f"says more than its claims {novel}")
    sentences = [s for s in re.split(r"(?<=[.!?;])\s+", low) if s.strip()]
    saw = str(place.get("saw") or "")
    if saw and saw.lower() not in low:
        fails.append("does not say what the eyes reported, as they reported it")
    if place.get("referent") is not None:
        ref_words = [w for w in _tokens(place.get("referent") or "") if len(w) >= 4 and w not in VOCABULARY]
        seen_it = bool(saw) and (not ref_words or any(re.search(rf"\b{re.escape(w)}s?\b", saw.lower()) for w in ref_words))
        if not seen_it and not NOT_KNOWING.search(low):
            fails.append("does not say it cannot tell which thing is meant")
        for s in sentences:
            # the pilot's word for the thing may appear INSIDE the eyes' own report and in an admission; nowhere else
            rest = s.replace(saw.lower(), " ") if saw else s
            if any(re.search(rf"\b{re.escape(w)}s?\b", rest) for w in ref_words) and not NOT_KNOWING.search(s):
                fails.append("says something about the thing it cannot see")
                break
    name = next((str(c["value"]) for c in spec.get("claims", [])
                 if c.get("predicate") in ("location.name", "location.log_label") and c.get("kind") == "OBSERVED"), "")
    if name and name.lower() not in low:
        fails.append("does not name the place")
    departed = str(place.get("departed") or "")
    if departed:
        named_in = [s for s in sentences if departed.lower() in s]
        if not named_in:
            fails.append("does not say which place was left")
        elif not all(DEPARTED.search(s) for s in named_in):
            fails.append("names the place that was left as where we are")
    return fails


# ---------------------------------------------------------------------------------------------------------------
# 3. The answer: the claims themselves, in the speaker's voice
# ---------------------------------------------------------------------------------------------------------------
def _val(spec: dict, predicate: str) -> str:
    for c in spec.get("claims", []):
        if c.get("predicate") == predicate and c.get("kind") != "UNKNOWN":
            return str(c["value"])
    return ""


# Each slot has a few wordings per speaker so the same question does not always get the same sentence. They carry
# no fact: every fact in the line is a {slot} filled from the spec. Elah is dry and short; Montaigne has everything
# secondhand and says so. The selftest holds every wording, for every kind of place answer, to place_problems().
_LINES = {
    "elah": {
        "pointing": ["I can't tell which {thing} you mean from here.",
                     "I can't see what you're looking at, so I can't tell you which {thing} that is.",
                     "No eyes on it from here. I can't tell which {thing} you mean."],
        # The glance answers in a sentence of its own ("A lone figure stands in a dark hangar"), so the frame ends
        # in a colon and the report follows whole.
        "seen": ["What I see: {saw}.", "Eyes on it: {saw}."],
        "seen_not_it": ["What I see: {saw}. I can't tell which {thing} you mean in that."],
        "looking": ["Looking."],
        "here": ["This is {where}.", "We're at {where}.", "{where}."],
        "label": ["The log calls this {where}. That is its label, not a name I know.",
                  "All I have is the log's label: {where}. I know no name for it."],
        "left": ["We left {departed}, and I have no name for where we are now.",
                 "{departed} is behind us. I have no name yet for where we are now."],
        "nowhere": ["I have no reading on where we are, and I will not guess.",
                    "No reading on where we are. I won't guess."],
        "fact": ["{fact}"],
        "body_fact": ["{fact}"],
        "bare": ["That is all I know about it.", "I have nothing more on it."],
    },
    "montaigne": {
        # J, 2026-10-05: he is a man aboard the ship who never goes out. Until then these two lines read "a ship
        # has only what he is told" and "I have no eyes of my own".
        "pointing": ["I cannot see which {thing} you mean, pilot; I stay aboard, and have only what I am told.",
                     "Which {thing} you mean I cannot see from in here; I have only the suit's feed."],
        "seen": ["The suit's eyes report this: {saw}.",
                 "I am shown this, by the suit's eyes and not my own: {saw}."],
        "seen_not_it": ["The suit's eyes report this: {saw}. Which {thing} you mean in that, I cannot tell."],
        "looking": ["One moment, pilot; I am asking the suit's eyes."],
        "here": ["The suit's feed says this is {where}.",
                 "I am told this is {where}; I have that from the suit's feed, not from my own eyes."],
        "label": ["The log calls this {where}; it is a label, and I know no name for it.",
                  "I have only the log's label for this place, {where}, and no name I know."],
        "left": ["We left {departed}, and the suit's feed has not told me where we are now.",
                 "{departed} is behind us, and I have not been told where we are now."],
        "nowhere": ["I have not been told where we are, and I will not guess.",
                    "Nobody has told me where we are, pilot, and I will not guess."],
        "fact": ["The brochure says this: {fact}",
                 "And the brochure, for what a brochure is worth, says this: {fact}"],
        "body_fact": ["The brochure for {title} says this: {fact}",
                      "Of this place my brochures say nothing; the one for {title} says this: {fact}"],
        "bare": ["My brochures have nothing more on it.", "Beyond the name, my brochures say nothing of it."],
    },
}


def holding_line(spec: dict) -> Optional[str]:
    """What is said at once when the look is taking a while: the model behind the eyes may have to load."""
    lines = _LINES.get(spec.get("speaker"), {}).get("looking")
    return lines[0] if lines else None


def answer_line(spec: dict, variant: int = 0) -> Optional[str]:
    """The answer to a place question, built from the spec's own claims. None when the spec is not one."""
    place = spec.get("place")
    if not place or spec.get("speaker") not in _LINES:
        return None
    lines = _LINES[spec["speaker"]]

    def pick(slot: str, **kw) -> str:
        return lines[slot][variant % len(lines[slot])].format(**kw)

    name, label = _val(spec, "location.name"), _val(spec, "location.log_label")
    body, fact = _val(spec, "location.body"), _val(spec, "topic.fact")
    title = _val(spec, "topic.name")
    departed, referent = str(place.get("departed") or ""), place.get("referent")
    parts = []
    saw = str(place.get("saw") or "")
    if referent is not None:
        # The pilot's own word for it, when they used one that names a thing ("big tower"); otherwise "one".
        ref_words = [w for w in _tokens(referent) if len(w) >= 4 and w not in VOCABULARY]
        thing = referent if ref_words else "one"
        if not saw:
            parts.append(pick("pointing", thing=thing))
        elif not ref_words or any(re.search(rf"\b{re.escape(w)}s?\b", saw.lower()) for w in ref_words):
            parts.append(pick("seen", saw=saw))           # the eyes' report has the thing the pilot named
        else:
            parts.append(pick("seen_not_it", saw=saw, thing=thing))
    here = name or label
    if departed:
        parts.append(pick("left", departed=departed))
    elif here:
        where = f"{here}, on {body}" if body and body.lower() != here.lower() else here
        parts.append(pick("here" if name else "label", where=where))
    else:
        parts.append(pick("nowhere"))
    if fact and here and not departed:
        f = fact if fact[-1:] in ".!?" else fact + "."
        own = not title or title.lower() == here.lower()
        parts.append(pick("fact" if own else "body_fact", fact=f, title=title))
    elif here and not departed:
        parts.append(pick("bare"))
    return " ".join(parts)


# ---------------------------------------------------------------------------------------------------------------
# Selftest (the graph and the pack as they ship; no model)
# ---------------------------------------------------------------------------------------------------------------
def _selftest() -> int:
    from topic_graph import TopicGraph
    import dev_facts as devf
    results = []

    def case(name, cond, detail=""):
        results.append((name, bool(cond), str(detail)))

    g = TopicGraph.load()
    pk = PlaceKnowledge(g, lambda: devf.load_pack())
    at = {"location": "Vivere OLP", "location_body": "Aberdeen", "system": "Stanton"}
    e = pk.facts("elah", at)
    case("Elah at Vivere OLP: the Hathor lore first, then Aberdeen",
         e and e[0]["node"] == "hathor" and any(f["node"] == "aberdeen" for f in e) and
         all(f["status"] in LORE_STATUSES for f in e), [f["node"] for f in e][:6])
    case("Elah never gets brochure copy or a dev fact", all(f["kind"] in ("lore", "amenity") for f in e))
    m = pk.facts("montaigne", {"location": "Lorville", "location_body": "Hurston", "system": "Stanton"})
    case("Montaigne at Lorville: brochure copy first, then dev history about Lorville or Hurston",
         m and [f["kind"] for f in m[:3]] == ["brochure", "brochure", "dev"])
    case("Montaigne never gets lore", all(f["kind"] in ("brochure", "dev") for f in m))
    r = Query(g, {"location": "Ruptura PAF-III", "location_body": "Aberdeen"}).site_nodes()
    case("Ruptura PAF-III is not answered from PAF-I's or PAF-II's brochure",
         "brochure_ruptura_paf_i" not in r and "brochure_ruptura_paf_ii" not in r and "hathor" in r, r)
    case("an unknown place has no facts", pk.facts("elah", {"location": "Nowhere Much"}) == [])
    case("the Galactapedia seam is registered for Elah and empty",
         galactapedia_source in SOURCES["elah"] and galactapedia_source(Query(g, at)) == [])
    # Every wording of the answer, for every kind of place answer, passes the gate it is held to.
    import conversation as conv
    states = {"known": dict(at, location_type="outpost", location_named=True),
              "city": {"location": "Lorville", "location_body": "Hurston", "location_type": "city"},
              "label": {"location": "1a ASD Delve Facility 001", "location_body": "Arial", "location_named": False},
              "left": {"departed_from": "Lorville"}, "nowhere": {}, "no facts": {"location": "Nowhere Much"}}
    bad = []
    for who in ("elah", "montaigne"):
        for sname, st in states.items():
            for utt in ("where are we", "what is this place", "whats that big tower do", "wow look at that"):
                lane = conv.ConversationLane(pk)
                for v in range(4):
                    lane.variant = v
                    spec = lane.handle(("montaigne " if who == "montaigne" else "") + utt, dict(st), {})
                    fails = conv.ground_direct(spec, spec["fixed_text"])
                    if fails:
                        bad.append((who, sname, utt, v, fails, spec["fixed_text"]))
    case("every wording of every kind of place answer passes the gate (192 lines)", not bad, bad[:2])
    for name, ok, detail in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   <{detail[:200]}>" if not ok else ""))
    bad = sum(not ok for _, ok, _ in results)
    print(f"place_knowledge selftest: {len(results) - bad}/{len(results)} passed")
    return 1 if bad else 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print(__doc__)
