"""picture_pace.py - how often the eyes take a PICTURE, by what the pilot is doing (J 2026-10-05).

J: "The eyes depending on the activity type should pull a minimum and maximum times per duration. Once every 5
minutes for salvage ... For mining 5 by default or trigger when a reliable capture has been fed by the mining reader
with a cooldown period so it's not spamming the player or GPU 85 times in 3 minutes. For combat missions 2 minutes by
default. Sandbox activities also 5 minutes by default. These sliders should be user selected from 5 seconds to 120
[minutes] maximum with a never checkbox next to each category."

A PICTURE is a frame handed to the vision model (eyes.py: a glance or a look). The cheap frame compare that runs every
few seconds is not one and is not paced here.

Three things live here, all plain code with an injected clock:

  the settings   one interval and one "never" flag per activity, clamped to 5 s .. 120 min when the file is loaded
  ActivityTracker  which of the four activities the pilot is in, from what the Suit can actually tell (see below)
  PicturePace    is a picture due for this activity, and may a mining capture ask for one early

WHAT CAN BE TOLD APART TODAY, and from what:

  combat_mission  an open contract whose title contract_history.classify() calls "bounty" or "combat" (the log's
                  "Contract Accepted" notice; closed by Contract Complete / Failed or the log's <EndMission> line)
  salvage         an open contract whose title classifies as "salvage" ("... Salvage Rights", "... clean up")
  mining          an open contract whose title classifies as "mining" ("Purchase Order: Ship Mined Ore"), OR the eyes'
                  current scene is "mining" (their own label for the mining HUD)
  sandbox         everything else

NOT detectable, and so counted as sandbox rather than guessed: salvaging or mining with no contract open and no
mining scene on screen (the log has no line for a scraper or a mining laser firing, and the eyes have no "salvage"
scene); a fight that is not a contract (CombatWatch knows a fight is HOT, which holds pictures altogether, but a hot
fight is not a "combat mission"). The ship the pilot is in (a Vulture, a Prospector) would be a hint, not a detection,
and is not used.

With more than one such contract open, the one accepted LAST decides. A mining scene on screen decides over any
contract: it is what the pilot is doing this minute.

THE MINING READER. It lives in another tool (tools/Mining_Signals) and SuitMk2 has no channel from it: narrator_seam.py
forbids importing another tool's tracking. PicturePace.mining_capture() is the Suit's side of that trigger, with its
cooldown. Nothing calls it yet.

    python picture_pace.py --selftest
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Callable, Optional

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

ACTIVITIES = ("salvage", "mining", "combat_mission", "sandbox")
LABELS = {"salvage": "Salvage", "mining": "Mining", "combat_mission": "Combat missions", "sandbox": "Everything else"}
DEFAULT_EVERY_S = {"salvage": 300.0, "mining": 300.0, "combat_mission": 120.0, "sandbox": 300.0}   # J's defaults
MIN_EVERY_S, MAX_EVERY_S = 5.0, 120.0 * 60.0      # J: "from 5 seconds to 120", then "Maximum 120 minutes"
# The slider's stops. 5 s to 120 min on one even scale would give the first minute a hundredth of the track, so the
# slider moves along this table instead: fine at the low end, coarse at the top, with 2 and 5 minutes exact.
STOPS = (5, 10, 15, 20, 30, 45, 60, 90, 120, 180, 240, 300, 420, 600, 900, 1200, 1800, 2700, 3600, 5400, 7200)
# A capture from the mining reader may ask for a picture early, at most this often. J's bound is "not 85 times in 3
# minutes"; 90 s is at most two in three minutes, and a rock takes about that long to break. A settings key.
MINING_CAPTURE_COOLDOWN_S = 90.0
MINING_COOLDOWN_KEY = "eyes_mining_capture_cooldown_s"
# contract_history's contract type -> the activity it counts as. Types not listed (cargo, delivery, rescue,
# investigation, other) are sandbox.
CONTRACT_ACTIVITY = {"salvage": "salvage", "mining": "mining", "bounty": "combat_mission", "combat": "combat_mission"}


def every_key(activity: str) -> str:
    return f"eyes_{activity}_every_s"


def never_key(activity: str) -> str:
    return f"eyes_{activity}_never"


def defaults() -> dict:
    """The settings keys this module owns, with J's defaults. settings.DEFAULTS takes them from here."""
    out: dict = {}
    for a in ACTIVITIES:
        out[every_key(a)] = DEFAULT_EVERY_S[a]
        out[never_key(a)] = False
    out[MINING_COOLDOWN_KEY] = MINING_CAPTURE_COOLDOWN_S
    return out


def clamp_every(value, activity: str) -> float:
    """A saved interval as seconds inside 5 s .. 120 min. Anything that is not a number is the activity's default."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return DEFAULT_EVERY_S[activity]
    if v != v:                                   # NaN
        return DEFAULT_EVERY_S[activity]
    return max(MIN_EVERY_S, min(MAX_EVERY_S, v))


def clean(s: dict) -> None:
    """Put a loaded settings dict's picture keys in range, in place. A file from before these keys existed has none
    of them; settings.load() has already filled in the defaults by the time this runs."""
    for a in ACTIVITIES:
        s[every_key(a)] = clamp_every(s.get(every_key(a)), a)
        s[never_key(a)] = s.get(never_key(a)) is True
    try:
        cd = float(s.get(MINING_COOLDOWN_KEY))
    except (TypeError, ValueError):
        cd = MINING_CAPTURE_COOLDOWN_S
    s[MINING_COOLDOWN_KEY] = max(MIN_EVERY_S, cd) if cd == cd else MINING_CAPTURE_COOLDOWN_S


def stop_index(seconds: float) -> int:
    """The slider stop nearest a saved interval (a hand-edited 7 s shows as 5 s; the file is not rewritten)."""
    return min(range(len(STOPS)), key=lambda i: abs(STOPS[i] - float(seconds)))


def label(seconds: float) -> str:
    """'5 s', '90 s', '2 min', '1 h 30 min': what the slider's value label says."""
    s = int(round(float(seconds)))
    if s < 120:
        return f"{s} s"
    if s < 3600:
        m, r = divmod(s, 60)
        return f"{m} min" + (f" {r} s" if r else "")
    h, r = divmod(s, 3600)
    return f"{h} h" + (f" {r // 60} min" if r >= 60 else "")


class ActivityTracker:
    """Which activity the pilot is in. Fed the classified log events and the raw log lines; asked with the eyes'
    current scene. See the module docstring for what it can and cannot tell."""

    def __init__(self, classify: Optional[Callable[[str], str]] = None):
        if classify is None:
            from contract_history import classify as _classify
            classify = _classify
        self._classify = classify
        self._open: list = []                    # (mission id or title, title, activity), oldest first

    def _close(self, mission_id: Optional[str], title: str = "") -> None:
        if mission_id and any(k == mission_id for k, _, _ in self._open):
            self._open = [o for o in self._open if o[0] != mission_id]
            return
        for i, (_, t, _) in enumerate(self._open):
            if title and t == title:
                del self._open[i]
                return

    def note_event(self, event_type: str, data: Optional[dict] = None) -> None:
        d = data or {}
        if event_type in ("session_start", "join_pu"):
            self._open = []                      # a new session or server: contracts of the last one are not open
        elif event_type == "contract_accepted":
            title = str(d.get("mission_name") or "")
            key = d.get("mission_id") or title
            self._close(d.get("mission_id"), title)
            kind = CONTRACT_ACTIVITY.get(self._classify(title))
            if kind and key:
                self._open.append((key, title, kind))
        elif event_type in ("contract_complete", "contract_failed"):
            self._close(d.get("mission_id"), str(d.get("mission_name") or ""))

    def on_line(self, line: str) -> None:
        """The log's own <EndMission> line: the contract is over however it ended (abandoned, the pilot left)."""
        if "<EndMission> Ending mission" not in line:
            return
        from contract_history import _END
        m = _END.search(line)
        if m:
            self._close(m.group(1))

    def activity(self, scene: Optional[str] = None) -> tuple:
        """(activity, why). Never guesses: with nothing to go on it is ("sandbox", ...)."""
        if scene == "mining":
            return "mining", "the eyes see the mining display"
        if self._open:
            _, title, kind = self._open[-1]
            return kind, f"open contract: {title[:60]}"
        return "sandbox", "no salvage, mining or combat contract open"


class PicturePace:
    """The per-activity picture interval, and the mining reader's early trigger. Holds no thread and takes no
    picture: the core asks it, and the eyes keep every guard of their own (game in front, headroom, hourly cap)."""

    def __init__(self, s: Optional[dict] = None, now: Callable[[], float] = time.time):
        self._now = now
        self._every: dict = {}
        self.mining_cooldown_s = MINING_CAPTURE_COOLDOWN_S
        self._last_capture: Optional[float] = None
        self.captures_taken = self.captures_held = 0
        self.configure(s or {})

    def configure(self, s: dict) -> None:
        """Read the picture keys of a settings dict. Missing keys are the defaults; values are clamped again here, so
        a dict that never went through settings.load() cannot put a 1 s interval in."""
        s = {**defaults(), **{k: v for k, v in (s or {}).items() if k in defaults()}}
        clean(s)
        self._every = {a: (None if s[never_key(a)] else s[every_key(a)]) for a in ACTIVITIES}
        self.mining_cooldown_s = s[MINING_COOLDOWN_KEY]

    def every(self, activity: str) -> Optional[float]:
        """Seconds between pictures for this activity. None = never."""
        return self._every.get(activity if activity in ACTIVITIES else "sandbox")

    def due(self, activity: str, picture_age_s: Optional[float]) -> bool:
        """True when this activity's interval has passed since the last picture (None = no picture yet)."""
        every = self.every(activity)
        if every is None:
            return False
        return picture_age_s is None or picture_age_s >= every

    def mining_capture(self, reliable: bool = True, picture_age_s: Optional[float] = None) -> bool:
        """The mining reader handed over a capture. True = take a picture now, ahead of the interval.
        False when the capture is not a reliable one, mining pictures are set to never, the last early picture was
        less than the cooldown ago, or ANY picture was taken less than the cooldown ago."""
        if not reliable or self.every("mining") is None:
            return False
        t = self._now()
        recent = self._last_capture is not None and t - self._last_capture < self.mining_cooldown_s
        if recent or (picture_age_s is not None and picture_age_s < self.mining_cooldown_s):
            self.captures_held += 1
            return False
        self._last_capture = t
        self.captures_taken += 1
        return True


def _selftest() -> int:
    ok = True

    def case(name, cond):
        nonlocal ok
        ok &= bool(cond)
        print(("  PASS  " if cond else "  FAIL  ") + name)
    case("the defaults are J's: 5, 5, 2 and 5 minutes", [DEFAULT_EVERY_S[a] for a in ACTIVITIES] == [300, 300, 120, 300])
    case("2 and 5 minutes are exact slider stops", 120 in STOPS and 300 in STOPS and STOPS[0] == 5 and STOPS[-1] == 7200)
    case("the stops only go up", list(STOPS) == sorted(set(STOPS)))
    s = {every_key("salvage"): 1, every_key("mining"): 99999, every_key("sandbox"): "soon"}
    s = {**defaults(), **s}
    clean(s)
    case("out of range is clamped, nonsense is the default",
         (s[every_key("salvage")], s[every_key("mining")], s[every_key("sandbox")]) == (5.0, 7200.0, 300.0))
    clock = [0.0]
    p = PicturePace({never_key("salvage"): True}, now=lambda: clock[0])
    case("never means never due", not p.due("salvage", None) and p.due("sandbox", None))
    case("due only once the interval has passed", not p.due("combat_mission", 119) and p.due("combat_mission", 120))
    got = []
    for _ in range(85):
        got.append(p.mining_capture())
        clock[0] += 180.0 / 85
    case("85 captures in 3 minutes ask for 2 pictures, not 85", sum(got) == 2)
    tr = ActivityTracker()
    case("nothing known is sandbox", tr.activity()[0] == "sandbox")
    tr.note_event("contract_accepted", {"mission_id": "a", "mission_name": "Verified Bounty: Someone"})
    case("a bounty contract is a combat mission", tr.activity()[0] == "combat_mission")
    case("the mining display on screen is mining", tr.activity("mining")[0] == "mining")
    tr.note_event("contract_complete", {"mission_id": "a", "mission_name": "Verified Bounty: Someone"})
    case("the contract done, it is sandbox again", tr.activity()[0] == "sandbox")
    case("labels read as time", [label(x) for x in (5, 90, 120, 300, 5400, 7200)]
         == ["5 s", "90 s", "2 min", "5 min", "1 h 30 min", "2 h"])
    print("picture_pace selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(_selftest() if "--selftest" in sys.argv else 0)
