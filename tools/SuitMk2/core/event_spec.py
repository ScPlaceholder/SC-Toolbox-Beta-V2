"""event_spec.py - semantic specs for EVENT reactions (the event lane; 2026-09-23).

Until now every event reaction in SuitMk2 was worded by a cloud LLM call (dynamic_dialogue -> WingmanAI llm_call).
This module is the event-lane twin of ambient_spec.py: one game event in, at most ONE semantic spec out, built only
from facts the event itself carries. Wording is the local realizer's job; faithfulness is the grounding gate's.

Same shape and rules as ambient_spec:
  * no invented data: if a fact is not in event.data (or the passed state), its claim is simply absent
  * move labels restricted to the ones the character adapters were trained on
  * a small pool of (speaker, move, stance) variants per event, rotated by the caller's counter
  * Montaigne only ever knows things secondhand (the suit's feed, the ship's log)

Event field names come from event_parser.py / event_classifier.py (see the event-lane map, 2026-09-23).
`incapacitated` is included even though the parser does not emit it yet: the moment it does, it speaks.
"""
from __future__ import annotations

import os
from typing import Any, Optional

from ambient_spec import _ELAH_MOVES, _MONT_MOVES, _claim

Spec = dict[str, Any]
_LEN = (6, 32)          # events are reactions: shorter than ambient chatter
# Mk II-style clipped edges. The gate drops a line outside length_words (+5 slack), so without this a one-word
# "Contact." failed as TOO SHORT and combat went silent (end-to-end test, 2026-09-23).
_LEN_BY_EVENT = {"combat_on": (1, 8), "combat_off": (1, 14)}
# Mk II-length edges (2026-09-24): the retrain taught both adapters 2-10 word lines for these events (short_* specs,
# 616 teacher lines, median 8 words). J: "You can keep longer lines as well to keep them interesting", so it is a
# MIX, not a replacement: SHORT_EDGE_SHARE of variants ask for the clipped length, the rest keep _LEN.
# SUITMK2_SHORT_EDGES=0 turns it off for an A/B dry run.
SHORT_EDGE_EVENTS = {"incapacitated", "player_respawned", "injury", "med_bed_heal", "qt_arrived", "boarded_ship",
                     "left_ship", "docking_detached", "contract_accepted", "contract_complete",
                     "entered_monitored_space", "exited_monitored_space", "refinery_complete", "platform_moving",
                     "out_of_medpens", "out_of_mags", "objective_new", "blueprint_received"}
SHORT_EDGE_LEN = (2, 10)
SHORT_EDGE_SHARE = 0.7
SHORT_EDGES = os.environ.get("SUITMK2_SHORT_EDGES", "1") != "0"


# What an injury DOES, so Elah can say it instead of guessing (J 2026-09-24: a sniper hit in the left arm 80 times;
# "Left arm. Your aim's going to drift."). From the game's own medical rules (starcitizen.tools/Medical, checked the
# same evening): arm injuries increase weapon sway, and from Moderate (Tier 2) up they also cost aiming accuracy; head
# injuries blur vision and muffle hearing. Other parts are left unstated rather than guessed.
def injury_effect(part: str, tier: Optional[int]) -> Optional[str]:
    p = str(part or "").lower()
    if "arm" in p or "hand" in p:
        return "more weapon sway and less accurate aim" if tier in (1, 2) else "more weapon sway"
    if "head" in p:
        return "blurred vision and muffled hearing"
    return None


def _length_for(event_type: str, variant: int) -> list:
    if event_type in _LEN_BY_EVENT:
        return list(_LEN_BY_EVENT[event_type])
    if SHORT_EDGES and event_type in SHORT_EDGE_EVENTS and (variant * 7) % 10 < SHORT_EDGE_SHARE * 10:
        return list(SHORT_EDGE_LEN)
    return list(_LEN)

_VARIANTS: dict[str, list[tuple[str, str, str]]] = {
    "injury": [
        ("elah", "PRACTICAL", "you are hurt; get it looked at"),
        ("elah", "DEADPAN", "that one will leave a mark"),
        ("elah", "CORRECTION", "it is not as bad as it felt"),
    ],
    "incapacitated": [
        ("elah", "PRACTICAL", "you went down; stay with me"),
        ("montaigne", "NEAR_RECOGNITION", "the suit has gone quiet; he fears the worst and hopes otherwise"),
    ],
    "med_bed_heal": [
        ("elah", "DEADPAN", "patched up again"),
        ("elah", "CALLBACK", "good as new, more or less"),
        ("montaigne", "SELF_DEPRECATION", "a ship cannot heal itself so neatly"),
    ],
    "qt_arrived": [
        ("montaigne", "ESSAY_DIGRESSION", "arrival is only the start of a new wandering"),
        ("elah", "DEADPAN", "we are here"),
        ("montaigne", "HORSE_ANALOGY", "the ship brought them in like a horse that knows the road"),
    ],
    "location_change": [
        ("elah", "DEADPAN", "noting where we are"),
        ("montaigne", "ESSAY_DIGRESSION", "every place changes the traveller a little"),
    ],
    "jurisdiction_change": [
        ("elah", "PRACTICAL", "different rules apply here"),
        ("montaigne", "EVIDENCE_SKEPTIC", "a new border, the same doubtful laws"),
    ],
    # Respawn: current SC builds log this reliably (~40 s after death), unlike the death itself (death agent, 09-23).
    "player_respawned": [
        ("elah", "DEADPAN", "back on your feet; try not to make it a habit"),
        ("elah", "PRACTICAL", "check your gear before heading back out"),
        ("montaigne", "GRAND_PHILOSOPHY_TO_TRIVIAL", "death reduced to an errand, again"),
    ],
    "reward_earned": [
        ("montaigne", "SELF_DEPRECATION", "paid work, well done, by a ship's modest judgement"),
        ("elah", "PRACTICAL", "worth banking"),
        ("montaigne", "SKEPTICAL_REVERSAL", "money earned, though he doubts it buys much wisdom"),
    ],
    # --- the edge layer (J 09-23: "fire on edges"; measured on his live log: these were parsed and never spoken) ---
    # Boarding / leaving the ship = its crew channel joined / left ("You have joined channel 'Aegis Reclaimer : ...'").
    "boarded_ship": [
        ("elah", "DEADPAN", "back aboard; one clipped line"),
        ("montaigne", "HORSE_ANALOGY", "the pilot is back in the saddle"),
        ("montaigne", "PILOT_CHARACTER", "the pilot has come back to him; he is pleased and hides it badly"),
    ],
    "left_ship": [
        ("montaigne", "SELF_DEPRECATION", "left behind again; he takes it well, mostly"),
        ("elah", "PRACTICAL", "off the ship; it is just the suit now"),
    ],
    "contract_accepted": [
        ("elah", "PRACTICAL", "new job taken; say what it is in a few words"),
        ("montaigne", "ESSAY_DIGRESSION", "a new errand, which he treats as a quest"),
    ],
    "objective_new": [
        ("elah", "PRACTICAL", "the next step, plainly"),
    ],
    "contract_complete": [
        ("elah", "DEADPAN", "job done; say so in a few words"),
        ("montaigne", "GRAND_PHILOSOPHY_TO_TRIVIAL", "a finished job, treated as a small triumph"),
    ],
    "entered_monitored_space": [
        ("elah", "PRACTICAL", "back under comm coverage; security can see us again"),
    ],
    "exited_monitored_space": [
        ("elah", "PRACTICAL", "out of comm coverage; nobody is watching, including help"),
        ("montaigne", "SKEPTICAL_REVERSAL", "unwatched at last, which is not the comfort it sounds"),
    ],
    # combat_watch (SC's own audio onsets + eyes confirm). Clipped on purpose: the Mk II's "Sneaking done. Fighting now."
    "combat_on": [
        ("elah", "PRACTICAL", "contact; one or two words"),
        ("elah", "DEADPAN", "fighting now; clipped"),
    ],
    "combat_off": [
        ("elah", "DEADPAN", "clear; you are still breathing"),
        ("montaigne", "GRAND_PHILOSOPHY_TO_TRIVIAL", "a battle survived, reduced to a footnote"),
    ],
    # "You've earned:" names ITEMS, never money (measured over 80 logs, 2026-09-23); blueprints are their own line.
    "item_earned": [
        ("elah", "DEADPAN", "new gear; name it, no fuss"),
        ("montaigne", "SELF_DEPRECATION", "a prize for the pilot, admired secondhand by a ship"),
    ],
    # Refinery job done (J 2026-09-24, from the old skill's audit): the game already logged it and the parser already
    # emitted it ("A Refinery Work Order has been Completed at Levski"), and nothing ever spoke it.
    # A ship or freight elevator actually MOVING (not the hangar streaming in). Spoken at the start of a freight run
    # and when a ship elevator brings a ship up; the presence throttle keeps a hauling loop from narrating every trip.
    "platform_moving": [
        ("elah", "PRACTICAL", "the lift is moving; one clipped line about what is going up or down"),
        ("montaigne", "ESSAY_DIGRESSION", "the great platform moving, which he finds more ceremonial than it is"),
    ],
    # The Nth boarding of one ship (5/10/25/50/100). Synthesised by companion_core from the boarding count, not a log line.
    "ship_milestone": [
        ("montaigne", "PILOT_CHARACTER", "the pilot has come aboard this ship this many times; he is touched and pretends not to be"),
        ("elah", "CALLBACK", "this many times aboard this ship; one dry line that notices it"),
    ],
    # Loadout (copied from Battle_Buddy): the moment the LAST one goes, not every use.
    "out_of_medpens": [
        ("elah", "PRACTICAL", "that was the last medpen; one clipped line, restock before the next fight"),
        ("montaigne", "SELF_DEPRECATION", "the last of the medicine gone, which worries him more than it should"),
    ],
    "out_of_mags": [
        ("elah", "PRACTICAL", "no spare magazines left; one clipped line about what is in the gun being all there is"),
    ],
    "refinery_complete": [
        ("elah", "PRACTICAL", "the refinery job is done; say where to collect it"),
        ("montaigne", "GRAND_PHILOSOPHY_TO_TRIVIAL", "raw rock made useful at last, which pleases him more than it should"),
    ],
    "blueprint_received": [
        ("elah", "PRACTICAL", "new blueprint; say what it is"),
        ("montaigne", "ESSAY_DIGRESSION", "a plan for a thing, which he suspects he prefers to the thing itself"),
    ],
    "docking_detached": [
        ("elah", "DEADPAN", "undocked; clipped"),
        ("montaigne", "HORSE_ANALOGY", "out of the stable and into the open"),
    ],
}


def _check() -> None:
    for et, pool in _VARIANTS.items():
        for speaker, move, _ in pool:
            assert move in (_ELAH_MOVES if speaker == "elah" else _MONT_MOVES), f"{et}: untrained move {move}"


_check()


def _as_int(v: Any) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _claims_for(event_type: str, d: dict, state: dict) -> tuple[list, list, list]:
    """-> (claims, required_claim_ids, required_values), or ([], ...) when the event lacks its key fact."""
    c, req, vals = [], [], []

    def add(pred, value, required=False, number=False):
        cid = f"C{len(c) + 1}"
        c.append(_claim(cid, "OBSERVED", pred, value))
        if required:
            req.append(cid)
        if number:
            vals.append(str(value))

    if event_type == "injury":
        part = str(d.get("body_part") or "").strip()
        if not part or part == "unknown":
            return [], [], []
        add("suit.injury_body_part", part, required=True)
        tier = _as_int(d.get("tier"))
        if tier is not None:
            add("suit.injury_tier", tier, number=True)
        if d.get("severity"):
            add("suit.injury_severity", str(d["severity"]))
        effect = injury_effect(part, tier)
        if effect:
            add("suit.injury_effect", effect, required=True)
    elif event_type == "incapacitated":
        add("suit.pilot_incapacitated", True, required=True)
        if state.get("location"):
            add("location.name", state["location"])
    elif event_type == "med_bed_heal":
        healed = d.get("healed_parts")
        add("suit.med_bed_heal", True, required=True)
        if isinstance(healed, dict) and healed:
            add("suit.healed_parts", ", ".join(str(k) for k in healed))
        if d.get("location"):
            add("location.name", d["location"])
    elif event_type == "qt_arrived":
        loc = d.get("location") or state.get("location")
        if not loc:
            return [], [], []
        add("qt.arrived_at", loc, required=True)
        if d.get("ship"):
            add("ship.name", d["ship"])
    elif event_type == "location_change":
        loc = d.get("location_name")
        if not loc:
            return [], [], []
        add("location.name", loc, required=True)
        if d.get("star_system"):
            add("location.system", d["star_system"])
        if d.get("is_return_visit") is not None:
            add("location.return_visit", bool(d["is_return_visit"]))
    elif event_type == "jurisdiction_change":
        j = d.get("jurisdiction")
        if not j:
            return [], [], []
        add("jurisdiction.zone", j, required=True)
    elif event_type == "player_respawned":
        add("suit.player_respawned", True, required=True)
        deaths = _as_int(state.get("recent_deaths"))
        if deaths:
            add("suit.regen_count_session", deaths, number=True)
    elif event_type == "reward_earned":
        amount = _as_int(d.get("amount"))
        if not amount:
            return [], [], []
        add("session.reward_auec", amount, required=True, number=True)
    elif event_type in ("combat_on", "combat_off"):
        add("combat.state", "on" if event_type == "combat_on" else "off", required=True)
    elif event_type == "item_earned":
        item = str(d.get("item") or "").strip()
        if not item:
            return [], [], []
        add("reward.item", item, required=True)
    elif event_type == "platform_moving":
        kind, direction = d.get("kind"), d.get("direction")
        # Speak the moments that mean something: a freight run starting (down) and a ship coming up (ship, up).
        if not ((kind == "freight" and direction == "down") or (kind == "ship" and direction == "up")):
            return [], [], []
        add("platform.kind", "freight elevator" if kind == "freight" else "ship elevator", required=True)
        add("platform.direction", direction, required=True)
    elif event_type == "ship_milestone":
        ship, n = str(d.get("ship") or "").strip(), _as_int(d.get("count"))
        if not ship or not n:
            return [], [], []
        add("ship.name", ship, required=True)
        add("ship.times_boarded", n, required=True, number=True)
    elif event_type in ("out_of_medpens", "out_of_mags"):
        add("loadout.item", "medpens" if event_type == "out_of_medpens" else "spare magazines", required=True)
        add("loadout.remaining", 0, required=True)
    elif event_type == "refinery_complete":
        loc = str(d.get("location") or "").strip()
        if not loc:
            return [], [], []
        add("refinery.location", loc, required=True)
    elif event_type == "blueprint_received":
        bp = str(d.get("blueprint") or "").strip()
        if not bp:
            return [], [], []
        add("reward.blueprint", bp, required=True)
    elif event_type in ("boarded_ship", "left_ship"):
        ship = str(d.get("channel") or "").strip()
        if not ship:
            return [], [], []
        add("ship.name", ship, required=True)
        add("ship.pilot_aboard", event_type == "boarded_ship")
    elif event_type in ("contract_accepted", "contract_complete"):
        name = str(d.get("mission_name") or "").strip()
        if not name or "~mission(" in name:          # an unresolved localisation key is not a name
            return [], [], []
        add("mission.name", name, required=True)
        add("mission.status", "accepted" if event_type == "contract_accepted" else "complete")
    elif event_type == "objective_new":
        obj = str(d.get("objective") or "").strip()
        if not obj or "~mission(" in obj:
            return [], [], []
        add("mission.objective", obj, required=True)
    elif event_type in ("entered_monitored_space", "exited_monitored_space"):
        add("space.monitored", event_type == "entered_monitored_space", required=True)
    elif event_type == "docking_detached":
        add("ship.undocked", True, required=True)
        if state.get("ship"):
            add("ship.name", state["ship"])
    return c, req, vals


def build_event_spec(event_type: str, data: dict, state: Optional[dict] = None, variant: int = 0) -> Optional[Spec]:
    """One event -> at most one spec, or None (not a speaking event, or its key fact is missing)."""
    # The classifier renames the pilot's own ship-channel lines to ship_channel_joined / ship_channel_left before the
    # core sees them, so those are the names that arrive in live play (channel_change only in direct calls).
    if event_type in ("ship_channel_joined", "ship_channel_left"):
        event_type = "boarded_ship" if event_type == "ship_channel_joined" else "left_ship"
    if event_type == "channel_change":            # only a SHIP's crew channel is boarding/leaving; party chat is not
        if not (data or {}).get("is_ship") or (data or {}).get("action") not in ("joined", "left"):
            return None
        event_type = "boarded_ship" if (data or {}).get("action") == "joined" else "left_ship"
    if event_type == "reward_earned" and not (data or {}).get("amount") and (data or {}).get("item"):
        event_type = "item_earned"
    pool = _VARIANTS.get(event_type)
    if not pool:
        return None
    claims, req, vals = _claims_for(event_type, data or {}, state or {})
    if not claims:
        return None
    # A tier-3 injury (the worst tier) is never "not as bad as it felt": only the stance that sends the pilot to
    # treatment. Same defect ambient_spec._SERIOUS_INJURY fixes for the follow-up line (2026-09-24).
    # SC counts tiers down: Tier 1 = Severe, Tier 3 = Minor (every injury line in J's logs; corrected 2026-09-24 evening).
    if event_type == "injury" and (_as_int((data or {}).get("tier")) == 1
                                   or str((data or {}).get("severity") or "").lower() == "severe"):
        pool = [("elah", "PRACTICAL", "the worst tier; get to a med bed now")]
    elif event_type == "injury" and injury_effect(str((data or {}).get("body_part") or ""),
                                                  _as_int((data or {}).get("tier"))):
        # Arm or head: what it does to the pilot's shooting or senses is the useful part (J 2026-09-24).
        pool = [("elah", "PRACTICAL", "one clipped line: the part, and what it does to the pilot's aim or senses"),
                ("elah", "DEADPAN", "dry; the part and its effect, no fuss")]
    speaker, move, stance = pool[variant % len(pool)]
    return {"scenario": f"event_{event_type}", "speaker": speaker, "rhetoric": [move], "claims": claims,
            "interpretation": {"owner": speaker, "text": stance}, "required_claims": req,
            "required_values": vals, "length_words": _length_for(event_type, variant),
            "id": f"evt_{event_type}_v{variant % len(pool)}"}


def speaking_events() -> list[str]:
    return list(_VARIANTS)


if __name__ == "__main__":
    import json
    import sys
    ok = True
    cases = [("injury", {"body_part": "left leg", "tier": 2, "severity": "minor", "age_seconds": 9}),
             ("injury", {"body_part": "unknown"}),
             ("reward_earned", {"amount": 15000}), ("reward_earned", {}),
             ("qt_arrived", {"location": "ARC-L1", "ship": "Cutlass Black"}),
             ("location_change", {"location_name": "Lorville", "star_system": "Stanton", "is_return_visit": True}),
             ("chat_noise", {"x": 1})]
    for et, d in cases:
        s = build_event_spec(et, d, {}, 0)
        print(et, "->", None if s is None else (s["speaker"], [c["predicate"] for c in s["claims"]], s["required_values"]))
        if et == "injury" and d.get("age_seconds") and s and any(str(d["age_seconds"]) in json.dumps(c) for c in s["claims"]):
            ok = False
    expect_none = [build_event_spec("injury", {"body_part": "unknown"}), build_event_spec("reward_earned", {}),
                   build_event_spec("chat_noise", {"x": 1})]
    ok = ok and all(x is None for x in expect_none)
    print("event_spec selftest:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)
