"""place_flavour.py - the April spec's hand-written place lines, re-checked, as Elah's seed flavour.

data/place_flavour.json keeps each April line VERBATIM beside a verdict per claim, re-checked against the current
sourced data (topics_lore.json facts, places.json):
    supported     a sourced fact says it; the fact is quoted in "by" and this module checks the quote exists
    unverified    nothing in current data says it (Yela's "drug labs", Clio "volcanic", Arial "tidally locked")
    contradicted  current data says otherwise (Pyro VI is Terminus and barren, not "a distant ice giant"; Ruin
                  Station is a live outlaw station, not "abandoned"; every gateway sits on the FAR side of its jump,
                  so all three April gateway lines had the side wrong; "Ariel", "Area 18", "Bajini Point", "GrimHEX"
                  are old spellings)
Only one LINE per place is kept: Elah's first-person impression, built from supported claims plus opinion. It joins
that place's topic node as her opinion, so it is said only by the topic walker, through the speak gate, pacing and
the grounding gate, and only when the walk reaches that place (anchored nodes come up when the pilot is there).

    python place_flavour.py --selftest
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

DATA_DIR = HERE.parent / "data"
DATA = DATA_DIR / "place_flavour.json"
SOURCE = "place_flavour.json (April spec line, re-checked 2026-09-25)"


def load(path: Path = DATA) -> list[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("places", [])
    except Exception:
        return []


def merge_into(graph, path: Path = DATA) -> int:
    """Add every kept line to its place's topic node as Elah's opinion. -> lines added. Idempotent per graph."""
    if getattr(graph, "_place_flavour_merged", False):
        return 0
    from topic_graph import _fact, _is_meta
    added = 0
    for p in load(path):
        line, nid = p.get("line"), p.get("node")
        n = graph.nodes.get(nid) if nid else None
        # the graph's own out-of-universe rule, with its real-place exemption ("Patch City" is a place, not a patch)
        if not line or n is None or _is_meta(line["text"], line.get("names", [])):
            continue
        if line["text"] not in {f["text"] for f in n["facts"]}:
            n["facts"].append(_fact(line["text"], "opinion", line.get("names", []), SOURCE))
            added += 1
    graph._place_flavour_merged = True
    return added


# ---- verification ---------------------------------------------------------------------------------------------------
def _evidence() -> tuple[set, dict]:
    lore = json.loads((DATA_DIR / "topics_lore.json").read_text(encoding="utf-8"))["nodes"]
    facts = {f["text"] for n in lore for f in n.get("facts", []) if f.get("source")}
    places = {p["name"].lower(): p for p in json.loads((DATA_DIR / "places.json").read_text(encoding="utf-8"))["places"]}
    return facts, places


def _places_statement_ok(by: str, places: dict) -> bool:
    m = re.match(r"places\.json: (.+?), (\w+), parent (.+?), system (\w+)$", by)
    if not m:
        return False
    p = places.get(m.group(1).lower())
    return bool(p) and (p["type"], p["parent"], p["system"]) == (m.group(2), m.group(3), m.group(4))


def check_problems(entry: dict, facts: set, places: dict) -> list[str]:
    probs = []
    for claim, verdict, by in entry.get("checks", []):
        if verdict not in ("supported", "unverified", "contradicted"):
            probs.append(f"bad verdict {verdict!r}")
        if verdict == "supported" and by not in facts and not _places_statement_ok(by, places):
            probs.append(f"'supported' without a real source: {claim!r}")
        if verdict == "contradicted" and by not in facts and not _places_statement_ok(by, places) \
                and not by.startswith("current data"):
            probs.append(f"'contradicted' without evidence: {claim!r}")
    return probs


def line_problems(entry: dict) -> list[str]:
    from topic_graph import _is_meta
    from grounding_validator import ELAH_NAMES_FEELING, FIRSTHAND_VISIT, CARDINAL_WORDS
    line = entry.get("line")
    if not line:
        return []
    text, probs = line["text"], []
    if not re.search(r"\b(?:I|my|me)\b", text):
        probs.append("not first person")
    if _is_meta(text, line.get("names", [])):
        probs.append("out-of-universe wording")
    low = text.lower()
    bare = low
    for name in line.get("names", []):                  # a digit inside a name ("Area18") is the name, not a number
        bare = bare.replace(name.lower(), " ")
    if ELAH_NAMES_FEELING.search(low):
        probs.append("Elah names her own feeling")
    if FIRSTHAND_VISIT.search(low):
        probs.append("claims a visit (the pilot's record decides that, not a seed line)")
    if re.search(r"\d", bare) or any(re.search(rf"\b{w}\b", bare) for w in CARDINAL_WORDS):
        probs.append("a number (grounding refuses a number no claim carries)")
    if not any(v == "supported" for _, v, _ in entry.get("checks", [])):
        probs.append("a line with no supported claim behind it")
    return probs


def _selftest() -> int:
    res = []

    def case(name, cond, detail=""):
        res.append((name + (f"  [{detail}]" if detail and not cond else ""), bool(cond)))
    entries = load()
    facts, places = _evidence()
    # The April section: 3 systems, 16 planets and moons, 17 stations and cities (Rest Stops, a category, is dropped).
    case("every April place row is here (3 systems + 16 bodies + 17 stations = 36)", len(entries) == 36, str(len(entries)))
    bad = [(e["place"], p) for e in entries for p in check_problems(e, facts, places)]
    case("every 'supported' claim quotes a real sourced fact or a real places.json row", not bad, str(bad[:3]))
    tied = [e["place"] for e in entries if e.get("line") and not e.get("system_only") and e["place"].lower() not in places]
    case("every kept line is tied to a places.json entry (systems use their lore node)", not tied, str(tied))
    lb = [(e["place"], p) for e in entries for p in line_problems(e)]
    case("every kept line is Elah's first-person impression, no numbers, no visit claim", not lb, str(lb[:3]))
    contra = {e["place"] for e in entries for _, v, _ in e["checks"] if v == "contradicted"}
    case("the outdated April claims are marked contradicted",
         {"Terminus", "Ruin Station", "Nyx Gateway", "Stanton Gateway", "Pyro Gateway", "Arial"} <= contra, str(contra))
    unv = {c for e in entries for c, v, _ in e["checks"] if v == "unverified"}
    case("unverified April claims are marked, not spoken", {"drug labs", "volcanic, active geology", "Starliners"} <= unv)
    for e in entries:
        for c, v, _ in e["checks"]:
            if v != "supported" and e.get("line") and c.lower() in e["line"]["text"].lower() and len(c) > 8:
                res.append((f"{e['place']}: the line repeats a non-supported claim {c!r}", False))
    from topic_graph import TopicGraph, TopicWalker
    from grounding_validator import ground
    g = TopicGraph.load()
    kept = [e for e in entries if e.get("line")]
    missing = [e["node"] for e in kept if e["node"] not in g.nodes]
    case("every kept line's node exists in the topic graph", not missing, str(missing))
    n = merge_into(g)
    case(f"all {len(kept)} kept lines join the graph", n == len(kept), str(n))
    w = TopicWalker(g, spent=lambda s: False)
    fails = []
    for e in kept:
        nid = e["node"]
        i = next((k for k, f in enumerate(g.nodes[nid]["facts"]) if f["text"] == e["line"]["text"]), None)
        if i is None:
            fails.append((e["place"], "not merged", []))
            continue
        w._next_fact[nid] = i
        spec = w._spec(nid, 0, set())
        r = ground(spec, e["line"]["text"])
        if spec["speaker"] != "elah" or spec["topic"]["fact"] != i or r:
            fails.append((e["place"], spec["speaker"], r))
    case("every line, as the walker would offer it, is Elah's and passes the grounding gate", not fails, str(fails[:3]))
    bad = [n_ for n_, ok in res if not ok]
    for n_ in bad:
        print("  FAIL  " + n_)
    print(f"place_flavour selftest: {len(res) - len(bad)}/{len(res)} passed ({len(kept)} lines kept of {len(entries)})")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(_selftest() if "--selftest" in sys.argv else 0)
