"""topic_graph.py - what they talk about once the live subjects are talked out (J's topic flowchart, 2026-09-23).

J, mid dry-run: "we should have an idle topic pool that they talk about ... two modes: 1. Event related flowchart
2. Random topic flowchart. So let's say we enter Crusader airspace. The agents might talk about Orison, space whales,
Crusader industries, the planets around Crusader, the daymar relay, Hathor, Wikelo, pirate activity, Yela, etc.
Then random topic flowchart would work the same way where let's say the topic is ships they could talk about
various manufacturers and her favorite ships and then talk about ship weapons, places they want to visit (if they
haven't been visited yet)."

Shape
  A graph of TOPIC NODES. Each node has sourced facts, `edges` to related nodes (the flowchart), and `anchors`
  (which live game state makes it an entry point). Two sources, merged at load:
    data/topics_lore.json  places / factions / creatures, every fact carrying its source URL (curated, not generated)
    data/ships.json        the ship DB snapshot; a ship branch (root -> manufacturers -> ships) is BUILT from it,
                           plus Elah's own opinions (favourite makers, dream ship), which are hers, not facts.
    data/ship_weapons.json weapon stats from scunpacked-data (tools/build_ship_weapons.py); a "ship weapons" branch (root ->
                           weapon makers -> weapons) is built from it, linked to ship makers that are the same company.
  EVENT mode: nodes anchored to where the pilot is are the entry; the walk goes outward along edges.
  RANDOM mode: when nothing anchors (or the local chart is talked out), a random root is walked the same way.
  The topic_ledger decides "talked out": a node gets a couple of mentions, then the walk moves along an edge.

Why facts and not a prompt: the realizer is a 1.5B model. Asked to "talk about Orison" it will invent. So every
line is a spec carrying ONE fact as a claim, and the spec sets `allowed_names`, which the grounding gate uses to
refuse any proper noun that is not in the fact (grounding_validator.ground: "unauthorized name"). A line that
invents a moon or a manufacturer is dropped, and silence is the designed failure.
"""
from __future__ import annotations

import json
import random
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Callable, Optional

DATA = Path(__file__).resolve().parent.parent / "data"
# Both characters live IN the verse: a fact that talks about patches, "the game" or developers breaks that, however
# true it is. Refused at load, so a new data file (J's brochure draft included) cannot bring one back in.
_META = re.compile(r"\b(alpha|beta|patch|developers?|CIG|Cloud Imperium|in the game|in-game|pledge|backers?|players?|concept|wiki)\b", re.I)
_LENGTH = (10, 40)
DESTINATION_SHARE = 0.4      # of topic picks while a contract names a destination
TEMPLATE_MIN_PLACES = 3      # a brochure line whose template recurs at this many places is filler (drop_template_boilerplate)


def _is_meta(text: str, places=()) -> bool:
    """Out-of-universe wording, EXCEPT inside a real place name: Pyro has "Patch City", "Broken Patch" and "People's
    Service Station Alpha" (J's 513-location pack, 2026-09-23). Blanket-matching 'patch'/'alpha' refused real places."""
    low = text.lower()
    for name in places:
        n = str(name).lower()
        if n and _META.search(n) and n in low:
            low = low.replace(n, " ")
    return _META.search(low) is not None

# Who says what, by fact status. Montaigne only ever has things secondhand; lore is his natural register.
_VOICES = {
    # J 2026-09-23: Montaigne's knowledge of PLACES comes from travel brochures and brand commercials, which he
    # quotes with far more trust than they deserve; the only thing he knows FIRSTHAND is ship and component specs.
    "in_game": [("montaigne", "ESSAY_DIGRESSION", "quoting a travel brochure he read, trusting it far more than he should"),
                ("elah", "PRACTICAL", "the part that is useful to a pilot"),
                ("montaigne", "NEAR_RECOGNITION", "he half remembers this from an advertisement and says so"),
                ("elah", "DEADPAN", "a dry aside about it")],
    "lore": [("montaigne", "ESSAY_DIGRESSION", "history he got from a tourist brochure, recited as scholarship"),
             ("montaigne", "GRAND_PHILOSOPHY_TO_TRIVIAL", "a large thought that lands on something small"),
             ("elah", "DEADPAN", "a dry aside about it")],
    # Five angles, not one. The first version had one Montaigne stance ("oddly certain about specs") and the dry run
    # heard it six times in twelve minutes, paraphrased near-verbatim each time (J 09-23: "obsessed with ships").
    "spec": [("montaigne", "EVIDENCE_SKEPTIC", "states the numbers flatly, the one thing he knows firsthand"),
             ("elah", "PRACTICAL", "what the numbers mean for actually using it"),
             ("montaigne", "HORSE_ANALOGY", "compares the ship to a horse, using its numbers"),
             ("montaigne", "PILOT_CHARACTER", "what choosing a ship like this says about a pilot"),
             ("elah", "DEADPAN", "one dry remark about what it is for")],
    # Ship weapons (Erkul numbers). Their own voices: the ship "spec" set asks Montaigne to compare the SHIP to a
    # horse and to read the pilot's character from the SHIP, which is nonsense for a laser repeater (flagged by the
    # weapons agent, 2026-09-23).
    # J 09-23: "Montaigne would be more likely be specific about weapon damage and Elah would look at the big picture
    # and not necessarily calculated stats". So the NUMERIC facts are his alone and the big-picture facts hers alone.
    "weapon": [("montaigne", "EVIDENCE_SKEPTIC", "states the gun's numbers flatly, the one thing he knows firsthand"),
               ("montaigne", "GRAND_PHILOSOPHY_TO_TRIVIAL", "a large thought about violence that lands on the number"),
               ("montaigne", "SELF_DEPRECATION", "he knows guns better than he knows himself, and says so")],
    "weapon_big": [("elah", "PRACTICAL", "what it is good for in a fight; no numbers"),
                   ("elah", "DEADPAN", "one dry line about the gun; no numbers")],
    # data/brochures.txt (J's GPT draft): marketing copy, TRUE AS QUOTED, not as fact. He reads it as scholarship.
    "brochure": [("montaigne", "ESSAY_DIGRESSION", "reading brochure copy aloud as if it were scholarship"),
                 ("montaigne", "SKEPTICAL_REVERSAL", "quoting an advertisement, then doubting it very slightly")],
    # ⛔ NO ELAH VOICE HERE, by J's rule 2026-09-24: brochures are MONTAIGNE'S limitation (a ship AI sees only what
    #   spaceport networks allow ship AIs); Elah is a suit AI who has been there and can search anything for mission
    #   prep, so she never speaks from an advertisement. See _REPLY and exchange() for how she answers his quotes.
    "changed": [("elah", "CORRECTION", "that has changed since; say it plainly"),
                ("montaigne", "SKEPTICAL_REVERSAL", "what everyone knew about it is no longer true")],
    "mont_opinion": [("montaigne", "SELF_DEPRECATION", "his own taste in ships, stated as his, first person"),
                     ("montaigne", "ESSAY_DIGRESSION", "his own taste in ships, first person, wandering a little")],
    "opinion": [("elah", "CALLBACK", "her own taste, stated as hers"),
                ("elah", "DEADPAN", "her own taste, dry")],
    # Where the current contract is taking them (J 09-23: "mission-aware"): anticipation, not arrival.
    "destination": [("montaigne", "ESSAY_DIGRESSION", "where they are headed; he has read about it and is looking forward to it"),
                    ("elah", "PRACTICAL", "where we're headed; the useful thing to know before we arrive")],
    "wishlist": [("montaigne", "ESSAY_DIGRESSION", "a place they have not been yet and he would like to see"),
                 ("elah", "PRACTICAL", "somewhere they have not been yet; worth a look")],
}

# Elah's own taste (identity, not lore). Opinions are claims she OWNS, so they need no source.
_ELAH_OPINIONS = [
    # FIRST person: she always voices these. Third person made the realizer hand her taste to the pilot
    # ("The ships you prefer are Elah's"), measured against the live service 2026-09-23.
    ("My favourite manufacturers are Drake and Aegis, for function and attitude.", ["Drake", "Aegis"]),
    ("My dream ship is the Drake Kraken, and the Privateer is the variant I want.", ["Drake", "Kraken", "Privateer"]),
    ("Crusader Industries makes the most beautiful ships in the verse, in my opinion.", ["Crusader", "Industries"]),
    # Added 2026-09-24 from her own dated takes (memory elah/fleet.md), J: "1-3 are very Elah".
    ("The Caterpillar is my favourite kind of Drake; it is built for the day it all goes wrong.", ["Caterpillar", "Drake"]),
    ("I love the Reclaimer unreasonably: gothic armour, and its job is eating garbage.", ["Reclaimer"]),
    ("The Vulture is the most honest thing Drake makes: one seat, somebody else's worst day.", ["Vulture", "Drake"]),
    # OPTIONAL (J: "keep them as optional"): placed LAST so the walk reaches them only after the core set.
    # The 400i was cut, J: "the most out of place based on your own interests".
    ("The Mercury Star Runner is my favourite Crusader; it assumes you have something to hide.", ["Mercury", "Star", "Runner", "Crusader"]),
    ("The C1 Spirit is the ship I would feel guilty scuffing.", ["C1", "Spirit"]),
    # The 400i's old slot. Was the Syulen (sc_ship_takes.md, 2026-07-17); the Railen took it 2026-09-24 after J's org
    # loaded one in under a minute, and J said "work through those" to the swap. Opinion only: FleetYards has no cargo
    # figure for the Railen yet, so she states no spec she would have to invent.
    ("The Railen is the ship I'd rather be aboard than write about: a whole crew on the grids, and the cargo's gone "
     "before anyone can count it.", ["Railen", "Gatac"]),
]

# Montaigne's taste (J 2026-09-24: his were "never fleshed out"). He knows ship SPECS firsthand, so he judges by
# engineering, and places by brochure. First person, like Elah's. The 890 Jump is the joke J asked for: he believes
# its sales pitch completely, having only ever read the brochure, and has no idea how impractical it is.
_MONT_OPINIONS = [
    ("The Carrack is the ship I dream about: built to go where no brochure has been written yet.", ["Carrack"]),
    ("I admire the Hull C for its honesty; it carries its cargo on the outside, where anyone can see it.", ["Hull", "C"]),
    ("The Constellation Andromeda is the classic, and I forgive it everything.", ["Constellation", "Andromeda"]),
    ("The 890 Jump is the pinnacle of human engineering. The brochure says so, and I have read it many times.", ["890", "Jump"]),
    ("I respect the Retaliator the way one respects a sentence with no adjectives in it.", ["Retaliator"]),
    ("A ship should be judged by its engineering, never its paint. My favourite is still the Carrack.", ["Carrack"]),
]

# Real companies a brochure may name (plus every ship maker in ships.json, which the ship branch already adds).
_COMPANIES = ("Hurston Dynamics", "microTech", "ArcCorp", "Crusader Industries", "MISC", "Tumbril", "Mirai",
              "Esperia", "Consolidated Outland", "Argo", "Anvil", "Origin", "Drake", "Aegis", "RSI",
              "Roberts Space Industries", "Covalex", "Shubin Interstellar", "Rayari", "Greycat Industrial")

_SHORT_MAKER = {"Drake Interplanetary": "Drake", "Aegis Dynamics": "Aegis", "Anvil Aerospace": "Anvil",
                "Roberts Space Industries": "RSI", "Origin Jumpworks": "Origin", "Crusader Industries": "Crusader",
                "Argo Astronautics": "Argo", "Consolidated Outland": "Consolidated Outland"}


def _slug(s: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in s.lower()).strip("_")


def _node(nid, title, facts, edges=(), anchors=None, aliases=(), root=False):
    return {"id": nid, "title": title, "facts": list(facts), "edges": list(edges), "anchors": anchors or {},
            "aliases": list(aliases), "root": root}


def _fact(text, status, names, source=""):
    return {"text": text, "status": status, "names": list(names), "source": source}


def ship_branch(ships: list[dict], per_maker: int = 5, makers: int = 10) -> list[dict]:
    """ships.json -> root 'ships' -> manufacturer nodes -> facts naming real ships. Nothing here is invented:
    every name, role and number is copied from the ship DB row. Only ships that are OUT: J 09-23, "they don't need
    to say stuff like fly now. They can talk about ... stats or aesthetics or functionality". Looks have no data
    behind them, so they stay Elah's opinion (_ELAH_OPINIONS), never a stated fact."""
    def num(v):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return int(f) if f == int(f) else round(f, 1)

    ships = [s for s in ships if s.get("in_game")]
    by_maker = defaultdict(list)
    for s in ships:
        by_maker[s["manufacturer"]].append(s)
    top = [m for m, _ in Counter({m: len(v) for m, v in by_maker.items()}).most_common(makers)]
    for fav in ("Drake Interplanetary", "Aegis Dynamics", "Crusader Industries"):
        if fav in by_maker and fav not in top:
            top.append(fav)
    nodes = []
    maker_ids = []
    for m in top:
        nid = "maker_" + _slug(m)
        maker_ids.append(nid)
        rows = sorted(by_maker[m], key=lambda s: s["name"])
        # spread the picks across the lineup instead of the first few alphabetically (Arrow, Asgard, Ballista...)
        step = max(1, len(rows) // per_maker)
        picks = rows[::step][:per_maker]
        facts = []
        if len(rows) >= 3:
            a, b, c = (s["name"] for s in picks[:3]) if len(picks) >= 3 else (s["name"] for s in rows[:3])
            facts.append(_fact(f"{m} builds the {a}, the {b} and the {c}, among others.", "spec",
                               [m, a, b, c], "ships.json"))
        for s in picks:
            focus = (s.get("focus") or "").lower().replace(" / ", " and ").replace("/", " and ")
            size = (s.get("size_label") or "").lower()
            if any(w in focus.split() for w in ("light", "medium", "heavy", "small", "large", "capital")):
                size = ""                          # "medium fighter" already carries a size: no "small medium fighter"
            length = num(s.get("length_m"))
            if focus:
                if size == "vehicle":
                    kind = f"{focus} ground vehicle"
                else:
                    kind = f"{size} {focus} ship" if size and size != "unknown" else f"{focus} ship"
                art = "an" if kind[:1] in "aeiou" else "a"
                tail = f", {length} metres long" if length else ""
                facts.append(_fact(f"The {m} {s['name']} is {art} {kind}{tail}.", "spec", [m, s["name"]],
                                   "ships.json"))
            crew, cargo = num(s.get("crew_max")), num(s.get("cargo_scu"))
            if crew and cargo:
                who = "one pilot" if crew == 1 else f"a crew of up to {crew}"
                facts.append(_fact(f"The {s['name']} takes {who} and carries {cargo} SCU of cargo.", "spec",
                                   [s["name"]], "ships.json"))
        if facts:
            nodes.append(_node(nid, m, facts, edges=["ships"], aliases=[_SHORT_MAKER.get(m, m)]))
    # Titled plainly: the title is spoken as topic.name, and "Elah's favourite ships" made her open with her own name.
    opinions = _node("elah_favourites", "favourite ships",
                     [_fact(t, "opinion", n) for t, n in _ELAH_OPINIONS],
                     edges=["maker_drake_interplanetary", "maker_aegis_dynamics", "maker_crusader_industries"])
    nodes.append(opinions)
    mont = _node("montaigne_favourites", "favourite ships",
                 [_fact(t, "mont_opinion", n) for t, n in _MONT_OPINIONS], edges=maker_ids[:0])
    nodes.append(mont)
    nodes.append(_node("ships", "ships", [], edges=["elah_favourites", "montaigne_favourites"] + maker_ids, root=True))
    return nodes


def _article(word: str) -> str:
    """'a' or 'an' for the word that follows. By sound, not spelling: 'an 8', 'an 11', 'an 18', 'a size'."""
    w = str(word).strip().lower()
    if not w:
        return "a"
    m = re.match(r"\d+", w.replace(",", ""))
    if m:                                  # eight, eighty-, eight hundred; eleven, eighteen (and their thousands)
        digits = m.group()
        lead = digits[: (len(digits) % 3) or 3]
        return "an" if digits[0] == "8" or lead in ("11", "18") else "a"
    return "an" if w[0] in "aeiou" else "a"


def _maker_key(name: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", str(name).lower().replace("'s", "")) if w != "and"]


def _same_company(a: str, b: str) -> bool:
    """'Esperia' is 'Esperia Incorporation', 'Banu' is 'Banu Souli': one name is the other's leading words."""
    ka, kb = _maker_key(a), _maker_key(b)
    n = min(len(ka), len(kb))
    return n > 0 and ka[:n] == kb[:n]


def _num(v) -> str:
    """A stored number as the fact will say it: 546, 7.6, 1230000. No thousands separator, because the gate strips
    commas from what is SPOKEN but not from the claim, and "1,230,000" in a claim would refuse its own number."""
    return str(int(v)) if float(v) == int(float(v)) else str(v)


def _listed(xs: list[str]) -> str:
    return xs[0] if len(xs) == 1 else ", ".join(xs[:-1]) + " and " + xs[-1]


_DESCRIPTORS = {"salvaged", "hazard-zone", "ballistic", "laser", "distortion", "plasma", "neutron", "tachyon",
                "driver", "mass", "defense", "division"}


# J 09-23: "Montaigne would be more likely be specific about weapon damage and Elah would look at the big picture and
# not necessarily calculated stats". So every weapon row yields two kinds of fact:
#   "weapon"      NUMERIC, Montaigne's: DPS, per-shot damage, burst vs sustained, missile payload.
#   "weapon_big"  BIG PICTURE, Elah's: what it is and what it is for, with NO numbers except a size ("size 3" is a
#                 spec, not a calculation) and whatever digits a weapon's own name carries ("CF-337").
# The big-picture words are derived from the row by the fixed rules below, never by judgement, so the same data always
# gives the same sentence and a patch that moves a gun across a line moves its description with it.
_FAST_HZ = 8.0     # >= 480 rounds a minute: repeaters (12.5) and gatlings (15-27) are fast-firing
_SLOW_HZ = 1.0     # <= 60 a minute: the C-788 (0.83) and the Slayer (0.08) are slow-firing; 1.67-6.67 says nothing
_LIGHT_X = 0.5     # per-shot damage under half the median gun of the SAME size: light hits
_HEAVY_X = 2.0     # over twice that median: heavy hits. Median per size, so size 1 and size 10 are not compared.
_LOCKS_ON = {"cross-section": "a target's cross-section", "infrared": "a target's infrared signature",
             "electromagnetic": "a target's electromagnetic signature"}


def _shown_name(r: dict, kind: str) -> str:
    shown = r["name"]
    for k in (kind, kind.split()[-1]):                 # "the CF-337 Panther is a size 3 laser repeater", not
        if shown.lower().endswith(" " + k) and len(shown) > len(k) + 1:   # "Panther Repeater ... repeater"
            cut = shown[:-len(k) - 1]
            if cut.split()[-1].lower() not in _DESCRIPTORS:   # never leave "the Ardor-1 Salvaged" dangling
                shown = cut
            break
    return shown


def _alpha_medians(weapons: list[dict]) -> dict:
    """size -> median per-shot damage over every gun of that size (the reference for light/heavy)."""
    by = defaultdict(list)
    for w in weapons:
        if w.get("family", "gun") == "gun" and w.get("alpha") and w.get("size"):
            by[w["size"]].append(float(w["alpha"]))
    out = {}
    for s, xs in by.items():
        xs.sort()
        out[s] = xs[len(xs) // 2] if len(xs) % 2 else (xs[len(xs) // 2 - 1] + xs[len(xs) // 2]) / 2
    return out


def _gun_traits(r: dict, medians: dict) -> list[str]:
    """The deterministic big-picture words for one gun. Nothing here that the row does not carry."""
    if r.get("fire_kind") == "beam":
        return ["a continuous beam"]
    traits = []
    hz = r.get("fire_rate_hz")
    if r.get("fire_kind") == "charged":
        traits.append("charged shots")
    elif hz and hz >= _FAST_HZ:
        traits.append("fast-firing")
    elif hz and hz <= _SLOW_HZ:
        traits.append("slow-firing")
    if (r.get("pellet_count") or 1) > 1:
        traits.append("a spread of pellets")
    ref = medians.get(r.get("size"))
    if r.get("alpha") and ref:
        if r["alpha"] < _LIGHT_X * ref:
            traits.append("light hits")
        elif r["alpha"] > _HEAVY_X * ref:
            traits.append("heavy hits")
    return traits


def _weapon_facts(m: str, r: dict, medians: Optional[dict] = None) -> list[dict]:
    """One weapon row -> [numeric "weapon" facts..., one "weapon_big" fact], interleaved so the node alternates
    speakers. Every number is the row's own stored (already rounded) value, copied."""
    if not r.get("size"):
        return []                                      # no size in the data: named in the lineup, never guessed
    kind = str(r.get("category") or "ship weapon").strip().lower()
    shown = _shown_name(r, kind)
    names, src, size = [m, r["name"], shown], "ship_weapons.json", _num(r["size"])
    types = [t for t in (r.get("damage_types") or []) if t]
    num, big = [], None
    if r.get("family", "gun") == "gun":
        dps = f" doing about {_num(r['dps'])} DPS" if r.get("dps") else ""
        num.append(_fact(f"The {m} {shown} is {_article('size')} size {size} {kind}{dps}.", "weapon", names, src))
        if r.get("alpha") and types:
            shot = "A fully charged shot" if r.get("fire_kind") == "charged" else "Each shot"
            num.append(_fact(f"{shot} from the {shown} hits for about {_num(r['alpha'])} "
                             f"{_listed(types)} damage.", "weapon", [r["name"], shown], src))
        sus = r.get("dps_sustain")                     # the builder stores it only when trustworthy
        if r.get("dps") and sus and abs(r["dps"] - sus) > 0.1 * r["dps"]:
            num.append(_fact(f"The {m} {shown} does about {_num(r['dps'])} DPS in a burst and about {_num(sus)} "
                             f"sustained.", "weapon", names, src))
        traits = _gun_traits(r, medians or {})
        tail = (": " + ", ".join(traits)) if traits else ""
        big = _fact(f"The {shown} is {_article('size')} size {size} {kind} from {m}{tail}".rstrip(".") + ".",
                    "weapon_big", names, src)        # rstrip: "from Amon and Reese Co." must not end ".."
    else:                                              # missiles, torpedoes, bombs: one payload, no DPS
        guide = f"{r['tracking']}-guided " if r.get("tracking") else ""
        lead = guide or "size"
        dmg = f" carrying about {_num(r['damage'])} damage" if r.get("damage") else ""
        num.append(_fact(f"The {m} {shown} is {_article(lead)} {guide}size {size} {kind}{dmg}.", "weapon", names, src))
        if r.get("tracking") in _LOCKS_ON:
            how = f" that locks on to {_LOCKS_ON[r['tracking']]}"
        elif types and kind == "bomb":
            how = f", mixing {_listed(types)} damage" if len(types) > 1 else f", all {types[0]} damage"
        else:
            how = ""
        big = _fact(f"The {shown} is {_article('size')} size {size} {kind} from {m}{how}".rstrip(".") + ".",
                    "weapon_big", names, src)
    return num[:1] + [big] + num[1:]


def weapon_branch(weapons: list[dict], ship_nodes: list[dict] = (), per_maker: int = 6,
                  makers: int = 10) -> list[dict]:
    """data/ship_weapons.json (built by tools/build_ship_weapons.py: scunpacked-data for the list and every number,
    UEX for maker names) -> root 'ship weapons' -> weapon-maker nodes -> facts naming real weapons. Same rule as
    ship_branch: every name, size, kind and number is copied from a data row (the builder rounds once; the fact carries
    that exact value, which is what lets the grounding gate authorise it). Numbers are Montaigne's (status "weapon",
    he knows components firsthand); the big picture is Elah's (status "weapon_big", no numbers). Re-running the builder after a patch swaps the numbers; nothing here changes.
    A weapon maker that also builds ships is linked both ways to its ship-maker node in `ship_nodes` (the list is
    mutated: the ship node gains the edge back), and such a maker is kept even outside the top `makers`, the way
    ship_branch keeps Elah's favourites."""
    ship_makers = [n for n in ship_nodes if n["id"].startswith("maker_")]
    by_maker = defaultdict(list)
    seen = set()
    for w in weapons:
        name, m = str(w.get("name") or "").strip(), str(w.get("manufacturer") or "").strip()
        if not name or not m or re.search(r'["“”]', name) or _META.search(name) or (m, name.lower()) in seen:
            continue
        seen.add((m, name.lower()))
        by_maker[m].append(dict(w, name=name, manufacturer=m))
    top = [m for m, _ in Counter({m: len(v) for m, v in by_maker.items()}).most_common(makers)]
    links = {m: [s for s in ship_makers if _same_company(m, s["title"])] for m in by_maker}
    top += [m for m in by_maker if links[m] and m not in top]
    medians = _alpha_medians([r for rs in by_maker.values() for r in rs])
    nodes, maker_ids = [], []
    for m in top:
        nid = "weapon_maker_" + _slug(m)
        rows = by_maker[m]
        # one representative per family ("Deadbolt I".."VI" is one family), spread across the lineup, so the list is
        # not six sizes of the same gun
        fam = defaultdict(list)
        for r in rows:                                 # "Ardor-1".."Ardor-3" and "Attrition-1".."-6" are families too
            fam[re.sub(r"^([A-Za-z]{3,})-\d+[A-Za-z]?$", r"\1", r["name"].split()[0])].append(r)
        fams = sorted(fam)
        step = max(1, len(fams) // per_maker)
        picks = []
        for f in fams[::step][:per_maker]:
            # the middle size; at equal size the plainest name, so "CF-117 Bulldog Repeater" beats its Hazard-Zone twin
            members = sorted(fam[f], key=lambda r: (r.get("size") is None, r.get("size") or 0, len(r["name"]),
                                                    r["name"]))
            sized = [r for r in members if r.get("size")]
            if sized:
                mid = sized[(len(sized) - 1) // 2]["size"]
                picks.append(next(r for r in sized if r["size"] == mid))
            else:
                picks.append(members[0])
        facts = []
        lineup = [r["name"] for r in picks]            # two families (Esperia's Deadbolt, Lightstrike) still make a list
        by_size = sorted(rows, key=lambda r: (r.get("size") or 0, len(r["name"])))
        lineup += [r["name"] for r in by_size[::max(1, len(by_size) // 3)] if r["name"] not in lineup]
        if len(lineup) >= 3:
            a, b, c = lineup[:3]
            facts.append(_fact(f"{m} builds the {a}, the {b} and the {c}, among others.", "weapon_big",
                               [m, a, b, c], "ship_weapons.json"))
        sizes = sorted({r["size"] for r in rows if r.get("size")})
        if len(sizes) >= 2:
            facts.append(_fact(f"{m} makes ship weapons from size {sizes[0]} up to size {sizes[-1]}.", "weapon_big",
                               [m], "ship_weapons.json"))
        for r in picks:
            facts += _weapon_facts(m, r, medians)
        facts = [f for f in facts if not _META.search(f["text"])]
        if not facts:
            continue
        maker_ids.append(nid)
        edges = ["ship_weapons"] + [s["id"] for s in links[m]]
        short = m.split()[0]
        nodes.append(_node(nid, m, facts, edges=edges, aliases=[short] if short != m else []))
        for s in links[m]:
            if nid not in s["edges"]:
                s["edges"].append(nid)
    nodes.append(_node("ship_weapons", "ship weapons", [], edges=maker_ids, root=True))
    for s in ship_nodes:                               # J's chain: ships, then makers and favourites, then weapons
        if s["id"] == "ships" and "ship_weapons" not in s["edges"]:
            s["edges"].append("ship_weapons")
    return nodes



# Statuses that are a character's OWN taste, not facts: first person, never "wishlist", never re-cast as KNOWN.
_OPINIONS = ("opinion", "mont_opinion")

class TopicGraph:
    def __init__(self, nodes: list[dict]):
        self.nodes = {n["id"]: n for n in nodes}
        self.places: dict[str, dict] = {}                   # data/places.json, lowercased name -> place row
        self.brochure_files: dict[str, dict] = {}
        for n in self.nodes.values():                       # drop dangling edges rather than crash on a bad file
            n["edges"] = [e for e in n["edges"] if e in self.nodes and e != n["id"]]

    @classmethod
    def load(cls, data_dir: Path = DATA) -> "TopicGraph":
        nodes: list[dict] = []
        lore = data_dir / "topics_lore.json"
        if lore.exists():
            for n in json.loads(lore.read_text(encoding="utf-8")).get("nodes", []):
                facts = [_fact(f["text"], f.get("status", "lore"), f.get("names", []), f.get("source", ""))
                         for f in n.get("facts", [])
                         if f.get("text") and f.get("source")          # unsourced: dropped
                         and not _META.search(f["text"])]              # out-of-universe: dropped
                nodes.append(_node(n["id"], n.get("title", n["id"]), facts, n.get("edges", []),
                                   n.get("anchors", {}), n.get("aliases", []), n.get("root", False)))
        ships = data_dir / "ships.json"
        ship_nodes: list[dict] = []
        if ships.exists():
            ship_nodes = ship_branch(json.loads(ships.read_text(encoding="utf-8")).get("ships", []))
            nodes += ship_nodes
        weapons = data_dir / "ship_weapons.json"
        if weapons.exists():
            nodes += weapon_branch(json.loads(weapons.read_text(encoding="utf-8")).get("weapons", []), ship_nodes)
        g = cls(nodes)
        pl = data_dir / "places.json"
        if pl.exists():
            for p in json.loads(pl.read_text(encoding="utf-8")).get("places", []):
                g.places[p["name"].lower()] = p
        bro = data_dir / "brochures.txt"
        if bro.exists():
            g.add_brochures(bro.read_text(encoding="utf-8"))
        g.brochure_files = {}
        recs = []
        bj = data_dir / "brochures.jsonl"
        if bj.exists():
            recs = [json.loads(ln) for ln in bj.read_text(encoding="utf-8").splitlines() if ln.strip()]
            for r in recs:                 # every catalogue name is a real place: known BEFORE any copy is checked
                if r.get("name") and r["name"].lower() not in g.places:
                    g.places[r["name"].lower()] = {"name": r["name"], "type": r.get("type"),
                                                   "parent": r.get("parent"), "system": r.get("system")}
        for f in sorted((data_dir / "brochures").glob("*.txt")) if (data_dir / "brochures").is_dir() else []:
            g.brochure_files[f.name] = g.add_brochure_file(f.read_text(encoding="utf-8"))
        for r in recs:
            g.brochure_files["jsonl:" + str(r.get("id"))] = g.add_brochure_record(r)
        g.template_report = g.drop_template_boilerplate()
        return g

    def apply_feelings(self, f: dict) -> int:
        """Favourites changed by experience (ship_feelings.feelings). A withdrawn favourite is MARKED, not deleted, so
        fact indices (and the told-before memory keyed on them) stay valid; new feelings are appended once each.
        Returns how many facts changed."""
        changed = 0
        for who, nid, status in (("elah", "elah_favourites", "opinion"),
                                 ("montaigne", "montaigne_favourites", "mont_opinion")):
            n = self.nodes.get(nid)
            if n is None:
                continue
            for fact in n["facts"]:
                w = fact["text"] in f.get("withdraw", {}).get(who, set())
                if w != bool(fact.get("withdrawn")):
                    fact["withdrawn"] = w
                    changed += 1
            have = {x["text"] for x in n["facts"]}
            for text, names in f.get(who, []):
                if text not in have:
                    n["facts"].append(_fact(text, status, names, "experience"))
                    changed += 1
        return changed

    def drop_template_boilerplate(self, min_places: int = TEMPLATE_MIN_PLACES) -> dict:
        """Brochure lines whose TEMPLATE (the text with its own place and names blanked out) appears at min_places or
        more different places are template filler, not a brochure. Measured 2026-09-23 on J's 513-location GPT pack:
        2,903 of 3,803 brochure lines (76%) were 114 templates reused at 3+ places ("Travel note: Amenities are listed
        only when in the starmap catalog." x376). Spoken, Montaigne would say the same sentence at hundreds of
        outposts: the per-topic ledger cannot see that, because every outpost is its own topic. Unique lines stay."""
        def key(n: dict, f: dict) -> str:
            k = re.sub(re.escape(n["title"]), "<P>", f["text"], flags=re.I)
            for nm in f["names"]:
                k = k.replace(nm, "<N>")
            return k
        where: dict[str, set] = defaultdict(set)
        for nid, n in self.nodes.items():
            if n.get("brochure"):
                for f in n["facts"]:
                    if f["status"] == "brochure":
                        where[key(n, f)].add(nid)
        dropped = 0
        for nid, n in self.nodes.items():
            if not n.get("brochure"):
                continue
            keep = [f for f in n["facts"] if f["status"] != "brochure" or len(where[key(n, f)]) < min_places]
            dropped += len(n["facts"]) - len(keep)
            n["facts"] = keep
        return {"dropped": dropped, "templates": sum(1 for v in where.values() if len(v) >= min_places)}

    def add_brochure_record(self, r: dict) -> dict:
        """One row of J's GPT pack (data/brochures.jsonl, 2026-09-23: 513 locations from SC DataHub's 4.10 catalogue).
        The copy goes through add_brochure_file, the same splitter and the same name gate as every other brochure.
        The row's `amenities` are GAME-PROVIDED (DataHub), so they become sourced in_game facts, Elah's practical
        register: "Levski has a hospital and a food court" is useful, not marketing."""
        name = str(r.get("name") or "").strip()
        if not name or r.get("access_restricted"):
            return {"node": None, "loaded": 0, "refused": ["no name, or access restricted"]}
        loc = ", ".join(x for x in (r.get("parent"), r.get("system")) if x) or "Stanton"
        # Headlines are ALL CAPS ("WELCOME TO THE CITY UNDER THE ROCK!"): every word would read as an unknown proper
        # noun to the name gate. Sentence case, with the place's own name restored where the headline uses it.
        head = str(r.get("headline") or "").strip().rstrip("!").lower()
        head = re.sub(re.escape(name.lower()), name, head)
        head = (head[:1].upper() + head[1:]) if head else ""
        text = "\n".join([name.upper(), loc, head + "!" if head else "",
                          str(r.get("brochure") or "").strip(),
                          ("Travel note: " + str(r["travel_note"]).strip()) if r.get("travel_note") else ""])
        res = self.add_brochure_file(text)
        nid = res.get("node")
        am = [a for a in (r.get("amenities") or []) if isinstance(a, str) and a.strip()]
        if nid and am:
            node = self.nodes[nid]
            title = node["title"]
            for i in range(0, min(len(am), 9), 3):          # three to a fact, at most three facts
                chunk = am[i:i + 3]
                listed = chunk[0] if len(chunk) == 1 else ", ".join(chunk[:-1]) + " and " + chunk[-1]
                node["facts"].append(_fact(f"{title} has {listed}.", "in_game", [title, *chunk],
                                           str(r.get("source_url") or "brochures.jsonl")))
                res["loaded"] = res.get("loaded", 0) + 1
        return res

    def add_brochure_file(self, text: str) -> dict:
        """One full brochure in J's format (2026-09-23, his Shepherd's Rest example):
              PLACE NAME            <- first line (all caps is fine)
              Body, Planet          <- location line, e.g. "Bloom, Pyro III"
              copy ...              <- paragraphs; short lines without end punctuation are headings
        The place becomes a node anchored to where it is (location contains its name or body), and the copy is
        cut into quotable pieces of 8-30 words. Each piece passes the same name check as a blurb, with the
        brochure's own place and location words added to what counts as known. Returns what loaded and what did
        not, with reasons, so a refused piece is visible rather than silently lost."""
        # An opening quote starts speech, so it becomes a colon: `said, "Perfect.` -> `said: Perfect.` (without this,
        # the quoted word reads as a mid-sentence capital and is refused as an unknown name).
        norm = text.replace("’", "'").replace("‘", "'")
        norm = re.sub(r",?\s*[“\"](?=[A-Z])", ": ", norm)
        norm = norm.replace("“", "").replace("”", "").replace('"', "")
        lines = [ln.strip() for ln in norm.splitlines()]
        lines = [ln for ln in lines if ln]
        if len(lines) < 3:
            return {"node": None, "loaded": 0, "refused": ["too short: need a title, a location line and copy"]}
        title = lines[0]
        place = self.places.get(title.lower())
        if place is not None:
            title = place["name"]                  # the starmap's spelling: "microTech", "Grim HEX", "ST1-02"
        elif title.isupper():
            title = " ".join(w if any(ch.isdigit() for ch in w) else w[:1].upper() + w[1:].lower()
                             for w in title.split())
        loc_words = re.findall(r"[A-Za-z][A-Za-z'-]*", lines[1])
        body = loc_words[0] if loc_words else ""
        system = loc_words[1] if len(loc_words) > 1 else ""
        if place is not None:                      # a known place anchors to its REAL system, not a parsed word
            body, system = "", place["system"]
        known = self.gazetteer() | {w.lower().removesuffix("'s") for w in re.findall(r"[A-Za-z][A-Za-z'-]*", title)}
        known |= {w.lower() for w in loc_words}
        # headings (no end punctuation) are glued to the text after them; then sentences; then merged to >= 8 words
        paras, buf = [], []
        for ln in lines[2:]:
            buf.append(ln if ln[-1] in ".!?" else ln + ":")
        sentences = re.split(r"(?<=[.!?])\s+", " ".join(buf))
        # A sentence of 8+ words stands alone. Short ones (headings, punchlines) gather with EACH OTHER; a short run
        # that never reaches 5 words rides on the next long sentence only if the pair stays under the 30-word cap.
        pieces, cur = [], ""
        for s in sentences:
            n = len(s.split())
            if n >= 8:
                if cur and len(cur.split()) >= 5:
                    pieces.append(cur)
                elif cur and len(cur.split()) + n <= 30:
                    s = f"{cur} {s}"
                cur = ""
                pieces.append(s)
                continue
            cur = f"{cur} {s}".strip()
            if len(cur.split()) >= 8:
                pieces.append(cur)
                cur = ""
        if cur and len(cur.split()) >= 5:
            pieces.append(cur)
        nid = "brochure_" + _slug(title)
        node = self.nodes.get(nid)
        if node is None:
            # Anchored by NAME only. A system anchor made every Stanton brochure "where the pilot is" the moment he
            # was anywhere in Stanton, and the walk read 46 brochures back to back. The system is kept on the node
            # for wishlist_spec (arrival in a system) instead.
            anchors = {"location_contains": [x for x in (title, body) if x]}
            node = self.nodes[nid] = _node(nid, title, [], anchors=anchors, aliases=[body] if body else [])
            node["system"] = system
            node["brochure"] = True
        loaded, refused = 0, []
        for p in pieces:
            words = re.findall(r"[A-Za-z][A-Za-z'-]*", p)
            if len(words) > 35:
                refused.append(f"too long ({len(words)} words): {p[:50]}")
                continue
            if _is_meta(p, [p2["name"] for p2 in self.places.values()] + [title]):
                refused.append(f"out-of-universe wording: {p[:50]}")
                continue
            starts = {m.end() for m in re.finditer(r"(^|[.!?:]\s+)", p)}
            unknown = [m.group() for m in re.finditer(r"[A-Za-z][A-Za-z'-]*", p)
                       if m.group()[0].isupper() and m.start() not in starts
                       and m.group().lower().removesuffix("'s") not in known and m.group() != "I"
                       and not m.group().startswith("I'")]
            if unknown:
                refused.append(f"unknown names {unknown}: {p[:50]}")
                continue
            names = sorted({m.group() for m in re.finditer(r"[A-Za-z][A-Za-z'-]*", p)
                            if m.group()[0].isupper() and m.start() not in starts and m.group() != "I"
                            and not m.group().startswith("I'")} | {title})
            node["facts"].append(_fact(p, "brochure", names, "brochures/" + nid))
            loaded += 1
        return {"node": nid, "title": title, "loaded": loaded, "refused": refused}

    def gazetteer(self) -> set:
        """Every name word the graph already knows (titles, aliases, fact names), lowercased."""
        words = set()
        srcs = [p["name"] for p in self.places.values()] + list(_COMPANIES)
        for n in self.nodes.values():
            srcs += [n["title"], *n.get("aliases", []), *(nm for f in n["facts"] for nm in f["names"])]
        for src in srcs:
            words |= {w.lower().removesuffix("'s") for w in re.findall(r"[A-Za-z][A-Za-z'-]*", src)}
        return words

    def add_brochures(self, text: str) -> list[str]:
        """'Place | brochure copy' per line. A blurb loads only if its place is a node and EVERY capitalised word
        after its first is a name the graph already knows: GPT hype is welcome, a GPT-invented moon is not.
        Returns (and keeps in self.brochure_report) one line per refused blurb, so the refusals are visible."""
        known = self.gazetteer()
        by_name = {}
        for nid, n in self.nodes.items():
            for nm in [n["title"], *n.get("aliases", [])]:
                by_name[nm.lower()] = nid
        report = []
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "|" not in line:
                report.append(f"no 'Place | text' separator: {line[:60]}")
                continue
            place, copy = (x.strip() for x in line.split("|", 1))
            nid = by_name.get(place.lower())
            if nid is None:
                report.append(f"unknown place {place!r}")
                continue
            if _is_meta(copy, [p2["name"] for p2 in self.places.values()]):
                report.append(f"out-of-universe wording: {copy[:60]}")
                continue
            if len(copy.split()) > 30:
                report.append(f"too long ({len(copy.split())} words): {copy[:60]}")
                continue
            caps = re.findall(r"[A-Za-z][A-Za-z'-]*", copy)[1:]
            unknown = [w for w in caps if w[0].isupper() and w.lower().removesuffix("'s") not in known]
            if unknown:
                report.append(f"unknown names {unknown}: {copy[:60]}")
                continue
            names = sorted({w for w in re.findall(r"[A-Za-z][A-Za-z'-]*", copy)
                            if w[0].isupper() and w.lower().removesuffix("'s") in known})
            self.nodes[nid]["facts"].append(_fact(copy, "brochure", names or [self.nodes[nid]["title"]],
                                                  "brochures.txt"))
        self.brochure_report = report
        return report

    def anchored(self, state: dict) -> list[str]:
        """Entry nodes for where the pilot is now (event mode)."""
        loc = str(state.get("location") or "").lower()
        jur = str(state.get("jurisdiction") or "").lower()
        system = str(state.get("system") or "").lower()
        hits = []
        for nid, n in self.nodes.items():
            a = n.get("anchors") or {}
            if any(x.lower() in loc for x in a.get("location_contains", []) if x) and loc:
                hits.append((0, nid))
            elif any(x.lower() in jur for x in a.get("jurisdiction_contains", []) if x) and jur:
                hits.append((1, nid))
            elif system and system in [x.lower() for x in a.get("system", [])]:
                hits.append((2, nid))
        return [nid for _, nid in sorted(hits)]

    def find_place(self, text: str) -> Optional[str]:
        """The topic node for the LONGEST known place name inside `text` ("Deliver 0/25 SCU ... to Seraphim Station"
        -> brochure_seraphim_station), preferring a brochure node, then a lore node with that title. None if none."""
        low = str(text or "").lower()
        names = sorted({p["name"] for p in self.places.values()} |
                       {n["title"] for n in self.nodes.values() if n.get("anchors") or n.get("brochure")},
                       key=len, reverse=True)
        for name in names:
            if len(name) >= 4 and name.lower() in low:
                for nid in ("brochure_" + _slug(name), _slug(name)):
                    if nid in self.nodes and self.nodes[nid]["facts"]:
                        return nid
                for nid, n in self.nodes.items():
                    if n["title"].lower() == name.lower() and n["facts"]:
                        return nid
        return None

    def roots(self) -> list[str]:
        return [nid for nid, n in self.nodes.items() if n.get("root")]


class TopicWalker:
    """Picks the next topic line. `spent(spec)` is topic_ledger.TopicLedger.spent: a node that has had its couple
    of mentions is passed over and the walk follows its edges instead."""

    def __init__(self, graph: TopicGraph, spent: Callable[[dict], bool], rng: Optional[random.Random] = None,
                 max_depth: int = 3):
        self.g, self.spent, self.rng, self.max_depth = graph, spent, rng or random.Random(), max_depth
        self._next_fact: dict[str, int] = defaultdict(int)
        self.told_before: set = set()                     # {(node_id, fact_index)} already told; see _spec
        self.destination: Optional[str] = None            # node id a contract is taking them to; set_destination
        self.focus: Optional[str] = None
        self.mode = "none"
        self._recent_branches: list[str] = []

    def _spec(self, nid: str, variant: int, visited: set) -> Optional[dict]:
        n = self.g.nodes[nid]
        if not n["facts"]:
            return None
        # The next fact of this node that has NOT been told before (this session, or in the pilot's memory within
        # the last TOLD_WINDOW_DAYS: CompanionCore seeds told_before from memory_store callbacks). A node whose facts
        # have all been told is out of material, not a place to start repeating (J 09-23: "obsessively repeating").
        start = self._next_fact[nid]
        i = next(((start + k) % len(n["facts"]) for k in range(len(n["facts"]))
                  if (nid, (start + k) % len(n["facts"])) not in self.told_before
                  and not n["facts"][(start + k) % len(n["facts"])].get("withdrawn")), None)
        if i is None:
            return None
        f = n["facts"][i]                                 # no counter write here: _spec also runs for nodes the
                                                          # walk only LOOKS at; _took moves the counter
        wish = self.mode == "wishlist" or (n.get("anchors") and n["title"].lower() not in visited
                                           and self.mode == "random" and f["status"] not in _OPINIONS)
        if self.mode == "destination" and f["status"] not in _OPINIONS + ("spec", "weapon", "weapon_big"):
            kind = "destination"
        elif wish:
            kind = "wishlist"
        else:
            kind = f["status"]
        if kind == "spec" and self.branch(nid) == "weapons":
            kind = "weapon"                               # guns get gun voices, not the ship-spec ones
        # "weapon" (numbers, Montaigne) and "weapon_big" (big picture, Elah) each map to their own list as-is
        voices = _VOICES.get(kind, _VOICES["lore"])
        speaker, move, stance = voices[variant % len(voices)]
        ckind = "OPINION" if f["status"] in _OPINIONS else "KNOWN"
        spec = {
            "scenario": "topic",
            "speaker": speaker,
            "rhetoric": [move],
            "claims": [{"id": "C1", "kind": ckind, "predicate": "topic.name", "value": n["title"]},
                       {"id": "C2", "kind": ckind, "predicate": "topic.fact", "value": f["text"]}],
            "interpretation": {"owner": speaker, "text": stance},
            "required_claims": ["C2"],
            "required_values": [],
            "length_words": list(_LENGTH),
            "allowed_names": sorted(set(f["names"]) | {n["title"]} | set(n.get("aliases", []))),
            "topic": {"node": nid, "fact": i, "status": f["status"], "source": f["source"], "mode": self.mode},
            "id": f"topic_{nid}_{i}_v{variant % len(voices)}",
        }
        return spec

    def _walk_from(self, start: list[str], variant: int, visited: set) -> Optional[dict]:
        seen, frontier = set(), list(start)
        for _ in range(self.max_depth + 1):
            nxt = []
            for nid in frontier:
                if nid in seen:
                    continue
                seen.add(nid)
                spec = self._spec(nid, variant, visited)
                if spec is not None and not self.spent(spec):
                    return spec
                edges = list(self.g.nodes[nid]["edges"])
                self.rng.shuffle(edges)
                nxt += edges
            frontier = nxt
        return None

    def set_destination(self, text: str) -> Optional[str]:
        """A contract or objective named a place: find it and lean the walk toward it. Returns the node id or None."""
        nid = self.g.find_place(text)
        self.destination = nid
        return nid

    def clear_destination(self) -> None:
        self.destination = None

    def next_spec(self, state: dict, variant: int = 0, visited: Optional[set] = None) -> Optional[dict]:
        visited = {v.lower() for v in (visited or set())}
        # Where the contract is taking them, some of the time (not every tick: the destination is one subject, and
        # a subject owned every tick is the obsession J complained about). Its own node first, then its neighbours.
        if getattr(self, "destination", None) in self.g.nodes and self.rng.random() < DESTINATION_SHARE:
            prev, self.mode = self.mode, "destination"
            spec = self._walk_from([self.destination], variant, visited)
            if spec is not None:
                return self._took(spec)
            self.mode = prev
        entry = self.g.anchored(state)
        if entry:
            self.mode = "event"
            start = ([self.focus] if self.focus in self.g.nodes and self.focus in self._reach(entry) else []) + entry
            spec = self._walk_from(start, variant, visited)
            if spec is not None:
                return self._took(spec)
        # Random mode walks from the roots AND from a few random places with brochures (J 09-23: "randomly select
        # locations to talk about"), shuffled together so a ship tangent and a far-off outpost are equally likely.
        places = [nid for nid, n in self.g.nodes.items() if n.get("brochure")
                  and n["title"].lower() not in visited]
        lore = [nid for nid in self.g.nodes if self.branch(nid) == "lore" and self.g.nodes[nid]["facts"]]
        roots = (self.g.roots() + self.rng.sample(places, min(3, len(places)))
                 + self.rng.sample(lore, min(3, len(lore))))
        # Branch fatigue: the last two lines' branches sit out when anything else is on offer. Without it the ship
        # branch (12 makers x 2 mentions) held the floor for twelve minutes of the dry run (J 09-23: "obsessed with
        # ships"). Falls back to the full list rather than going silent when only a tired branch is left.
        tired = set(self._recent_branches[-2:])
        fresh = [nid for nid in roots if self.branch(nid) not in tired]
        roots = fresh or roots
        self.rng.shuffle(roots)
        self.mode = "random"
        # The last topic gets a head start only if its branch is not tired, or it re-wins every tick.
        keep_focus = (self.focus in self.g.nodes and self.focus not in entry
                      and (self.branch(self.focus) not in tired or not fresh))
        start = ([self.focus] if keep_focus else []) + roots
        spec = self._walk_from(start, variant, visited)
        return self._took(spec) if spec is not None else None

    # Who answers whom in a topic exchange (J 09-23: "banter also needs to fire off more", and the Mk II "occasionally
    # talks about random topics too"). Turn 2 carries turn 1's claims and adds none: the same subset rule as banter.py.
    # Elah answers from FIRSTHAND knowledge, never from the advertisement (J 2026-09-24). The old stances told her
    # "she has heard this brochure before", and ~1,100 of her training lines duly quoted brochures.
    # ⚠ Until 2026-09-24 these two said "she has actually been there" / "from having been there herself" for EVERY topic,
    #   visited or not, and the dry run on J's log heard it: "I've been to Aberdeen", "I've actually been to Hickes
    #   Research Outpost", with nothing in his record. The model was not overreaching; the stance told it to. Firsthand
    #   is now only _REPLY_BEEN, chosen when the pilot's record says so.
    _REPLY = {"elah": [("CORRECTION", "one line on what it is really like, from what she actually knows; never the advertisement"),
                       ("DEADPAN", "one dry line from her own knowledge; she does not quote ads")],
              "montaigne": [("SKEPTICAL_REVERSAL", "he doubts her in one line, secondhand as always"),
                            ("SELF_DEPRECATION", "he concedes, in one line, that she may know better")]}

    # Elah answering one of Montaigne's BROCHURE quotes, by whether the pilot has actually been there.
    _REPLY_BEEN = [("CORRECTION", "she has been there with the pilot; one line on what it is really like, never the advertisement"),
                   ("DEADPAN", "one dry line from having actually been there; she does not quote ads")]
    _REPLY_SEARCHED = [("CORRECTION", "she ran a real search on it for mission prep; the actual facts, not tourist copy, and a poke at him for taking travel advice from ads"),
                       ("DEADPAN", "one dry line from her own search results, needling him for reading advertisements")]

    def exchange(self, state: dict, variant: int = 0, visited: Optional[set] = None) -> list[dict]:
        """A two-turn banter exchange about the next topic: [opening, reply], or [] if no topic is left.
        The opening is Montaigne whenever his voice fits the fact (he is the one who quotes brochures); Elah opens
        on her own opinions and on a gun's big picture ("weapon_big" has no Montaigne voice to swap in). Specs follow banter.plan_exchange's shape so run_exchange can play them."""
        first = self.next_spec(state, variant, visited)
        if first is None:
            return []
        kind = first["topic"]["status"]
        if first["speaker"] != "montaigne" and kind != "opinion":
            mont = [v for v in _VOICES.get(kind, []) if v[0] == "montaigne"]
            if mont:
                _, move, stance = mont[variant % len(mont)]
                first = dict(first, speaker="montaigne", rhetoric=[move],
                             interpretation={"owner": "montaigne", "text": stance})
        other = "elah" if first["speaker"] == "montaigne" else "montaigne"
        move, stance = self._REPLY[other][variant % len(self._REPLY[other])]
        topic_title = str(self.g.nodes[first["topic"]["node"]]["title"]).lower()
        been = topic_title in {str(v).lower() for v in (visited or set())}
        if other == "elah" and been:
            move, stance = self._REPLY_BEEN[variant % 2]
        ex_id = f"ban_topic_{first['topic']['node']}_{first['topic']['fact']}"
        first = dict(first, scenario="banter_topic", id=f"{ex_id}_t1", exchange_id=ex_id, turn=1)
        claims, required, names = [dict(c) for c in first["claims"]], ["C1"], list(first["allowed_names"])
        if other == "elah" and first["topic"]["status"] == "brochure":
            # She does not get his brochure line to answer (she would only quote it back). She gets the place's
            # REAL facts if any exist, so her reply is what she actually knows; otherwise just the place's name.
            node = self.g.nodes[first["topic"]["node"]]
            # Been there vs searched it (J 2026-09-24 06:57): where the pilot HAS been, she speaks from having been
            # there; where they have NOT, from a real mission-prep search, which is also her chance to needle him
            # for taking his travel advice from ads. `visited` holds lowercased titles, as in _spec.
            move, stance = (self._REPLY_BEEN if been else self._REPLY_SEARCHED)[variant % 2]
            real = [f for f in node["facts"] if f["status"] not in ("brochure",) + _OPINIONS]
            claims = [c for c in claims if c["predicate"] == "topic.name"]
            if real:
                f = real[variant % len(real)]
                claims.append({"id": "C2", "kind": "KNOWN", "predicate": "topic.fact", "value": f["text"]})
                required = ["C2"]
                names = sorted(set(names) | set(f["names"]))
        reply = {
            "scenario": "banter_topic", "speaker": other, "rhetoric": [move],
            "claims": claims,
            "interpretation": {"owner": other, "text": stance},
            "required_claims": required, "required_values": [], "length_words": [6, 24],
            "allowed_names": names, "topic": dict(first["topic"]),
            "id": f"{ex_id}_t2", "exchange_id": ex_id, "turn": 2,
            # grounding refuses a firsthand visit claim when the record says she has not been (grounding_validator)
            **({"visited": bool(been)} if other == "elah" else {}),
            "responds_to": {"speaker": first["speaker"], "stance": first["interpretation"]["text"],
                            "move": first["rhetoric"][0], "relation": "deflate"},
        }
        return [first, reply]

    def wishlist_spec(self, system: str, variant: int = 0, visited: Optional[set] = None) -> Optional[dict]:
        """On entering a system (J 09-23: "mention locations they want to visit when they enter the system"): one
        place in it with a brochure that the pilot has not been to, spoken as somewhere they would like to see."""
        visited = {v.lower() for v in (visited or set())}
        cands = [nid for nid, n in self.g.nodes.items() if n.get("brochure")
                 and str(n.get("system", "")).lower() == str(system or "").lower()
                 and n["title"].lower() not in visited]
        self.rng.shuffle(cands)
        prev, self.mode = self.mode, "wishlist"
        try:
            for nid in cands:
                spec = self._spec(nid, variant, visited)
                if spec is not None and not self.spent(spec):
                    return self._took(spec)
        finally:
            self.mode = prev
        return None

    def _reach(self, entry: list[str]) -> set:
        seen, frontier = set(), list(entry)
        for _ in range(self.max_depth + 1):
            nxt = []
            for nid in frontier:
                if nid not in seen:
                    seen.add(nid)
                    nxt += self.g.nodes[nid]["edges"]
            frontier = nxt
        return seen

    def branch(self, nid: str) -> str:
        n = self.g.nodes.get(nid) or {}
        if nid == "ships" or nid.startswith("maker_") or nid == "elah_favourites":
            return "ships"
        if nid == "ship_weapons" or nid.startswith("weapon_maker_"):
            return "weapons"                   # its own branch, so fatigue does not count a gun as a ship line
        return "brochure" if n.get("brochure") else "lore"

    def told(self, nid: str, fact: int) -> None:
        """A topic fact was actually SPOKEN (the core calls this from _spoke); never offer it again."""
        self.told_before.add((nid, int(fact)))

    def _took(self, spec: dict) -> dict:
        nid = spec["topic"]["node"]
        self._next_fact[nid] = spec["topic"]["fact"] + 1  # the next mention starts after the fact just taken
        self.focus = nid
        self._recent_branches = (self._recent_branches + [self.branch(nid)])[-4:]
        return spec


def _selftest() -> int:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from topic_ledger import TopicLedger
    ok = total = 0

    def case(name, cond, detail=""):
        nonlocal ok, total
        total += 1
        ok += bool(cond)
        print(("  PASS  " if cond else "  FAIL  ") + name + (f"  ({detail})" if detail and not cond else ""))

    lore = [
        _node("crusader", "Crusader", [_fact("Crusader is a gas giant in Stanton.", "in_game", ["Crusader", "Stanton"], "u")],
              edges=["orison", "yela"], anchors={"jurisdiction_contains": ["Crusader"]}),
        _node("orison", "Orison", [_fact("Orison floats in the clouds of Crusader.", "in_game", ["Orison", "Crusader"], "u"),
                                   _fact("Orison is built on floating platforms.", "in_game", ["Orison"], "u")],
              edges=["crusader"], anchors={"location_contains": ["Orison"]}),
        _node("yela", "Yela", [_fact("Yela is an icy moon of Crusader with an asteroid ring.", "in_game",
                                     ["Yela", "Crusader"], "u")], edges=["crusader"]),
    ]
    ships = [{"name": n, "manufacturer": m, "focus": "Combat", "in_game": True}
             for m, ns in (("Drake Interplanetary", ["Cutlass Black", "Buccaneer", "Herald", "Kraken"]),
                           ("Aegis Dynamics", ["Gladius", "Hammerhead", "Sabre"])) for n in ns]
    g = TopicGraph(lore + ship_branch(ships))
    case("anchors: Orison location is the first entry", g.anchored({"location": "Orison - Batamoa",
                                                                     "jurisdiction": "Crusader Security"})[0] == "orison")
    case("ship branch has a root", "ships" in g.roots())
    case("favourites node carries Elah's opinions", len(g.nodes["elah_favourites"]["facts"]) == len(_ELAH_OPINIONS))
    case("Montaigne has his own favourites, all his", len(g.nodes["montaigne_favourites"]["facts"]) == len(_MONT_OPINIONS)
         and all(f["status"] == "mont_opinion" for f in g.nodes["montaigne_favourites"]["facts"])
         and all(v[0] == "montaigne" for v in _VOICES["mont_opinion"]))
    case("the 400i is not among Elah's opinions (J cut it)", not any("400i" in t for t, _ in _ELAH_OPINIONS))
    case("maker facts name real ships only", "Cutlass Black" in json.dumps(g.nodes["maker_drake_interplanetary"]))

    led = TopicLedger()
    w = TopicWalker(g, led.spent, rng=random.Random(7))
    st = {"location": "Orison - Batamoa", "jurisdiction": "Crusader Security", "system": "Stanton"}
    said = []
    for v in range(12):
        s = w.next_spec(st, v, visited={"Orison"})
        if s is None:
            said.append(None)
            continue
        led.record(s)
        said.append((s["topic"]["node"], s["topic"]["mode"]))
    nodes = [x[0] for x in said if x]
    case("event mode starts where the pilot is", said[0] == ("orison", "event"), str(said[:2]))
    case("a node gets at most two mentions", max(Counter(nodes).values()) <= 2, str(Counter(nodes)))
    case("it walks the chart: Crusader and Yela come up", {"crusader", "yela"} <= set(nodes), str(nodes))
    case("once the local chart is talked out it goes random", any(x and x[1] == "random" for x in said), str(said))
    case("two mentions of Orison use two different facts", w._next_fact["orison"] == 2, str(w._next_fact["orison"]))
    s = w._spec("elah_favourites", 0, set())
    case("opinions are OPINION claims spoken by Elah", s["speaker"] == "elah" and s["claims"][1]["kind"] == "OPINION")
    chosen = g.nodes["elah_favourites"]["facts"][s["topic"]["fact"]]
    case("allowed_names carries the chosen fact's names", set(chosen["names"]) <= set(s["allowed_names"]),
         f"{chosen['names']} vs {s['allowed_names']}")
    g2 = TopicGraph(lore)
    rep = g2.add_brochures("# comment\n"
                           "Orison | Orison: where the clouds hold you up so your worries don't have to.\n"
                           "Orison | Visit Orison and the famous Glimmerdome of Yela!\n"
                           "Atlantis | The lost city awaits.\n"
                           "Yela | Yela, now in beta, is colder than ever.\n"
                           "no separator here\n")
    bro = [f for f in g2.nodes["orison"]["facts"] if f["status"] == "brochure"]
    case("brochure: a clean blurb loads as a brochure fact", len(bro) == 1 and "clouds hold you" in bro[0]["text"])
    case("brochure: an invented name is refused", any("Glimmerdome" in r for r in rep), str(rep))
    case("brochure: an unknown place is refused", any("Atlantis" in r for r in rep))
    case("brochure: out-of-universe wording is refused", any("out-of-universe" in r for r in rep))
    case("brochure: a malformed line is reported, not crashed on", any("separator" in r for r in rep))
    case("brochure facts are Montaigne's to quote", _VOICES["brochure"][0][0] == "montaigne")
    g3 = TopicGraph(lore)
    g3.places = {"grim hex": {"name": "Grim HEX", "type": "Manmade", "parent": "Yela", "system": "Stanton"},
                 "ruin station": {"name": "Ruin Station", "type": "Manmade", "parent": "Pyro", "system": "Pyro"}}
    r1 = g3.add_brochure_file("GRIM HEX\nYela, Stanton\nThe friendliest rock in the Yela belt, if you do not ask "
                              "questions. Come for the docking, stay because leaving is complicated.\n")
    g3.add_brochure_file("RUIN STATION\nPyro\nA station that has seen things, and would like to see more of your "
                         "credits. Everything here is second hand, including the air.\n")
    case("brochure title takes the starmap's spelling", r1["title"] == "Grim HEX", r1["title"])
    n3 = g3.nodes[r1["node"]]
    case("a known place anchors by NAME only, and keeps its real system for arrivals",
         n3["anchors"] == {"location_contains": ["Grim HEX"]} and n3["system"] == "Stanton" and n3["aliases"] == [],
         str(n3["anchors"]))
    w3 = TopicWalker(g3, TopicLedger().spent, rng=random.Random(3))
    ws = w3.wishlist_spec("Pyro", 0, visited=set())
    case("entering Pyro: a Pyro place they have not visited", ws is not None and ws["topic"]["node"] ==
         "brochure_ruin_station" and ws["interpretation"]["text"].startswith(("a place", "somewhere")), str(ws))
    case("wishlist skips places already visited", w3.wishlist_spec("Pyro", 0, visited={"Ruin Station"}) is None)
    case("wishlist never offers another system's place", w3.wishlist_spec("Nyx", 0, visited=set()) is None)
    seen = set()
    w4 = TopicWalker(g3, TopicLedger().spent, rng=random.Random(11))
    for v in range(8):
        s = w4.next_spec({}, v, visited=set())
        if s:
            seen.add(s["topic"]["node"])
    case("random mode reaches places, not only roots", bool(seen & {"brochure_grim_hex", "brochure_ruin_station"}),
         str(seen))
    w5 = TopicWalker(g, TopicLedger().spent, rng=random.Random(5))
    branches = []
    for v in range(12):
        s = w5.next_spec({}, v, visited=set())
        if s:
            branches.append(w5.branch(s["topic"]["node"]))
    runs = max((len(list(grp)) for _, grp in __import__("itertools").groupby(branches)), default=0)
    case("branch fatigue: never three ship lines running while lore is on offer", runs <= 2 and "lore" in branches,
         str(branches))
    import banter as _banter
    w6 = TopicWalker(g, TopicLedger().spent, rng=random.Random(2))
    ex = w6.exchange({"location": "Orison - Batamoa", "system": "Stanton"}, 0, visited=set())
    case("topic exchange: two turns, different speakers", len(ex) == 2 and ex[0]["speaker"] != ex[1]["speaker"],
         str([e["speaker"] for e in ex]))
    case("topic exchange: Montaigne opens on a non-opinion fact",
         ex[0]["speaker"] == "montaigne" or ex[0]["topic"]["status"] == "opinion")
    case("topic exchange: the reply adds no claim (banter's subset rule)",
         not _banter._subset_violation(ex[0], ex[1]) and ex[1]["responds_to"]["speaker"] == ex[0]["speaker"])
    wg = TopicGraph.load()
    wmakers = [nid for nid in wg.nodes if TopicWalker(wg, TopicLedger().spent).branch(nid) == "weapons"
               and wg.nodes[nid]["facts"]]
    if wmakers:
        ww = TopicWalker(wg, TopicLedger().spent)
        stances = {ww._spec(wmakers[0], v, set())["interpretation"]["text"] for v in range(4)}
        case("weapon facts use the weapon voices, never the ship 'horse' or 'pilot' ones",
             not any("horse" in s or "choosing a ship" in s for s in stances), str(stances))
    dg = TopicGraph.load()
    nid = dg.find_place("Deliver 0/25 SCU of Construction Salvage to Seraphim Station")
    case("find_place: the objective's destination is found", nid is not None and "seraphim" in nid, str(nid))
    case("find_place: no place named -> None", dg.find_place("Locate the missing cargo") is None)
    dw = TopicWalker(dg, TopicLedger().spent, rng=random.Random(1))
    dw.set_destination("Deliver 0/25 SCU of Construction Salvage to Seraphim Station")
    picks = [dw.next_spec({"location": "Orison - Batamoa", "system": "Stanton"}, v, visited=set()) for v in range(20)]
    dest = [p for p in picks if p and p["topic"]["mode"] == "destination"]
    case("destination: part of the talk leans to where they're headed, not all of it",
         0 < len(dest) < len(picks) and all("headed" in p["interpretation"]["text"] for p in dest),
         f"{len(dest)}/{len(picks)}")
    dw.clear_destination()
    case("destination: cleared -> no destination picks",
         all(p is None or p["topic"]["mode"] != "destination"
             for p in [dw.next_spec({"system": "Stanton"}, v, visited=set()) for v in range(8)]))
    real = TopicGraph.load()
    case("loads the bundled data without crashing", len(real.nodes) > 0, str(len(real.nodes)))
    case("out-of-universe facts are refused", _META.search("Orison became playable in Alpha 3.14") is not None
         and _META.search("Orison floats in the clouds of Crusader.") is None)
    leaks = [f["text"] for n in real.nodes.values() for f in n["facts"] if _is_meta(f["text"], [p["name"] for p in real.places.values()])]
    case("no loaded fact is out-of-universe", not leaks, str(leaks[:2]))

    # --- ship weapons branch (J 09-23: "then talk about ship weapons") ---
    from grounding_validator import ground, unauthorized_names
    kw = [{"name": n, "manufacturer": "Klaus and Werner", "family": "gun", "category": "laser repeater", "size": s,
           "dps": d, "alpha": a, "dps_sustain": su, "fire_kind": "single", "fire_rate_hz": 12.5,
           "damage_types": ["energy"]}
          for n, s, d, a, su in (("CF-117 Bulldog Repeater", 1, 219, 17, 210), ("CF-227 Badger Repeater", 2, 328, 26, None),
                                 ("CF-337 Panther Repeater", 3, 546, 44, 279), ("CF-447 Rhino Repeater", 4, 818, 65, None))]
    kw.append({"name": "Sledge III Mass Driver Cannon", "manufacturer": "Klaus and Werner", "family": "gun",
               "category": "mass driver cannon", "size": 3, "dps": 562, "alpha": 1125, "fire_kind": "charged",
               "fire_rate_hz": 0.5, "damage_types": ["physical"]})
    rsi = [{"name": n, "manufacturer": "Roberts Space Industries", "family": "gun", "category": "ballistic cannon",
            "size": s, "dps": d, "alpha": a, "fire_kind": "single", "fire_rate_hz": 0.83, "damage_types": ["physical"]}
           for n, s, d, a in (("Leonids Cannon", 5, 1333, 1600), ("Maris Cannon", 6, 2833, 3400),
                              ("RSI Medusa Cannon", 8, 4500, 5400))]
    extra = [{"name": "Colossus Bomb", "manufacturer": "Klaus and Werner", "family": "bomb", "category": "bomb",
              "size": None, "damage": 568297},
             {"name": "", "manufacturer": "Klaus and Werner", "category": "cannon", "size": 2},
             {"name": "Viper III Missile", "manufacturer": "Nova Pyrotechnica", "family": "missile",
              "category": "missile", "size": 3, "damage": 2650, "tracking": "infrared"}]
    sb = ship_branch(ships + [{"name": n, "manufacturer": "Roberts Space Industries", "focus": "Exploration",
                               "in_game": True} for n in ("Constellation Andromeda", "Aurora MR", "Polaris")])
    wb = weapon_branch(kw + rsi + extra, sb)
    gw = TopicGraph(lore + sb + wb)
    ww = TopicWalker(gw, TopicLedger().spent, rng=random.Random(1))
    kid, rid = "weapon_maker_klaus_and_werner", "weapon_maker_roberts_space_industries"
    case("weapons: a 'ship weapons' root", gw.nodes.get("ship_weapons", {}).get("root") is True
         and gw.nodes["ship_weapons"]["title"] == "ship weapons" and {kid, rid} <= set(gw.nodes["ship_weapons"]["edges"]))
    kfacts = [f["text"] for f in gw.nodes[kid]["facts"]]
    case("weapons: maker lineup names real weapons", any(t.startswith("Klaus and Werner builds the CF-") and
                                                         t.endswith(", among others.") for t in kfacts), str(kfacts))
    panther = "The Klaus and Werner CF-337 Panther is a size 3 laser repeater doing about 546 DPS."
    case("weapons: per-weapon fact with the stored DPS, kind not repeated", panther in kfacts, str(kfacts))
    case("weapons: alpha fact copies the stored number and damage type",
         "Each shot from the CF-337 Panther hits for about 44 energy damage." in kfacts, str(kfacts))
    vfacts = [f["text"] for f in gw.nodes["weapon_maker_nova_pyrotechnica"]["facts"]]
    case("weapons: a missile reads 'an infrared-guided', with its payload",
         "The Nova Pyrotechnica Viper III is an infrared-guided size 3 missile carrying about 2650 damage." in vfacts,
         str(vfacts))
    case("weapons: numbers are written without separators (the gate strips commas only from speech)",
         _num(1230000) == "1230000" and _num(7.6) == "7.6" and _num(546.0) == "546")
    case("weapons: a weapon with no size is never given one", not any("Colossus" in t for t in kfacts)
         and all(f["status"] in ("weapon", "weapon_big") for f in gw.nodes[kid]["facts"]), str(kfacts))
    case("weapons: a/an by sound", (_article("size"), _article("8"), _article("11"), _article("18"), _article("80"),
                                    _article("1"), _article("ion cannon"), _article("repeater"))
         == ("a", "an", "an", "an", "an", "a", "an", "a"))
    case("weapons: branch label is 'weapons', ships stay 'ships'",
         (ww.branch("ship_weapons"), ww.branch(kid), ww.branch("maker_drake_interplanetary")) ==
         ("weapons", "weapons", "ships"))
    case("weapons: RSI guns link to RSI ships, both ways; Klaus and Werner links to no ship maker",
         "maker_roberts_space_industries" in gw.nodes[rid]["edges"]
         and rid in gw.nodes["maker_roberts_space_industries"]["edges"]
         and not [e for e in gw.nodes[kid]["edges"] if e.startswith("maker_")], str(gw.nodes[rid]["edges"]))
    case("weapons: the ships root leads on to ship weapons", "ship_weapons" in gw.nodes["ships"]["edges"])
    ww._next_fact[kid] = kfacts.index(panther)
    ws = ww._spec(kid, 0, set())
    line = "Klaus and Werner's CF-337 Panther Repeater, size three, puts out about 546 DPS. I would trust it over a horse."
    case("weapons: the gate passes a line repeating weapon and maker names and the DPS",
         not unauthorized_names(ws, line) and not ground(ws, line), f"{unauthorized_names(ws, line)} {ground(ws, line)}")
    case("weapons: ... and still refuses an invented maker",
         unauthorized_names(ws, "The CF-337 Panther outguns anything Gallenson ever built, said the pilot.")
         == ["Gallenson"])
    case("weapons: ... and a DPS figure that is not the stored one",
         any("unauthorized numbers" in x for x in ground(ws, line.replace("546", "600"))))
    if (DATA / "ship_weapons.json").exists():
        case("weapons: the bundled data loads a weapons root with maker nodes", "ship_weapons" in real.roots()
             and len(real.nodes["ship_weapons"]["edges"]) >= 5, str(real.nodes.get("ship_weapons")))

    # --- J 09-23: numbers are Montaigne's ("weapon"), the big picture is Elah's ("weapon_big") ---
    def no_digits_but_size(f):
        t = re.sub(r"\bsize \d+\b", "", f["text"])
        for nm in sorted(f["names"], key=len, reverse=True):   # a weapon's own name may carry digits ("CF-337")
            t = t.replace(nm, "")
        return not re.search(r"\d", t)
    bigs = [f for n in list(gw.nodes.values()) + [real.nodes[x] for x in real.nodes if x.startswith("weapon_maker_")]
            for f in n["facts"] if f["status"] == "weapon_big"]
    case("weapon_big: no digits except a size or inside a weapon's own name",
         bigs and all(no_digits_but_size(f) for f in bigs), str([f["text"] for f in bigs if not no_digits_but_size(f)][:3]))
    wfacts = gw.nodes[kid]["facts"]
    wb_i = next(i for i, f in enumerate(wfacts) if f["status"] == "weapon_big"
                and f["text"].startswith("The CF-337 Panther"))
    speakers = set()
    for i, st in ((kfacts.index(panther), "weapon"), (wb_i, "weapon_big")):
        ww._next_fact[kid] = i
        speakers |= {(st, ww._spec(kid, v, set())["speaker"]) for v in range(6)}
    case("Montaigne speaks every numeric 'weapon' fact, Elah every 'weapon_big' fact",
         speakers == {("weapon", "montaigne"), ("weapon_big", "elah")}, str(speakers))
    case("weapon_big: fast-firing, light hits, from the row (Panther 12.5 Hz, 44 vs size-3 median)",
         wfacts[wb_i]["text"] == "The CF-337 Panther is a size 3 laser repeater from Klaus and Werner: fast-firing, "
                                 "light hits.", wfacts[wb_i]["text"])
    case("weapon_big: charged and slow-firing are read from fire_kind and fire_rate_hz",
         "The Sledge III is a size 3 mass driver cannon from Klaus and Werner: charged shots." in kfacts and
         any(t.startswith("The Leonids is a size 5 ballistic cannon from Roberts Space Industries: slow-firing")
             for t in (f["text"] for f in gw.nodes[rid]["facts"])), str(kfacts))
    case("weapon_big: the maker lineup is Elah's big picture",
         all(f["status"] == "weapon_big" for f in wfacts if "among others" in f["text"]))
    case("a trustworthy sustain gives the burst-and-sustained sentence; one within 10% of burst does not",
         "The Klaus and Werner CF-337 Panther does about 546 DPS in a burst and about 279 sustained." in kfacts
         and not any("Bulldog does about" in t for t in kfacts), str(kfacts))
    if (DATA / "ship_weapons.json").exists():
        rows = {w["name"]: w for w in json.loads((DATA / "ship_weapons.json").read_text(encoding="utf-8"))["weapons"]}
        if "Slayer Cannon" in rows:
            # erkul_dps once gave it 5.24 (a heat-model bug, fixed 09-23: per-shot overheat); now sustain == burst,
            # so it is trusted and gets no burst-and-sustained sentence (not >10% apart).
            slayer = rows["Slayer Cannon"]
            case("the Slayer Cannon's sustain equals its burst and adds no burst-and-sustained line",
                 slayer.get("dps") and slayer.get("dps_sustain") == slayer["dps"]
                 and not any("Slayer does about" in f["text"] and "sustained" in f["text"]
                             for n in real.nodes.values() for f in n["facts"]), str(slayer))
    try:                                                   # the builder needs elah-audio's erkul modules (dev copy)
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
        from build_ship_weapons import trusted_sustain
    except ImportError:
        trusted_sustain = None
    if trusted_sustain is not None:
        case("builder: sustain trusted only if weapon-local and 0.2x..1x of burst",
             trusted_sustain({"dps_burst": 2666.67, "dps_sustain": 5.24, "sustain_model": "heat", "gaps": []})[0] is None
             and trusted_sustain({"dps_burst": 545.62, "dps_sustain": 279.4, "sustain_model": "pool", "gaps": []})[0]
             == 279
             and trusted_sustain({"dps_burst": 500, "dps_sustain": 500, "sustain_model": "none(=burst)",
                                  "gaps": []})[0] is None
             and trusted_sustain({"dps_burst": 2666.67, "dps_sustain": 2666.67,
                                  "sustain_model": "heat(per-shot overheat)", "gaps": []})[0] == 2667)
    print(f"topic_graph selftest: {ok}/{total} passed")
    return 0 if ok == total else 1


if __name__ == "__main__":
    import sys
    sys.exit(_selftest() if "--selftest" in sys.argv else 0)
