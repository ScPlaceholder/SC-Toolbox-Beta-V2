"""pico/sprites.py — WHOLE-BODY SPRITE LOOPS, chosen by mood. The Drake Pico, wired in.

J, 2026-10-01: after the bone-driven tweens kept glitching, J picked rendered key-frame loops
("Left is better") and asked to wire the Drake Pico into the Pico Pals tool. 89 Drake loops
exist (BrAi/_forJ/VNCCS/pico_anim_sequences/<anim>_<expr>.gif), each with its expression baked in.

This is a SECOND renderer path beside the bone rig, not a replacement for it. The rig, face.py
and the skin format are untouched. Both consume the same MoodReading from events.MoodSource, so
the state machine (layer C) stays the single source of what Pico is feeling.

Pure logic, no Qt, headless-testable, same rule as layer A. The window lives in sprite_pal.py.

WHAT IT REFUSES, CARRIED OVER FROM face.py:
  1. UNKNOWN never falls back to a happy or calm loop. A mascot that idles contentedly while the
     feed is dead is a dashboard that lies. UNKNOWN gets its own pool.
     ⚠ MY PICK, OPEN FOR J: the art has no blank-faced loop, so UNKNOWN plays `confused` —
       "I can't tell" is the honest claim. Overrule in MOOD_LOOPS["unknown"].
  2. A mood whose whole pool is missing from the catalog is an error at load time, not a silent
     substitute at play time.
"""
from __future__ import annotations

import os
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional

UNKNOWN = "unknown"

# mood (face.DEFAULT_MOODS names) -> loop names (<anim>_<expr>, no extension).
# Pools are small on purpose, per face.py: "a few readable stages, not a tour through the library".
MOOD_LOOPS: Mapping[str, tuple[str, ...]] = {
    # J 2026-10-01 16:15 "Sure go ahead": drift, look_up, peek and scratch added for variety.
    "calm": ("idle_look_default", "idle_settle_default", "idle_shuffle_default",
             "idle_stargaze_default", "idle_preen_default", "idle_tap_foot_default",
             "idle_drift_default", "idle_look_up_default", "idle_peek_default",
             "idle_scratch_default"),
    "alert": ("radar_contact_surprised", "determined_focused", "weapon_draw_focused"),
    "hurt": ("sad_sad", "disappointed_sad", "sulk_sad", "cry_sad"),
    "happy": ("happy_happy", "cheer_happy", "giggle_happy", "proud_happy", "idle_dance_happy"),
    "startled": ("startled_surprised", "shocked_surprised", "scared_surprised"),
    "irritated": ("annoyed_angry", "angry_angry", "disgust_angry"),
    UNKNOWN: ("confused_confused",),
}

# Game.log event type (SuitMk2 event_parser) -> a loop played ONCE, then Pico returns to his mood.
# An event is something that HAPPENED; a mood is how he feels about it. The mood engine already turns
# events into feelings, so this layer only adds the gesture. MY PICKS, OPEN FOR J, like MOOD_LOOPS.
EVENT_LOOPS: Mapping[str, str] = {
    "docking_ready": "docking_focused",
    "docking_detached": "undock_default",
    "qt_route_calculated": "nav_plot_focused",
    "qt_target_selected": "quantum_spool_default",
    "qt_arrived": "quantum_drop_default",
    # qt_error is NOT here, on evidence. Measured over J's last 20 logs (2026-08-02..09-26, 161k
    # lines): 352 qt_error against 21 qt_arrived, median gap 17.8s, 112 under 5s apart. The line is
    # "Failed to get starmap route data! ... No Route loaded!" - background noise, not a failed jump.
    # Mapped, Pico would spin dizzily every 18 seconds of every session.
    "hangar_queue": "hangar_wait_default",
    "hangar_ready": "ship_enter_default",
    "injury": "hull_warn_surprised",
    "incapacitated": "crash_X_X",
    "session_crash": "crash_X_X",
    "player_respawned": "relieved_happy",
    "med_bed_heal": "relieved_happy",
    "contract_accepted": "determined_focused",
    "contract_complete": "celebrate_happy",
    "contract_failed": "disappointed_sad",
    "objective_complete": "cheer_happy",
    "reward_earned": "proud_happy",
    "incoming_call": "radar_contact_surprised",
    "exited_monitored_space": "nervous_confused",
    "entered_monitored_space": "relieved_happy",
    "session_start": "idle_stretch_default",
    "session_end": "idle_yawn_sleepy",
}

# What is in the player's right hand -> the loop Pico holds while it is there (J 2026-10-01: "red and
# white alternate between the users 1 & 2 weapons"). Read from Game.log, measured over J's last 20
# sessions: every draw is an <AttachmentReceived> into Port[weapon_attach_hand_right], and the item's
# PREVIOUS port says which slot it came from (wep_stocked_2 = slot 1, LMG, 118 draws; wep_stocked_3 =
# slot 2, sniper, 147; utility_attach_N = multitool, 87; wep_sidearm = J's medgun, 148, and medPen
# ports = med pens -> "medical"). There is no detach line: putting it away is the same item arriving
# back in its port. Grenades and drinks are deliberately unmapped for now.
HAND_LOOPS: Mapping[str, str] = {
    "slot1": "weapon_draw_focused_prop40",     # white pistol
    "slot2": "weapon_draw_focused_prop41",     # red pistol
    "utility": "weapon_draw_focused_prop42",   # the utility gun, for the multitool
    # J 2026-10-01 18:16: "For a medgun or med pen he should pull out a first aid kit." Prop 07, held in
    # front with both flippers (the reload pose). J's sidearm slot holds a medgun, so it maps here too.
    "medical": "weapon_reload_focused_prop07",
    # J 2026-10-01 18:19: "Bomb should make him do the reload animation except holding a big bomb."
    "bomb": "weapon_reload_focused_prop17",
}
# A held key with no holster line in this long is assumed gone. J asked whether the log says a grenade
# was thrown: it does not, directly. Measured over 20 logs: of 30 grenades that reached the hand, 19
# were never mentioned again (thrown) and 11 went back to a grenade_attach port (put away). Without a
# cap a thrown grenade leaves Pico hugging the bomb forever.
HAND_MAX_S: Mapping[str, float] = {"bomb": 8.0}
# J's bomb gag, 2026-10-01 18:29-18:32: hold it; at 4 s his eyes become "!"; at 8 s one of FIVE random
# endings plays ("aim for 5 random bomb animation sequences"). A step is (loop, seconds): 0 = play it
# once, N = keep repeating it for N seconds. Endings whose FIRST loop is missing for an outfit are
# skipped, and missing later steps are dropped, so a half-built outfit still does something sensible.
# The explosions are baked into the *_boom loops by elah-audio/pico_bomb_gag.py.
BOMB_ALERT_S = 4.0
BOMB_ALERT_LOOP = "weapon_reload_exclaim_prop17"
BOMB_ENDINGS = [
    [("celebrate_exclaim_boom", 0), ("idle_settle_spiral", 4.0)],   # 1 panic jump, lands dizzy (J's)
    [("cheer_happy", 0)],                                            # 2 toss it away
    [("crash_X_X_boom", 0), ("idle_settle_spiral", 3.0)],            # 3 goes off in his hands
    [("scared_surprised_boom", 0), ("sulk_sad", 4.0)],               # 4 it blows, he flinches, sulks
    [("confused_confused", 3.0), ("relieved_happy", 0)],             # 5 a dud
]
_ATTACH = re.compile(r"<AttachmentReceived> Player\[[^\]]*\] Attachment\[([^,]+), ([^,]+),.*?Port\[([^\]]+)\]")
HAND_PORT = "weapon_attach_hand_right"


def _slot_of(port: str) -> Optional[str]:
    if port == "wep_stocked_2":
        return "slot1"
    if port == "wep_stocked_3":
        return "slot2"
    if port.startswith("utility_attach"):
        return "utility"
    if port == "wep_sidearm" or port.startswith("medPen_attach"):
        return "medical"
    if port.startswith("grenade_attach"):
        return "bomb"
    return None


class HandTracker:
    """Game.log lines in; what Pico should be holding out. Tracks each item's last port by its id."""

    def __init__(self):
        self.last_port: dict[str, str] = {}
        self.holding: Optional[str] = None     # a HAND_LOOPS key, or None
        self.held_uid: Optional[str] = None

    def feed_line(self, line: str) -> Optional[tuple[str, Optional[str]]]:
        """Returns ("draw", key) / ("holster", None) when the hand changes, else None."""
        m = _ATTACH.search(line)
        if not m:
            return None
        uid, _item, port = m.groups()
        prev = self.last_port.get(uid)
        self.last_port[uid] = port
        if port == HAND_PORT:
            key = _slot_of(prev or "")
            if key is None:                            # something unmapped went into the hand
                if self.holding is not None:
                    self.holding, self.held_uid = None, None
                    return ("holster", None)
                return None
            self.holding, self.held_uid = key, uid
            return ("draw", key)
        if uid == self.held_uid:                       # the held item went back to a holster
            self.holding, self.held_uid = None, None
            return ("holster", None)
        return None


# The same event again within this many seconds plays nothing. A burst (injury ticks, repeated
# monitored-space flips) is one thing that happened, not a reason to restart the gesture.
EVENT_COOLDOWN_S = 45.0

# How long one mood loop keeps repeating before Pico switches to another from the pool, in seconds.
# Measured 2026-10-01: a loop is ~1.4s (10 frames at 7fps), and rotating on every loop end swapped his
# idle every 1.4s with no rest - twitchy. J asked "how much time is there between idle animations".
DWELL_S = (8.0, 14.0)

DEFAULT_DIR = Path(os.path.expanduser("~")) / "BrAi" / "_forJ" / "VNCCS" / "pico_anim_sequences"


class SpriteError(Exception):
    pass


@dataclass
class Catalog:
    """Which loops exist on disk for one outfit."""

    root: Path
    loops: dict[str, Path] = field(default_factory=dict)

    @classmethod
    def scan(cls, root: Path | str = DEFAULT_DIR) -> "Catalog":
        root = Path(root)
        if not root.is_dir():
            raise SpriteError("no loop folder at %s" % root)
        # .webp is the desktop copy: transparent, and every loop on one shared frame so Pico does not
        # jump between loops. The .gif is the flattened review copy, used only where no .webp exists.
        loops = {p.stem: p for p in sorted(root.glob("*.gif"))}
        loops.update({p.stem: p for p in sorted(root.glob("*.webp"))})
        if not loops:
            raise SpriteError("%s holds no .webp or .gif loops" % root)
        return cls(root, loops)

    def check(self, moods: Mapping[str, tuple[str, ...]] = MOOD_LOOPS) -> dict[str, list[str]]:
        """Missing loops per mood. Raises if any mood has NONE — it could never show anything."""
        missing = {m: [n for n in pool if n not in self.loops] for m, pool in moods.items()}
        dead = [m for m, pool in moods.items() if len(missing[m]) == len(pool)]
        if dead:
            raise SpriteError("moods with no loop on disk: %s" % ", ".join(dead))
        return {m: v for m, v in missing.items() if v}


class LoopChooser:
    """Mood in, loop path out. Stays on the current loop until the mood changes or the loop ends."""

    def __init__(self, catalog: Catalog, moods: Mapping[str, tuple[str, ...]] = MOOD_LOOPS,
                 rng: Optional[random.Random] = None):
        catalog.check(moods)
        self.catalog = catalog
        self.pools = {m: tuple(n for n in pool if n in catalog.loops) for m, pool in moods.items()}
        self.rng = rng or random.Random()
        self.mood: Optional[str] = None
        self.current: Optional[str] = None
        self.events = {e: n for e, n in EVENT_LOOPS.items() if n in catalog.loops}
        self.oneshot = False          # an event loop is playing; moods wait until it ends
        self.last_fired: dict[str, float] = {}
        self.until = 0.0               # the current mood loop repeats until this time
        self.hand = {k: n for k, n in HAND_LOOPS.items() if n in catalog.loops}
        self.held: Optional[str] = None  # a loop name while something is in his hand
        self.held_key: Optional[str] = None
        self.held_since = 0.0
        self.endings = [[st for st in e if st[0] in catalog.loops] for e in BOMB_ENDINGS
                        if e[0][0] in catalog.loops]
        self.seq: list = []               # remaining steps of a running bomb ending
        self.in_seq = False
        self.step_until: Optional[float] = None

    def _pick(self, mood: str) -> str:
        pool = self.pools[mood]
        if len(pool) > 1 and self.current in pool:
            pool = tuple(n for n in pool if n != self.current)   # never the same loop twice in a row
        return self.rng.choice(pool)

    def on_mood(self, mood: Optional[str]) -> Optional[Path]:
        """Call on every reading. Returns a new loop path when the loop should change, else None."""
        key = mood if mood is not None else UNKNOWN
        if key not in self.pools:
            raise SpriteError("mood %r has no loop pool (known: %s)" % (key, ", ".join(self.pools)))
        if self.oneshot or self.held or self.in_seq:
            self.mood = key           # remembered; shown when the event loop / held item / ending ends
            return None
        if key == self.mood and self.current is not None:
            return None
        self.mood = key
        self.current = self._pick(key)
        self.until = time.time() + self.rng.uniform(*DWELL_S)
        return self.catalog.loops[self.current]

    def on_event(self, event_type: str, at: Optional[float] = None) -> Optional[Path]:
        """A Game.log event happened. Returns its one-shot loop, or None (no gesture, or cooling down)."""
        name = self.events.get(event_type)
        if name is None or self.in_seq:   # a bomb ending is never cut off by a game event
            return None
        at = time.time() if at is None else at
        if at - self.last_fired.get(event_type, float("-inf")) < EVENT_COOLDOWN_S:
            return None
        self.last_fired[event_type] = at
        self.oneshot = True
        self.current = name
        return self.catalog.loops[name]

    def on_hand(self, change: Optional[tuple[str, Optional[str]]]) -> Optional[Path]:
        """A HandTracker change. Draw -> hold that loop until holstered; holster -> back to the mood."""
        if change is None:
            return None
        kind, key = change
        if kind == "draw" and key in self.hand:
            self.held_key, self.held_since = key, time.time()
            self.held = self.current = self.hand[key]
            self.oneshot = False
            return self.catalog.loops[self.held]
        if kind == "holster" and self.held:
            self.held = None
            self.current = self._pick(self.mood or UNKNOWN)
            self.until = time.time() + self.rng.uniform(*DWELL_S)
            return self.catalog.loops[self.current]
        return None

    def expire(self, at: Optional[float] = None) -> Optional[Path]:
        """Time-driven hand changes (a thrown grenade never logs a holster): the bomb's "!" at 4 s,
        and at its cap a random ending. Call every tick."""
        at = time.time() if at is None else at
        if not self.held:
            return None
        age = at - self.held_since
        if (self.held_key == "bomb" and BOMB_ALERT_S <= age < HAND_MAX_S["bomb"]
                and self.held != BOMB_ALERT_LOOP and BOMB_ALERT_LOOP in self.catalog.loops):
            self.held = self.current = BOMB_ALERT_LOOP
            return self.catalog.loops[self.held]
        cap = HAND_MAX_S.get(self.held_key or "")
        if cap is not None and age >= cap:
            if self.held_key == "bomb" and self.endings:
                self.held = None
                self.seq = list(self.rng.choice(self.endings))
                self.in_seq = True
                return self._next_step(at)
            return self.on_hand(("holster", None))
        return None

    def _next_step(self, at: float) -> Optional[Path]:
        if not self.seq:
            self.in_seq = False
            return None
        name, secs = self.seq.pop(0)
        self.current = name
        self.step_until = at + secs if secs else None
        return self.catalog.loops[name]

    def on_loop_end(self, at: Optional[float] = None) -> Path:
        """The current loop finished. After an event: back to the mood. Otherwise repeat the same
        loop until its dwell runs out, then rotate to another from the pool."""
        at = time.time() if at is None else at
        was_event, self.oneshot = self.oneshot, False
        if self.in_seq:
            if self.step_until is not None and at < self.step_until:
                return self.catalog.loops[self.current]     # a timed step keeps repeating
            nxt = self._next_step(at)
            if nxt is not None:
                return nxt
            was_event = True                                # ending done: back to a fresh mood loop
        if self.held:                                   # still holding it: keep the held loop
            self.current = self.held
            return self.catalog.loops[self.held]
        if self.mood is None:
            self.mood = UNKNOWN
        if not was_event and self.current is not None and at < self.until:
            return self.catalog.loops[self.current]
        self.current = self._pick(self.mood)
        self.until = at + self.rng.uniform(*DWELL_S)
        return self.catalog.loops[self.current]


def selftest() -> int:
    import tempfile
    fails = 0

    def ck(name, ok):
        nonlocal fails
        print(("PASS " if ok else "FAIL ") + name)
        fails += 0 if ok else 1

    with tempfile.TemporaryDirectory() as d:
        for pool in MOOD_LOOPS.values():
            for n in pool:
                (Path(d) / (n + ".gif")).write_bytes(b"GIF89a")
        (Path(d) / "happy_happy.webp").write_bytes(b"RIFF")
        c = LoopChooser(Catalog.scan(d), rng=random.Random(1))
        ck("unknown mood plays the unknown pool, not calm",
           c.on_mood(None).stem in MOOD_LOOPS[UNKNOWN])
        ck("same mood again returns None (keeps playing)", c.on_mood(None) is None)
        p = c.on_mood("happy")
        ck("mood change picks from the new pool", p.stem in MOOD_LOOPS["happy"])
        ck("loop end INSIDE the dwell repeats the same loop", c.on_loop_end(at=c.until - 1).stem == p.stem)
        q = c.on_loop_end(at=c.until + 1)
        ck("loop end after the dwell rotates to a different loop",
           q.stem != p.stem and q.stem in MOOD_LOOPS["happy"])
        try:
            c.on_mood("bogus")
            ck("unknown mood name raises", False)
        except SpriteError:
            ck("unknown mood name raises", True)
        ck("a .webp wins over the .gif of the same loop",
           Catalog.scan(d).loops["happy_happy"].suffix == ".webp")
        for n in set(EVENT_LOOPS.values()):
            (Path(d) / (n + ".gif")).write_bytes(b"GIF89a")
        c2 = LoopChooser(Catalog.scan(d), rng=random.Random(2))
        c2.on_mood("calm")
        ck("an event plays its own loop", c2.on_event("qt_arrived").stem == "quantum_drop_default")
        ck("a mood change during an event does NOT interrupt it", c2.on_mood("happy") is None)
        ck("after the event, Pico returns to the LATEST mood",
           c2.on_loop_end(at=0.0).stem in MOOD_LOOPS["happy"])
        ck("an event with no gesture changes nothing", c2.on_event("weapon_holstered") is None)
        ck("qt_error (route-data noise) has no gesture", c2.on_event("qt_error", at=0.0) is None)
        c2.on_event("injury", at=1000.0)
        ck("the same event inside the cooldown plays nothing", c2.on_event("injury", at=1010.0) is None)
        ck("after the cooldown it plays again", c2.on_event("injury", at=1000.0 + EVENT_COOLDOWN_S) is not None)
        for n in HAND_LOOPS.values():
            (Path(d) / (n + ".gif")).write_bytes(b"GIF89a")
        c3 = LoopChooser(Catalog.scan(d), rng=random.Random(3)); c3.on_mood("calm")
        ht = HandTracker()
        L = "<t> [Notice] <AttachmentReceived> Player[J] Attachment[%s, item, 1] Status[x] Port[%s] Elapsed[0]"
        ht.feed_line(L % ("lmg_7", "wep_stocked_2"))
        ch = ht.feed_line(L % ("lmg_7", HAND_PORT))
        ck("slot-1 weapon into the hand reads as a slot1 draw", ch == ("draw", "slot1"))
        ck("a slot1 draw holds the white pistol loop", c3.on_hand(ch).stem == HAND_LOOPS["slot1"])
        ck("while held, a mood change does not interrupt", c3.on_mood("happy") is None)
        ck("while held, loop end repeats the held loop", c3.on_loop_end(at=1e12).stem == HAND_LOOPS["slot1"])
        ch = ht.feed_line(L % ("lmg_7", "wep_stocked_2"))
        ck("the same item back in its holster reads as holster", ch == ("holster", None))
        ck("after holstering, Pico returns to the latest mood", c3.on_hand(ch).stem in MOOD_LOOPS["happy"])
        ht.feed_line(L % ("snp_9", "wep_stocked_3"))
        ck("slot-2 weapon reads as slot2", ht.feed_line(L % ("snp_9", HAND_PORT)) == ("draw", "slot2"))
        ck("a magazine attaching elsewhere changes nothing", ht.feed_line(L % ("mag_1", "magazine_attach")) is None)
        ht.feed_line(L % ("med_3", "wep_sidearm"))
        ck("the sidearm medgun reads as medical", ht.feed_line(L % ("med_3", HAND_PORT)) == ("draw", "medical"))
        ht.feed_line(L % ("gren_5", "grenade_attach_1"))
        ck("a grenade reads as bomb", ht.feed_line(L % ("gren_5", HAND_PORT)) == ("draw", "bomb"))
        c3.on_hand(("draw", "bomb"))
        ck("a held bomb is kept before its cap", c3.expire(at=c3.held_since + 7) is None and c3.held)
        for e in BOMB_ENDINGS:
            for n, _ in e:
                (Path(d) / (n + ".gif")).write_bytes(b"GIF89a")
        (Path(d) / (BOMB_ALERT_LOOP + ".gif")).write_bytes(b"GIF89a")
        c3 = LoopChooser(Catalog.scan(d), rng=random.Random(4)); c3.on_mood("calm"); c3.on_hand(("draw", "bomb"))
        t0 = c3.held_since
        ck("before 4 s he just holds the bomb", c3.expire(at=t0 + 2) is None and c3.held == HAND_LOOPS["bomb"])
        ck("at 4 s his eyes go to exclamation marks", c3.expire(at=t0 + 5).stem == BOMB_ALERT_LOOP)
        first = c3.expire(at=t0 + 9)
        ck("at 8 s a random ending starts", first is not None and not c3.held and c3.in_seq
           and first.stem in {e[0][0] for e in BOMB_ENDINGS})
        ck("a mood change cannot cut an ending short", c3.on_mood("happy") is None)
        for i in range(10):                      # time moves: each loop end is 10 s later
            if not c3.in_seq:
                break
            c3.on_loop_end(at=t0 + 10 + 10 * i)
        ck("the ending runs to completion and he returns to his mood",
           not c3.in_seq and c3.current in MOOD_LOOPS["happy"])
        picks = set()
        for seed in range(40):
            cx = LoopChooser(Catalog.scan(d), rng=random.Random(seed)); cx.on_hand(("draw", "bomb"))
            picks.add(cx.expire(at=cx.held_since + 9).stem)
        ck("all five endings get picked across seeds (%d seen)" % len(picks), len(picks) == len(BOMB_ENDINGS))
        c3.on_hand(("draw", "slot1"))
        ck("a weapon has no cap and is kept", c3.expire(at=c3.held_since + 3600) is None and c3.held)
        (Path(d) / "confused_confused.gif").unlink()
        try:
            Catalog.scan(d).check()
            ck("a mood with no loops on disk is refused at load", False)
        except SpriteError:
            ck("a mood with no loops on disk is refused at load", True)
    real = DEFAULT_DIR
    if real.is_dir():
        missing = Catalog.scan(real).check()
        ck("real Drake folder covers every mood (missing: %s)" % (missing or "none"), True)
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(selftest())
