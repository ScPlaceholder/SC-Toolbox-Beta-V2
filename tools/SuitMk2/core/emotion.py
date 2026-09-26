"""emotion.py - Elah's and Montaigne's feelings, fed by the game (J 2026-09-24: "Should the companions have your
emotional engine except have SC hooks added to every emotion?").

Ported from the DESIGN of Elah's own affect engine (elah-audio/affect_model.py), not its code. Two rules carried over:

  1. A FEELING MUST CHANGE BEHAVIOUR, or it is a lookup table wearing a feeling's clothes. So mood reaches the output
     in deterministic places only: the RHETORICAL MOVE of the next spec (a move the model was trained to perform), its
     LENGTH ceiling (a nudge), whether idle talk is HUSHED for a while, and, only for the Claude API backend, a STANCE
     phrase (the local model narrates stance text instead of showing it; measured, see RHETORIC below). It never
     touches facts; grounding is unchanged.
  2. THE WORK GOES INTO INPUTS AND CONSEQUENCES, NOT VOCABULARY. The emotion list is short and arbitrary on purpose
     (affect_model.py's standing decision, after James: any classification of emotions is as true as any other if it
     serves a purpose). What matters is the HOOKS table: which game event moves which feeling, per character.

And one thing SC gives them that Elah's own engine never had: involuntary input. Her engine is underfed because almost
nothing she does is an event she did not author. The game is nothing but such events.

The two characters care about different things. Elah is the suit: she fears FOR THE PILOT, takes pride in clean work,
gets irritated when the pilot keeps dying the same way. Montaigne is the ship: he dreads for himself and his hull, is
warmed when the pilot comes back aboard, a little bereft when left behind.

Time: every level decays by wall-clock half-life, and state persists per pilot (affect.json in the pilot's memory dir)
with its timestamp, so a scare from last night is aged on load rather than resurrected fresh (the same fix Elah's
affect_boot.py applies to her).
    python emotion.py --selftest
"""
from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path
from typing import Callable, Optional

SPEAKERS = ("elah", "montaigne")

# name -> half-life in seconds. Fear flares and fades; warmth and grief linger.
EMOTIONS = {
    "fear": 300.0, "relief": 240.0, "pride": 900.0, "joy": 600.0, "curiosity": 300.0,
    "boredom": 1200.0, "irritation": 900.0, "warmth": 1800.0, "grief": 1800.0,
}
CAP = 1.0

# ---- the hooks: game event -> {speaker: [(emotion, amount)]} ----------------------------------------------------------
HOOKS = {
    "incapacitated":           {"elah": [("fear", 0.8)], "montaigne": [("fear", 0.5), ("grief", 0.2)]},
    "player_respawned":        {"elah": [("relief", 0.6)], "montaigne": [("relief", 0.5)]},
    "injury":                  {"elah": [("fear", 0.25)], "montaigne": [("fear", 0.1)]},
    "med_bed_heal":            {"elah": [("relief", 0.4)], "montaigne": [("relief", 0.3)]},
    "combat_on":               {"elah": [("fear", 0.2)], "montaigne": [("fear", 0.5)]},
    "combat_off":              {"elah": [("relief", 0.4)], "montaigne": [("relief", 0.5)]},
    "contract_accepted":       {"elah": [("curiosity", 0.3)], "montaigne": [("curiosity", 0.4)]},
    "objective_new":           {"elah": [("curiosity", 0.15)], "montaigne": [("curiosity", 0.15)]},
    "contract_complete":       {"elah": [("pride", 0.6)], "montaigne": [("joy", 0.4)]},
    "reward_earned":           {"elah": [("joy", 0.2)], "montaigne": [("joy", 0.3)]},     # scaled by amount below
    "item_earned":             {"elah": [("joy", 0.2)], "montaigne": [("joy", 0.2)]},
    "blueprint_received":      {"elah": [("joy", 0.3), ("curiosity", 0.2)], "montaigne": [("curiosity", 0.3)]},
    "refinery_complete":       {"elah": [("pride", 0.3)], "montaigne": [("joy", 0.3)]},
    "location_change":         {"elah": [("curiosity", 0.2)], "montaigne": [("curiosity", 0.3)]},  # x3 on first visit
    "qt_arrived":              {"elah": [("curiosity", 0.15)], "montaigne": [("curiosity", 0.2)]},
    "boarded_ship":            {"elah": [("relief", 0.1)], "montaigne": [("warmth", 0.6)]},
    "left_ship":               {"montaigne": [("grief", 0.25)]},
    "entered_monitored_space": {"elah": [("relief", 0.2)], "montaigne": [("relief", 0.2)]},
    "exited_monitored_space":  {"elah": [("fear", 0.15)], "montaigne": [("fear", 0.25)]},
    "pilot_spoke":             {"elah": [("warmth", 0.3)], "montaigne": [("warmth", 0.35)]},
    "scene_notable":           {"elah": [("curiosity", 0.25)], "montaigne": [("curiosity", 0.3)]},
    # bdl_tracker.py (an ESTIMATE from med pen use): too many stims worries the suit; the load coming down relieves it
    "bdl_warning":             {"elah": [("fear", 0.3)], "montaigne": [("fear", 0.1)]},
    "bdl_clear":               {"elah": [("relief", 0.3)]},
}
# Follow-up lines that are still ABOUT an event (ambient_spec scenarios), so appraisal() can find their feeling.
EVENT_OF_SCENARIO = {"injury_followup": "injury", "regen_followup": "player_respawned",
                     "session_reward": "reward_earned"}
# Anything that happens takes the edge off boredom: novelty is the cure for it, not time.
BOREDOM_RELIEF_PER_EVENT = 0.25
# Slow, deterministic drift while nothing happens in a loop scene (mining / trading / PRESENCE mode): banter territory.
BOREDOM_PER_MIN = 0.02
REPEAT_DEATH_WINDOW_S = 1800.0      # a second death inside 30 min: Elah is irritated as well as frightened

# ---- consequences -----------------------------------------------------------------------------------------------
MOOD_FLOOR = 0.25                   # below this, no emotion colours a line
HUSH_LEVEL = 0.5                    # fear or grief this strong: idle talk waits
# Stance = what the feeling DOES to the line, never the feeling's name (2026-09-24). The first version named the
# feeling ("she is quietly pleased with how that went") and the 1.5B repeated it: 11 of 14 feeling-naming lines on
# the replay said "pleased", including on an injury. A cue made of behaviour gives it nothing to parrot, and it is the
# "show, don't tell" the characters need anyway. No emotion word may appear in these strings (selftest enforces it).
STANCE = {
    "elah": {
        "fear": "clipped sentences that end on a short question to the pilot",
        "relief": "loosen up a notch; one dry joke is allowed",
        "pride": "understated; state the result and move on",
        "joy": "a lighter touch than usual; one small flourish",
        "curiosity": "lean in and point at one detail",
        "boredom": "needle the pilot a little about doing this again",
        "irritation": "blunt and protective; say what to do differently",
        "warmth": "speak to the pilot directly, a shade softer than usual",
        "grief": "very few words; no jokes",
    },
    "montaigne": {
        "fear": "fuss over the hull and talk a little too fast",
        "relief": "a touch theatrical; breathe out into a flourish",
        "pride": "make a little too much of it",
        "joy": "expansive; one extra clause just because",
        "curiosity": "start composing an essay about it out loud",
        "boredom": "reach for a digression",
        "irritation": "one pointed aside, then act as if he said nothing",
        "warmth": "treat the pilot as good company; one extra clause spent on them",
        "grief": "quieter than usual; think aloud about being left alone",
    },
}
# Gentle on purpose. MEASURED 2026-09-24: fear at 0.7 cut Montaigne's topic ceiling 40 -> 28 words, his normal line
# ran ~36, and grounding REFUSED it as too long. The model does not honour LENGTH tightly, so a hard cut buys refusals
# and retries (and silence), not shorter lines. A nudge, not a wall.
LENGTH_SCALE = {"fear": 0.85, "grief": 0.9, "irritation": 0.9, "joy": 1.15, "curiosity": 1.1, "boredom": 1.1}

# ⚠ MEASURED 2026-09-24 on realizer-elah at temp 0: handed the STANCE text above, the small local model NARRATES the
#   mood instead of showing it: "Back aboard Argo MOLE. The scare remains hidden in this clipped sentence." /
#   "I'm holding back on the excitement." / "That was a quiet success for me." It was never trained on stance phrases
#   like these, so it reads them as content. Grounding passes (no invented facts), and the lines are still bad.
# ⇒ For the LOCAL model mood uses only levers IN ITS TRAINING: the rhetorical move (which it performs and never names)
#   and the length. The stance text goes only to backends that can show a mood without announcing it (the Claude API:
#   set stance_text=True). Teaching the local model mood-conditioned lines is a retrain job, not a prompt trick.
RHETORIC = {
    "elah": {"fear": "PRACTICAL", "grief": "PRACTICAL", "irritation": "CORRECTION", "pride": "DEADPAN",
             "relief": "DEADPAN", "boredom": "DEADPAN", "joy": "CALLBACK", "warmth": "CALLBACK"},
    "montaigne": {"fear": "SELF_DEPRECATION", "grief": "SELF_DEPRECATION", "irritation": "SKEPTICAL_REVERSAL",
                  "pride": "GRAND_PHILOSOPHY_TO_TRIVIAL", "relief": "GRAND_PHILOSOPHY_TO_TRIVIAL",
                  "joy": "ESSAY_DIGRESSION", "boredom": "ESSAY_DIGRESSION", "curiosity": "NEAR_RECOGNITION",
                  "warmth": "PILOT_CHARACTER"},
}
RHETORIC_LEVEL = 0.4                # a feeling must be this strong before it changes HOW a line is built


# 2026-09-24: the local adapters were retrained on 864 mood-coloured teacher lines (stance phrase included) whose
# feeling-NAMING lines were filtered out, so the local model is now TRAINED to show the stance rather than narrate
# it. Whether that holds on real play is what the dry run decides; SUITMK2_LOCAL_STANCE=0/1 overrides for an A/B.
# ⛔ MEASURED THE SAME DAY (dry_ab.py, J's five sessions, same models): with stance text on, lines that NAME a feeling
# went 3 -> 14 of ~510 ("Back aboard Drake Clipper. I'm rather pleased with myself." / "I am relieved to have that
# fact."). The retrain did not teach it; the local model still narrates. OFF by default; the API still gets it.
# ✓ THEN FIXED THE CUES, NOT THE MODEL (same evening): with the cues rewritten as behaviour (no emotion words), the
# replay gave 4 feeling-naming lines against a mood-off baseline of 3, and 0 wrong-feeling lines. J: "Let's turn it on."
# ON by default again; SUITMK2_LOCAL_STANCE=0 turns it off.
LOCAL_STANCE_TRAINED = os.environ.get("SUITMK2_LOCAL_STANCE", "1") != "0"


class CompanionAffect:
    def __init__(self, path: Optional[Path] = None, now: Callable[[], float] = time.time,
                 stance_text: Optional[bool] = None):
        self.path = Path(path) if path else None
        self._now = now
        # True for a backend that shows a mood instead of narrating it: the API always, the local model once trained
        self.stance_text = LOCAL_STANCE_TRAINED if stance_text is None else stance_text
        self.levels = {s: {e: 0.0 for e in EMOTIONS} for s in SPEAKERS}
        self._t = now()
        self._deaths: list = []
        self.last_cause = {s: "" for s in SPEAKERS}
        self._seen_places: set = set()
        self._load()

    # -- time --------------------------------------------------------------------------------------------------
    def _decay(self, t: Optional[float] = None) -> None:
        t = self._now() if t is None else t
        dt = max(0.0, t - self._t)
        if dt:
            for s in SPEAKERS:
                for e, hl in EMOTIONS.items():
                    self.levels[s][e] *= math.pow(0.5, dt / hl)
            self._t = t

    def _add(self, speaker: str, emotion: str, amount: float, cause: str) -> None:
        lv = self.levels[speaker]
        lv[emotion] = min(CAP, lv[emotion] + amount)
        if amount > 0:
            self.last_cause[speaker] = cause

    # -- inputs ------------------------------------------------------------------------------------------------
    def feed(self, event_type: str, data: Optional[dict] = None) -> bool:
        """One game event. -> True if it moved anything."""
        hook = HOOKS.get(event_type)
        if hook is None:
            return False
        self._decay()
        data = data or {}
        scale = 1.0
        if event_type == "reward_earned":                # 1.5k -> ~0.5x, 100k -> ~1.5x, capped
            amt = float(data.get("amount") or 0)
            scale = max(0.3, min(2.0, math.log10(max(amt, 1.0)) / 3.3))
        if event_type == "injury":
            # SC counts injury tiers DOWN: Tier 1 is the worst ("get to a med bed"), Tier 3 the mildest.
            # This was reversed until 2026-09-25 (J: "reversed ... happened by accident").
            scale = {1: 2.4, 2: 1.6, 3: 1.0}.get(int(data.get("tier") or 3), 1.0)
        # contract_history.py: finishing the pilot's specialty (or a first of a new kind) is felt a little more.
        scale *= max(0.5, min(2.0, float(data.get("affect_scale") or 1.0)))
        if event_type == "location_change":
            place = data.get("location_name") or data.get("location")
            if place and place not in self._seen_places and not data.get("is_return_visit"):
                self._seen_places.add(place)
                scale = 3.0
        for s, moves in hook.items():
            for e, amt in moves:
                self._add(s, e, amt * scale, event_type)
            self.levels[s]["boredom"] = max(0.0, self.levels[s]["boredom"] - BOREDOM_RELIEF_PER_EVENT)
        if event_type == "incapacitated":
            t = self._now()
            self._deaths = [d for d in self._deaths if t - d < REPEAT_DEATH_WINDOW_S] + [t]
            if len(self._deaths) >= 2:
                self._add("elah", "irritation", 0.4 * (len(self._deaths) - 1), "repeat_death")
        if event_type in ("player_respawned", "combat_off", "med_bed_heal", "entered_monitored_space"):
            for s in SPEAKERS:                           # relief eats fear rather than sitting beside it
                self.levels[s]["fear"] *= 0.5
        self._save()
        return True

    def drift(self, minutes: float, looping: bool) -> None:
        """Called on a tick. In a slow loop (hauling, mining, PRESENCE) boredom creeps up; otherwise only decay."""
        self._decay()
        if looping and minutes > 0:
            for s in SPEAKERS:
                self._add(s, "boredom", BOREDOM_PER_MIN * minutes, "loop")

    # -- consequences ------------------------------------------------------------------------------------------
    def dominant(self, speaker: str) -> tuple[Optional[str], float]:
        self._decay()
        lv = self.levels.get(speaker) or {}
        if not lv:
            return None, 0.0
        e = max(lv, key=lv.get)
        return (e, lv[e]) if lv[e] >= MOOD_FLOOR else (None, lv[e])

    def hushed(self) -> bool:
        """Fear or grief strong enough that idle talk should wait (a death is a beat, not a cue for banter)."""
        self._decay()
        return any(self.levels[s]["fear"] >= HUSH_LEVEL or self.levels[s]["grief"] >= HUSH_LEVEL for s in SPEAKERS)

    def appraisal(self, spec: dict) -> tuple[Optional[str], float]:
        """The feeling a line is ABOUT. A line reacting to an event takes that event's own emotion (its strongest hook
        for this speaker), at least MOOD_FLOOR strong; only idle talk takes the background dominant mood.

        J, 2026-09-24, on the replay: "hey you're injured, I'm pleased... where the player is injured worry or concern
        should show. When they're better the ai should feel relief not pleasure." The cause was here: color() used the
        strongest mood overall, an injury adds only 0.25 fear, and warmth left over from boarding outranked it, so the
        injury line was cued "she is pleased". Appraisal = event x concern, not whatever mood is loudest."""
        sp = spec.get("speaker")
        scen = str(spec.get("scenario") or "")
        ev = scen[len("event_"):] if scen.startswith("event_") else EVENT_OF_SCENARIO.get(scen)
        hooks = (HOOKS.get(ev) or {}).get(sp) if ev else None
        if hooks:
            e = max(hooks, key=lambda h: h[1])[0]
            self._decay()
            return e, max(self.levels[sp][e], MOOD_FLOOR)
        return self.dominant(sp) if sp in SPEAKERS else (None, 0.0)

    def color(self, spec: dict) -> dict:
        """Colour one spec IN PLACE for its speaker's mood; returns what it did (for the log). Facts untouched."""
        sp = spec.get("speaker")
        e, lvl = self.appraisal(spec) if sp in SPEAKERS else (None, 0.0)
        if e is None:
            return {}
        if spec.get("mood"):
            return {}                                     # already coloured once; never stack
        did = {"emotion": e, "level": round(lvl, 2)}
        move = RHETORIC.get(sp, {}).get(e)
        if move and lvl >= RHETORIC_LEVEL and spec.get("rhetoric"):
            spec["rhetoric"] = [move] + [r for r in spec["rhetoric"][1:] if r != move]
            did["rhetoric"] = move
        if self.stance_text:
            interp = spec.setdefault("interpretation", {"owner": sp, "text": ""})
            mood = STANCE[sp][e]
            interp["text"] = ((interp.get("text") or "").rstrip(". ") + "; " + mood).lstrip("; ")
            did["stance"] = True
        lo, hi = spec.get("length_words") or (4, 18)
        k = LENGTH_SCALE.get(e, 1.0)
        spec["length_words"] = [lo, max(lo + 2, int(round(hi * k)))]
        spec["mood"] = {"emotion": e, "level": round(lvl, 2)}
        did["length"] = spec["length_words"]
        return did

    def state(self) -> dict:
        self._decay()
        return {s: {e: round(v, 2) for e, v in self.levels[s].items() if v >= 0.05} for s in SPEAKERS}

    # -- persistence -------------------------------------------------------------------------------------------
    def _save(self) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({"t": self._t, "levels": self.levels, "deaths": self._deaths,
                                       "places": sorted(self._seen_places)[-500:]}), encoding="utf-8")
            os.replace(tmp, self.path)
        except Exception:
            pass                                          # a mood that cannot be saved is still a mood

    def _load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            for s in SPEAKERS:
                for e in EMOTIONS:
                    self.levels[s][e] = float(d.get("levels", {}).get(s, {}).get(e, 0.0))
            self._t = float(d.get("t", self._now()))
            self._deaths = [float(x) for x in d.get("deaths", [])]
            self._seen_places = set(d.get("places", []))
            self._decay()                                 # AGE it: the time the app was closed counts
        except Exception:
            self.levels = {s: {e: 0.0 for e in EMOTIONS} for s in SPEAKERS}


def _selftest() -> int:
    import tempfile
    ok = True

    def case(name, cond):
        nonlocal ok
        ok &= bool(cond)
        print(("  PASS  " if cond else "  FAIL  ") + name)
    clock = [1_000_000.0]
    case("the default follows LOCAL_STANCE_TRAINED (09-24 retrain)",
         CompanionAffect(now=lambda: clock[0]).stance_text == LOCAL_STANCE_TRAINED)
    a = CompanionAffect(now=lambda: clock[0], stance_text=False)      # an UNTRAINED local backend
    case("starts calm: no mood colours a line", a.dominant("elah") == (None, 0.0))
    a.feed("incapacitated")
    case("a death frightens Elah FOR the pilot", a.dominant("elah")[0] == "fear")
    case("...and Montaigne too, less", a.levels["montaigne"]["fear"] < a.levels["elah"]["fear"])
    case("a death hushes idle talk", a.hushed())
    spec = {"speaker": "elah", "interpretation": {"owner": "elah", "text": "one clipped line"}, "rhetoric": ["DEADPAN"],
            "length_words": [4, 20], "claims": [{"id": "C1", "value": "x"}]}
    did = a.color(spec)
    case("fear shortens her line (a nudge: 20 -> 17)", spec["length_words"] == [4, 17] and did["emotion"] == "fear")
    case("LOCAL model: fear switches her to a TRAINED move (PRACTICAL)", spec["rhetoric"] == ["PRACTICAL"])
    case("UNTRAINED local backend: no stance text (it narrates it: 'the scare remains hidden...')",
         spec["interpretation"]["text"] == "one clipped line")
    # J's case (2026-09-24): warm from boarding, then hurt. The injury line must carry concern, not the warmth.
    w = CompanionAffect(now=lambda: clock[0], stance_text=False)
    w.levels["elah"]["warmth"] = 0.7
    w.feed("injury")
    hurt = {"speaker": "elah", "scenario": "event_injury", "rhetoric": ["DEADPAN"], "length_words": [4, 20],
            "interpretation": {"owner": "elah", "text": "x"}, "claims": []}
    case("an injury line is coloured by the injury (fear), not the louder leftover warmth",
         w.dominant("elah")[0] == "warmth" and w.color(hurt).get("emotion") == "fear")
    healed = dict(hurt, scenario="event_med_bed_heal", rhetoric=["DEADPAN"])
    healed.pop("mood", None)
    case("a heal line is coloured by relief", w.color(healed).get("emotion") == "relief")
    idle = dict(hurt, scenario="ship_context", rhetoric=["DEADPAN"])
    idle.pop("mood", None)
    case("idle talk keeps the background mood", w.color(idle).get("emotion") == "warmth")
    # Stance cues describe behaviour and never NAME a feeling: the model repeats whatever word it is handed.
    import re as _re
    _named = _re.compile(r"\b(?:pleas\w*|happ\w*|glad|proud|pride|reliev\w*|relief|afraid|scared|fear\w*|nervous|"
                         r"rattled|curious|curiosity|bored\w*|irritat\w*|peevish|warm\w*|melanchol\w*|subdued|sad\w*|"
                         r"joy\w*|grie\w*|mood)\b", _re.I)
    leaks = [(s_, e_, t_) for s_ in STANCE for e_, t_ in STANCE[s_].items() if _named.search(t_)]
    case(f"no stance cue names a feeling ({leaks[:2] if leaks else 'none'})", not leaks)
    case("colouring never touches the facts", spec["claims"] == [{"id": "C1", "value": "x"}])
    a.color(spec)
    case("colouring twice does not stack", spec["length_words"] == [4, 17])
    api = CompanionAffect(now=lambda: clock[0], stance_text=True)
    api.feed("incapacitated")
    s2 = {"speaker": "elah", "interpretation": {"owner": "elah", "text": "one clipped line"}, "rhetoric": ["DEADPAN"],
          "length_words": [4, 20], "claims": []}
    api.color(s2)
    case("API backend: stance text IS added (it shows a mood rather than announcing it)",
         STANCE["elah"]["fear"] in s2["interpretation"]["text"])
    case("every rhetoric swap is a move the model was trained on",
         all(m in {"DEADPAN", "PRACTICAL", "CORRECTION", "CALLBACK"} for m in RHETORIC["elah"].values()) and
         all(m in {"ESSAY_DIGRESSION", "SELF_DEPRECATION", "SKEPTICAL_REVERSAL", "GRAND_PHILOSOPHY_TO_TRIVIAL",
                   "NEAR_RECOGNITION", "EVIDENCE_SKEPTIC", "PILOT_CHARACTER", "HORSE_ANALOGY"}
             for m in RHETORIC["montaigne"].values()))
    a.feed("player_respawned")
    case("respawn: relief eats fear", a.levels["elah"]["fear"] < 0.5 and a.levels["elah"]["relief"] > 0.5)
    clock[0] += 600
    a.feed("incapacitated")
    case("a second death inside 30 min irritates Elah", a.levels["elah"]["irritation"] > 0)
    clock[0] += 3600
    case("an hour later the scare has faded below the floor", a.levels["elah"]["fear"] < 0.05 or
         a.dominant("elah")[0] != "fear")
    b = CompanionAffect(now=lambda: clock[0])
    b.feed("boarded_ship")
    case("boarding warms MONTAIGNE, not Elah", b.dominant("montaigne")[0] == "warmth" and
         b.levels["elah"]["warmth"] == 0)
    c = CompanionAffect(now=lambda: clock[0])
    c.feed("location_change", {"location_name": "Orison"})
    first = c.levels["montaigne"]["curiosity"]
    c.levels["montaigne"]["curiosity"] = 0
    c.feed("location_change", {"location_name": "Orison"})
    case("a first visit is three times as interesting as a return", abs(first - 3 * c.levels["montaigne"]["curiosity"]) < 1e-6)
    d = CompanionAffect(now=lambda: clock[0])
    for _ in range(20):
        clock[0] += 60
        d.drift(1.0, looping=True)
    case("twenty minutes of hauling makes them bored", d.dominant("elah")[0] == "boredom")
    d.feed("contract_complete")
    case("something happening takes the edge off boredom", d.levels["elah"]["boredom"] < 0.3)
    small, big = CompanionAffect(now=lambda: clock[0]), CompanionAffect(now=lambda: clock[0])
    small.feed("reward_earned", {"amount": 1500})
    big.feed("reward_earned", {"amount": 125000})
    case("a big payout pleases more than a small one", big.levels["montaigne"]["joy"] > small.levels["montaigne"]["joy"])
    case("unknown events move nothing", not CompanionAffect(now=lambda: clock[0]).feed("chat_line"))
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "affect.json"
        e = CompanionAffect(path=p, now=lambda: clock[0])
        e.feed("incapacitated")
        saved = e.levels["elah"]["fear"]
        clock[0] += 300                                  # one fear half-life while the app was closed
        f = CompanionAffect(path=p, now=lambda: clock[0])
        case("mood persists AND is aged by the time the app was closed",
             abs(f.levels["elah"]["fear"] - saved / 2) < 1e-6)
        p.write_text("{not json", encoding="utf-8")
        case("a corrupt mood file starts calm, never crashes", CompanionAffect(path=p, now=lambda: clock[0])
             .dominant("elah") == (None, 0.0))
    t1 = CompanionAffect(now=lambda: clock[0], stance_text=False); t1.feed("injury", {"tier": 1})
    t3 = CompanionAffect(now=lambda: clock[0], stance_text=False); t3.feed("injury", {"tier": 3})
    case("a Tier 1 injury (SC's worst) frightens more than a Tier 3 (mildest)",
         t1.levels["elah"]["fear"] > t3.levels["elah"]["fear"] > 0)
    case("every hook names only known speakers and emotions",
         all(s in SPEAKERS and all(e in EMOTIONS for e, _ in mv) for h in HOOKS.values() for s, mv in h.items()))
    case("every emotion has a stance for both speakers", all(set(STANCE[s]) == set(EMOTIONS) for s in SPEAKERS))
    print("emotion selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    sys.exit(_selftest() if "--selftest" in sys.argv else 0)
