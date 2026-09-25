"""ship_feelings.py - favourite ships that change with what actually happens aboard them.

J 2026-09-24: "Their favorite ships and other interests should also be able to change based on player relation and
personal experience. For example if the Privateer gets blown up and the user dies every time Elah is on it maybe Elah
won't like it anymore."

Raw EVENTS are stored, never conclusions: each death or completed mission aboard a ship is one memory callback
(kind "ship_event"), so the pilot's export/import carries it and the rule can change without rewriting history.
Feelings are computed at read time with a half-life, so one bad week fades instead of setting an opinion forever.

  sour : a ship the character favours, with >= SOUR_DEATHS weighted deaths aboard -> the favourite is withdrawn and
         replaced by a first-person line about why.
  warm : a ship the character never mentioned, with >= WARM_MISSIONS weighted missions and ~no deaths -> a grudging
         new favourite.
Montaigne IS a ship AI: he sours on any ship where the pilot keeps dying, in brochure terms.
No game state is ever changed; this only decides what they say.
"""
from __future__ import annotations

import math
import time
from datetime import datetime, timezone

KIND = "ship_event"
HALF_LIFE_DAYS = 14.0
# 2.5, not 3.0: weights DECAY, so three deaths even one day ago weigh 2.86 and a threshold of 3 would never fire
# on 'three recent deaths'. 2.5 ~= three deaths within about a week.
SOUR_DEATHS = 2.5
WARM_MISSIONS = 5.0
WARM_MAX_DEATHS = 0.5


def event(ship: str, what: str) -> dict:
    """The callback payload for one event aboard `ship`. what: 'death' | 'mission'."""
    return {"kind": KIND, "meta": {"ship": str(ship).strip(), "what": what}}


# Ship familiarity (J 2026-09-24, from the old skill's ship_familiarity_tracker): the Nth time the pilot boards a ship
# is worth a remark. Boardings are raw events like deaths, but COUNTED WHOLE, never decayed: the 25th time aboard is
# the 25th time, however long ago the first was.
MILESTONES = (5, 10, 25, 50, 100)


def boardings(callbacks: list, ship: str) -> int:
    ship = str(ship).strip().lower()
    n = 0
    for cb in callbacks:
        m = cb.get("meta") or {}
        if cb.get("kind") == KIND and m.get("what") == "board" and str(m.get("ship", "")).strip().lower() == ship:
            n += 1
    return n


def _age_days(created: str, now: float) -> float:
    try:
        t = datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0
    return max(0.0, (now - t) / 86400.0)


def tally(callbacks: list[dict], now: float | None = None) -> dict[str, dict[str, float]]:
    """{ship: {'death': w, 'mission': w, 'raw_death': n, 'raw_mission': n}} with exponential decay by age."""
    now = time.time() if now is None else now
    out: dict[str, dict[str, float]] = {}
    for cb in callbacks:
        meta = cb.get("meta") or {}
        if cb.get("kind") != KIND or not meta.get("ship") or meta.get("what") not in ("death", "mission"):
            continue
        w = math.pow(0.5, _age_days(cb.get("created", ""), now) / HALF_LIFE_DAYS)
        row = out.setdefault(meta["ship"], {"death": 0.0, "mission": 0.0, "raw_death": 0, "raw_mission": 0})
        row[meta["what"]] += w
        row["raw_" + meta["what"]] += 1
    return out


def _mentions(text: str, ship: str) -> bool:
    return ship.lower() in text.lower()


def feelings(t: dict, elah_opinions: list, mont_opinions: list) -> dict:
    """What the record changes. Returns {'elah': [(text, names)], 'montaigne': [...], 'withdraw': {'elah': set(texts),
    'montaigne': set(texts)}}. Opinion lists are the static (text, names) pairs from topic_graph."""
    out = {"elah": [], "montaigne": [], "withdraw": {"elah": set(), "montaigne": set()}}
    for ship, r in sorted(t.items()):
        n = int(r["raw_death"])
        if r["death"] >= SOUR_DEATHS:
            for who, ops, line in (
                    ("elah", elah_opinions, f"I used to love the {ship}. It has killed you {n} times, and I am reconsidering."),
                    ("montaigne", mont_opinions, f"The {ship}'s brochure did not mention this. {n} times now.")):
                fav = [txt for txt, _ in ops if _mentions(txt, ship)]
                out["withdraw"][who].update(fav)
                if fav or who == "montaigne":          # Elah sours only on a favourite; he sours on any ship he is
                    out[who].append((line, [ship]))
        elif r["mission"] >= WARM_MISSIONS and r["death"] <= WARM_MAX_DEATHS:
            if not any(_mentions(txt, ship) for txt, _ in elah_opinions):
                out["elah"].append((f"I did not expect to like the {ship}, but it keeps bringing you home.", [ship]))
    return out


def _selftest() -> int:
    ok = True

    def case(name, cond):
        nonlocal ok
        ok &= bool(cond)
        print(("  PASS  " if cond else "  FAIL  ") + name)
    now = time.time()
    iso = lambda days: datetime.fromtimestamp(now - days * 86400, timezone.utc).isoformat()
    cbs = [dict(event("Kraken Privateer", "death"), created=iso(1)) for _ in range(3)]
    cbs += [dict(event("Cutlass Black", "mission"), created=iso(2)) for _ in range(6)]
    cbs += [dict(event("Aurora MR", "death"), created=iso(90)) for _ in range(5)]
    cbs += [{"kind": "topic", "meta": {"ship": "x"}, "created": iso(0)}]
    elah = [("My dream ship is the Drake Kraken, and the Kraken Privateer is the variant I want.", ["Kraken"])]
    mont = [("The Carrack is the ship I dream about.", ["Carrack"])]
    t = tally(cbs, now)
    f = feelings(t, elah, mont)
    case("other callback kinds are ignored", "x" not in t)
    case("recent deaths on a favourite sour Elah", any("used to love the Kraken Privateer" in x for x, _ in f["elah"]))
    case("...and withdraw that favourite", elah[0][0] in f["withdraw"]["elah"])
    case("the death count is the real count", any("killed you 3 times" in x for x, _ in f["elah"]))
    case("Montaigne sours in brochure terms", any("brochure did not mention" in x for x, _ in f["montaigne"]))
    case("safe missions warm Elah to a ship she never named", any("Cutlass Black" in x for x, _ in f["elah"]))
    case("old deaths fade (90 days, 14-day half-life)", t["Aurora MR"]["death"] < SOUR_DEATHS
         and not any("Aurora" in x for x, _ in f["elah"] + f["montaigne"]))
    case("no events, no change", feelings(tally([], now), elah, mont) == {"elah": [], "montaigne": [],
                                                                         "withdraw": {"elah": set(), "montaigne": set()}})
    print("ship_feelings selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    sys.exit(_selftest() if "--selftest" in sys.argv else 0)
