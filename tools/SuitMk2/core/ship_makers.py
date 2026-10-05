"""ship_makers.py - WHO MAKES A SHIP IS LOOKED UP, NEVER REMEMBERED (J, 2026-10-05).

"I imagine we can assign manufacturer intelligently to any ship so they'd never mistake a ship for the wrong
manufacturer." The pilot said "I love this ship", the ship was an Aegis Avenger Titan, and a model answered "It is
a Drake." A model is never the source of a maker. This module finds the ships a sentence names and gives each its
maker from the Suit's own ship list (data/ships.json, 242 ships, every one with a maker).

    ships_in(text)       every ship the sentence names -> [{"said", "ship", "maker"}]
    maker_of(name)       one ship name, as the pilot or the log gives it -> the maker's name, or None
    maker_words()        every word that names a maker ("Drake", "RSI", "Aegis Dynamics"), for the chat gate

How a name is found, in this order, longest first:
  1. A FULL name from the list ("Cutlass Red", "Kraken Privateer", "C8X Pisces Expedition", "300i").
  2. A NICKNAME from the small table below ("Connie", "MSR", "Herc"). A nickname points at a SHIP FAMILY, never at
     a maker; the maker still comes from the list.
  3. A FAMILY word: one word of a ship's name that belongs to exactly one maker across the whole list ("Cutlass",
     "Hornet", "Constellation", "Avenger"). Every variant of a family has the same maker, so "the Cutlass" is
     answered without knowing whether it is the Black, the Blue, the Red or the Steel.
  A family word that is also an ordinary English word ("Fury", "Spirit", "Storm", "Titan", "Hull") counts only
  when the pilot's sentence writes it with a capital and it is not the first word. Speech-to-text does not always
  capitalise, so some of those are missed. That is the safe direction: a missed ship means no maker is supplied and
  none may be said.

Not used by the running Suit's speech yet: the evaluation (contract_eval.py) calls it to supply FACTS and the chat
gate uses maker_words(). It imports nothing outside this folder.

Selftest: python ship_makers.py --selftest
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import manufacturers as mf   # noqa: E402

# nickname -> a ship name or family word that IS in the list. Hand-written; each is checked by the selftest.
NICKNAMES = {"connie": "Constellation", "msr": "Mercury Star Runner", "herc": "Hercules", "tali": "Retaliator",
             "cutty": "Cutlass", "bucc": "Buccaneer", "lancer": "Freelancer", "cat": "Caterpillar", "titan": "Avenger Titan"}
# Words in ship names that are never a family by themselves.
_GENERIC = set("mk i ii iii iv the edition pirate black blue red steel white max dur mis star runner super ghost tracker "
               "wildfire heartseeker expedition best in show executive emerald valiant warlock stalker renegade titan "
               "andromeda aquila phoenix taurus harbinger sentinel warden bomber rescue medic medivac cargo touring "
               "sport racing light heavy civilian military combat ion inferno collector competition dunlevy starlifter".split())
# Family words that are ordinary English: they count only with a capital, not as the first word.
AMBIGUOUS = set("fury spirit storm hull mole mule arrow hawk blade pioneer guardian meteor apollo eclipse reliant ranger "
                "razor nomad cutter intrepid expanse odyssey genesis herald atlas zeus titan mercury lightning hurricane "
                "pulse fortune cat nova lynx raft roc talon scythe glaive defender prowler liberator endeavor crucible "
                "orion vanguard hammerhead javelin polaris perseus nautilus redeemer sabre gladiator terrapin valkyrie "
                "dragonfly mustang aurora cyclone spartan centurion ballista paladin legionnaire golem ironclad corsair "
                "buccaneer vulture prospector freelancer starfarer hercules ares scorpius stinger shiv mantis hawk "
                "khartu merchantman reclaimer caterpillar constellation avenger hornet pisces".split()) - {
    # these are common enough as ship names, and rare enough as talk, to match in any case
    "caterpillar", "constellation", "avenger", "hornet", "pisces", "vulture", "prospector", "freelancer", "starfarer",
    "hercules", "corsair", "buccaneer", "ironclad", "terrapin", "valkyrie", "sabre", "gladiator", "redeemer",
    "hammerhead", "polaris", "perseus", "nautilus", "reclaimer", "merchantman", "khartu", "scorpius", "vanguard",
    "mustang", "aurora", "dragonfly", "cyclone", "ballista", "centurion", "paladin", "legionnaire", "golem"}
_cache: dict = {}


def _norm(s: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", str(s or "").lower().replace("-", " ").replace("'", "").replace("’", "")).split())


def _index() -> dict:
    """{"full": {normalised full name: (ship, maker)}, "family": {word: (word as written, maker)}}"""
    if "index" in _cache:
        return _cache["index"]
    full, by_word = {}, {}
    for r in mf._ship_rows():
        name, maker = r.get("name") or "", r.get("manufacturer") or ""
        if not name or not maker:
            continue
        full[_norm(name)] = (name, maker)
        for w in re.findall(r"[A-Za-z][A-Za-z0-9]+", name):
            if len(w) >= 3 and w.lower() not in _GENERIC:
                by_word.setdefault(w.lower(), {})[maker] = w
    family = {w: (next(iter(m.values())), next(iter(m))) for w, m in by_word.items() if len(m) == 1}
    _cache["index"] = {"full": full, "family": family}
    return _cache["index"]


def maker_short(maker: str) -> str:
    """The name people say: "Drake" for "Drake Interplanetary", "RSI" for "Roberts Space Industries"."""
    for e in mf.load():
        if e.get("name") == maker:
            al = [a for a in e.get("aliases", []) if a and len(a) < len(maker)]
            return min(al, key=len) if al else maker
    return maker


def ships_in(text: str) -> list[dict]:
    """Every ship the sentence names, each once, with its maker. `text` should be the sentence as it was written
    (capitals matter for the ambiguous words)."""
    ix = _index()
    low = " " + _norm(text) + " "
    taken, out = low, []

    def claim(key: str, said: str, ship: str, maker: str):
        nonlocal taken
        if f" {key} " in taken:
            taken = taken.replace(f" {key} ", " ~ ", 1)
            out.append({"said": said, "ship": ship, "maker": maker})
            return True
        return False

    for key in sorted(ix["full"], key=len, reverse=True):
        if len(key) >= 3 and not key.isdigit():
            ship, maker = ix["full"][key]
            if key in AMBIGUOUS and not _capitalised(text, key):
                continue
            claim(key, ship, ship, maker)
    for nick, target in NICKNAMES.items():
        if f" {nick} " in taken or f" {nick}s " in taken:
            if nick in AMBIGUOUS and not _capitalised(text, nick):
                continue
            maker = maker_of(target)
            if maker:
                claim(nick, nick, target, maker) or claim(nick + "s", nick, target, maker)
    for w, (written, maker) in sorted(ix["family"].items(), key=lambda kv: -len(kv[0])):
        for form in (w, w + "s"):
            if f" {form} " in taken:
                if w in AMBIGUOUS and not _capitalised(text, w):
                    break
                claim(form, written, written, maker)
                break
    return out


def _capitalised(text: str, word: str) -> bool:
    """The sentence writes this word with a capital somewhere other than its first word."""
    toks = re.findall(r"[A-Za-z][A-Za-z0-9'’-]*", str(text or ""))
    return any(t.lower().rstrip("s") == word.rstrip("s") and t[0].isupper() for t in toks[1:])


def maker_of(name: str) -> Optional[str]:
    """One ship name ("Aegis Avenger Titan", "Cutlass", "Kraken Privateer", "@vehicle_NameDRAK_Golem_OX") -> maker."""
    if not str(name or "").strip():
        return None
    e = mf.resolve(name)
    if e:
        return e["name"]
    ix = _index()
    n = _norm(name)
    if n in ix["full"]:
        return ix["full"][n][1]
    for w in n.split():
        if w in ix["family"]:
            return ix["family"][w][1]
    return None


def maker_words() -> list[str]:
    """Every word or phrase that names a ship maker, longest first, as written."""
    if "words" not in _cache:
        ws = set()
        for e in mf.load():
            ws.add(e["name"])
            ws.update(a for a in e.get("aliases", []) if a)
            ws.add(e["name"].split()[0])
        for r in mf._ship_rows():
            if r.get("manufacturer"):
                ws.add(r["manufacturer"])
                ws.add(r["manufacturer"].split()[0])
        _cache["words"] = sorted((w for w in ws if len(w) >= 3), key=len, reverse=True)
    return _cache["words"]


def coverage() -> dict:
    rows = mf._ship_rows()
    ok = [r for r in rows if r.get("name") and maker_of(r["name"]) == r.get("manufacturer")]
    found = [r for r in rows if r.get("name") and any(s["maker"] == r["manufacturer"] for s in ships_in("I flew the " + r["name"] + " today"))]
    return {"ships": len(rows), "maker_of_right": len(ok), "found_in_a_sentence": len(found),
            "not_found": [r["name"] for r in rows if r not in found],
            "wrong": [r["name"] for r in rows if any(s["maker"] != r["manufacturer"] for s in ships_in("I flew the " + r["name"] + " today"))],
            "families": len(_index()["family"])}


def _selftest() -> int:
    results = []

    def case(name, cond, detail=""):
        results.append((name, bool(cond), str(detail)))

    c = coverage()
    case("every ship in the list resolves to its own maker by name", c["maker_of_right"] == c["ships"], c)
    case("no ship in the list is given the WRONG maker when named in a sentence", not c["wrong"], c["wrong"])
    for nick, target in NICKNAMES.items():
        case(f"nickname {nick} points at something in the list", maker_of(target), target)
    got = {s["said"]: s["maker"] for s in ships_in("My mate flies a Cutlass Red and I used to have a Connie")}
    case("a variant and a nickname", got == {"Cutlass Red": "Drake Interplanetary", "connie": "Roberts Space Industries"}, got)
    case("an ordinary word is not a ship", ships_in("what a storm, it cost me a fortune and my spirit") == [])
    case("the same word with a capital is", [s["maker"] for s in ships_in("I bought a Spirit")] == ["Crusader Industries"])
    for name, ok, detail in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   <{detail[:300]}>" if not ok else ""))
    bad = sum(not ok for _, ok, _ in results)
    print(f"ship_makers selftest: {len(results) - bad}/{len(results)} passed")
    return 1 if bad else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print(__doc__)
