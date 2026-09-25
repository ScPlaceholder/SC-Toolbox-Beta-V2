"""idle_spec.py - "idle relationship lines" for quiet stretches: the suit and the ship talking about the PILOT, not the game.

Pattern 4 of the Stealth Suit Mk II (companion_design/REFERENCE_STEALTH_SUIT_MK2.md): "Did you even know you were
wearing me?" / "Nobody ever notices me." Our two ends of it (companion_design/EDGE_LINE_STYLE.md, last section):
  - Elah, the suit: dry, brief, STATES things, never needy. "You forget I'm here. I don't forget you're in me."
  - Montaigne, the ship: warm, digressive, knows things only secondhand. "I have been thinking about you, which is a
    thing ships are not built to do."

Why these specs cannot invent a game fact:
  - The ONLY claim is a speaker-owned OPINION about the relationship (predicate relationship.observation, value a
    short lowercase theme). No location, ship, number or event is read from state and put in front of the realizer.
  - required_values is empty and no claim value contains a digit, so the grounding gate refuses ANY number
    (digits or cardinal words from "two" up).
  - allowed_names is [], which switches the name gate ON with nothing extra allowed: only the gate's permanent list
    (the speakers themselves, "pilot", Montaigne's classical company) survives. See the limitation below.
  - length_words is short: Elah 4-14, Montaigne 8-22.

WHEN (build_idle_spec returns None otherwise). Deliberately simple, all read from the ambient state dict the core
already builds (_ambient_state) plus two optional keys the caller may add:
  1. state["in_combat"] truthy                        -> None  (combat is never a quiet stretch)
  2. state["seconds_since_event"] < QUIET_AFTER_EVENT_S -> None  (optional key; absent means "unknown", not "recent")
  3. any of recent_injuries / recent_deaths / recent_rewards non-empty -> None. VolatileContext's snapshot only
     reports those inside its 10-minute relevance window, so a non-empty value IS "something happened recently",
     and ambient_spec already has a line for it.
Rate limiting (at most one idle line per IDLE_MIN_INTERVAL_S) is the CALLER's job: this module is pure and keeps
no clock. The core owns time.

Rotation: `variant` indexes a fixed, precomputed order of every (theme, move) pair, both speakers spread evenly, so
one full cycle (len(ROTATION) consecutive variants) reaches every theme with every move exactly once.

Known limitation, inherited from grounding_validator.unauthorized_names and stated so it is not mistaken for
coverage: a SENTENCE-INITIAL invented proper noun is not caught (a capital there is ordinary English).

Usage: python idle_spec.py --selftest
"""
from __future__ import annotations

import sys
from typing import Any, Optional

Spec = dict[str, Any]

SCENARIO = "idle_relationship"
PREDICATE = "relationship.observation"
LENGTH = {"elah": (4, 14), "montaigne": (8, 22)}
QUIET_AFTER_EVENT_S = 300          # rule 2: five minutes after any event before an idle line is appropriate
IDLE_MIN_INTERVAL_S = 600          # recommended caller-side floor: one idle line per ten minutes at most

# Trained move labels. Kept in step with ambient_spec's sets (imported when available, so drift fails the selftest).
_ELAH_MOVES = {"CORRECTION", "DEADPAN", "CALLBACK", "PRACTICAL"}
_MONT_MOVES = {"SELF_DEPRECATION", "ESSAY_DIGRESSION", "GRAND_PHILOSOPHY_TO_TRIVIAL", "PILOT_CHARACTER",
               "SKEPTICAL_REVERSAL", "NEAR_RECOGNITION", "EVIDENCE_SKEPTIC", "HORSE_ANALOGY"}

# (key, theme = the OPINION claim value, stance = interpretation, moves). Themes are lowercase and name-free on
# purpose: every word of a claim value becomes an allowed name for the gate.
# Elah: never needy. She states; care shows as fact or practicality, never as a request for attention.
ELAH_THEMES: list[tuple[str, str, str, tuple[str, ...]]] = [
    ("forgets_suit", "the pilot forgets the suit is there",
     "you forget I'm here; I don't forget you. Stated, not a complaint", ("DEADPAN", "CORRECTION")),
    ("notices_more", "the suit notices more than the pilot thinks",
     "I see what you miss; no pride in it, just a fact", ("DEADPAN", "PRACTICAL")),
    ("quiet_is_fine", "quiet stretches suit the suit fine",
     "quiet is fine; no chatter needed", ("DEADPAN", "CALLBACK")),
    ("no_thanks_needed", "the suit keeps the pilot alive without being thanked",
     "thanks not required; it's the job", ("DEADPAN", "CORRECTION")),
    ("worn_not_stored", "the suit would rather be worn than stored",
     "better on you than in a locker; said flat", ("DEADPAN", "CORRECTION")),
    ("ship_talks_more", "the ship talks more than the suit does",
     "the ship does the talking; she does the work", ("DEADPAN", "CALLBACK")),
    ("same_side", "the suit is on the pilot's side",
     "on your side; that is not up for discussion", ("DEADPAN", "CORRECTION")),
    ("knows_habits", "the suit knows the pilot's habits by now",
     "she has learned the pilot's habits; states one without judging it", ("CALLBACK", "PRACTICAL")),
    ("no_reply_needed", "the suit does not need the pilot to talk back",
     "no answer expected; she is fine either way", ("DEADPAN", "PRACTICAL")),
    ("closest_company", "nobody is closer to the pilot than the suit",
     "closest company the pilot keeps, literally; dry, not sentimental", ("DEADPAN", "CALLBACK")),
]

# Montaigne: warm, digressive, and everything he knows about the pilot came from somewhere else (the suit, the
# logs, the pilot's own habits observed from the outside). He may be fond; he may not beg.
MONT_THEMES: list[tuple[str, str, str, tuple[str, ...]]] = [
    ("thinking_of_pilot", "the ship has been thinking about the pilot",
     "he has been thinking about the pilot, which ships are not built to do",
     ("SELF_DEPRECATION", "ESSAY_DIGRESSION", "NEAR_RECOGNITION")),
    ("secondhand_pilot", "the ship knows the pilot mostly secondhand, from the suit",
     "all he knows of the pilot came to him secondhand, and he suspects the suit edits",
     ("EVIDENCE_SKEPTIC", "SELF_DEPRECATION", "PILOT_CHARACTER")),
    ("fond_or_not", "the ship wonders whether the pilot is fond of him",
     "he wonders whether the pilot is fond of him, then decides it hardly matters",
     ("SKEPTICAL_REVERSAL", "ESSAY_DIGRESSION", "SELF_DEPRECATION")),
    ("old_friendship", "a ship and its pilot are an old kind of friendship",
     "a pilot and a ship are one of the oldest friendships, or so he has read",
     ("HORSE_ANALOGY", "GRAND_PHILOSOPHY_TO_TRIVIAL", "ESSAY_DIGRESSION")),
    ("unsure_of_self", "the ship is not certain he is who he believes he is",
     "he is fairly sure who he is, most days, and the pilot is kind not to ask",
     ("NEAR_RECOGNITION", "SELF_DEPRECATION", "SKEPTICAL_REVERSAL")),
    ("pilot_returns", "the pilot keeps coming back to the ship",
     "the pilot always comes back, and he chooses to take it as a compliment",
     ("PILOT_CHARACTER", "HORSE_ANALOGY", "SELF_DEPRECATION")),
    ("silence_between", "friends do not need to fill every silence",
     "a friendship that survives silence is the only kind worth the name",
     ("GRAND_PHILOSOPHY_TO_TRIVIAL", "ESSAY_DIGRESSION", "SKEPTICAL_REVERSAL")),
    ("suit_and_ship", "the suit and the ship compare notes about the pilot",
     "he and the suit compare notes on the pilot; the suit's are shorter",
     ("PILOT_CHARACTER", "EVIDENCE_SKEPTIC", "SELF_DEPRECATION")),
    ("private_essay", "the ship is writing a private essay about the pilot",
     "he is composing an essay on the pilot, and it keeps wandering off the subject",
     ("ESSAY_DIGRESSION", "PILOT_CHARACTER", "GRAND_PHILOSOPHY_TO_TRIVIAL")),
    ("notices_absence", "the ship notices when the pilot is away",
     "he notices the pilot's absence the way a house notices winter, and does not complain of it",
     ("NEAR_RECOGNITION", "ESSAY_DIGRESSION", "HORSE_ANALOGY")),
    # 2026-09-24: from the real Montaigne, Essays II.12 (~line 24,580), who argues for other worlds on the strength of
    # Pliny's headless men. His knowledge of far places is travel writing; so is ours, from brochures. No game facts.
    ("brochure_faith", "the ship's knowledge of far places comes from brochures",
     "among so many systems there must be one where the brochures are accurate, and he means to find it",
     ("SELF_DEPRECATION", "EVIDENCE_SKEPTIC", "GRAND_PHILOSOPHY_TO_TRIVIAL")),
]

THEMES = {"elah": ELAH_THEMES, "montaigne": MONT_THEMES}


def _build_rotation() -> list[tuple[str, int, int]]:
    """Every (speaker, theme index, move index), both speakers spread evenly through one cycle. Within a speaker,
    all themes are visited once with their first move before any theme repeats with its next move."""
    order = []
    for speaker, themes in THEMES.items():
        combos = [(speaker, t, m) for m in range(max(len(x[3]) for x in themes))
                  for t in range(len(themes)) if m < len(themes[t][3])]
        order += [((i + 0.5) / len(combos), speaker, c) for i, c in enumerate(combos)]
    return [c for _, _, c in sorted(order)]


ROTATION = _build_rotation()


def make_spec(speaker: str, theme_idx: int, move_idx: int) -> Spec:
    """One idle spec for a fixed (speaker, theme, move). Pure; used by the runtime rotation and the generator."""
    key, theme, stance, moves = THEMES[speaker][theme_idx]
    move = moves[move_idx % len(moves)]
    return {
        "scenario": SCENARIO,
        "speaker": speaker,
        "rhetoric": [move],
        "claims": [{"id": "C1", "kind": "OPINION", "predicate": PREDICATE, "value": theme}],
        "interpretation": {"owner": speaker, "text": stance},
        "required_claims": ["C1"],
        "required_values": [],
        "length_words": list(LENGTH[speaker]),
        "allowed_names": [],
        "idle_theme": key,
        "id": f"idle_{speaker}_{key}_{move.lower()}",
    }


def all_idle_specs() -> list[Spec]:
    """Every (theme, move) spec, rotation order. For teacher-data generation and tests."""
    return [make_spec(*c) for c in ROTATION]


def quiet_reason(state: dict) -> Optional[str]:
    """Why this is NOT a quiet stretch, or None if it is. Rules 1-3 in the module docstring."""
    if state.get("in_combat"):
        return "in combat"
    since = state.get("seconds_since_event")
    if since is not None:
        try:
            if float(since) < QUIET_AFTER_EVENT_S:
                return f"event {float(since):.0f}s ago"
        except (TypeError, ValueError):
            pass
    for k in ("recent_injuries", "recent_deaths", "recent_rewards"):
        if state.get(k):
            return f"recent {k[7:]}"
    return None


def build_idle_spec(state: dict, variant: int = 0) -> Optional[Spec]:
    """Quiet stretch -> one idle relationship spec (ROTATION[variant % len(ROTATION)]); otherwise None.
    Reads nothing from `state` except the quiet test: no state value ever reaches the spec."""
    if quiet_reason(state or {}) is not None:
        return None
    return make_spec(*ROTATION[variant % len(ROTATION)])


def _check_moves() -> None:
    for speaker, themes in THEMES.items():
        allowed = _ELAH_MOVES if speaker == "elah" else _MONT_MOVES
        for key, _, _, moves in themes:
            for mv in moves:
                assert mv in allowed, f"{speaker}/{key}: {mv} is not a trained {speaker} move"


_check_moves()


def _selftest() -> int:
    import os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from grounding_validator import ground
    results = []

    def case(name, ok, detail=""):
        results.append(ok)
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({detail})" if detail and not ok else ""))

    try:
        import ambient_spec
        case("move sets match ambient_spec's trained sets",
             _ELAH_MOVES == ambient_spec._ELAH_MOVES and _MONT_MOVES == ambient_spec._MONT_MOVES)
    except Exception as e:  # pragma: no cover
        case("ambient_spec importable for the move-set comparison", False, repr(e))

    quiet = {"location": "somewhere", "recent_injuries": [], "recent_deaths": 0, "recent_rewards": 0}
    specs = all_idle_specs()
    keys = {"scenario", "speaker", "rhetoric", "claims", "interpretation", "required_claims", "required_values",
            "length_words", "allowed_names", "id"}
    case("shape: every spec has the ambient spec keys + allowed_names", all(keys <= set(s) for s in specs))
    case("shape: exactly one OPINION claim, owned by the speaker",
         all(len(s["claims"]) == 1 and s["claims"][0]["kind"] == "OPINION" and s["interpretation"]["owner"] == s["speaker"]
             for s in specs))
    case("shape: allowed_names is [] (name gate on, nothing extra)", all(s["allowed_names"] == [] for s in specs))
    case("shape: no digits in any claim value, no required values",
         all(not any(ch.isdigit() for ch in s["claims"][0]["value"]) and not s["required_values"] for s in specs))
    case("shape: ids unique", len({s["id"] for s in specs}) == len(specs))
    case("only trained moves",
         all(s["rhetoric"][0] in (_ELAH_MOVES if s["speaker"] == "elah" else _MONT_MOVES) for s in specs))
    case(">= 8 themes per character", len(ELAH_THEMES) >= 8 and len(MONT_THEMES) >= 8,
         f"{len(ELAH_THEMES)}/{len(MONT_THEMES)}")

    reached = {(s["speaker"], s["idle_theme"], s["rhetoric"][0])
               for s in (build_idle_spec(quiet, v) for v in range(len(ROTATION)))}
    want = {(sp, k, mv) for sp, th in THEMES.items() for k, _, _, mvs in th for mv in mvs}
    case("every theme x move reachable in one rotation cycle", reached == want, f"{len(reached)}/{len(want)}")
    case("rotation deterministic", [build_idle_spec(quiet, v)["id"] for v in range(60)]
         == [build_idle_spec(quiet, v)["id"] for v in range(60)])
    first = [build_idle_spec(quiet, v)["speaker"] for v in range(len(ROTATION))]
    from itertools import groupby
    runs = max(len(list(g)) for _, g in groupby(first))
    case("rotation spreads speakers (no run longer than 3)", runs <= 3, f"longest run {runs}")

    case("None in combat", build_idle_spec({**quiet, "in_combat": True}, 0) is None)
    case("None right after an event", build_idle_spec({**quiet, "seconds_since_event": 30}, 0) is None)
    case("line allowed once the event is old", build_idle_spec({**quiet, "seconds_since_event": 900}, 0) is not None)
    case("None with a recent injury", build_idle_spec({**quiet, "recent_injuries": [{"body_part": "left leg"}]}, 0) is None)
    case("None with a recent death", build_idle_spec({**quiet, "recent_deaths": 1}, 0) is None)
    case("None with a recent reward", build_idle_spec({**quiet, "recent_rewards": 5000}, 0) is None)
    case("a line on an empty state (nothing known is still quiet)", build_idle_spec({}, 0) is not None)

    elah = make_spec("elah", 0, 0)
    mont = make_spec("montaigne", 0, 0)
    ok_e = "You forget I'm here. I don't forget you're in me."
    ok_m = "I have been thinking about you, which is a thing ships are not built to do."
    case("gate passes the reference Elah line", ground(elah, ok_e) == [], str(ground(elah, ok_e)))
    case("gate passes the reference Montaigne line", ground(mont, ok_m) == [], str(ground(mont, ok_m)))
    bad_name = "You forget I'm here, and so does Kestrel."
    case("name gate refuses an invented mid-sentence name",
         any("unauthorized names" in f for f in ground(elah, bad_name)), str(ground(elah, bad_name)))
    bad_name_m = "I have been thinking about you, as the captain of the Vesper once did."
    case("name gate refuses an invented ship name (Montaigne)",
         any("unauthorized names" in f for f in ground(mont, bad_name_m)), str(ground(mont, bad_name_m)))
    case("number gate refuses an invented count", any("unauthorized numbers" in f
                                                      for f in ground(elah, "You forget I'm here. Twelve hours now.")))
    case("length gate refuses a long Elah line",
         any("length" in f for f in ground(elah, " ".join(["quiet"] * 22))))

    n_pass = sum(results)
    print(f"idle_spec selftest: {n_pass}/{len(results)} passed; {len(ELAH_THEMES)} Elah + {len(MONT_THEMES)} Montaigne "
          f"themes, rotation cycle {len(ROTATION)}")
    return 0 if n_pass == len(results) else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print(__doc__)
