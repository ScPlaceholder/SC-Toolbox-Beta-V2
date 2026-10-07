"""grounding_validator.py - the deterministic HARD GATE from the migration architecture.

Self-contained copy of the grounding check from ../realizer_bakeoff.py's ground() (v4, the version
that survived the 2026-09-23 bake-off: digits-only failed natural speech, parsing all number words
made "no one" a number, and "use digits" in the prompt gets ignored by small models -- v4 checks
each REQUIRED value against its own closed set of spoken forms and never parses free text).

Kept as its own module (stdlib only, no WingmanAI imports, no cross-directory import into
companion_design/) so main.py stays importable wherever this skill folder is copied. If
companion_design/realizer_bakeoff.py's ground() changes, port the change here too -- this is a
deliberate fork for deployability, not a shared source of truth.

ground(spec, text) -> list[str]: empty list = grounded (safe to speak). Non-empty = why it failed;
the caller (main.py's _realize_spec) must not enqueue speech when this is non-empty.
"""
from __future__ import annotations

import re

NUM = re.compile(r"\d+(?:\.\d+)?")
_UNITS = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split()
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_ORD = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
        # "once"/"twice" ARE the spoken forms of 1 and 2 as counts ("killed you once", "regenerated you twice"). Missing
        # until 2026-09-24 night, so the most natural lines were refused as "missing 1" (retrain #2's eval caught it).
        "once": 1, "twice": 2}
CARDINAL_WORDS = {w: i for i, w in enumerate(_UNITS) if i >= 2} | _TENS  # 'one'/'zero' excluded: ordinary English


def spoken_forms(n: int) -> set[str]:
    """Every way a speaker might say the required number n. Closed set, so the check stays deterministic."""
    forms = {str(n), f"{n:,}"}
    if n < 20:
        forms.add(_UNITS[n])
    elif n < 100:
        tens = [w for w, v in _TENS.items() if v == n - n % 10][0]
        forms |= {tens} if n % 10 == 0 else {f"{tens}-{_UNITS[n % 10]}", f"{tens} {_UNITS[n % 10]}"}
    if n % 1000 == 0 and 0 < n // 1000 < 100:
        forms |= {f"{w} thousand" for w in spoken_forms(n // 1000) if not w[0].isdigit()} | {f"{n // 1000}k", f"{n // 1000} thousand"}
    elif 1000 < n < 10000 and n % 100 == 0:
        th, hu = n // 1000, (n % 1000) // 100
        forms.add(f"{_UNITS[th]} thousand {_UNITS[hu]} hundred" if hu else f"{_UNITS[th]} thousand")
        forms.add(f"{th},{hu}00")
    forms |= {w for w, v in _ORD.items() if v == n}
    return forms


# Names either character may always say: themselves, and the classical company Montaigne keeps. None of these can
# be a false claim about the game.
_ALWAYS_NAMES = {"i", "elah", "montaigne", "michel", "pilot", "plutarch", "seneca", "socrates", "cicero", "horace",
                 "lucretius", "virgil", "caesar", "cato", "aristotle", "plato", "sextus", "pyrrho", "bordeaux",
                 "france", "gascony", "god", "ovid", "homer"}
_WORD = re.compile(r"[A-Za-z][A-Za-z'’-]*")


def unauthorized_names(spec: dict, text: str) -> list[str]:
    """Topic lines (spec['allowed_names'] set): a capitalised word that is not the first word of a sentence must
    come from the allowed names or the claim values. This is what stops a lore line inventing a moon.
    Limitation, stated so it is not mistaken for coverage: a sentence-INITIAL invented name is not caught, because
    a capital there is ordinary English."""
    allowed = set(_ALWAYS_NAMES)
    for src in list(spec.get("allowed_names") or []) + [str(c.get("value", "")) for c in spec.get("claims", [])]:
        allowed |= {w.lower().replace("’", "'").removesuffix("'s") for w in _WORD.findall(str(src))}
    bad = []
    for sentence in re.split(r"(?<=[.!?;:])\s+|\s+[-–—]\s+", text):
        words = _WORD.findall(sentence)
        for w in words[1:]:
            if not w[0].isupper():
                continue
            key = w.lower().replace("’", "'").removesuffix("'s")
            if key in allowed or key.split("'")[0] in ("i",):
                continue
            bad.append(w)
    return bad


# Visit phrasing only: "I've been flying it" or "I've been meaning to" must not trip it, and "I've never been to" is
# the opposite of a claim.
FIRSTHAND_VISIT = re.compile(
    r"\b(?:i|we)(?:'ve| have)\s+(?:actually\s+|already\s+|personally\s+|once\s+)?"
    r"(?:been\s+(?:to|there|at|in|inside|on|down to|out to|over to)\b|visited\b|stood\s+(?:on|in|at)\b|"
    r"walked\s+(?:on|in|through)\b|landed\s+(?:at|on|in)\b)"
    r"|\b(?:i|we)\s+went\s+(?:there|to|into)\b"
    r"|\b(?:i|we)\s+(?:walked|landed|flew)\s+(?:there|to|into|in|at|on)\b"
    r"|\bwhere (?:i|we)(?:'ve| have) been\b"
    r"|\bwhen (?:i|we) (?:were|went|visited)\b")


SEVERITY_WORDS = {"minor", "moderate", "severe", "critical"}
DOWNPLAY = re.compile(r"\b(?:lowest|mildest|least|merely|only a|just a|nothing serious|not serious|no rush|"
                      r"can wait|no hurry|it'll keep|reads worse than it is|tougher than)\b")


ATTRIBUTION = re.compile(r"\b(?:brochures?|pamphlets?|leaflets?|adverts?|advertis\w*|tourist|guide ?book|travel notes?|"
                         r"according to|says|said|reads?|read that|heard|claims?|calls? it|promis\w*|boasts?|so they say|"
                         r"the sign|the listing|listed|the ad|catalogu?e|description|apparently|supposedly|reportedly|"
                         r"they say|word is|pitch|sales|slogan|tagline|little sentence|that sentence|marketing)\b")
_STOP = set("a an the and or of to in on at for with by from is are was were be been it its this that these those "
            "as your you our we us i my me he his she her they their there here not no so if but about into than then "
            "very just only also all any some more most".split())


def _uses_fact(spec: dict, low: str) -> int:
    """How many content words of the brochure's topic.fact (minus the topic's own name) the line reuses."""
    fact = " ".join(str(c["value"]) for c in spec["claims"] if c.get("predicate") == "topic.fact").lower()
    name = " ".join(str(c["value"]) for c in spec["claims"] if c.get("predicate") == "topic.name").lower()
    skip = _STOP | set(re.findall(r"[a-z']+", name))
    words = {w for w in re.findall(r"[a-z']+", fact) if len(w) > 3 and w not in skip}
    return sum(1 for w in words if re.search(rf"\b{re.escape(w)}\b", low))
ELAH_NAMES_FEELING = re.compile(
    r"\b(?:i am|i'm|i feel|i felt|i was|makes me|leaves me|i'm trying to be|i am trying to be)\s+"
    r"(?:so\s+|rather\s+|quite\s+|a little\s+|a bit\s+|very\s+|really\s+|almost\s+)?"
    r"(?:scared|afraid|frightened|nervous|anxious|worried|rattled|relieved|proud|pleased|happy|cheerful|glad|"
    r"delighted|excited|curious|bored|irritated|annoyed|frustrated|warm|touched|moved|sad|melancholy|subdued|"
    r"content|satisfied|grateful|thrilled)\b")
# Any mention of the pilot's name. "I do not know your name or when you first flew her" (blind test #24) came from
# ship.name = unknown: the model read the SHIP's name field as the pilot's. No spec carries a pilot name.
YOUR_NAME = re.compile(r"\byour (?:own )?name\b")


# DEV-HISTORY ASIDES (dev_facts.py, J 2026-09-25). A real-world fact about how the game was made is only ever said as
# its own aside, carrying its provenance, and never as something a character remembers. Enforced here, not only in the
# template, so a model that rephrases one later is held to the same rules as the numbers above.
# ⚠ "carrying its provenance" replaced "with its date" on 2026-09-27: the date must be ON THE SPEC, not IN THE MOUTH.
#   J: "we want to entertain the users not annoy them" — the player can look the fact up in the dev fact finder.
DEV_FACT_FRAME = "fun fact from the dev history"
DEV_FACT_MEMORY = re.compile(r"\b(?:i|we)\s+(?:(?:still|clearly|vividly|distinctly)\s+)?(?:remember|recall|recollect)\b"
                             r"|\bi was there\b|\bmy memory\b|\bback when (?:i|we)\b"
                             r"|\bwhen (?:i|we) (?:saw|watched|heard|read)\b")
URL_SPOKEN = re.compile(r"https?://|www\.|\b[a-z0-9-]+\.(?:com|org|net|io|gg|tv)\b")


def _int_or_none(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def ground(spec: dict, text: str) -> list[str]:
    """Deterministic hard gate. Returns [] if `text` is safe to speak for `spec`, else failure reasons.

    Checks: every required number survives (in some spoken form), no unauthorized number appears,
    no quotation marks, length inside spec['length_words'] (+5 word slack).
    """
    # A number inside a claim's PREDICATE is authorized too (e.g. "pilot.low_fuel_aborts": 2 means
    # "2 aborts"): without this, spec-naming artefacts get flagged as invented numbers.
    allowed = ({str(c["value"]) for c in spec["claims"]} | {str(v) for v in spec.get("required_values", [])}
               | {str(c["predicate"]) for c in spec["claims"]})
    allowed_nums = {n for a in allowed for n in NUM.findall(a)}

    # Curly apostrophes fold to straight: the realizers emit "It’s" (85 in the 09-23 evals), and every contraction in
    # the guards below is spelled with "'", so "I’m pleased" walked straight past ELAH_NAMES_FEELING (2026-09-25).
    low = text.lower().replace("’", "'")
    fails: list[str] = []
    for v in spec.get("required_values", []):
        hit = False
        for f in sorted(spoken_forms(int(v)), key=len, reverse=True):  # longest first
            pat = rf"(?<![\w.]){re.escape(f)}(?![\w])"
            if re.search(pat, low):
                low = re.sub(pat, " ", low)  # consume it so its parts aren't re-counted as stray numbers
                hit = True
                break
        if not hit:
            fails.append(f"missing {v}")

    bad = [n for n in NUM.findall(text.replace(",", "")) if n not in allowed_nums]
    for word, val in CARDINAL_WORDS.items():
        if re.search(rf"\b{word}\b", low) and str(val) not in allowed_nums:
            bad.append(word)
    if bad:
        fails.append(f"unauthorized numbers {bad}")

    if re.search(r'["“”]', text):
        fails.append("quotation marks")

    if spec.get("allowed_names") is not None:
        names = unauthorized_names(spec, text)
        if names:
            fails.append(f"unauthorized names {names}")

    # A visit the record does not have (dry run on J's log, 2026-09-24): with the pilot's memory saying NOT visited,
    # Elah answered "I've actually been to Hickes Research Outpost" and "I've been to Aberdeen" (2 of 3 replies). The
    # CAUSE was a stance telling her she had been everywhere (topic_graph._REPLY, fixed); this guard is the backstop in
    # case a model says it anyway. A visit is a fact about the pilot's history, so it is grounded like a number: only when
    # the spec says visited=False, and only first-person claims ("we haven't visited" never trips it).
    if spec.get("visited") is False and FIRSTHAND_VISIT.search(low):
        fails.append("claims a visit the record does not have")

    # The suit's severity LABEL is a fact like a number (2026-09-24): on a tier-3 head injury labelled "minor", the new
    # serious-injury cue produced "Head. Tier 3. Severe." and every check above passed it, because a label word is
    # not a number. With the label on the record, any OTHER label word contradicts it.
    labels = {str(c["value"]).lower() for c in spec["claims"] if c.get("predicate") == "suit.injury_severity"}
    if labels:
        wrong = [w for w in SEVERITY_WORDS - labels if re.search(rf"\b{w}\b", low)]
        if wrong:
            fails.append(f"contradicts the severity label {sorted(labels)}: {wrong}")

    # A tier-3 injury is never played down. Tested live the same afternoon: with correct facts, the model still said
    # "tier 3, which is its lowest possible reading" and Montaigne called it "merely minor". The numbers were right
    # and the meaning was backwards, which no number check can see, so the words that shrink it are refused here.
    tiers = [_int_or_none(c["value"]) for c in spec["claims"] if c.get("predicate") == "suit.injury_tier"]
    # SC counts tiers down (Tier 1 = Severe, Tier 3 = Minor; every injury line in J's logs). Corrected the same evening.
    if any(t == 1 for t in tiers) and DOWNPLAY.search(low):
        fails.append(f"plays down a severe (tier 1) injury: {DOWNPLAY.search(low).group(0)!r}")

    # BROCHURE COPY IS QUOTED, NEVER STATED (2026-09-24, J's replay). The teacher brief has always said a brochure fact
    # is something the speaker QUOTES and never states as their own knowledge; nothing at runtime enforced it. Every
    # word of "Crusader was purchased by the gas giant" and "Your name is Vasil Levski, who dreamed of an egalitarian
    # society" came from the brochure, so every number/name check passed, and the recombined sentence was false. The
    # cheap, checkable half of the rule: a brochure line must say where it got it.
    # Only a line that USES the brochure's content needs to attribute it: "We haven't visited X. Worth checking." quotes
    # nothing. Measured on the retrained topic_test: without this, 40 of 96 brochure lines were refused, most harmless.
    if ((spec.get("topic") or {}).get("status") == "brochure" and not ATTRIBUTION.search(low)
            and _uses_fact(spec, low) >= 2):
        fails.append("states brochure copy as fact (no attribution)")
    # ELAH NEVER NAMES HER OWN FEELING (J, 2026-09-24, on Montaigne's "I'm trying to be cheerful": "because he's broken
    # that's not necessarily a bad thing unless Elah does it too"). She states, shows and moves on. Montaigne, a ship
    # convinced he is an essayist, may confess a feeling now and then: for him it is character, so this is Elah-only.
    if spec.get("speaker") == "elah" and ELAH_NAMES_FEELING.search(low):
        fails.append("Elah names her own feeling")
    if spec.get("aside") == "dev_fact":
        if DEV_FACT_FRAME not in " ".join(low.split()[:16]):
            fails.append("dev fact not framed as an aside")
        if DEV_FACT_MEMORY.search(low):
            fails.append("dev fact claimed as memory")
        # ⛔ THIS USED TO REQUIRE THE DATE IN THE SPOKEN TEXT. Changed 2026-09-27 (J).
        #   The old rule — `any(d in low for d in dates)` — forced every aside to recite its
        #   publication date to the pilot, which is what made Montaigne sound like a
        #   bibliography instead of a ship.
        #   ★ The point of the rule was TRACEABILITY, not recitation: a listener must be able
        #     to check he did not invent it. That is served by the provenance travelling with
        #     the spec (and, J's point, by the in-game dev fact finder the player can search).
        #     It was never served by the words leaving his mouth.
        #   ⇒ So the requirement MOVES rather than disappears, and lands STRICTER: the spec must
        #     carry a date AND a source excerpt, not merely a date. A fact whose provenance is
        #     absent is still refused — it just is not refused for failing to say it out loud.
        #   ⚠ Everything that made this gate bite is untouched: numbers and names are still
        #     checked against the excerpt, so a rephrase that invents a year is still caught by
        #     the number gate rather than by this line.
        dates = [str(c["value"]).strip() for c in spec["claims"]
                 if c.get("predicate") in ("devfact.date", "devfact.date_spoken")]
        excerpts = [str(c["value"]).strip() for c in spec["claims"]
                    if c.get("predicate") == "devfact.excerpt"]
        if not any(dates):
            fails.append("dev fact without its date")
        if not any(excerpts):
            fails.append("dev fact without its source excerpt")
        if URL_SPOKEN.search(low):
            fails.append("dev fact speaks a URL")
    # The pilot's name is never in a spec, so "your name is ..." is always invented.
    if YOUR_NAME.search(low) and not any(c.get("predicate") == "pilot.name" for c in spec["claims"]):
        fails.append("tells the pilot their own name")

    n = len(text.split())
    lo, hi = spec["length_words"]
    if not lo <= n <= hi + 5:
        fails.append(f"length {n} not in {lo}-{hi}")

    return fails


def _selftest() -> int:
    """The visit guard (dry run 2026-09-24): catches unearned visit claims, never ordinary phrasing."""
    base = {"claims": [{"id": "C1", "predicate": "topic.name", "value": "Aberdeen"}], "required_values": [],
            "length_words": [3, 30], "allowed_names": None}
    claims = ["I've been to Aberdeen.", "I've actually been to Hickes Research Outpost.",
              "ArcCorp has a moon named Lyria where I've been.", "We went there last month.",
              "I have visited Lyria twice.", "We landed at Hickes once.", "I've been there."]
    fine = ["I've been flying it all week.", "I've been meaning to say it.", "I've never been to Lyria.",
            "We haven't visited Aberdeen.", "I ran the search on Aberdeen.", "I have been saying so.",
            "We have not been there yet.", "I went on about it.", "I went in circles."]
    visit = lambda s, t: any("visit" in f for f in ground(s, t))
    no = dict(base, visited=False)
    results = [(f"refuses: {t}", visit(no, t)) for t in claims]
    results += [(f"allows: {t}", not visit(no, t)) for t in fine]
    results.append(("a real visit (visited=True) is allowed", not visit(dict(base, visited=True), claims[0])))
    results.append(("no visited key: guard is off", not visit(base, claims[0])))
    # The severity-label guard: a different label word is refused, the recorded one and "severity" are not.
    # SC pairs Tier 1 = Severe, Tier 3 = Minor (every injury line in J's logs), so fixtures use real pairs only.
    inj = {"claims": [{"id": "C1", "predicate": "suit.injury_tier", "value": 1},
                      {"id": "C2", "predicate": "suit.injury_severity", "value": "severe"}],
           "required_values": [], "length_words": [2, 30]}
    label = lambda t: any("severity label" in f for f in ground(inj, t))
    results.append(("refuses a contradicting label: Head. Tier 1. Minor.", label("Head. Tier 1. Minor.")))
    results.append(("refuses: labelled moderate", label("Tier 1, labelled moderate. Med bed.")))
    results.append(("allows the recorded label", not label("Tier 1, severe. Med bed now.")))
    results.append(("allows the word severity", not label("The severity label is the one to trust. Med bed.")))
    down = lambda s, t: any("plays down" in f for f in ground(s, t))
    results.append(("refuses: tier 1 as the lowest reading", down(inj, "Head injury. Tier 1, the lowest reading.")))
    results.append(("refuses: merely a scratch", down(inj, "It is merely a scratch, tier 1.")))
    results.append(("refuses: treatment can wait", down(inj, "Tier 1, severe. Treatment can wait for now.")))
    results.append(("allows: tier 1, med bed now", not down(inj, "Tier 1, severe. Get to a med bed now.")))
    minor = dict(inj, claims=[{"id": "C1", "predicate": "suit.injury_tier", "value": 3},
                              {"id": "C2", "predicate": "suit.injury_severity", "value": "minor"}])
    results.append(("a MINOR tier 3 may be played down (it is minor)", not down(minor, "Tier 3, minor. No rush; it can wait.")))
    bro = {"claims": [{"id": "C1", "predicate": "topic.name", "value": "Crusader"},
                      {"id": "C2", "predicate": "topic.fact", "value": "Crusader Industries purchased the gas giant."}],
           "required_values": [], "length_words": [2, 40], "allowed_names": None, "topic": {"status": "brochure"}}
    stated = lambda t: any("brochure copy" in f for f in ground(bro, t))
    results.append(("refuses brochure copy stated as fact", stated("Crusader was purchased by the gas giant.")))
    results.append(("allows it attributed", not stated("The brochure says Crusader Industries bought the gas giant.")))
    results.append(("allows 'according to'", not stated("According to the ad, Crusader owns the whole planet.")))
    results.append(("a known (non-brochure) topic may be stated",
                    not any("brochure copy" in f for f in ground(dict(bro, topic={"status": "visited"}),
                                                                  "Crusader owns the gas giant."))))
    feel = lambda sp, t: any("names her own feeling" in f for f in ground(dict(base, speaker=sp), t))
    results.append(("Elah may not name her feeling", feel("elah", "Med bed repaired your arm. I am pleased to report it.")))
    results.append(("...nor 'trying to be cheerful'", feel("elah", "Back aboard. I'm trying to be cheerful.")))
    results.append(("...nor with a curly apostrophe", feel("elah", "Back aboard. I’m pleased.")))
    results.append(("Montaigne may (he is broken, J)", not feel("montaigne", "Back aboard. I'm trying to be cheerful.")))
    results.append(("Elah may describe the PILOT", not feel("elah", "You look pleased with yourself.")))
    results.append(("refuses 'I do not know your name' (ship.name misread, blind test #24)",
                    any("their own name" in f for f in ground(base, "I do not know your name or when you first flew her."))))
    results.append(("refuses telling the pilot their name",
                    any("their own name" in f for f in ground(bro, "Your name is Vasil Levski, says the brochure."))))
    dev = {"aside": "dev_fact", "speaker": "montaigne", "required_values": [], "length_words": [4, 40],
           "allowed_names": [], "claims": [{"id": "C1", "predicate": "devfact.title", "value": "Inside Star Citizen, Salvage Operation"},
                                           {"id": "C2", "predicate": "devfact.date", "value": "2023-12-20"},
                                           {"id": "C3", "predicate": "devfact.date_spoken", "value": "20 December 2023"}]}
    good = "Fun fact from the dev history: the video Inside Star Citizen, Salvage Operation came out on 20 December 2023."
    results.append(("dev fact: the template passes", not ground(dev, good)))
    results.append(("dev fact: an unframed statement is refused",
                    any("framed" in f for f in ground(dev, good.split(": ", 1)[1].capitalize()))))
    results.append(("dev fact: a memory claim is refused", any("memory" in f for f in ground(
        dev, "Fun fact from the dev history: I remember Inside Star Citizen, Salvage Operation, 20 December 2023."))))
    results.append(("dev fact: a wrong year is refused", any("unauthorized" in f for f in ground(
        dev, good.replace("2023", "2024")))))
    results.append(("dev fact: dropping the date is refused", any("date" in f for f in ground(
        dev, "Fun fact from the dev history: the video Inside Star Citizen, Salvage Operation came out a while ago."))))
    results.append(("dev fact: a spoken URL is refused", any("URL" in f for f in ground(dev, good + " See youtube.com."))))
    results.append(("non-dev lines are untouched by the dev-fact guards",
                    not any("dev fact" in f for f in ground(base, "Aberdeen is a sulphur moon."))))
    results.append(("no label on record: guard is off",
                    not any("severity label" in f for f in ground(base, "That looks severe."))))
    bad = [n for n, ok in results if not ok]
    for n in bad:
        print("  FAIL  " + n)
    print(f"grounding_validator selftest: {len(results) - len(bad)}/{len(results)} passed")
    return 1 if bad else 0


if __name__ == "__main__":
    import sys
    sys.exit(_selftest() if "--selftest" in sys.argv else 0)
