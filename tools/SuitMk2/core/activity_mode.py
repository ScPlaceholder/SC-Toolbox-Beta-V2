"""activity_mode.py - when to be PRESENT and when to be a PRESENCE (J 2026-09-24).

J: "on certain hooks as well as curiosity it should use the eyes and see what is going on and comment on it ...
During stuff like moving boxes, hauling, salvaging and other slow gameplay not much happens so that's where groups
do a lot more small talk and banter and not comment on every box they move. So the system needs to be intelligent
and deterministic on when to be present and when to be a presence."
And: "the logs are so scarce ... going down an elevator and going indoors [are not logged] so the eyes at times will
need to be doing the heavy lifting."

DETERMINISTIC by design: a novelty score, not a model. Things that are NEW add points; the score decays (half-life
NOVELTY_HALF_LIFE_S); slow-loop evidence (a mining/trading scene, a long stay, many near-identical events) pulls it
down. Hysteresis keeps it from flapping: enter PRESENT at >= ENTER_PRESENT, fall back to PRESENCE only below
EXIT_PRESENT. Every change of mode carries the reasons that caused it, so it can be read and tuned.

  PRESENT   eyes-driven: curiosity looks on hooks, comment on the environment and what just happened.
  PRESENCE  company: routine event lines are throttled (not every box), banter and idle topics carry the talk.

The eyes feed it as much as the log does: a visual scene TRANSITION (elevator, doorway, indoors) is novelty the log
never records.
    python activity_mode.py --selftest
"""
from __future__ import annotations

import math
import re
import time
from collections import deque
from typing import Optional

PRESENT, PRESENCE = "present", "presence"
NOVELTY_HALF_LIFE_S = 240.0
ENTER_PRESENT = 3.0
EXIT_PRESENT = 1.0

# Points per event type. Absent = 0 (routine). A FIRST occurrence of a location adds FIRST_VISIT_BONUS.
EVENT_POINTS = {
    "location_change": 3.0, "qt_arrived": 2.0, "jurisdiction_change": 1.0, "channel_change": 1.5,
    "contract_accepted": 2.0, "objective_new": 1.5, "contract_complete": 2.0,
    "combat_on": 3.0, "incapacitated": 3.0, "player_respawned": 2.0, "injury": 1.5,
    "entered_monitored_space": 0.5, "exited_monitored_space": 0.5, "docking_detached": 1.0,
    # the eyes: a visual scene transition the log never records, and a change of recognised scene
    "scene_transition": 1.5, "scene_change": 1.0,
}
FIRST_VISIT_BONUS = 2.0
# Scenes that ARE the slow loop. While the eyes see one, novelty drains faster (the loop is not news).
LOOP_SCENES = ("mining", "trading")
LOOP_DRAIN_PER_MIN = 0.5
# Many events of the SAME type in a short window is a loop (cargo lines, repeated rewards), not novelty.
REPEAT_WINDOW_S = 300.0
REPEAT_DISCOUNT_AFTER = 2


class ActivityMode:
    def __init__(self, now=time.time):
        self._now = now
        self._score, self._t = 0.0, now()
        self.mode = PRESENCE
        self.reasons: deque = deque(maxlen=6)      # recent (points, why) that moved the score
        self.changed_at = self._t
        self.change_reason = "start"
        self._recent: deque = deque()              # (t, event_type) for repeat discounting
        self._seen_places: set = set()

    def _decay(self, t: float) -> None:
        dt = max(0.0, t - self._t)
        if dt:
            self._score *= math.pow(0.5, dt / NOVELTY_HALF_LIFE_S)
            self._t = t

    def _update_mode(self, t: float, why: str) -> None:
        if self.mode == PRESENCE and self._score >= ENTER_PRESENT:
            self.mode, self.changed_at, self.change_reason = PRESENT, t, why
        elif self.mode == PRESENT and self._score < EXIT_PRESENT:
            self.mode, self.changed_at, self.change_reason = PRESENCE, t, why

    def feed(self, event_type: str, data: Optional[dict] = None, t: Optional[float] = None) -> float:
        """One event (from the log, or from the eyes as 'scene_transition'/'scene_change'). -> points added."""
        t = self._now() if t is None else t
        self._decay(t)
        pts = EVENT_POINTS.get(event_type, 0.0)
        while self._recent and t - self._recent[0][0] > REPEAT_WINDOW_S:
            self._recent.popleft()
        same = sum(1 for _, e in self._recent if e == event_type)
        self._recent.append((t, event_type))
        if pts and same >= REPEAT_DISCOUNT_AFTER:
            pts /= (same - REPEAT_DISCOUNT_AFTER + 2)      # the 3rd, 4th... of a kind count for less and less
        place = (data or {}).get("location_name") or (data or {}).get("location")
        if event_type == "location_change" and place and place not in self._seen_places:
            self._seen_places.add(place)
            pts += FIRST_VISIT_BONUS
        if pts:
            self._score += pts
            self.reasons.append((round(pts, 2), event_type))
        self._update_mode(t, event_type)
        return pts

    def tick(self, scene: Optional[str] = None, t: Optional[float] = None) -> str:
        """Called on the companion's ambient tick with the eyes' current scene. Applies decay and loop drain."""
        t = self._now() if t is None else t
        prev_t = self._t
        self._decay(t)
        if scene in LOOP_SCENES:
            self._score = max(0.0, self._score - LOOP_DRAIN_PER_MIN * (t - prev_t) / 60.0)
        self._update_mode(t, "decay" if scene not in LOOP_SCENES else f"loop scene: {scene}")
        return self.mode

    def score(self, t: Optional[float] = None) -> float:
        t = self._now() if t is None else t
        return self._score * math.pow(0.5, max(0.0, t - self._t) / NOVELTY_HALF_LIFE_S)

    def state(self) -> dict:
        return {"mode": self.mode, "score": round(self.score(), 2), "since": self.changed_at,
                "because": self.change_reason, "recent": list(self.reasons)}


# ---- AFK: nobody at the keyboard, so nobody to talk to ----------------------------------------------------------------
# PRESENT/PRESENCE says HOW to keep the pilot company; AFK says whether there is anyone to keep company at all. It is a
# third, orthogonal state, so it lives here as its own small watch instead of a third ActivityMode value (a novelty score
# has no business knowing about the keyboard).
#
# READ-ONLY, on purpose (narrator_seam): the OS is asked when the last key or mouse input happened (user32
# GetLastInputInfo). Nothing is ever sent. On anything that is not Windows, or if the call fails, the answer is None =
# "cannot tell", and "cannot tell" is NEVER treated as AFK: a companion that falls silent on a guess is worse than one
# that talks to an empty chair.
#
# Known blind spot: GetLastInputInfo counts keyboard and mouse, NOT joysticks/HOTAS/gamepads. A HOTAS-only pilot would
# read idle, so the core also pokes this watch on pilot-driven game signals (arriving somewhere, combat, speaking to
# them) and those count as activity for the same window.
DEFAULT_AFK_MINUTES = 5.0


def os_idle_seconds() -> Optional[float]:
    """Seconds since the last keyboard/mouse input anywhere on the desktop, or None when it cannot be read."""
    import sys
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class _LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]
        info = _LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(_LASTINPUTINFO)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        # both are 32-bit millisecond tick counts; the mask keeps the difference right across the 49.7-day wrap
        ticks = ctypes.windll.kernel32.GetTickCount() & 0xFFFFFFFF
        return ((ticks - info.dwTime) & 0xFFFFFFFF) / 1000.0
    except Exception:
        return None


class AfkWatch:
    """afk() is True only when the idle source POSITIVELY says no input for afk_after_s AND no pilot-driven game
    signal (poke) landed in that window either. idle_source=None, a None reading, or afk_after_s <= 0 -> never AFK."""

    def __init__(self, idle_source: Optional[callable] = None, afk_after_s: float = DEFAULT_AFK_MINUTES * 60.0,
                 now=time.time):
        self.idle_source, self.afk_after_s, self._now = idle_source, float(afk_after_s or 0.0), now
        self._poked = -1e18
        self.is_afk = False                        # last answer, for the status window
        self.changed_at = now()

    def poke(self, t: Optional[float] = None) -> None:
        """A pilot-driven signal the OS idle clock cannot see (HOTAS flying, a voice question, a new place)."""
        self._poked = self._now() if t is None else t

    def afk(self) -> bool:
        t = self._now()
        state = False
        if self.idle_source is not None and self.afk_after_s > 0 and t - self._poked >= self.afk_after_s:
            try:
                idle = self.idle_source()
            except Exception:
                idle = None
            state = idle is not None and idle >= self.afk_after_s
        if state != self.is_afk:
            self.is_afk, self.changed_at = state, t
        return state


# ---- the curiosity look -> one line --------------------------------------------------------------------------------
# J's model, the Onyx night: "Hang on, I thought we were here to collect a dossier, this looks like the set of an
# Alien movie." ... "Is that the worm? Oh wait, that's a dead guy." A REACTION to what is on screen, allowed to be
# unsure. The eyes' description is the only fact; the stance invites a reaction, never a report.
LOOK_VOICES = [
    ("elah", "DEADPAN", "she looks around and reacts to what she sees, dry and curious; if something is unclear she "
                        "wonders out loud what it is; no names of places or people"),
    ("montaigne", "ESSAY_DIGRESSION", "he reacts to the place as if it were a scene from something he once read or "
                                      "watched, not quite sure what he is looking at; no names of places or people"),
    ("elah", "CORRECTION", "one line: what the place actually looks like, and whether it matches what they expected"),
    ("montaigne", "NEAR_RECOGNITION", "he half-recognises something on screen and is not sure he is right"),
]


# NEVER COMMENT ON AN ABSENCE (J 2026-10-05). "If nothing is being salvaged do not comment on it. Depending on the
# salvage approach 3 ships could've been munched since the last picture and being like 'yeah slim pickings today' just
# breaks the immersion. If something does happen or there's a ship to salvage the engine should choose to comment or
# not." A picture is one moment; what it does not show may have come and gone between two pictures. So a picture is
# only ever a reason to speak about something that IS in it.
#
# The rule is positive, with the refusal on top: a description may become a line only if it NAMES something (at least
# one word that is not filler), and it is refused whole if any part of it reports an absence. Whole, not trimmed: "an
# empty hangar, no ships" with the absence cut out would hand the model half a sentence it did not get from the eyes.
# Being refused costs nothing: silence is always allowed.
# Not on the list, on purpose: "abandoned", "dark", "still", "calm", "few", "lone". Each can describe a thing that is
# there ("an abandoned outpost", "a few ships docked").
ABSENCE = re.compile(
    r"\b(?:no|not|none|nothing|nobody|noone|never|without|nil|n/a|empty|emptiness|quiet|silent|silence|deserted|"
    r"vacant|barren|bare|blank|void|lifeless|devoid|absent|absence|missing|lack|lacks|lacking|uneventful|"
    r"unremarkable|nondescript|slim|sparse|cannot|cant|isnt|arent|dont|doesnt|wont|\w+n't)\b")
# Words that name nothing by themselves: a description made only of these has not said what is there.
LOOK_FILLER = frozenset("""the and but for with from into onto over under near around about above below behind
some something anything everything thing things stuff maybe perhaps possibly probably just only very quite rather
here there this that these those its their are was were has have had being been seems seem appears appear looks
look looking like kind sort scene view screen image frame picture shot game notable particular special usual normal
ordinary typical standard same usual visible seen see sees shows show what which where when more much many any all
""".split())


def names_something_there(notable: str) -> bool:
    """True when the eyes' description names a thing that is in the picture and reports no absence. See ABSENCE."""
    low = " ".join(str(notable or "").replace("\u2019", "'").lower().split())
    if not low or ABSENCE.search(low):
        return False
    return any(len(w) >= 3 and w not in LOOK_FILLER for w in re.findall(r"[a-z]+", low))


def build_look_spec(notable: str, reason: str, variant: int) -> Optional[dict]:
    """A spec from one curiosity look. notable = the eyes' short description (may carry uncertainty).
    None when the description names nothing that is there (names_something_there): no spec, so no line."""
    notable = (notable or "").strip()
    if not names_something_there(notable):
        return None
    speaker, move, stance = LOOK_VOICES[variant % len(LOOK_VOICES)]
    return {
        "scenario": "scene_look", "speaker": speaker, "rhetoric": [move],
        "claims": [{"id": "C1", "kind": "OBSERVED", "predicate": "scene.looks_like", "value": notable}],
        "interpretation": {"owner": speaker, "text": stance},
        "required_claims": [], "required_values": [], "length_words": [4, 18],
        # No name gate here, on purpose: "the set of an Alien movie" is exactly the reaction J wants, and a name
        # gate would refuse it. The stance forbids naming places or people instead.
        "allowed_names": None,
        "look_reason": reason,
        "id": f"look_{reason}_{variant}",
    }


def _selftest() -> int:
    ok = True

    def case(name, cond):
        nonlocal ok
        ok &= bool(cond)
        print(("  PASS  " if cond else "  FAIL  ") + name)
    clock = [1000.0]
    m = ActivityMode(now=lambda: clock[0])
    case("starts as PRESENCE", m.mode == PRESENCE)
    m.feed("location_change", {"location_name": "Onyx Facility"})
    case("a NEW place makes it PRESENT", m.mode == PRESENT and m.change_reason == "location_change")
    clock[0] += 60
    m.feed("scene_transition")
    case("a visual transition (no log line) adds novelty", m.reasons[-1] == (1.5, "scene_transition"))
    clock[0] += 60 * 20
    m.tick("on_foot")
    case("20 quiet minutes decays back to PRESENCE", m.mode == PRESENCE)
    m2 = ActivityMode(now=lambda: clock[0])
    m2.feed("location_change", {"location_name": "Area18"})
    clock[0] += 30
    m2.feed("location_change", {"location_name": "Area18"})
    case("a second visit earns no first-visit bonus", m2.reasons[-1] == (3.0, "location_change"))
    m3 = ActivityMode(now=lambda: clock[0])
    pts = [m3.feed("reward_earned") for _ in range(5)]
    case("routine events (a box, a reward) add nothing", sum(pts) == 0 and m3.mode == PRESENCE)
    m4 = ActivityMode(now=lambda: clock[0])
    got = [m4.feed("objective_new") for _ in range(5)]
    case("repeats of one kind count for less and less", got[0] > got[3] > 0)
    m5 = ActivityMode(now=lambda: clock[0])
    m5.feed("combat_on")
    case("combat is PRESENT", m5.mode == PRESENT)
    for _ in range(6):
        clock[0] += 60
        m5.tick("mining")
    case("six minutes in a mining scene drains to PRESENCE faster than decay alone",
         m5.mode == PRESENCE and m5.change_reason.startswith("loop scene"))
    m6 = ActivityMode(now=lambda: clock[0])
    m6.feed("contract_accepted")
    case("hysteresis: 2.0 is not enough to enter PRESENT", m6.mode == PRESENCE)
    case("state() says why", set(m5.state()) >= {"mode", "score", "because", "recent"})
    # AFK watch: an injectable idle source, never AFK on "cannot tell".
    idle = [0.0]
    w = AfkWatch(idle_source=lambda: idle[0], afk_after_s=300, now=lambda: clock[0])
    case("afk: fresh input is not AFK", not w.afk())
    idle[0] = 301
    case("afk: 5 idle minutes is AFK", w.afk() and w.is_afk)
    idle[0] = 2
    case("afk: input again resumes", not w.afk())
    idle[0] = 900
    w.poke()
    case("afk: a pilot-driven game signal (HOTAS, voice) counts as activity", not w.afk())
    clock[0] += 301
    case("afk: ... for the same window only", w.afk())
    case("afk: no idle source -> never AFK (the dry run)", not AfkWatch(None, now=lambda: clock[0]).afk())
    case("afk: an unreadable idle source -> never AFK", not AfkWatch(lambda: None, now=lambda: clock[0]).afk())
    for nothing in ("", "nothing", "Nothing notable.", "no ships", "quiet", "An empty hangar.", "slim pickings",
                    "Empty space, no ships in sight", "just the usual", "there isn't much here"):
        case(f"an absence makes no spec: {nothing!r}", build_look_spec(nothing, "interval", 0) is None)
    spec = build_look_spec("a wrecked hull drifting, or maybe a station", "interval", 0)
    case("a word that only ends like \"isn't\" is not an absence",
         build_look_spec("a giant plant in front of a distant vent", "interval", 0) is not None)
    case("a thing that is there makes a spec, word for word",
         spec is not None and spec["claims"][0]["value"] == "a wrecked hull drifting, or maybe a station")
    case("afk: a raising idle source -> never AFK",
         not AfkWatch(lambda: 1 / 0, afk_after_s=1, now=lambda: clock[0]).afk())
    case("afk: afk_after_s 0 disables it", not AfkWatch(lambda: 1e9, afk_after_s=0, now=lambda: clock[0]).afk())
    real = os_idle_seconds()
    case(f"afk: the OS idle reading is a number or None ({real!r})", real is None or real >= 0)
    print("activity_mode selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    sys.exit(_selftest() if "--selftest" in sys.argv else 0)
