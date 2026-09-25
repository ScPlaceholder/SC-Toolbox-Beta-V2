"""ambient_spec.py - pure, WingmanAI-free spec builder for ambient (non-event) companion chatter.

Replaces the ~1,010 hard-coded finished lines that used to live inline in main.py's
_generate_ambient_dialogue (see legacy_ambient_lines.py for the retired bank, kept for reference /
teacher seeding only, never imported). This module invents NO game data: every claim value below
is read straight out of the `state` dict main.py builds from its own trackers (StateStore,
VolatileContext) before calling build_ambient_spec(). If a fact isn't in `state`, its situation
simply doesn't match -- nothing here fabricates a number, name, or event to fill a slot.

Output shape matches companion_design/specs/train.jsonl (one semantic spec):
    {"scenario": str, "speaker": "elah" | "montaigne", "rhetoric": [str, ...],
     "claims": [{"id", "kind", "predicate", "value"}, ...],
     "interpretation": {"owner", "text"}, "required_claims": [str, ...],
     "required_values": [str, ...],   # numbers that MUST survive the realizer, verbatim
     "length_words": [lo, hi], "id": str}

This module decides WHETHER and WHAT (character-engine layer). Wording is the realizer's job;
faithfulness to these claims is the grounding validator's job. See companion_design/ARCHITECTURE.md.

v2 (2026-09-23), three defects found by running the real realizer on these specs:
  1. INJURY passed the whole VolatileContext dict as ONE claim value, so the realizer was handed
     "{'body_part': 'left leg', 'severity': ..., 'tier': 2, 'age_seconds': 41}" and age_seconds became an
     authorised number. Now: body part, tier and severity are separate claims; age never leaves the tracker.
  2. The "session" injury count was len() of VolatileContext's LAST-5 buffer, so it was false after five
     injuries. It is now labelled for what it is: suit.injuries_recent.
  3. Every situation had ONE fixed (speaker, move, stance), so the same situation could only ever mean the
     same thing: sameness was designed in, and the realizer's repeat filter was treating a symptom. Each
     situation now has a small pool of VARIANTS; the caller passes a rotating `variant` counter.
Move labels are restricted to the ones the character adapters were trained on (see _ELAH_MOVES/_MONT_MOVES).
"""
from __future__ import annotations

from typing import Any, Callable, Optional

Spec = dict[str, Any]

_DEFAULT_LENGTH = (8, 40)
_ELAH_MOVES = {"CORRECTION", "DEADPAN", "CALLBACK", "PRACTICAL"}
_MONT_MOVES = {"SELF_DEPRECATION", "ESSAY_DIGRESSION", "GRAND_PHILOSOPHY_TO_TRIVIAL", "PILOT_CHARACTER",
               "SKEPTICAL_REVERSAL", "NEAR_RECOGNITION", "EVIDENCE_SKEPTIC", "HORSE_ANALOGY"}

# (speaker, move, stance). Montaigne only ever has things secondhand; his stances say so or imply it.
_VARIANTS: dict[str, list[tuple[str, str, str]]] = {
    "injury_followup": [
        ("elah", "DEADPAN", "worth a caution flag, not a panic"),
        ("elah", "PRACTICAL", "a med bed will sort it; no rush yet"),
        ("elah", "CORRECTION", "it reads worse than it is"),
        ("montaigne", "NEAR_RECOGNITION", "the suit reports it; he suspects the pilot is tougher than the report"),
    ],
    "regen_followup": [
        ("elah", "DEADPAN", "back to baseline, try not to make it a habit"),
        ("elah", "PRACTICAL", "check the gear before heading back out"),
        ("montaigne", "GRAND_PHILOSOPHY_TO_TRIVIAL", "death reduced to an errand"),
    ],
    "session_reward": [
        ("montaigne", "SELF_DEPRECATION", "not a bad run, for what a ship's opinion is worth"),
        ("montaigne", "SKEPTICAL_REVERSAL", "money well earned, though he doubts it buys wisdom"),
        ("elah", "PRACTICAL", "worth banking before the next risk"),
    ],
    "extended_stay": [
        ("elah", "CALLBACK", "taking your time here, and that's fine"),
        ("elah", "PRACTICAL", "staying or leaving, either is fine; the choice is the pilot's"),
        ("montaigne", "ESSAY_DIGRESSION", "lingering in one place is its own kind of travel"),
    ],
    "busy_session": [
        ("montaigne", "ESSAY_DIGRESSION", "a full day's wandering, by any measure"),
        ("montaigne", "PILOT_CHARACTER", "a restless pilot, and he rather admires it"),
        ("elah", "DEADPAN", "a lot of ground covered today"),
    ],
    "jurisdiction": [
        ("elah", "DEADPAN", "worth knowing before it matters"),
        ("elah", "PRACTICAL", "know what the local law allows here"),
        ("montaigne", "EVIDENCE_SKEPTIC", "laws change at every border; he doubts they grow wiser"),
    ],
    "ship_context": [
        ("montaigne", "ESSAY_DIGRESSION", "worth remarking on, where we've ended up"),
        ("montaigne", "HORSE_ANALOGY", "the ship carried them here the way a horse carries a rider"),
        ("elah", "DEADPAN", "just noting where we are"),
    ],
    "quiet_interval": [
        ("elah", "DEADPAN", "nothing urgent; presence is enough"),
        ("montaigne", "SELF_DEPRECATION", "quiet suits a ship with little to report"),
    ],
}


# Tier 3 is the WORST tier, and the stances above downplay ("no rush yet", "it reads worse than it is"). Performed
# faithfully on a tier-3 head injury that the suit happened to label "minor", they produced "Treatment can wait for
# now" (blind test vs Wingman, 2026-09-24). The realizer did exactly what it was cued; the cue was wrong. So a tier-3
# injury gets its own stances, and the TIER outranks the severity label, which can disagree with it.
# ⛔ CORRECTED THE SAME EVENING: STAR CITIZEN COUNTS TIERS THE OTHER WAY. Every injury line in J's logs pairs Tier 1 with
# "Severe" and Tier 3 with "Minor" (Minor/Tier 3 left arm alone: ~60 injuries; count "Added notification" lines, the rest are echoes). The glossary this was built on said "1 is the
# mildest, 3 the worst", and nobody checked it against a log. As first shipped, this told the pilot to get to a med bed
# NOW for the commonest scratch in the game. A serious injury is Tier 1 (or the label Severe).
SERIOUS_INJURY_TIER = 1
_SERIOUS_INJURY: list[tuple[str, str, str]] = [
    ("elah", "PRACTICAL", "the worst tier; get to a med bed now"),
    ("elah", "CORRECTION", "severe; this one needs a med bed now, not later"),
]
# Elah only: the suit's medical call is hers, and Montaigne's hedging moves ("nearly", "I suspect") turned a tier-3
# head injury into "merely minor" the first time they were tried.


def _claim(cid: str, kind: str, predicate: str, value: Any) -> dict:
    return {"id": cid, "kind": kind, "predicate": predicate, "value": value}


def _spec(scenario: str, variant: int, claims: list, required_claims: list, required_values: list) -> Spec:
    speaker, move, stance = _VARIANTS[scenario][variant % len(_VARIANTS[scenario])]
    return {
        "scenario": scenario,
        "speaker": speaker,
        "rhetoric": [move],
        "claims": claims,
        "interpretation": {"owner": speaker, "text": stance},
        "required_claims": required_claims,
        "required_values": required_values,
        "length_words": list(_DEFAULT_LENGTH),
        "id": f"amb_{scenario}_v{variant % len(_VARIANTS[scenario])}",
    }


def _as_int(v: Any) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _situation_injury(state: dict, variant: int) -> Optional[Spec]:
    """The most recent on-foot injury, from VolatileContext's recent_injuries entries
    ({body_part, severity, tier, age_seconds}); a bare string entry is accepted as the body part."""
    injuries = state.get("recent_injuries") or []
    if not injuries:
        return None
    last = injuries[-1]
    if isinstance(last, dict):
        part = str(last.get("body_part") or "").strip()
        tier = _as_int(last.get("tier"))
        severity = str(last.get("severity") or "").strip()
    else:
        part, tier, severity = str(last).strip(), None, ""
    if not part or part == "unknown":
        return None
    claims = [_claim("C1", "OBSERVED", "suit.injury_body_part", part)]
    required_values = []
    if tier is not None:
        claims.append(_claim("C2", "OBSERVED", "suit.injury_tier", tier))
        required_values.append(str(tier))
    if severity:
        claims.append(_claim("C3", "OBSERVED", "suit.injury_severity", severity))
    if len(injuries) > 1:
        claims.append(_claim("C4", "OBSERVED", "suit.injuries_recent", len(injuries)))
    spec = _spec("injury_followup", variant, claims, ["C1"], required_values)
    if (tier is not None and tier <= SERIOUS_INJURY_TIER) or severity.lower() == "severe":
        k = variant % len(_SERIOUS_INJURY)
        speaker, move, stance = _SERIOUS_INJURY[k]
        spec.update(speaker=speaker, rhetoric=[move], interpretation={"owner": speaker, "text": stance},
                    id=f"amb_injury_followup_serious_v{k}")
    return spec


def _situation_regen(state: dict, variant: int) -> Optional[Spec]:
    deaths = _as_int(state.get("recent_deaths")) or 0
    if not deaths:
        return None
    return _spec("regen_followup", variant,
                 [_claim("C1", "OBSERVED", "suit.regen_count_session", deaths)], ["C1"], [str(deaths)])


def _situation_reward(state: dict, variant: int) -> Optional[Spec]:
    reward = _as_int(state.get("recent_rewards")) or 0
    if not reward:
        return None
    return _spec("session_reward", variant,
                 [_claim("C1", "OBSERVED", "session.reward_auec", reward)], ["C1"], [str(reward)])


def _situation_extended_stay(state: dict, variant: int) -> Optional[Spec]:
    minutes = _as_int(state.get("minutes_at_location")) or 0
    location = state.get("location")
    if not location or minutes <= 15:
        return None
    return _spec("extended_stay", variant,
                 [_claim("C1", "OBSERVED", "location.name", location),
                  _claim("C2", "OBSERVED", "location.minutes_this_visit", minutes)],
                 ["C1", "C2"], [str(minutes)])


def _situation_busy_session(state: dict, variant: int) -> Optional[Spec]:
    recent_locations = state.get("recent_locations") or []
    if len(recent_locations) < 3:
        return None
    count = len(recent_locations)
    return _spec("busy_session", variant,
                 [_claim("C1", "OBSERVED", "session.locations_visited", count)], ["C1"], [str(count)])


def _situation_jurisdiction(state: dict, variant: int) -> Optional[Spec]:
    jurisdiction = state.get("jurisdiction")
    if not jurisdiction:
        return None
    return _spec("jurisdiction", variant,
                 [_claim("C1", "OBSERVED", "jurisdiction.zone", jurisdiction),
                  _claim("C2", "OBSERVED", "jurisdiction.armistice", bool(state.get("in_armistice", False)))],
                 ["C1"], [])


def _situation_ship_context(state: dict, variant: int) -> Optional[Spec]:
    ship = state.get("ship")
    system = state.get("system")
    if not ship or not system:
        return None
    claims = [_claim("C1", "OBSERVED", "ship.name", ship), _claim("C2", "OBSERVED", "ship.system", system)]
    required = ["C1"]
    if state.get("vehicle_kind"):          # J 09-23: "Montaigne is talking about land vehicles that can fly"
        claims.append(_claim("C3", "OBSERVED", "ship.kind", state["vehicle_kind"]))
        required.append("C3")
    return _spec("ship_context", variant, claims, required, [])


def _situation_quiet(state: dict, variant: int) -> Optional[Spec]:
    """Last resort: nothing else matched. DISABLED for speech (2026-09-23, J's first dry run): its only fact is the
    companion's own speech-queue size, so Elah said "Nothing to do, given the queue's emptiness" to a player.
    Nothing to say means silence. Kept (returning None) so the variant table and training data stay valid."""
    if not state.get("_allow_quiet_line"):
        return None
    return _spec("quiet_interval", variant,
                 [_claim("C1", "OBSERVED", "dialogue.queue_size", int(state.get("queue_size", 0)))], ["C1"], [])


# Ordered highest-signal first: recent, personal, concrete events before ambient location/ship
# color. _situation_quiet always matches, so it is the deliberate last resort, not a gap.
_SITUATIONS: list[Callable[[dict, int], Optional[Spec]]] = [
    _situation_injury,
    _situation_regen,
    _situation_reward,
    _situation_extended_stay,
    _situation_busy_session,
    _situation_jurisdiction,
    _situation_ship_context,
    _situation_quiet,
]


def build_ambient_spec(state: dict, variant: int = 0,
                       skip: Optional[Callable[[Spec], bool]] = None) -> Optional[Spec]:
    """One state dict in -> at most ONE semantic spec out (or None if nothing at all to say).

    `variant` picks the (speaker, move, stance) for the matched situation; the caller rotates it
    (main.py keeps a counter) so the same situation does not always mean the same thing. First
    matching situation wins, UNLESS `skip(spec)` says its subject is already talked out (topic_ledger):
    then the next matching situation gets its turn, and if every one is spent the answer is silence.
    Without `skip`, one situation can own every tick for as long as its state holds (J 2026-09-23).
    """
    for situation in _SITUATIONS:
        spec = situation(state, variant)
        if spec is not None and not (skip is not None and skip(spec)):
            return spec
    return None


def all_variants(state: dict) -> list[Spec]:
    """Every variant of the situation this state matches. For teacher-data generation and tests."""
    first = build_ambient_spec(state, 0)
    if first is None:
        return []
    return [build_ambient_spec(state, v) for v in range(len(_VARIANTS[first["scenario"]]))]


def _check_variants() -> None:
    for scenario, pool in _VARIANTS.items():
        for speaker, move, _ in pool:
            allowed = _ELAH_MOVES if speaker == "elah" else _MONT_MOVES
            assert move in allowed, f"{scenario}: {move} is not a trained {speaker} move"


_check_variants()
