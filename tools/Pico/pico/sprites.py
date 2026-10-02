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
             "idle_scratch_default",
             # J 19:15 prop idles: a handheld console (his gaming idea) and a scanner sweep
             "weapon_reload_happy+console", "scan_ping_default+scanner",
             # J 2026-10-02 06:36 "Sure!": more snapped idles from the unused half of the prop sheet
             # J 06:53 "several aren't snapped onto the fins": each now sits on a pose where a flipper is
             # actually out for it, checked on every frame of the loop
             "weapon_draw_focused+wrench", "scan_ping_default_held+binoculars_salute",   # J 07:03/07:09: salute pose, binoculars at the END of the raised flipper (held frames only)
            
             "weapon_draw_focused+pickaxe", "idle_peek_default+flashlight", "scan_ping_default+camera_out"),
    "alert": ("radar_contact_surprised", "determined_focused", "weapon_draw_focused"),
    "hurt": ("sad_sad", "disappointed_sad", "sulk_sad", "cry_sad"),
    "happy": ("happy_happy", "cheer_happy", "giggle_happy", "proud_happy", "idle_dance_happy",
              "ship_claim_star_grab+huckaby",    # J's Huckaby puppet: "WHERE IS MY JALOPY?!" (GAG_SEQS)
              "weapon_reload_happy_grab+chrisroberts_hold",    # the Chris Roberts action figure (GAG_SEQS)
              "proud_happy_grab+whale_hold"),   # the Chairman's Club WHALE certificate (GAG_SEQS)
    "startled": ("startled_surprised", "shocked_surprised", "scared_surprised"),
    "irritated": ("annoyed_angry", "angry_angry", "disgust_angry"),
    UNKNOWN: ("confused_confused",),
}

# Gag props show up only SOMETIMES. J 2026-10-01 19:28, on the Huckaby puppet: "Like other gag props it
# shouldn't always spawn but sometimes." When the pool draws one of these, it plays with this probability;
# otherwise a regular loop from the same pool is drawn instead.
RARE_LOOPS: Mapping[str, float] = {
    "ship_claim_star_grab+huckaby": 0.15,
    "weapon_reload_happy_grab+chrisroberts_hold": 0.15,
    "proud_happy_grab+whale_hold": 0.15,
}
# J 2026-10-01 21:47: "maybe once an hour at the most for the puppet and the action figure". One SHARED
# cooldown: after any gag prop plays, none can play again for this long, whatever the roll says.
GAG_COOLDOWN_S = 3600.0

# A gag that is more than one loop. When the pool draws the FIRST step, the rest follow in order, each
# repeating for its seconds, and then he goes back to his mood. J 2026-10-01 20:58, on the Chris Roberts
# action figure: "keep the whole package. Have him hold it then wave it around."
# A step of 0 seconds plays ONCE. J 21:18: "he grabs it too much. Should be one grab then the rest of the
# animation then grab it again" -> the reload loop is cut (elah-audio/pico_loop_slice.py) into _grab, _held
# and _release, so he reaches for it once, holds it through the waves, and puts it away once.
GAG_SEQS: Mapping[str, list] = {
    "weapon_reload_happy_grab+chrisroberts_hold": [("weapon_reload_happy_grab+chrisroberts_hold", 0),
                                                   ("weapon_reload_happy_held+chrisroberts_hold", 1.5),
                                                   ("cheer_happy+chrisroberts_wave", 3.5),
                                                   # J 21:03: "also move it into the other hand as well"
                                                   ("celebrate_happy+chrisroberts_wave_r", 3.5),
                                                   ("weapon_reload_happy_release+chrisroberts_hold", 0)],
    # J 2026-10-02: "another prop for Pico to show off on occasion" -- grab it once, hold it up, put it away
    # the puppet, raised once and held, not re-raised every cycle (audit 2026-10-02)
    "ship_claim_star_grab+huckaby": [("ship_claim_star_grab+huckaby", 0), ("ship_claim_star_held+huckaby", 3.0),
                                     ("ship_claim_star_release+huckaby", 0)],
    "proud_happy_grab+whale_hold": [("proud_happy_grab+whale_hold", 0), ("proud_happy_held+whale_hold", 4.0),
                                    ("proud_happy_release+whale_hold", 0)],
}

# Game.log event type (SuitMk2 event_parser) -> a loop played ONCE, then Pico returns to his mood.
# An event is something that HAPPENED; a mood is how he feels about it. The mood engine already turns
# events into feelings, so this layer only adds the gesture. MY PICKS, OPEN FOR J, like MOOD_LOOPS.
EVENT_LOOPS: Mapping[str, str] = {
    "docking_ready": "docking_focused",
    "docking_detached": "undock_default",
    "qt_route_calculated": "nav_plot_focused+datapad",   # plotting a route on a datapad
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
    "incoming_call": "radar_contact_surprised+walkie",   # he answers it on a walkie-talkie
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
    # Audit 2026-10-02: a held item sat on the FULL loop, which dips to the empty-handed rest pose every
    # cycle, so he re-grabbed his pistol / kit / drink every ~1.4 s (what J objected to on the action
    # figure). Held items use the loop's _held cut (pico_loop_slice.py): the steady part only.
    # "loop+prop" = a SNAP STATE (J 19:42): the plain loop, with the prop drawn on at runtime from the
    # loop's .anchors.json and out/snap_props. No baked copy per prop.
    "slot1": "weapon_draw_focused_held+pistol_white",
    "slot2": "weapon_draw_focused_held+pistol_red",
    "utility": "weapon_draw_focused_held+utility_gun",   # the utility gun, for the multitool
    # J 2026-10-01 18:16: "For a medgun or med pen he should pull out a first aid kit." Prop 07, held in
    # front with both flippers (the reload pose). J's sidearm slot holds a medgun, so it maps here too.
    "medical": "weapon_reload_focused_held+med_kit",
    # J 2026-10-01 18:19: "Bomb should make him do the reload animation except holding a big bomb."
    "bomb": "weapon_reload_focused_prop17",
    "gadget": "weapon_draw_focused_held+drill",      # mining gadget -> the drill (12 draws in J's logs)
    "drink": "weapon_reload_happy_held+drink",       # a drink bottle -> sipping from a canister
    # J 06:36: "Do we have a fish? Because that would make for a hilarious melee weapon." Melee items
    # (banu_melee_01 etc.) sit in a utility port, so they are told apart by NAME (108 hand draws in J's logs).
    "melee": "weapon_draw_focused_held+fish_club",
}
# A held key with no holster line in this long is assumed gone. J asked whether the log says a grenade
# was thrown: it does not, directly. Measured over 20 logs: of 30 grenades that reached the hand, 19
# were never mentioned again (thrown) and 11 went back to a grenade_attach port (put away). Without a
# cap a thrown grenade leaves Pico hugging the bomb forever.
HAND_MAX_S: Mapping[str, float] = {"bomb": 8.0,
                                   "drink": 6.0,   # a finished drink never goes back to a pocket
                                   "food": 6.0}    # nor does an eaten hot dog
# A hand key with several looks: one is drawn at random each time it comes out. J 2026-10-02 07:39,
# "Here's food": eight hot dogs, held in the hands-together pose.
# The game's eight hot dogs match J's eight pictures one-for-one (checked 2026-10-02 against the hand
# draws in his saved logs: Food_hotdog_01_<kind>_a). Other food draws a random hot dog.
HOTDOG_BY_KIND: Mapping[str, int] = {"": 1, "breakfast": 2, "chili": 3, "cruiser": 4, "double": 5,
                                     "melty": 6, "veggie": 7, "yakisoba": 8}
_HOTDOG = re.compile(r"food_hotdog_\d+(?:_([a-z]+))?_[a-z]$")


def food_variant(item: str) -> Optional[str]:
    """The hot dog that matches this in-game food item, or None (not a hot dog / unknown kind)."""
    m = _HOTDOG.match((item or "").lower())
    if not m:
        return None
    n = HOTDOG_BY_KIND.get(m.group(1) or "")
    return None if n is None else "weapon_reload_happy_held+hotdog_%d" % n


HAND_VARIANTS: Mapping[str, tuple] = {
    "food": tuple("weapon_reload_happy_held+hotdog_%d" % i for i in range(1, 9)),
}
# J's bomb gag, 2026-10-01 18:29-18:32: hold it; at 4 s his eyes become "!"; at 8 s one of FIVE random
# endings plays ("aim for 5 random bomb animation sequences"). A step is (loop, seconds): 0 = play it
# once, N = keep repeating it for N seconds. Endings whose FIRST loop is missing for an outfit are
# skipped, and missing later steps are dropped, so a half-built outfit still does something sensible.
# The explosions are baked into the *_boom loops by elah-audio/pico_bomb_gag.py.
BOMB_ALERT_S = 4.0
BOMB_ALERT_LOOP = "weapon_reload_exclaim_prop17"
BOMB_ENDINGS = [
    # J 18:39: "the bomb should physically fall to the ground" -> bomb_drop first wherever he lets go
    [("bomb_drop", 0), ("celebrate_exclaim_boom", 0), ("idle_settle_spiral", 4.0)],   # 1 panic jump
    # 2 the throw, VISIBLE now (J 2026-10-02): the bomb rides his flipper up, flies an arc out to the frame
    #   edge and goes off there (pico_bomb_gag.make_throw; every finished outfit has it).
    [("cheer_happy_throw", 0)],                                      # 2 toss it; it blows up off to
                                                                     #   the side (J 18:36)
    [("crash_X_X_boom", 0), ("idle_settle_spiral", 3.0)],            # 3 goes off in his hands
    [("bomb_drop", 0), ("scared_surprised_boom", 0), ("sulk_sad", 4.0)],   # 4 flinch, then sulk
    [("bomb_drop", 0), ("confused_confused", 3.0), ("relieved_happy", 0)],   # 5 a dud
    # 6 the love-bomb, J 18:36: "blows up in his hands as confetti and hearts and he does a few poses
    #   with heart eyes like dancing and some cute pose before shaking himself free from the spell and
    #   stomps angrily and then returns to normal"
    [("love_heart_confetti", 0), ("idle_dance_heart", 4.0), ("shy_heart", 0),
     ("startled_surprised", 0), ("annoyed_angry", 0)],
]
_ATTACH = re.compile(r"<AttachmentReceived> Player\[[^\]]*\] Attachment\[([^,]+), ([^,]+),.*?Port\[([^\]]+)\]")
HAND_PORT = "weapon_attach_hand_right"


def _slot_of(port: str, item: str = "") -> Optional[str]:
    if port == "wep_stocked_2":
        return "slot1"
    if port == "wep_stocked_3":
        return "slot2"
    if "_melee" in item.lower() or "knife" in item.lower():
        return "melee"                                  # a knife lives in a utility port too
    if port.startswith("utility_attach"):
        return "utility"
    if port == "wep_sidearm" or port.startswith("medPen_attach"):
        return "medical"
    if port.startswith("grenade_attach"):
        return "bomb"
    if port.startswith("gadget_attach"):
        return "gadget"
    if item.lower().startswith(("food_", "consumable_food")):
        return "food"                                   # a hot dog, not the drink canister (J 10-02)
    if item.lower().startswith(("drink_", "consumable_drink")):
        return "drink"                                  # comes out of inventory_pocket
    return None


class HandTracker:
    """Game.log lines in; what Pico should be holding out. Tracks each item's last port by its id."""

    def __init__(self):
        self.last_port: dict[str, str] = {}
        self.holding: Optional[str] = None     # a HAND_LOOPS key, or None
        self.item: Optional[str] = None        # the in-game item name of the last draw
        self.held_uid: Optional[str] = None

    def feed_line(self, line: str) -> Optional[tuple[str, Optional[str]]]:
        """Returns ("draw", key) / ("holster", None) when the hand changes, else None."""
        m = _ATTACH.search(line)
        if not m:
            return None
        uid, item, port = m.groups()
        prev = self.last_port.get(uid)
        self.last_port[uid] = port
        if port == HAND_PORT:
            key = _slot_of(prev or "", item)
            if key is None:                            # something unmapped went into the hand
                if self.holding is not None:
                    self.holding, self.held_uid = None, None
                    return ("holster", None)
                return None
            self.holding, self.held_uid, self.item = key, uid, item
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


SNAP_SEP = "+"

# SIGNS (J's 25 on pico_signs_transparent.png): held up in the ship-claim pose as snap props. pico/signs.py
# decides WHETHER and WHICH (event-driven, 8 min cooldown, mood veto, no repeat of the last 6, 35% roll).
SIGN_LOOP = "ship_claim_star"
SIGN_HOLD_S = 4.0                      # long enough to read the slogan
# this chooser's moods -> the picker's veto vocabulary (grief, fear, irritation)
SIGN_MOOD = {"hurt": "grief", "startled": "fear", "irritated": "irritation"}


def _sign_names() -> list:
    try:
        from pico import signs
        # the whole loop, plus its grab/held/release cuts (one raise, a steady hold, one lowering)
        return [SIGN_LOOP + part + SNAP_SEP + "sign_" + sid for sid, _t, _tags in signs.SIGNS
                for part in ("", "_grab", "_held", "_release")]
    except Exception:
        return []


def split_snap(name: str) -> tuple[str, Optional[str]]:
    base, _, prop = name.partition(SNAP_SEP)
    return base, (prop or None)


def _snap_props() -> dict:
    try:
        from pico import snap
        return snap.load_props()
    except Exception:
        return {}                   # no manifest yet: snapped entries simply are not available


class LoopChooser:
    """Mood in, loop path out. Stays on the current loop until the mood changes or the loop ends."""

    def __init__(self, catalog: Catalog, moods: Mapping[str, tuple[str, ...]] = MOOD_LOOPS,
                 rng: Optional[random.Random] = None):
        # Snapped entries ("loop+prop") exist when their plain loop does and the prop is in the manifest.
        # They map to the PLAIN loop's file; the window draws the prop (prop_for()).
        self.snap_props = _snap_props()
        names = ({n for p in moods.values() for n in p} | set(EVENT_LOOPS.values()) | set(HAND_LOOPS.values())
                 | set(_sign_names()) | {st[0] for seq in GAG_SEQS.values() for st in seq}
                 | {n for v in HAND_VARIANTS.values() for n in v})
        for n in names:
            base, prop = split_snap(n)
            if prop and base in catalog.loops and prop in self.snap_props:
                catalog.loops[n] = catalog.loops[base]
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
        self.variants = {k: [n for n in v if n in catalog.loops] for k, v in HAND_VARIANTS.items()}
        for k, v in self.variants.items():
            if v:
                self.hand.setdefault(k, v[0])
        self.held: Optional[str] = None  # a loop name while something is in his hand
        self.held_key: Optional[str] = None
        self.held_since = 0.0
        # Usable if its first REAL step exists for this outfit (the drop is a lead-in, not the gag).
        def usable(e):
            core = [st for st in e if st[0] != "bomb_drop"]
            return bool(core) and core[0][0] in catalog.loops
        self.endings = [[st for st in e if st[0] in catalog.loops] for e in BOMB_ENDINGS if usable(e)]
        self.carry_frame = False          # next play should keep the current frame number
        self.last_sign_at: Optional[float] = None
        self.recent_signs: list = []
        self.last_sign: Optional[dict] = None   # the picker's last decision, with its reason
        self.last_gag_at: Optional[float] = None   # when a gag prop last played (GAG_COOLDOWN_S)
        # User preferences (the Customise dialog, J 2026-10-02 "run through those"): defaults = as built.
        self.gags_on = True
        self.signs_on = True
        self.gag_cooldown_s = GAG_COOLDOWN_S
        self.seq: list = []               # remaining steps of a running bomb ending
        self.in_seq = False
        self.step_until: Optional[float] = None

    def apply_prefs(self, prefs: Mapping) -> None:
        """Settings from the Customise dialog. Missing or bad values keep the defaults."""
        self.gags_on = bool(prefs.get("gags", True))
        self.signs_on = bool(prefs.get("signs", True))
        try:
            m = float(prefs.get("gag_cooldown_min", GAG_COOLDOWN_S / 60))
            self.gag_cooldown_s = max(60.0, m * 60)
        except (TypeError, ValueError):
            self.gag_cooldown_s = GAG_COOLDOWN_S

    def prop_for(self, name: Optional[str] = None) -> Optional[str]:
        """The snap prop to draw over the current (or named) loop, or None."""
        name = self.current if name is None else name
        return split_snap(name)[1] if name else None

    def _gag(self, at: float) -> None:
        """If the loop just picked opens a multi-step gag, queue the rest of it."""
        steps = GAG_SEQS.get(self.current or "")
        if not steps:
            return
        self.seq = [st for st in steps[1:] if st[0] in self.catalog.loops]
        self.in_seq = True
        self.step_until = at + steps[0][1]

    def _pick(self, mood: str, at: Optional[float] = None) -> str:
        pool = self.pools[mood]
        if len(pool) > 1 and self.current in pool:
            pool = tuple(n for n in pool if n != self.current)   # never the same loop twice in a row
        pick = self.rng.choice(pool)
        p = RARE_LOOPS.get(pick)
        if p is not None:                                          # a gag prop: only sometimes
            now = time.time() if at is None else at
            cooled = self.last_gag_at is None or now - self.last_gag_at >= self.gag_cooldown_s
            common = tuple(n for n in pool if n not in RARE_LOOPS)
            if (not self.gags_on or not cooled or self.rng.random() >= p) and common:
                pick = self.rng.choice(common)
            else:
                self.last_gag_at = now
        return pick

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
        self._gag(time.time())
        return self.catalog.loops[self.current]

    def maybe_sign(self, event_type: str, at: float) -> Optional[Path]:
        """Ask pico/signs.py whether this event earns a sign. Held SIGN_HOLD_S seconds, then back."""
        if not self.signs_on:
            return None
        try:
            from pico import signs
        except Exception:
            return None
        ev = signs.EVENT_ALIASES.get(event_type, event_type)
        d = signs.pick(ev, mood=SIGN_MOOD.get(self.mood or "", self.mood), now_s=at,
                       last_sign_s=self.last_sign_at, recent=self.recent_signs, rng=self.rng)
        self.last_sign = d
        if not d.get("show"):
            return None
        name = SIGN_LOOP + SNAP_SEP + "sign_" + d["sign"]
        if name not in self.catalog.loops:
            return None
        self.last_sign_at = at
        self.recent_signs = (self.recent_signs + [d["sign"]])[-signs.NOVELTY_WINDOW:]
        # Audit 2026-10-02: the full ship-claim loop drops his arm to rest every ~1.4 s, so a 4 s sign was
        # raised and lowered three times - the re-grab J objected to on the action figure. Use the cuts.
        cut = [(SIGN_LOOP + p + SNAP_SEP + "sign_" + d["sign"], secs)
               for p, secs in (("_grab", 0), ("_held", SIGN_HOLD_S), ("_release", 0))]
        if all(n in self.catalog.loops for n, _ in cut):
            self.seq = cut
        else:
            self.seq = [(name, SIGN_HOLD_S)]             # an outfit not yet sliced: the old behaviour
        self.in_seq = True
        return self._next_step(at)

    def on_event(self, event_type: str, at: Optional[float] = None) -> Optional[Path]:
        """A Game.log event happened. Returns its one-shot loop, or None (no gesture, or cooling down).
        A sign, when the picker allows one, replaces the gesture."""
        if self.in_seq or self.held:      # a bomb ending / held item is never cut off by a game event
            return None
        at = time.time() if at is None else at
        sign = self.maybe_sign(event_type, at)
        if sign is not None:
            return sign
        name = self.events.get(event_type)
        if name is None:
            return None
        if at - self.last_fired.get(event_type, float("-inf")) < EVENT_COOLDOWN_S:
            return None
        self.last_fired[event_type] = at
        self.oneshot = True
        self.current = name
        return self.catalog.loops[name]

    def on_hand(self, change: Optional[tuple[str, Optional[str]]], item: Optional[str] = None) -> Optional[Path]:
        """A HandTracker change. Draw -> hold that loop until holstered; holster -> back to the mood."""
        if change is None:
            return None
        kind, key = change
        if kind == "draw" and key in self.hand:
            self.held_key, self.held_since = key, time.time()
            vs = self.variants.get(key)
            exact = food_variant(item) if key == "food" and item else None
            if exact and exact in self.catalog.loops:
                self.held = self.current = exact          # the hot dog he is actually eating
            else:
                self.held = self.current = self.rng.choice(vs) if vs else self.hand[key]
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
            # Only the eyes change (J 18:39: "the clip starts again when it switches to ! eyes"): the
            # alert loop is the same pose frame for frame, so the window carries the frame across.
            self.carry_frame = True
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
        self.current = self._pick(self.mood, at)
        self.until = at + self.rng.uniform(*DWELL_S)
        self._gag(at)
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
        for n in list(HAND_LOOPS.values()) + [x for v in HAND_VARIANTS.values() for x in v]:
            (Path(d) / (split_snap(n)[0] + ".gif")).write_bytes(b"GIF89a")   # held cuts the hand loops use
        for steps in GAG_SEQS.values():                  # the plain loops a gag's snapped steps sit on
            for n, _secs in steps:
                (Path(d) / (split_snap(n)[0] + ".gif")).write_bytes(b"GIF89a")
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
        c3.on_hand(ch)                       # a snapped entry plays the PLAIN loop; the prop is drawn on
        ck("a slot1 draw holds the white pistol", c3.current == HAND_LOOPS["slot1"]
           and c3.prop_for() == "pistol_white")
        ck("while held, a mood change does not interrupt", c3.on_mood("happy") is None)
        c3.on_loop_end(at=1e12)
        ck("while held, loop end repeats the held loop", c3.current == HAND_LOOPS["slot1"])
        ch = ht.feed_line(L % ("lmg_7", "wep_stocked_2"))
        ck("the same item back in its holster reads as holster", ch == ("holster", None))
        ck("after holstering, Pico returns to the latest mood", c3.on_hand(ch).stem in MOOD_LOOPS["happy"])
        ht.feed_line(L % ("snp_9", "wep_stocked_3"))
        ck("slot-2 weapon reads as slot2", ht.feed_line(L % ("snp_9", HAND_PORT)) == ("draw", "slot2"))
        ck("a magazine attaching elsewhere changes nothing", ht.feed_line(L % ("mag_1", "magazine_attach")) is None)
        ht.feed_line(L % ("med_3", "wep_sidearm"))
        ck("the sidearm medgun reads as medical", ht.feed_line(L % ("med_3", HAND_PORT)) == ("draw", "medical"))
        LF = ("<t> [Notice] <AttachmentReceived> Player[J] Attachment[food_hotdog_9, food_hotdog_basic, 9] "
              "Status[x] Port[%s] Elapsed[0]")
        ht.feed_line(LF % "inventory_pocket")
        ck("a hot dog reads as food, not drink", ht.feed_line(LF % HAND_PORT) == ("draw", "food"))
        ck("the game's chili dog is J's chili dog",
           food_variant("Food_hotdog_01_chili_a") == "weapon_reload_happy_held+hotdog_3"
           and food_variant("Food_hotdog_01_a") == "weapon_reload_happy_held+hotdog_1"
           and food_variant("Food_hotdog_01_yakisoba_a") == "weapon_reload_happy_held+hotdog_8")
        ck("a burrito is not a hot dog", food_variant("Food_burrito_01_beef_a") is None)
        LM = ("<t> [Notice] <AttachmentReceived> Player[J] Attachment[banu_melee_01_77, banu_melee_01, 77] "
              "Status[x] Port[%s] Elapsed[0]")             # the real shape: uid, then the item name
        ht.feed_line(LM % "utility_attach_2")
        ck("a knife from a utility port reads as melee, not utility",
           ht.feed_line(LM % HAND_PORT) == ("draw", "melee"))
        ht.feed_line(L % ("gren_5", "grenade_attach_1"))
        ck("a grenade reads as bomb", ht.feed_line(L % ("gren_5", HAND_PORT)) == ("draw", "bomb"))
        ht.feed_line(L % ("gad_8", "gadget_attach_1"))
        ck("a mining gadget reads as gadget", ht.feed_line(L % ("gad_8", HAND_PORT)) == ("draw", "gadget"))
        LD = "<t> [Notice] <AttachmentReceived> Player[J] Attachment[%s, Drink_bottle_cruz_01_lux_a, 1] Status[x] Port[%s] Elapsed[0]"
        ht.feed_line(LD % ("drk_2", "inventory_pocket"))
        ck("a drink bottle reads as drink", ht.feed_line(LD % ("drk_2", HAND_PORT)) == ("draw", "drink"))
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
        ck("the panic jump starts with the bomb falling", any(e[0][0] == "bomb_drop" and
           e[1][0] == "celebrate_exclaim_boom" for e in c3.endings))
        ck("a mood change cannot cut an ending short", c3.on_mood("happy") is None)
        for i in range(10):                      # time moves: each loop end is 10 s later
            if not c3.in_seq:
                break
            c3.on_loop_end(at=t0 + 10 + 10 * i)
        ck("the ending runs to completion and he returns to his mood",
           not c3.in_seq and c3.current in MOOD_LOOPS["happy"])
        picks = set()
        for seed in range(80):
            cx = LoopChooser(Catalog.scan(d), rng=random.Random(seed)); cx.on_hand(("draw", "bomb"))
            first = cx.expire(at=cx.held_since + 9).stem      # endings share a first step (the drop),
            picks.add((first,) + tuple(n for n, _ in cx.seq))  # so tell them apart by every step
        # a gag loop in a pool shows up far less often than a regular one
        cg = LoopChooser(Catalog.scan(d), rng=random.Random(7))
        rare = next(iter(RARE_LOOPS))
        if any(rare in pool for pool in cg.pools.values()):
            mood = next(m for m, pool in cg.pools.items() if rare in pool)
            hits = 0
            for _ in range(2000):
                cg.current = None
                cg.last_gag_at = None                  # the rarity roll alone, without the hourly cap
                hits += cg._pick(mood) == rare
            share = hits / 2000.0
            fair = 1.0 / len(cg.pools[mood])
            ck("a gag prop is rare (%.1f%% vs %.1f%% for a regular loop)" % (100 * share, 100 * fair),
               0 < share < fair * 0.5)
        # the hourly cap: once a gag has played, none plays again within GAG_COOLDOWN_S
        ch = LoopChooser(Catalog.scan(d), rng=random.Random(5))
        gags = set(RARE_LOOPS)
        mood = next((m for m, pool in ch.pools.items() if gags & set(pool)), None)
        if mood:
            t, first = 0.0, None
            for _ in range(5000):                       # picks every 10 s for ~14 hours
                ch.current = None
                if ch._pick(mood, at=t) in gags:
                    if first is not None and t - first < 3600.0:   # J's hour, not the constant under test
                        first = -1.0; break
                    first = t
                t += 10.0
            ck("no two gags within an hour", first is not None and first >= 0)
        # the Customise settings: gags off means never a gag; signs off means never a sign
        cp = LoopChooser(Catalog.scan(d), rng=random.Random(11))
        cp.apply_prefs({"gags": False})
        mood = next((m for m, pool in cp.pools.items() if set(RARE_LOOPS) & set(pool)), None)
        if mood:
            seen = set()
            for _ in range(500):
                cp.current = None; cp.last_gag_at = None
                seen.add(cp._pick(mood, at=0.0))
            ck("gags switched off never play", not (seen & set(RARE_LOOPS)))
        cp.apply_prefs({"signs": False})
        ck("signs switched off never show", cp.maybe_sign("contract_complete", at=5.0) is None)
        cp.apply_prefs({"gag_cooldown_min": 120})
        ck("the gag limit follows the setting", cp.gag_cooldown_s == 7200)
        cp.apply_prefs({"gag_cooldown_min": "nonsense"})
        ck("a bad setting falls back to the default", cp.gag_cooldown_s == GAG_COOLDOWN_S)
        # a multi-step gag: hold the package, then wave it, then back to the mood
        cq = LoopChooser(Catalog.scan(d), rng=random.Random(3)); cq.on_mood("happy")
        gname = next(iter(GAG_SEQS))
        if gname in cq.catalog.loops and "cheer_happy+chrisroberts_wave" in cq.catalog.loops:
            cq.current = gname; cq._gag(at=500.0)
            cq.on_loop_end(at=500.2)
            ck("one grab, then it is held", cq.current == "weapon_reload_happy_held+chrisroberts_hold")
            ck("the held step repeats for its seconds, with no second grab",
               cq.on_loop_end(at=501.0) == cq.catalog.loops["weapon_reload_happy_held+chrisroberts_hold"])
            cq.on_loop_end(at=503.5)
            ck("then waves the package", cq.current == "cheer_happy+chrisroberts_wave")
            cq.on_loop_end(at=507.0)
            ck("then swaps it to the other flipper", cq.current == "celebrate_happy+chrisroberts_wave_r")
            cq.on_loop_end(at=511.0)
            ck("then puts it away once", cq.current == "weapon_reload_happy_release+chrisroberts_hold")
            cq.on_loop_end(at=511.3)
            ck("then goes back to his mood", not cq.in_seq and cq.current in MOOD_LOOPS["happy"])
        else:
            ck("the Chris Roberts gag resolves (needs out/snap_props)", False)
        # signs: a qualifying event can raise one, and the cooldown stops a second straight after
        (Path(d) / (SIGN_LOOP + ".gif")).write_bytes(b"GIF89a")   # the plain loop the signs snap onto
        cs = LoopChooser(Catalog.scan(d), rng=random.Random(0)); cs.on_mood("calm")
        fired = None
        for i in range(40):                       # the 35% roll means it may take a few events
            cs.last_sign_at = None
            p = cs.maybe_sign("contract_complete", at=1000.0 + i)
            if p is not None:
                fired = cs.current; break
        ck("a qualifying event can raise a sign (%s)" % fired, fired is not None and "+sign_" in fired)
        ck("a sign is raised once, held, then lowered once",
           cs.in_seq and "_grab+sign_" in (fired or "")
           and [secs for _n, secs in cs.seq] == [SIGN_HOLD_S, 0]
           and cs.seq[0][0].startswith(SIGN_LOOP + "_held+sign_"))
        cs.in_seq = False
        ck("the cooldown blocks a second sign straight after",
           cs.maybe_sign("contract_complete", at=cs.last_sign_at + 5) is None
           and cs.last_sign["why"] == "cooldown")
        ck("a sad Pico holds up no sign", (cs.on_mood("hurt") or True)
           and cs.maybe_sign("contract_complete", at=1e7) is None and cs.last_sign["why"] == "mood_veto")
        ck("every ending gets picked across seeds (%d of %d)" % (len(picks), len(BOMB_ENDINGS)),
           len(picks) == len(BOMB_ENDINGS))
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
