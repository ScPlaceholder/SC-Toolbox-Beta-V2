# pico/signs.py - copied from elah-audio/sign_picker.py (2026-09-28, J: "Can you write up a program for RNG
# for what sign gets pulled out? And also ... the qualifier of how the engine would decide to pull out a
# sign"), so the desktop Pico can use it. Wired in 2026-10-01 for the 25 signs on J's sheet, held up as
# snap props (pico/snap.py). EVENT_ALIASES maps Game.log parser names onto this module's event vocabulary.
"""sign_picker.py — WHEN Pico pulls out a sign, and WHICH one.

J, 2026-09-28: "Can you write up a program for RNG for what sign gets pulled out? And also can you
decide on the qualifier of how the engine would decide to pull out a sign."

So: two jobs, and the QUALIFIER is the harder and more important one. A picker that answers "which
sign" is twenty lines of weighted random. A picker that answers "should there be a sign at all" is
the difference between a character and a slot machine.

## THE QUALIFIER, and why it is shaped like this

★ A sign is a PROP, not a mood. Moods are a state Pico is in; a sign is a thing he chooses to hold
  up. That makes it a PERFORMANCE, and the entire comic value of a performance is that it is rare
  and it lands. Fire one every thirty seconds and it stops being a joke and becomes weather.

⇒ Five gates, and a sign must pass ALL of them:

  1. AN EVENT ASKED. Signs are event-driven, never idle-driven. Nothing pulls a sign out of
     nowhere — something happened (a ship was bought, a contract completed, a death). An idle Pico
     is expressive through his FACE, which costs nothing and can run constantly.
  2. COOLDOWN. Hard floor between signs, regardless of how much happens. Default 8 minutes.
  3. MOOD CONSISTENCY. A celebratory sign while the mood channel says grief is a bug, not a gag.
     The mood layer is authoritative and a sign must not contradict it.
  4. NOVELTY. No sign from the last N shown (default 6). The second time you see the same joke it
     is half as funny; the third time it is a glitch.
  5. A DIE ROLL. Even having passed 1-4, it fires with probability p (default 0.35). This is the
     gate that makes it feel like a character rather than a vending machine — the SAME event twice
     should not reliably produce the same behaviour, or the player learns the trigger and the
     illusion of choice dies.

⚠ GATE 5 IS LAST ON PURPOSE. Rolling first and then checking relevance would waste the roll on
  candidates that could never have shown, which silently raises the true fire-rate above p. The
  roll is the final say, not the first.

⚠ AND THE HONEST LIMIT: I chose 8 minutes, 6, and 0.35 by taste, not measurement. Nobody has
  watched this run yet. They are CONSTANTS AT THE TOP so they can be moved in one place once
  somebody has, and I would rather say "these are guesses" than have a future reader mistake them
  for findings. [[a-single-sample-cannot-license-a-claim-about-a-trend]]

## WHICH sign: weighted, not uniform

Uniform random over 25 signs means the mining joke fires after a dogfight and the fleet joke fires
while mining. Each sign carries TAGS; a candidate's weight is its base weight times a bonus for
every tag matching the current context. Signs that fit the moment beat signs that do not, without
ever making the choice deterministic — a low-relevance sign is UNLIKELY, not impossible, which is
where the surprise lives.

⚠ Weighting is NOT filtering. An earlier draft of this dropped non-matching signs entirely; that
  makes every buy-event produce one of the same three signs forever and reintroduces gate 4's
  problem through the back door. Everything stays in the bag; the bag is just tilted.

Pure and testable: `pick()` takes state in and returns a decision out, touches no files, no clock,
no global RNG. Seedable, so the selftest is deterministic.

    python sign_picker.py --selftest
    python sign_picker.py --demo          # 40 simulated events, see what actually fires
"""
from __future__ import annotations

import argparse
import json
import random
import sys

# ── TUNABLES. Chosen by taste, not measurement — see the honest-limit note above. ───────────────
COOLDOWN_S = 8 * 60          #: hard floor between any two signs
NOVELTY_WINDOW = 6           #: do not repeat a sign shown within the last N
FIRE_CHANCE = 0.35           #: probability once every other gate has passed
TAG_BONUS = 2.5              #: weight multiplier per matching tag
# J 2026-10-04: "Can we make the signs come up randomly regardless of tasks". Until then a sign needed a
# game event (gate 1 of pick). An IDLE sign needs none: it is offered each time a rest ends. It keeps the
# mood veto and the no-repeat window, has its own shorter floor, and a low chance per offer so it stays
# an occasional thing. With rests of 10-20 s that works out to roughly one sign every four to five minutes.
IDLE_COOLDOWN_S = 3 * 60     #: floor between an idle sign and ANY sign before it
IDLE_CHANCE = 0.20           #: chance per offer (one offer each time a rest ends)

#: The 25 signs, read off pico_signs_transparent.png. `text` is verbatim from the art — J's
#: corrections are in it ("IT'S NOT A PROBLEM, IT'S A FLEET!" with no doubled word, "IN SPACE"
#: not "IN PACE"). Tags are mine.
SIGNS = [
    ("buy_more_ships",      "BUY MORE SHIPS!",                    ("buy", "fleet", "greed")),
    ("fly_dangerously",     "FLY DANGEROUSLY!",                   ("combat", "reckless", "flight")),
    ("concept_now",         "CONCEPT NOW REAL LATER!",            ("buy", "wry", "patience")),
    ("another_hull",        "ANOTHER HULL WON'T HURT!",           ("buy", "fleet", "greed")),
    ("more_scu",            "MORE SCU = MORE YOU!",               ("cargo", "trade", "greed")),
    ("collect_them_all",    "COLLECT THEM ALL!",                  ("buy", "fleet", "greed")),
    ("space_capitalism",    "SUPPORT SPACE CAPITALISM!",          ("trade", "wry", "greed")),
    ("in_ships_we_trust",   "IN SHIPS WE TRUST",                  ("fleet", "solemn")),
    ("not_a_problem",       "IT'S NOT A PROBLEM, IT'S A FLEET!",  ("fleet", "wry", "greed")),
    ("cargo_happy",         "CARGO MAKES ME HAPPY",               ("cargo", "trade", "content")),
    ("crew_bigger",         "CREW BIGGER FLY FURTHER!",           ("crew", "social")),
    ("more_variety",        "MORE VARIETY, MORE FUN!",            ("buy", "fleet")),
    ("pledge_today",        "PLEDGE TODAY, EXPLORE TOMORROW!",    ("buy", "explore", "patience")),
    ("just_one_more",       "JUST ONE MORE!",                     ("buy", "greed", "wry")),
    ("big_ships",           "BIG SHIPS BIG DREAMS!",              ("buy", "fleet", "dream")),
    ("mining_pays",         "MINING PAYS FOR MORE SHIPS!",        ("mining", "trade", "greed")),
    ("trucks_today",        "TRUCKS TODAY, CAPITAL SHIPS TOMORROW!", ("cargo", "trade", "dream")),
    ("small_ships",         "SMALL SHIPS BIG ADVENTURES!",        ("explore", "dream", "flight")),
    ("pirate_today",        "PIRATE TODAY, PLEDGE TOMORROW!",     ("combat", "wry", "greed")),
    ("space_therapy",       "SPACE THERAPY!",                     ("explore", "content", "wry")),
    ("ship_every_mood",     "A SHIP FOR EVERY MOOD!",             ("fleet", "wry", "mood")),
    ("more_friends",        "MORE SHIPS = MORE FRIENDS!",         ("fleet", "social", "wry")),
    ("explore_trade_mine",  "EXPLORE TRADE MINE REPEAT!",         ("explore", "trade", "mining")),
    ("upgrade_happiness",   "UPGRADE YOUR HAPPINESS!",            ("buy", "content", "wry")),
    ("life_better",         "LIFE IS BETTER IN SPACE!",           ("explore", "content", "dream")),
]

#: Which events may ask for a sign at all, and the context tags each one contributes.
#: ⚠ Event names mirror SuitMk2/core/emotion.py's vocabulary where they overlap. An event NOT in
#:   this table can never produce a sign — that is gate 1, and it is a table, not a guess.
EVENT_TAGS = {
    "ship_purchased":     ("buy", "fleet", "greed"),
    "item_earned":        ("buy", "content"),
    "contract_complete":  ("trade", "content"),
    "contract_accepted":  ("trade", "patience"),
    "cargo_sold":         ("cargo", "trade", "greed"),
    "mining_full":        ("mining", "trade"),
    "combat_off":         ("combat", "flight"),
    "qt_arrived":         ("explore", "flight"),
    "location_change":    ("explore",),
    "boarded_ship":       ("fleet", "crew"),
}

#: Moods that FORBID a sign outright. Holding up a joke placard mid-grief is the bug gate 3 exists
#: for. Everything not listed permits one.
#: Game.log parser event names (SuitMk2 event_parser) -> this module's event vocabulary.
EVENT_ALIASES = {"reward_earned": "item_earned", "refinery_complete": "mining_full",
                 "hangar_ready": "boarded_ship"}

MOOD_VETO = frozenset({"grief", "fear", "irritation"})


def pick(event, mood=None, now_s=0.0, last_sign_s=None, recent=(), rng=None,
         cooldown_s=COOLDOWN_S, novelty=NOVELTY_WINDOW, chance=FIRE_CHANCE):
    """-> dict. Always returns a DECISION with a reason, never a bare None.

    A caller that only checks truthiness still behaves correctly, but the reason is the point:
    "no sign" has five distinct causes and a silent None makes them indistinguishable — which is
    the defect I keep finding in my own instruments.
    """
    rng = rng or random.Random()

    if event not in EVENT_TAGS:                                          # gate 1
        return {"show": False, "why": "no_event_claim",
                "detail": "%r is not in EVENT_TAGS; nothing idle pulls a sign" % (event,)}
    if mood in MOOD_VETO:                                                # gate 3 (cheap, before RNG)
        return {"show": False, "why": "mood_veto",
                "detail": "mood %r forbids a sign; the mood layer is authoritative" % (mood,)}
    if last_sign_s is not None and (now_s - last_sign_s) < cooldown_s:   # gate 2
        return {"show": False, "why": "cooldown",
                "detail": "%.0fs since last sign, floor is %ds" % (now_s - last_sign_s, cooldown_s)}

    recent = list(recent)[-novelty:] if novelty else []
    pool = [s for s in SIGNS if s[0] not in recent]                      # gate 4
    if not pool:
        return {"show": False, "why": "novelty_exhausted",
                "detail": "every sign appears in the last %d shown" % novelty}

    if rng.random() >= chance:                                           # gate 5, LAST
        return {"show": False, "why": "die_roll",
                "detail": "passed every gate, rolled above %.2f" % chance}

    ctx = set(EVENT_TAGS[event])
    weights = [1.0 * (TAG_BONUS ** len(ctx & set(tags))) for _sid, _t, tags in pool]
    sid, text, tags = rng.choices(pool, weights=weights, k=1)[0]
    return {"show": True, "why": "fired", "sign": sid, "text": text,
            "tags": list(tags), "matched": sorted(ctx & set(tags)),
            "weight_share": round(
                (1.0 * (TAG_BONUS ** len(ctx & set(tags)))) / sum(weights), 4)}


def pick_idle(mood=None, now_s=0.0, last_sign_s=None, recent=(), rng=None,
              cooldown_s=IDLE_COOLDOWN_S, novelty=NOVELTY_WINDOW, chance=IDLE_CHANCE):
    """-> dict, like pick(), for a sign with NO game event behind it. Same gates minus the event claim:
    mood veto, cooldown, novelty, then the roll. Every sign is equally likely (there is no event to match
    tags against)."""
    rng = rng or random.Random()
    if mood in MOOD_VETO:
        return {"show": False, "why": "mood_veto",
                "detail": "mood %r forbids a sign; the mood layer is authoritative" % (mood,)}
    if last_sign_s is not None and (now_s - last_sign_s) < cooldown_s:
        return {"show": False, "why": "cooldown",
                "detail": "%.0fs since last sign, idle floor is %ds" % (now_s - last_sign_s, cooldown_s)}
    recent = list(recent)[-novelty:] if novelty else []
    pool = [x for x in SIGNS if x[0] not in recent]
    if not pool:
        return {"show": False, "why": "novelty_exhausted",
                "detail": "every sign appears in the last %d shown" % novelty}
    if rng.random() >= chance:
        return {"show": False, "why": "die_roll",
                "detail": "passed every gate, rolled above %.2f" % chance}
    sid, text, tags = rng.choice(pool)
    return {"show": True, "why": "fired_idle", "sign": sid, "text": text, "tags": list(tags), "matched": []}


def selftest():
    ok = True

    def check(label, cond, detail=""):
        nonlocal ok
        print("  %-52s %s%s" % (label, "PASS" if cond else "FAIL", ("  " + detail) if detail else ""))
        if not cond:
            ok = False

    r = random.Random(7)
    # gate 1
    d = pick("brushed_teeth", rng=r)
    check("unknown event never shows a sign", d["show"] is False and d["why"] == "no_event_claim")
    # gate 3 beats gate 5 — a veto must not depend on a roll
    vet = [pick("ship_purchased", mood="grief", now_s=1e9, rng=random.Random(i))
           for i in range(40)]
    check("mood veto holds across 40 seeds", all(v["why"] == "mood_veto" for v in vet))
    # gate 2
    d = pick("ship_purchased", now_s=100.0, last_sign_s=1.0, rng=r)
    check("cooldown blocks a second sign", d["show"] is False and d["why"] == "cooldown")
    d = pick("ship_purchased", now_s=1e9, last_sign_s=1.0, rng=random.Random(1))
    check("cooldown released once elapsed", d["why"] != "cooldown")
    # gate 4
    everything = [s[0] for s in SIGNS]
    d = pick("ship_purchased", now_s=1e9, recent=everything, novelty=len(everything),
             rng=random.Random(3))
    check("novelty can exhaust the pool", d["why"] == "novelty_exhausted")
    d = pick("ship_purchased", now_s=1e9, recent=("buy_more_ships",), chance=1.0,
             rng=random.Random(5))
    check("a recent sign is never re-picked", d.get("sign") != "buy_more_ships")
    # gate 5 — probabilistic, so assert the RATE not one outcome
    fires = sum(1 for i in range(4000)
                if pick("ship_purchased", now_s=1e9, rng=random.Random(i))["show"])
    rate = fires / 4000.0
    check("fire rate near FIRE_CHANCE (%.2f)" % FIRE_CHANCE, 0.30 <= rate <= 0.40,
          "measured %.3f" % rate)
    # ⚠ chance=0 must be IMPOSSIBLE, not merely unlikely — a threshold bug hides at the boundary
    never = any(pick("ship_purchased", now_s=1e9, chance=0.0, rng=random.Random(i))["show"]
                for i in range(500))
    check("chance=0.0 never fires", not never)
    # relevance TILTS, does not FILTER — the whole point of the weighting note
    got = set()
    for i in range(3000):
        d = pick("mining_full", now_s=1e9, chance=1.0, rng=random.Random(i))
        if d["show"]:
            got.add(d["sign"])
    check("relevance tilts but does not filter", len(got) > 12, "%d distinct signs reachable" % len(got))
    mining = sum(1 for i in range(3000)
                 if pick("mining_full", now_s=1e9, chance=1.0,
                         rng=random.Random(i))["sign"] in ("mining_pays", "explore_trade_mine"))
    check("but relevant signs really are favoured", mining / 3000.0 > 0.15,
          "mining-tagged share %.3f" % (mining / 3000.0))
    # every sign is reachable at all — a typo'd tag could orphan one forever
    reach = set()
    for ev in EVENT_TAGS:
        for i in range(600):
            d = pick(ev, now_s=1e9, chance=1.0, rng=random.Random(i))
            if d["show"]:
                reach.add(d["sign"])
    check("every one of the 25 signs is reachable", len(reach) == len(SIGNS),
          "%d/%d" % (len(reach), len(SIGNS)))

    print("\nsign_picker selftest: %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def demo(n=40, seed=11):
    rng = random.Random(seed)
    evs = list(EVENT_TAGS)
    now, last, recent = 0.0, None, []
    shown = 0
    for i in range(n):
        now += rng.uniform(60, 600)
        ev = rng.choice(evs)
        mood = rng.choice([None, None, None, "joy", "curiosity", "grief"])
        d = pick(ev, mood=mood, now_s=now, last_sign_s=last, recent=recent, rng=rng)
        if d["show"]:
            last = now
            recent.append(d["sign"])
            shown += 1
            print("  %6.0fs  %-18s -> %s" % (now, ev, d["text"]))
        else:
            print("  %6.0fs  %-18s    (%s)" % (now, ev, d["why"]))
    print("\n  %d sign(s) in %d events over %.0f minutes" % (shown, n, now / 60.0))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--json", action="store_true", help="dump SIGNS/EVENT_TAGS as JSON and exit")
    a = ap.parse_args()
    if a.json:
        print(json.dumps({"signs": [{"id": s, "text": t, "tags": list(g)} for s, t, g in SIGNS],
                          "event_tags": {k: list(v) for k, v in EVENT_TAGS.items()},
                          "mood_veto": sorted(MOOD_VETO)}, indent=2))
        return 0
    if a.demo:
        return demo()
    return selftest()


if __name__ == "__main__":
    sys.exit(main())
