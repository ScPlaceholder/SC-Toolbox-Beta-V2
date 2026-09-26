"""manufacturers.py - what Elah and Montaigne SAY about a ship's maker (J 2026-09-25, April spec section 3 + 4).

"For the manufacturers you can have all the manufacturers." Every manufacturer in data/ships.json (19) has an entry in
data/manufacturer_lore.json. The April spec switched the ship AI's VOICE per maker; SuitMk2's voices stay Elah and
Montaigne, so the idea is carried as FLAVOUR in what they say:

  * FACTS (status "brochure"): marketing copy from the game's own ship descriptions (scunpacked Description field,
    build 4.10.1-LIVE.12660092). Each fact is a close paraphrase of a VERBATIM excerpt stored beside it, with the ship
    class it came from. Montaigne quotes them the way he quotes every brochure: attributed, trusting it too much. The
    grounding gate already refuses brochure copy stated as fact, so Elah never states one and he never states one bare.
  * TAKES (status "opinion" / "mont_opinion"): each character's own first-person view of the maker. Opinions, never
    facts: no numbers, no claims about the maker's history. Elah's agree with her standing favourites (Drake and Aegis
    for function and attitude, Crusader the most beautiful).
  * BOARD CUE: boarding a ship adds its maker as a claim and leans the line's stance (Drake "plain and short", Anvil
    "like a readiness report", ...): the spec's per-maker voice profile, as a cue to the same two voices.

Facts and takes join the topic walker's graph as the maker's node (the ship branch already has nodes for the ten
biggest makers; the other nine are added under the same "ships" root), so they reach speech only through the walker,
the speak gate, pacing and grounding, like every other topic line.

    python manufacturers.py --selftest       every fact checked against its excerpt (and the game data when present)
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

DATA = HERE.parent / "data" / "manufacturer_lore.json"
SHIPS = HERE.parent / "data" / "ships.json"
SCUNPACKED = Path.home() / ".sctoolbox" / "scunpacked"

_cache: dict = {}


def load(path: Path = DATA) -> list[dict]:
    if path not in _cache:
        try:
            _cache[path] = json.loads(path.read_text(encoding="utf-8")).get("makers", [])
        except Exception:
            _cache[path] = []
    return _cache[path]


def _slug(s: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in s.lower()).strip("_")


def node_id(entry: dict) -> str:
    return "maker_" + _slug(entry["name"])          # the ship branch's own id scheme (topic_graph.ship_branch)


def _ship_rows() -> list:
    if "ships" not in _cache:
        try:
            _cache["ships"] = json.loads(SHIPS.read_text(encoding="utf-8")).get("ships", [])
        except Exception:
            _cache["ships"] = []
    return _cache["ships"]


def resolve(ship: str) -> Optional[dict]:
    """A ship as the log names it -> its maker's entry. The crew channel reads "Drake Ironclad", "RSI Ursa Medivac" or
    "@vehicle_NameDRAK_Golem_OX" (measured on J's logs); the parser cleans the last to "Golem OX". So: the maker's
    name or alias leading the name, then a code prefix, then the longest data/ships.json ship name inside it."""
    raw = str(ship or "").strip()
    if not raw:
        return None
    makers = load()
    if raw.startswith("@vehicle_Name"):
        raw = raw[len("@vehicle_Name"):]
    up = raw.upper()
    for e in makers:
        for code in e.get("codes", []):
            if up.startswith(code + "_"):
                return e
    low = " ".join(raw.lower().replace("_", " ").split())
    names = sorted(((a.lower(), e) for e in makers for a in [e["name"]] + e.get("aliases", [])),
                   key=lambda x: len(x[0]), reverse=True)
    for a, e in names:
        if low == a or low.startswith(a + " "):
            return e
    by_name = {e["name"]: e for e in makers}
    hit = None
    for r in _ship_rows():
        n = r.get("name", "").lower()
        if n and f" {n} " in f" {low} " and (hit is None or len(n) > len(hit[0])):
            hit = (n, r.get("manufacturer"))
    return by_name.get(hit[1]) if hit else None


def flavour_boarding(spec: Optional[dict], ship: str) -> bool:
    """Boarding line: add the maker as a claim and lean the stance by its cue. True if it changed the spec."""
    if not spec or not str(spec.get("scenario", "")).endswith("boarded_ship"):
        return False
    e = resolve(ship)
    if e is None:
        return False
    claims = spec.setdefault("claims", [])
    if not any(c.get("predicate") == "ship.manufacturer" for c in claims):
        claims.append({"id": f"C{len(claims) + 1}", "kind": "OBSERVED", "predicate": "ship.manufacturer",
                       "value": e["name"]})
    interp = spec.setdefault("interpretation", {"owner": spec.get("speaker"), "text": ""})
    cue = e.get("board_cue")
    if cue and cue not in interp.get("text", ""):
        interp["text"] = ((interp.get("text") or "").rstrip(". ") + "; " + cue).lstrip("; ")
    spec["manufacturer"] = e["name"]
    return True


def merge_into(graph) -> int:
    """Add every maker's facts and takes to a topic_graph.TopicGraph. -> facts added. Idempotent per graph."""
    if getattr(graph, "_manufacturers_merged", False):
        return 0
    from topic_graph import _node, _fact, _META
    added = 0
    root = graph.nodes.get("ships")
    for e in load():
        nid = node_id(e)
        n = graph.nodes.get(nid)
        if n is None:
            n = _node(nid, e["name"], [], edges=["ships"] if root else [], aliases=e.get("aliases", []))
            graph.nodes[nid] = n
            if root is not None and nid not in root["edges"]:
                root["edges"].append(nid)
        n["aliases"] = sorted(set(n.get("aliases", [])) | set(e.get("aliases", [])))
        have = {f["text"] for f in n["facts"]}
        new = [_fact(f["text"], "brochure", [e["name"]] + f.get("names", []),
                     f"game ship description {f['class']} (scunpacked 4.10.1-LIVE.12660092)") for f in e["facts"]]
        for key, status in (("elah_take", "opinion"), ("montaigne_take", "mont_opinion")):
            t = e.get(key)
            if t:
                new.append(_fact(t["text"], status, t.get("names", []), "manufacturer_lore.json"))
        for f in new:
            # Same rule TopicGraph.load applies to every data file: out-of-universe wording never enters. (The first
            # draft named the "Mustang Alpha", and "alpha" is on that list: the fact would have been dropped silently.)
            if f["text"] not in have and not _META.search(f["text"]):
                n["facts"].append(f)
                added += 1
    graph._manufacturers_merged = True
    return added


# ---- verification (selftest) ----------------------------------------------------------------------------------------
_STOP = set("a an the and or of to in on at for with by from is are was were be been it its this that these those as "
            "your you our we us i my me he his she her they their there here not no so if but about into than then "
            "very just only also all any some more most has have had can will".split())
# Brochure framing verbs: a fact may say WHO claims it without the excerpt using the same verb.
_FRAMING = {"claims", "says", "advertises", "presents", "presented", "calls"}


def _fold(s: str) -> str:
    return (str(s).replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
            .replace(" ", " "))


def _stem(w: str) -> str:
    """Crude on purpose, and only used to check a paraphrase against its excerpt: makes/making, exceed/exceeds."""
    w = w.lower().removesuffix("'s").strip("'")
    for suf in ("ing", "ed", "es", "s"):
        if w.endswith(suf) and len(w) > len(suf) + 2 and not (suf == "ed" and w.endswith("eed")):
            w = w[: -len(suf)]
            break
    return w.rstrip("e")


def _words(s: str) -> list[str]:
    return re.findall(r"[A-Za-z][A-Za-z']*", _fold(s))


def scunpacked_descriptions() -> Optional[dict]:
    """ClassName -> (Name, Description) from the newest local scunpacked ships.json, or None when there is none."""
    if "scun" not in _cache:
        builds = sorted(SCUNPACKED.glob("*/ships.json"), key=lambda p: p.stat().st_mtime) if SCUNPACKED.is_dir() else []
        if not builds:
            _cache["scun"] = None
        else:
            rows = json.loads(builds[-1].read_text(encoding="utf-8"))
            _cache["scun"] = {r["ClassName"]: (r.get("Name") or "", _fold(r.get("Description") or "").replace("\\n", " "))
                              for r in rows if r.get("ClassName")}
    return _cache["scun"]


def fact_problems(entry: dict, fact: dict, scun: Optional[dict] = None) -> list[str]:
    from topic_graph import _META
    probs = []
    text, ex = _fold(fact["text"]), _fold(fact.get("excerpt", ""))
    maker_words = {_stem(w) for a in [entry["name"]] + entry.get("aliases", []) for w in _words(a)}
    ship_name = scun[fact["class"]][0] if scun and fact.get("class") in scun else ""
    ok_words = {_stem(w) for w in _words(ex)} | maker_words | {_stem(w) for w in _words(ship_name)}
    for w in _words(text):
        lw = w.lower()
        if len(lw) > 3 and lw not in _STOP and lw not in _FRAMING and _stem(w) not in ok_words:
            probs.append(f"word not in excerpt: {w}")
    nums = set(re.findall(r"\d+", ex))
    probs += [f"number not in excerpt: {n}" for n in re.findall(r"\d+", text) if n not in nums]
    named = {w.lower().removesuffix("'s") for n in fact.get("names", []) + [entry["name"]] + entry.get("aliases", [])
             for w in _words(n)}
    for sent in re.split(r"(?<=[.!?:;])\s+", text):
        for w in _words(sent)[1:]:
            # exactly the key grounding_validator.unauthorized_names builds: "Jumpworks'" is NOT "jumpworks"
            if w[0].isupper() and w.lower().removesuffix("'s") not in named:
                probs.append(f"capitalised word not in names (grounding would refuse it): {w}")
    if _META.search(text):
        probs.append("out-of-universe wording")
    if scun is not None:
        if fact.get("class") not in scun:
            probs.append(f"class {fact.get('class')} not in the game data")
        elif " ".join(ex.split()) not in " ".join(scun[fact["class"]][1].split()):
            probs.append(f"excerpt is not verbatim in {fact['class']}'s description")
    return probs


def take_problems(entry: dict, who: str) -> list[str]:
    from topic_graph import _META
    from grounding_validator import ELAH_NAMES_FEELING
    t = entry.get(f"{who}_take") or {}
    text, probs = _fold(t.get("text", "")), []
    if not text:
        return [f"no {who} take"]
    if re.search(r"\d", text):
        probs.append("a take carries a number (a take is an opinion, never a fact)")
    if not re.search(r"\b(?:I|my|me)\b", text):
        probs.append("not first person")
    named = {w.lower().removesuffix("'s") for n in t.get("names", []) for w in _words(n)} | {"i"}
    for sent in re.split(r"(?<=[.!?:;])\s+", text):
        for w in _words(sent)[1:]:
            if w[0].isupper() and w.lower().removesuffix("'s") not in named:
                probs.append(f"capitalised word not in names: {w}")
    if _META.search(text):
        probs.append("out-of-universe wording")
    if who == "elah" and ELAH_NAMES_FEELING.search(text.lower()):
        probs.append("Elah names her own feeling")
    return probs


def _selftest() -> int:
    res = []

    def case(name, cond, detail=""):
        res.append((name + (f"  [{detail}]" if detail and not cond else ""), bool(cond)))
    makers = load()
    ship_makers = {r["manufacturer"] for r in _ship_rows()}
    have = {e["name"] for e in makers}
    case(f"ALL {len(ship_makers)} manufacturers in ships.json have an entry", ship_makers <= have,
         str(sorted(ship_makers - have)))
    scun = scunpacked_descriptions()
    bad = [(e["name"], f["text"][:40], p) for e in makers for f in e["facts"] for p in fact_problems(e, f, scun)]
    nfacts = sum(len(e["facts"]) for e in makers)
    case(f"every one of {nfacts} facts is a faithful paraphrase of its stored excerpt", not bad, str(bad[:4]))
    if scun is None:
        print("  NOTE  no scunpacked ships.json on this machine: the verbatim-excerpt check did NOT run")
    else:
        case("...and every excerpt is verbatim in the game's own description (scunpacked)", not bad)
    tb = [(e["name"], w, p) for e in makers for w in ("elah", "montaigne") for p in take_problems(e, w)]
    case("every take is a first-person opinion with no numbers", not tb, str(tb[:4]))
    case("every maker has a board cue", all(e.get("board_cue") for e in makers))
    for ship, want in (("Drake Ironclad", "Drake Interplanetary"), ("RSI Ursa Medivac", "Roberts Space Industries"),
                       ("Golem OX", "Drake Interplanetary"), ("@vehicle_NameDRAK_Golem_OX", "Drake Interplanetary"),
                       ("Gatac Railen", "Gatac Manufacture"), ("Kruger L-21 Wolf", "Kruger Intergalactic"),
                       ("Crusader C2 Hercules Starlifter", "Crusader Industries"), ("Aegis Reclaimer", "Aegis Dynamics"),
                       ("Argo MOTH", "Argo Astronautics"), ("Party chat", None)):
        case(f"resolve {ship!r} -> {want}", ((resolve(ship) or {}).get("name")) == want)
    from topic_graph import TopicGraph, TopicWalker
    from grounding_validator import ground, ATTRIBUTION
    g = TopicGraph.load()
    before = len(g.nodes)
    n = merge_into(g)
    case("every fact and take joins the topic graph", n == nfacts + 2 * len(makers), f"{n}")
    case("the nine makers the ship branch skipped get their own node under 'ships'",
         all(node_id(e) in g.nodes and node_id(e) in g.nodes["ships"]["edges"] for e in makers)
         and len(g.nodes) - before == len({node_id(e) for e in makers} - set(list(g.nodes)[:before])))
    w = TopicWalker(g, spent=lambda s: False)
    refused_bare, passed_quoted, opinions_ok, speakers = [], [], [], set()
    for e in makers:
        node = g.nodes[node_id(e)]
        for i, f in enumerate(node["facts"]):
            if f["source"] not in ("manufacturer_lore.json",) and not f["source"].startswith("game ship description"):
                continue
            w._next_fact[node_id(e)] = i                  # _spec speaks the node's next untold fact from here
            spec = w._spec(node_id(e), 0, set())
            if spec["topic"]["fact"] != i:
                passed_quoted.append((f["text"][:30], ["walker picked another fact"]))
                continue
            speakers.add((f["status"], spec["speaker"]))
            if f["status"] == "brochure":
                quoted = f"The brochure says {f['text'][0].lower() + f['text'][1:]}"
                passed_quoted.append((f["text"][:30], ground(spec, quoted)))
                if not ATTRIBUTION.search(f["text"].lower()):     # "Drake claims ..." attributes itself
                    refused_bare.append(any("brochure copy" in x
                                            for x in ground(spec, f["text"] + " Worth knowing, pilot.")))
            else:
                opinions_ok.append((f["text"][:30], ground(spec, f["text"])))
    case("every fact, quoted as a brochure, passes the grounding gate", all(not r for _, r in passed_quoted),
         str([x for x in passed_quoted if x[1]][:3]))
    case("every fact stated bare (no attribution) is REFUSED by the grounding gate", refused_bare and all(refused_bare))
    case("every take passes the grounding gate as its speaker's opinion", all(not r for _, r in opinions_ok),
         str([x for x in opinions_ok if x[1]][:3]))
    case("brochure facts are Montaigne's; Elah's takes are hers",
         all(sp == "montaigne" for st, sp in speakers if st in ("brochure", "mont_opinion"))
         and all(sp == "elah" for st, sp in speakers if st == "opinion"))
    from event_spec import build_event_spec
    spec = build_event_spec("ship_channel_joined", {"channel": "Drake Ironclad"})
    changed = flavour_boarding(spec, "Drake Ironclad")
    case("boarding a Drake adds the maker and leans the stance plain and short",
         changed and {c["predicate"]: c["value"] for c in spec["claims"]}.get("ship.manufacturer") == "Drake Interplanetary"
         and "plain and short" in spec["interpretation"]["text"])
    case("boarding a ship nobody makes changes nothing",
         not flavour_boarding(build_event_spec("ship_channel_joined", {"channel": "Mystery Barge"}), "Mystery Barge"))
    bad = [n for n, ok in res if not ok]
    for n_ in bad:
        print("  FAIL  " + n_)
    print(f"manufacturers selftest: {len(res) - len(bad)}/{len(res)} passed ({len(makers)} makers, {nfacts} facts)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(_selftest() if "--selftest" in sys.argv else 0)
