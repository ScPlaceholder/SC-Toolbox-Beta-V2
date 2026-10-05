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

import math
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
    # J 2026-10-03 11:43: "Settle he grows extra flippers" -- out of the pool until it is re-rendered.
    "calm": ("idle_look_default", "idle_shuffle_default",
             "idle_stargaze_default", "idle_preen_default", "idle_tap_foot_default",
             "idle_drift_default", "idle_look_up_default", "idle_peek_default",
             "idle_scratch_default",
             # J 19:15 prop idles: a handheld console (his gaming idea) and a scanner sweep
             "weapon_reload_happy+console", "scan_ping_default+scanner",
             # J 2026-10-02 06:36 "Sure!": more snapped idles from the unused half of the prop sheet
             # J 06:53 "several aren't snapped onto the fins": each now sits on a pose where a flipper is
             # actually out for it, checked on every frame of the loop
             "weapon_draw_focused+wrench", "scan_ping_default_held+binoculars_salute",   # J 07:03/07:09: salute pose, binoculars at the END of the raised flipper (held frames only)
            
             "weapon_draw_focused+pickaxe", "idle_peek_default+flashlight", "scan_ping_default+camera_out",
             # J 2026-10-03 13:13: toy ships "during any idle animation even in mid combat". A toy, not a gag:
             # in the plain pools, no RARE roll and no shared cooldown. Held aloft at the END of the raised
             # flipper on the two poses that raise one -- J ruled out reload and proud ("he's not holding it"),
             # and grab/release are left out because the flipper is down there and the ship would float.
             "ship_claim_star_held+ship_gladius_claim", "scan_ping_default_held+ship_gladius_scan",
             # J 2026-10-03 15:22: the Banu skin's merchant stall ("I'M THE REAL BMM"), Banu only (BRAND_ONLY)
             "idle_shuffle_default+bmm_table",
             # J 2026-10-04 17:35: "the gags should also happen when he's happy" (he saw one gag all day).
             # Every gag is now in BOTH the calm and the happy pool; before, the stall was calm-only and
             # the other three happy-only, and he is calm most of a session.
             "ship_claim_star_grab+huckaby", "weapon_reload_happy_grab+chrisroberts_hold",
             "proud_happy_grab+whale_hold"),
    "alert": ("radar_contact_surprised", "determined_focused", "weapon_draw_focused",
              "ship_claim_star_held+ship_gladius_claim"),
    "hurt": ("sad_sad", "disappointed_sad", "sulk_sad", "cry_sad"),
    "happy": ("happy_happy", "cheer_happy", "giggle_happy", "proud_happy", "idle_dance_happy",
              "ship_claim_star_grab+huckaby",    # J's Huckaby puppet: "WHERE IS MY JALOPY?!" (GAG_SEQS)
              "weapon_reload_happy_grab+chrisroberts_hold",    # the Chris Roberts action figure (GAG_SEQS)
              "proud_happy_grab+whale_hold",    # the Chairman's Club WHALE certificate (GAG_SEQS)
              "idle_shuffle_default+bmm_table"),   # the Banu stall, Banu only (BRAND_ONLY); J 2026-10-04
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
    "idle_shuffle_default+bmm_table": 0.15,
}
# Toy-ship poses (J 2026-10-03, "a toy of every ship"): when the pool draws a ship pose, ANY ship in the snap manifest
# held in that pose (ship_<name>_<suffix>) can be swapped in, so the whole fleet is reachable from two pool entries.
TOY_POSES: Mapping[str, str] = {
    "ship_claim_star_held": "_claim",
    "scan_ping_default_held": "_scan",
}


# Gags that belong to ONE outfit: shown only when the loop folder is that brand's (pico_anim_sequences_<brand>).
BRAND_ONLY: Mapping[str, str] = {
    "idle_shuffle_default+bmm_table": "banu",
}


def brand_allows(name: str, root: Path) -> bool:
    """False when this loop is another outfit's gag. Drake's base folder has no brand suffix, so it gets none."""
    brand = BRAND_ONLY.get(name)
    return brand is None or Path(root).name.endswith("_" + brand)
# J 2026-10-01 21:47: "maybe once an hour at the most for the puppet and the action figure". One SHARED
# cooldown: after any gag prop plays, none can play again for this long, whatever the roll says.
# J 2026-10-04 17:35, after seeing one gag in a day of play: "Yeah let's do that" to about four an hour.
GAG_COOLDOWN_S = 900.0


def gag_cooldown_s(prefs: Mapping) -> float:
    """The gag cooldown these settings give, in seconds. LoopChooser.apply_prefs and the Customise dialog
    both ask here, so the dialog shows what the chooser does. A missing or bad value is GAG_COOLDOWN_S."""
    try:
        m = float(prefs.get("gag_cooldown_min", GAG_COOLDOWN_S / 60))
    except (TypeError, ValueError):
        return GAG_COOLDOWN_S
    if not math.isfinite(m):
        return GAG_COOLDOWN_S
    return max(60.0, m * 60)

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
    # the Banu merchant behind his stall, then hawking with both flippers (J 15:4x: "the animation where he also
    # waves around both his hands"). idle_dance keeps his feet planted and shares shuffle's chest, so the table,
    # anchored there, stays on the same spot of floor across both steps.
    "idle_shuffle_default+bmm_table": [("idle_shuffle_default+bmm_table", 3.0), ("idle_dance_happy+bmm_table", 4.0)],
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


# J 2026-10-03 10:28, "Did you wire in all the foodstuffs": the 43 other foods J had Picofied, keyed by the
# game's entity name (from Data.p4k, the same Food_<kind>_<nn>_<variant>_a shape the log hands us). Held in
# the same hands-together pose as the hot dogs. A food with no entry here still falls back to a hot dog.
FOOD_HOLD = "weapon_reload_happy_held"
FOOD_BY_ENTITY: Mapping[str, str] = {
    "food_bar_com_01_a": "cal-o-meal-chocolate-deluxe-protein-bar",
    "food_bar_com_01_lunes_a": "cal-o-meal-lunes-protein-bar",
    "food_bar_com_01_vanilla_a": "cal-o-meal-vanilla-protein-bar",
    "food_bar_onemeal_01_a": "onemeal-nutrition-bar-roast-chicken",
    "food_bar_onemeal_01_salmon_a": "onemeal-nutrition-bar-spicy-salmon",
    "food_bar_onemeal_01_steak_a": "onemeal-nutrition-bar-grilled-steak",
    "food_bar_onemeal_01_tofu_a": "onemeal-nutrition-bar-fried-tofu",
    "food_bar_snaggle_01_a": "snaggle-stick-original",
    "food_bar_snaggle_01_newaustin_a": "snaggle-stick-new-austin-bold",
    "food_biscuit_1_a": "ringaling",
    "food_box_noodle_01_a": "shoyu-lapsha",
    "food_burger_wham_01_a": "whamburger",
    "food_burrito_01_a": "dak-galbi-chicken-burrito",
    "food_icecream_eff_01_a": "ermer-family-farms-chibanzoo-ice-cream",
    "food_icecream_eff_01_choc_a": "ermer-family-farms-chocolate-ice-cream",
    "food_icecream_eff_01_coffee_a": "ermer-family-farms-coffee-ice-cream",
    "food_icecream_eff_01_fatfree_a": "ermer-family-farms-fat-free-ice-cream",
    "food_icecream_eff_01_lunes_a": "ermer-family-farms-lunes-ice-cream",
    "food_pickle_01": "pickle",
    "food_pizza_slice_01_pepperoni_a": "pepperoni-pizza-slice",
    "food_sachet_readymeal_01_beef_a": "readymeal-beef-chunks",
    "food_sachet_readymeal_01_burrito_a": "readymeal-bean-and-rice-burrito",
    "food_sachet_readymeal_01_meatball_a": "readymeal-meatball-marinara",
    "food_sachet_readymeal_01_noodles_a": "readymeal-chicken-patty-and-noodles",
    "food_sachet_readymeal_01_vegetarian_a": "readymeal-vegetarian",
    "food_sachet_uee_01_formula_a": "special-operation-formula-combat-ration",
    "food_skewered_rat_1_a": "aloprat-skewer",
    "food_tin_bogo_01_a": "bo-go-angeli-original",
    "food_tin_bogo_01_crawdads_a": "bo-go-crawdad",
    "food_tin_bogo_01_hotsweet_a": "bo-go-hot-and-sweet",
    "food_tin_mre_01_a": "ma-s-ready-to-eat-beef-home-stew",
    "food_tin_mre_01_chicken_a": "ma-s-ready-to-eat-chicken-home-stew",
    "food_tin_mre_01_fish_a": "ma-s-ready-to-eat-fish-home-stew",
    "food_tin_mre_01_noodle_a": "ma-s-ready-to-eat-noodle-red",
    "food_tin_mre_01_vegetable_a": "ma-s-ready-to-eat-vegetable-soup",
    "food_tin_noodle_1_a": "carafi-noodles",
    "food_tin_omni_01_a": "boumbo-stew-omni-pack",
    "food_tin_omni_01_stirfry_a": "stir-fry-vegetables-and-beef-omni-pack",
    "food_tin_omni_01_vegan_a": "vegan-delight-omni-pack",
    "food_tin_uee_01_a": "spiced-protein-stew-combat-ration",
    "food_tin_uee_01_chili_a": "chili-mac-combat-ration",
    "food_tin_uee_01_paneer_a": "sag-paneer-combat-ration",
    "food_vent_slug": "vent-slug",
}


def food_variant(item: str) -> Optional[str]:
    """The prop that matches this in-game food item: its own Picofied food, the matching hot dog, or None
    (unknown food)."""
    low = (item or "").lower()
    if low in FOOD_BY_ENTITY:
        return FOOD_HOLD + "+" + FOOD_BY_ENTITY[low]
    m = _HOTDOG.match(low)
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
# J 2026-10-02 12:39, the eating gag: hold it, then "use opacity from top down to make it look like he's
# eating it". A food whose bite stages are in the manifest (<prop>_eat_1..N, from pico_snap_export) stays
# whole for EAT_START_S, then steps through them, finishing EAT_END_S before the food cap puts it away.
EAT_STAGES = 5
EAT_START_S = 1.5
EAT_END_S = 0.5
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

# RESTS (J 2026-10-04: "He also doesn't need to do an animation every second. He can be idle at times",
# "He also doesn't need to loop an animation 8,000 times. He can [stand] still at times", "if you pull out
# a weapon he should still periodically idle"). Until today one mood loop repeated for a DWELL_S of 8-14 s
# (six to ten passes of a 1.4 s loop) and the next loop started on the very next frame, so he was never
# still; and a held weapon looped its held cut until the holster line. Now:
#   an animation plays ACT_PASSES times (a loop is 0.8-1.4 s), then he STANDS STILL for REST_S seconds,
#   then another animation. A mood change or a game event cuts a rest short at once.
#   with a weapon out he holds it still for HOLD_STILL_S, then puts it away, does one idle, takes it out
#   again and holds it still again. The holster line still ends all of it.
# "Standing still" needs no new art: frame 0 of every full loop is the same neutral stand (measured on all
# 19 outfit folders: the nine idle_*_default loops share frame 0 exactly, and a mood loop's frame 0 differs
# only by its face). The window pauses the loop on that frame (LoopChooser.resting).
ACT_PASSES = (1, 3)            # how many times one animation plays before he rests
REST_S = (10.0, 20.0)          # how long he stands still between animations, in seconds (J 2026-10-04:
                               # "Let's try an animation every 10-20 seconds"; was 10-25)
HOLD_STILL_S = (10.0, 20.0)    # how long a drawn weapon is held still between idle breaks
# The Customise dialog's "How lively" choice: a multiplier on REST_S and HOLD_STILL_S.
LIVELINESS: Mapping[str, float] = {"calm": 2.0, "normal": 1.0, "lively": 0.4}
# The loop whose frame 0 is his standing pose for each mood (the face is baked into the loop, so a hurt
# Pico rests looking hurt, and UNKNOWN rests confused, never calm). A mood not listed, or whose loop is
# missing for an outfit, uses the first plain loop of its pool.
REST_LOOPS: Mapping[str, str] = {
    "calm": "idle_look_default", "alert": "determined_focused", "hurt": "sad_sad", "happy": "happy_happy",
    "startled": "startled_surprised", "irritated": "annoyed_angry", UNKNOWN: "confused_confused",
}
_CUT = re.compile(r"_(held|grab|release)$")    # a slice of a loop: its frame 0 is not the standing pose

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

# J 2026-10-03 11:53: "add a bunch of the props for those animations to keep them interesting and diverse like
# the average user won't notice us reusing the same animation for different props if we cycle them correctly
# with other animations". Each idle keeps ONE slot in its pool; when the pool draws it, PROP_IDLE_P of the time
# it comes out holding a random prop that suits the pose. Listing every combo as its own pool entry would have
# made props ~80% of all calm picks and drowned the plain loops.
PROP_IDLE_P = 0.7
PROP_IDLES: Mapping[str, tuple[str, ...]] = {
    # flippers near the chest: things held up in front
    "idle_preen_default": ("drink", "camera", "binoculars", "med_kit", "console", "tool_02", "tool_03",
                           "tool_06", "tool_19", "tool_21", "tool_25", "tool_26", "tool_29", "tool_30",
                           "tool_36", "tool_50"),
    # one flipper raised: handled tools at the tip
    "idle_peek_default": ("flashlight", "datapad", "walkie", "wrench", "tool_04", "tool_05", "tool_08",
                          "tool_10", "tool_12", "tool_14", "tool_35", "tool_38", "tool_47", "binoculars",
                          "scanner"),
    # J: "Tap foot would work for a small prop"
    "idle_tap_foot_default": ("drink", "walkie", "datapad", "console", "tool_03", "tool_06", "tool_29",
                              "tool_30", "tool_47", "tool_50"),
}

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
                 rng: Optional[random.Random] = None, clock=time.time):
        self.clock = clock            # injected by tests, so hours of idling run in no time
        # Snapped entries ("loop+prop") exist when their plain loop does and the prop is in the manifest.
        # They map to the PLAIN loop's file; the window draws the prop (prop_for()).
        self.snap_props = _snap_props()
        names = ({n for p in moods.values() for n in p} | set(EVENT_LOOPS.values()) | set(HAND_LOOPS.values())
                 | set(_sign_names()) | {st[0] for seq in GAG_SEQS.values() for st in seq}
                 | {n for v in HAND_VARIANTS.values() for n in v}
                 | {base + SNAP_SEP + pid for base, pids in PROP_IDLES.items() for pid in pids}
                 | {FOOD_HOLD + SNAP_SEP + pid for pid in FOOD_BY_ENTITY.values()}
                 | {FOOD_HOLD + SNAP_SEP + "%s_eat_%d" % (pid, i) for pid in FOOD_BY_ENTITY.values()
                    for i in range(1, EAT_STAGES + 1)}
                 | {base + SNAP_SEP + pid for base, suf in TOY_POSES.items() for pid in self.snap_props
                    if pid.startswith("ship_") and pid.endswith(suf)}
                 # the put-away and take-out cuts of each held item, for an idle break with a weapon out
                 | {split_snap(n)[0][:-len("_held")] + part + SNAP_SEP + split_snap(n)[1]
                    for n in HAND_LOOPS.values() for part in ("_grab", "_release")
                    if split_snap(n)[1] and split_snap(n)[0].endswith("_held")})
        for n in names:
            base, prop = split_snap(n)
            if prop and base in catalog.loops and prop in self.snap_props:
                catalog.loops[n] = catalog.loops[base]
        catalog.check(moods)
        self.catalog = catalog
        self.pools = {m: tuple(n for n in pool if n in catalog.loops and brand_allows(n, catalog.root))
                      for m, pool in moods.items()}
        self.rng = rng or random.Random()
        self.mood: Optional[str] = None
        self.current: Optional[str] = None
        self.events = {e: n for e, n in EVENT_LOOPS.items() if n in catalog.loops}
        self.oneshot = False          # an event loop is playing; moods wait until it ends
        self.last_fired: dict[str, float] = {}
        self.resting = False          # the window should HOLD frame 0 of the current loop, not play it
        self.rest_until = 0.0         # ... until this time (on_tick ends it)
        self.passes_left = 0          # plays of the current animation still to come
        self.last_act: Optional[str] = None     # the last animation's loop (no prop), for no-repeat
        self.shown_mood: Optional[str] = None   # the mood whose animation he last played
        self.rest_scale = 1.0         # LIVELINESS
        self.break_phase: Optional[str] = None  # weapon out: "away" / "idle" / "back" during an idle break
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
        self.gag_cooldown_s = gag_cooldown_s(prefs)
        self.rest_scale = LIVELINESS.get(str(prefs.get("liveliness", "normal")), 1.0)

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

    def _pick(self, mood: str, at: Optional[float] = None, on_break: bool = False) -> str:
        pool = self.pools[mood]
        if on_break and self.held:
            # an idle between two holds of a weapon: no gag (its steps would run into the hold), and not
            # the empty-handed version of the pose he has just put the weapon away from
            stem = _CUT.sub("", split_snap(self.held)[0])
            pool = tuple(n for n in pool if n not in RARE_LOOPS
                         and not split_snap(n)[0].startswith(stem)) or pool
        # what he did last: the loop on screen, or, when that is only the frame he rests on, the
        # animation before the rest
        last = self.last_act if (self.resting or self.break_phase) else self.current
        cur_base = split_snap(last)[0] if last else None
        if len(pool) > 1 and cur_base is not None:
            # never the same loop twice in a row -- nor the same idle again with a different prop
            pool = tuple(n for n in pool if split_snap(n)[0] != cur_base) or pool
        pick = self.rng.choice(pool)
        if pick in PROP_IDLES and self.rng.random() < PROP_IDLE_P:
            options = [pick + SNAP_SEP + pid for pid in PROP_IDLES[pick] if pick + SNAP_SEP + pid in self.catalog.loops]
            if options:
                return self.rng.choice(options)
        base, prop = split_snap(pick)
        if prop and prop.startswith("ship_") and base in TOY_POSES:   # any ship of the fleet, not always the Gladius
            ships = [n for n in self.catalog.loops if split_snap(n)[0] == base
                     and (split_snap(n)[1] or "").startswith("ship_") and n.endswith(TOY_POSES[base])]
            if ships:
                return self.rng.choice(sorted(ships))
        p = RARE_LOOPS.get(pick)
        if p is not None:                                          # a gag prop: only sometimes
            now = self.clock() if at is None else at
            cooled = self.last_gag_at is None or now - self.last_gag_at >= self.gag_cooldown_s
            common = tuple(n for n in pool if n not in RARE_LOOPS)
            if (not self.gags_on or on_break or not cooled or self.rng.random() >= p) and common:
                pick = self.rng.choice(common)
            else:
                self.last_gag_at = now
        return pick

    def _stand(self, mood: str) -> str:
        """The loop whose frame 0 is his standing pose in this mood."""
        pref = REST_LOOPS.get(mood)
        if pref in self.catalog.loops and (pref in self.pools[mood] or not self.pools[mood]):
            return pref
        for n in self.pools[mood]:
            base, prop = split_snap(n)
            if not prop and not _CUT.search(base):
                return n
        return split_snap(self.pools[mood][0])[0] if self.pools[mood] else (self.current or pref)

    def _act(self, mood: str, at: float) -> Path:
        """Start an animation from the mood's pool. It plays ACT_PASSES times, then he rests."""
        pick = self._pick(mood, at)
        self.resting, self.break_phase = False, None
        self.current = pick
        self.last_act = split_snap(pick)[0]
        self.shown_mood = mood
        self.passes_left = self.rng.randint(*ACT_PASSES)
        self._gag(at)
        return self.catalog.loops[self.current]

    def _rest(self, at: float) -> Path:
        """Stand still: frame 0 of the mood's standing loop, held until rest_until."""
        self.resting, self.break_phase = True, None
        self.current = self._stand(self.mood or UNKNOWN)
        self.rest_until = at + self.rng.uniform(*REST_S) * self.rest_scale
        return self.catalog.loops[self.current]

    def _settle(self, at: float) -> Path:
        """An event, an ending or a held item is over. If the mood moved meanwhile he shows the new one;
        otherwise he just stands still again."""
        if self.mood is None:
            self.mood = UNKNOWN
        if self.mood != self.shown_mood:
            return self._act(self.mood, at)
        return self._rest(at)

    def on_mood(self, mood: Optional[str], at: Optional[float] = None) -> Optional[Path]:
        """Call on every reading. Returns a new loop path when the loop should change, else None.
        A CHANGE of mood plays that mood's animation at once, even in the middle of a rest."""
        key = mood if mood is not None else UNKNOWN
        if key not in self.pools:
            raise SpriteError("mood %r has no loop pool (known: %s)" % (key, ", ".join(self.pools)))
        if self.oneshot or self.held or self.in_seq:
            self.mood = key           # remembered; shown when the event loop / held item / ending ends
            return None
        if key == self.mood and self.current is not None:
            return None
        self.mood = key
        return self._act(key, self.clock() if at is None else at)

    def on_tick(self, at: Optional[float] = None) -> Optional[Path]:
        """Call every tick. Time-driven changes: a held item's cap (expire), and the END OF A REST, which
        nothing else can report because a resting loop is paused and never reaches its last frame."""
        at = self.clock() if at is None else at
        path = self.expire(at)
        if path is not None:
            return path
        if self.oneshot or self.in_seq or not self.resting or at < self.rest_until:
            return None
        if self.held:
            return self._break_start(at)
        sign = self.maybe_idle_sign(at)                 # now and then a sign, whatever he is doing
        if sign is not None:
            return sign
        return self._act(self.mood or UNKNOWN, at)

    # -- a weapon is out: hold it still, and every so often put it away for one idle -------------------
    def _cut_of(self, held: str, part: str) -> Optional[str]:
        """The _grab / _release cut that goes with a _held loop (same prop), if this outfit has it."""
        base, prop = split_snap(held)
        if not base.endswith("_held"):
            return None
        name = base[:-len("_held")] + part + (SNAP_SEP + prop if prop else "")
        return name if name in self.catalog.loops else None

    def _hold_still(self, at: float) -> Path:
        self.break_phase, self.resting = None, True
        self.current = self.held
        self.rest_until = at + self.rng.uniform(*HOLD_STILL_S) * self.rest_scale
        return self.catalog.loops[self.current]

    def _break_start(self, at: float) -> Path:
        away = self._cut_of(self.held, "_release")
        if away is None:
            return self._break_idle(at)
        self.resting, self.break_phase = False, "away"
        self.current = away
        return self.catalog.loops[away]

    def _break_idle(self, at: float) -> Path:
        if self.mood is None:
            self.mood = UNKNOWN
        self.break_phase = "idle"
        pick = self._pick(self.mood, at, on_break=True)
        self.resting = False
        self.current = pick
        self.last_act = split_snap(pick)[0]
        self.passes_left = self.rng.randint(*ACT_PASSES)
        return self.catalog.loops[pick]

    def _held_loop_end(self, at: float) -> Path:
        if self.held_key in HAND_MAX_S:               # bomb, drink, food: seconds long, run by expire()
            self.current = self.held
            return self.catalog.loops[self.held]
        phase = self.break_phase
        if phase == "away":
            return self._break_idle(at)
        if phase == "back":
            return self._hold_still(at)
        if phase is None and self.resting:            # a paused loop should not end; if one does, stay put
            self.current = self.held
            return self.catalog.loops[self.held]
        self.passes_left -= 1
        if self.passes_left > 0:
            return self.catalog.loops[self.current]
        if phase == "idle":
            back = self._cut_of(self.held, "_grab")
            if back is not None:
                self.break_phase, self.current = "back", back
                return self.catalog.loops[back]
        return self._hold_still(at)

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
        return self._raise_sign(d, at)

    def maybe_idle_sign(self, at: float) -> Optional[Path]:
        """A sign with no game event behind it, offered each time a rest ends (J 2026-10-04: "make the signs
        come up randomly regardless of tasks"). Never while he holds something or is mid-sequence."""
        if not self.signs_on or self.held or self.in_seq:
            return None
        if self.mood in (None, UNKNOWN):                # no readable game log: he only stands confused
            return None
        try:
            from pico import signs
        except Exception:
            return None
        d = signs.pick_idle(mood=SIGN_MOOD.get(self.mood or "", self.mood), now_s=at,
                            last_sign_s=self.last_sign_at, recent=self.recent_signs, rng=self.rng)
        self.last_sign = d
        return self._raise_sign(d, at)

    def _raise_sign(self, d: dict, at: float) -> Optional[Path]:
        """Start the grab / hold / release sequence for the picker's decision, or None if it said no."""
        try:
            from pico import signs
        except Exception:
            return None
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
        at = self.clock() if at is None else at
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
        self.resting = False              # an event cuts a rest short: the gesture plays now
        self.current = name
        return self.catalog.loops[name]

    def on_hand(self, change: Optional[tuple[str, Optional[str]]], item: Optional[str] = None,
                at: Optional[float] = None) -> Optional[Path]:
        """A HandTracker change. Draw -> hold that item until holstered (with idle breaks, for a weapon);
        holster -> the item is put away and he is back to his mood."""
        if change is None:
            return None
        at = self.clock() if at is None else at
        kind, key = change
        if kind == "draw" and key in self.hand:
            self.held_key, self.held_since = key, at
            self.resting, self.break_phase = False, None
            self.passes_left = self.rng.randint(*ACT_PASSES)
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
            return self._settle(at)
        return None

    def expire(self, at: Optional[float] = None) -> Optional[Path]:
        """Time-driven hand changes (a thrown grenade never logs a holster): the bomb's "!" at 4 s,
        and at its cap a random ending. on_tick() calls this every tick."""
        at = self.clock() if at is None else at
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
        if self.held_key == "food":
            bite = self._bite(age)
            if bite and bite != self.held:
                self.held = self.current = bite
                self.carry_frame = True          # same pose frame for frame: only the food changes
                return self.catalog.loops[self.held]
        cap = HAND_MAX_S.get(self.held_key or "")
        if cap is not None and age >= cap:
            if self.held_key == "bomb" and self.endings:
                self.held = None
                self.seq = list(self.rng.choice(self.endings))
                self.in_seq = True
                return self._next_step(at)
            return self.on_hand(("holster", None), at=at)
        return None

    def _bite(self, age: float) -> Optional[str]:
        """Which stage of the held food to show at this age, or None if it has no bite stages."""
        base, prop = split_snap(self.held or "")
        if not prop:
            return None
        root = prop.split("_eat_")[0]
        stages = [base + SNAP_SEP + "%s_eat_%d" % (root, i) for i in range(1, EAT_STAGES + 1)]
        stages = [n for n in stages if n in self.catalog.loops]
        if not stages:
            return None
        if age < EAT_START_S:
            return base + SNAP_SEP + root
        span = max(0.1, HAND_MAX_S["food"] - EAT_END_S - EAT_START_S)
        i = min(len(stages) - 1, int((age - EAT_START_S) / (span / len(stages))))
        return stages[i]

    def _next_step(self, at: float) -> Optional[Path]:
        if not self.seq:
            self.in_seq = False
            return None
        name, secs = self.seq.pop(0)
        self.resting = False
        self.current = name
        self.step_until = at + secs if secs else None
        return self.catalog.loops[name]

    def on_loop_end(self, at: Optional[float] = None) -> Path:
        """The current loop finished one pass. An animation repeats until its ACT_PASSES are used up and
        then he RESTS (on_tick starts the next one). After an event or an ending he rests too, unless the
        mood changed while it played. With a weapon out, see _held_loop_end."""
        at = self.clock() if at is None else at
        was_event, self.oneshot = self.oneshot, False
        if self.in_seq:
            if self.step_until is not None and at < self.step_until:
                return self.catalog.loops[self.current]     # a timed step keeps repeating
            nxt = self._next_step(at)
            if nxt is not None:
                return nxt
            was_event = True                                # ending done: back to a fresh mood loop
        if self.held:                                   # still holding it
            return self._held_loop_end(at)
        if self.mood is None:
            self.mood = UNKNOWN
        if self.current is None:
            return self._act(self.mood, at)
        if was_event:
            return self._settle(at)
        if self.resting:                                # a paused loop should not end; if one does, stay put
            return self.catalog.loops[self.current]
        self.passes_left -= 1
        if self.passes_left > 0:
            return self.catalog.loops[self.current]
        return self._rest(at)


SIM_PASS_S = 1.4     # one pass of a loop, for simulate(); the real loops are 0.8-1.4 s


def simulate(c: "LoopChooser", clock: list, seconds: float, mood: Optional[str] = "calm",
             step: float = 0.1) -> list:
    """Drive a chooser the way sprite_pal.py does, on a FAKE clock (clock[0], advanced here): a tick every
    second (on_tick, then on_mood), and a loop end each time a running loop finishes a pass; a resting
    loop is paused, so it never ends. Returns one row per step: (t, loop name, resting, held loop).
    No sleeping: an hour of Pico takes a moment. Where the running pass ends and when the next tick is due
    are kept on the chooser (c.sim), so the simulation can be advanced in several calls."""
    rows = []
    end = clock[0] + seconds
    sim = getattr(c, "sim", None)
    if sim is None:
        sim = c.sim = {"ends_at": None if (c.resting or c.current is None) else clock[0] + SIM_PASS_S,
                       "next_tick": clock[0]}

    def played(path):
        if path is not None:
            sim["ends_at"] = None if c.resting else clock[0] + SIM_PASS_S

    while clock[0] < end:
        t = clock[0]
        if t >= sim["next_tick"]:
            sim["next_tick"] += 1.0
            played(c.on_tick(t))
            played(c.on_mood(mood, at=t))
        if sim["ends_at"] is not None and t >= sim["ends_at"]:
            played(c.on_loop_end(t))
        rows.append((t, c.current, c.resting, c.held))
        clock[0] = round(t + step, 6)
    return rows


def _runs(rows) -> list:
    """Consecutive rows with the same (loop, resting) as [loop, resting, seconds, held]."""
    out = []
    for i, (t, name, resting, held) in enumerate(rows):
        if out and out[-1][0] == name and out[-1][1] == resting:
            continue
        if out:
            out[-1][2] = t - out[-1][2]
        out.append([name, resting, t, held])
    if out:
        out[-1][2] = rows[-1][0] - out[-1][2]
    return out


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
        n_pass = c.passes_left
        for _ in range(n_pass - 1):
            ck("a loop end with passes left repeats the same loop",
               c.on_loop_end(at=1.0).stem == p.stem and not c.resting)
        q = c.on_loop_end(at=2.0)
        ck("after its last pass (%d of at most %d) he rests on the mood's standing loop" % (n_pass, ACT_PASSES[1]),
           c.resting and 1 <= n_pass <= ACT_PASSES[1] and q.stem == REST_LOOPS["happy"]
           and REST_S[0] <= c.rest_until - 2.0 <= REST_S[1])
        ck("during the rest nothing new starts", c.on_tick(at=c.rest_until - 0.5) is None and c.resting)
        q = c.on_tick(at=c.rest_until + 0.5)
        ck("when the rest is over a DIFFERENT loop starts",
           q is not None and not c.resting and q.stem != p.stem and q.stem in MOOD_LOOPS["happy"])
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
        ck("a Picofied food holds its own prop, not a hot dog",
           food_variant("Food_tin_bogo_01_a") == FOOD_HOLD + "+bo-go-angeli-original"
           and food_variant("FOOD_SKEWERED_RAT_1_A") == FOOD_HOLD + "+aloprat-skewer")
        ck("every Picofied food is in the snap manifest",
           not _snap_props() or all(v in _snap_props() for v in FOOD_BY_ENTITY.values()))
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
        # the cap: once a gag has played, none plays again within GAG_COOLDOWN_S. J asked for an hour on
        # 2026-10-01 and for about four an hour on 2026-10-04; the 900 below is his number, written out.
        ch = LoopChooser(Catalog.scan(d), rng=random.Random(5))
        gags = set(RARE_LOOPS)
        mood = next((m for m, pool in ch.pools.items() if gags & set(pool)), None)
        if mood:
            t, first = 0.0, None
            for _ in range(5000):                       # picks every 10 s for ~14 hours
                ch.current = None
                if ch._pick(mood, at=t) in gags:
                    if first is not None and t - first < 900.0:    # J's quarter hour, not the constant under test
                        first = -1.0; break
                    first = t
                t += 10.0
            ck("no two gags within a quarter of an hour", first is not None and first >= 0)
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
        # wrapped foods (J 2026-10-03 "start them wrapped"): shown closed, then the opened art, then eaten
        cw = LoopChooser(Catalog.scan(d), rng=random.Random(4))
        wrapped = FOOD_HOLD + SNAP_SEP + "ma-s-ready-to-eat-fish-home-stew"
        if wrapped + "_eat_1" in cw.catalog.loops:
            cw.held = wrapped
            ck("a wrapped food is held closed, then opened, then eaten",
               cw._bite(0.5) == wrapped and cw._bite(EAT_START_S + 0.1) == wrapped + "_eat_1"
               and cw._bite(HAND_MAX_S["food"] - 0.1) == wrapped + "_eat_%d" % EAT_STAGES)
        else:
            ck("the opened stages of a wrapped food resolve (needs out/snap_props)", False)
        # the toy pose draws a random ship from the whole fleet, in the right pose
        ct = LoopChooser(Catalog.scan(d), rng=random.Random(11))
        fleet = [n for n in ct.catalog.loops if n.startswith("ship_claim_star_held+ship_")]
        if len(fleet) > 5:
            got = set()
            for _ in range(400):
                ct.current = None
                n = ct._pick("calm")
                if split_snap(n)[0] in TOY_POSES and (split_snap(n)[1] or "").startswith("ship_"):
                    got.add(n)
            ck("toy poses draw many different ships from the fleet (%d seen)" % len(got),
               len(got) >= 10 and all(n.endswith(TOY_POSES[split_snap(n)[0]]) for n in got))
        else:
            ck("the toy fleet resolves (needs out/snap_props with ship_* props)", False)
        # the Banu stall gag shows up on the Banu outfit only, and is a rare gag like the others
        bmm = "idle_shuffle_default+bmm_table"
        ck("the BMM stall is a rare gag in calm", bmm in MOOD_LOOPS["calm"] and bmm in RARE_LOOPS)
        ck("the BMM stall is Banu only",
           brand_allows(bmm, Path("x/pico_anim_sequences_banu")) and not brand_allows(bmm, Path("x/pico_anim_sequences"))
           and not brand_allows(bmm, Path("x/pico_anim_sequences_drake")))
        ck("the BMM merchant hawks with both flippers after idling",
           [st[0] for st in GAG_SEQS.get(bmm, [])] == [bmm, "idle_dance_happy+bmm_table"])
        cdr = LoopChooser(Catalog.scan(d), rng=random.Random(9))
        ck("Drake's chooser never draws the BMM stall", bmm not in cdr.pools["calm"])
        # the toy Gladius (J 2026-10-03): any idle, even in combat, so a plain pool entry -- never gated by the gag
        # roll or the shared cooldown -- and only on HELD frames, where the raised flipper is out to hold it
        sc, ss = "ship_claim_star_held+ship_gladius_claim", "scan_ping_default_held+ship_gladius_scan"
        ck("the toy ship is in the calm AND alert pools, and is not a rare gag",
           sc in MOOD_LOOPS["calm"] and ss in MOOD_LOOPS["calm"] and sc in MOOD_LOOPS["alert"]
           and sc not in RARE_LOOPS and ss not in RARE_LOOPS)
        ck("the toy ship never rides a grab or release cut (the flipper is down there)",
           not any(split_snap(n)[1] and split_snap(n)[1].startswith("ship_") and
                   split_snap(n)[0].endswith(("_grab", "_release")) for pool in MOOD_LOOPS.values() for n in pool))
        cs = LoopChooser(Catalog.scan(d), rng=random.Random(5))
        ck("both toy-ship loops resolve against the snap manifest (needs out/snap_props)",
           sc in cs.catalog.loops and ss in cs.catalog.loops)
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
        # idle signs (J 2026-10-04): no event needed; offered when a rest ends
        from pico import signs as _sg
        ck("a sad Pico holds up no idle sign either",
           cs.maybe_idle_sign(at=2e7) is None and cs.last_sign["why"] == "mood_veto")
        ci = LoopChooser(Catalog.scan(d), rng=random.Random(3)); ci.on_mood("calm")
        clk = [0.0]; ci.clock = lambda: clk[0]
        rows_i = simulate(ci, clk, 3600.0, mood="calm")
        sign_starts = [t for (t, name, _r, _h), prev in zip(rows_i[1:], rows_i[:-1])
                       if "_grab+sign_" in (name or "") and name != prev[1]]
        ck("with no game event at all, signs still come up over an hour (%d)" % len(sign_starts),
           3 <= len(sign_starts) <= 20)
        gaps = [b - a for a, b in zip(sign_starts, sign_starts[1:])]
        ck("idle signs keep their floor apart (shortest gap %.0fs)" % (min(gaps) if gaps else 0),
           bool(gaps) and min(gaps) >= _sg.IDLE_COOLDOWN_S)
        co = LoopChooser(Catalog.scan(d), rng=random.Random(3)); co.on_mood("calm")
        co.apply_prefs({"signs": False})
        clk2 = [0.0]; co.clock = lambda: clk2[0]
        ck("signs switched off: no idle sign in an hour either",
           not any("+sign_" in (name or "") for _t, name, _r, _h in simulate(co, clk2, 3600.0, mood="calm")))
        ch = LoopChooser(Catalog.scan(d), rng=random.Random(3)); ch.on_mood("calm")
        ch.held = "weapon_reload_happy_held+hotdog_1"
        ck("no idle sign while he is holding something", ch.maybe_idle_sign(at=5e7) is None)
        # gags are in the calm pool since 2026-10-04: over a few calm hours some play, and each ends in a rest
        lasts = {seq[-1][0] for seq in GAG_SEQS.values()}
        played, after_gag = 0, []
        for seed in range(4):
            cgq = LoopChooser(Catalog.scan(d), rng=random.Random(40 + seed)); cgq.on_mood("calm")
            cgq.apply_prefs({"signs": False})
            clkg = [0.0]; cgq.clock = lambda: clkg[0]
            rows_g = simulate(cgq, clkg, 2 * 3600.0, mood="calm")
            for (t, name, _r, _h), nxt in zip(rows_g[:-1], rows_g[1:]):
                if name in RARE_LOOPS and nxt[1] != name:
                    played += 1
                if name in lasts and nxt[1] != name:
                    after_gag.append(nxt[2])
        ck("calm hours: gags play without any happy mood (%d in 8 h)" % played, played >= 4)
        ck("after a gag is put away he rests (%d of %d)" % (sum(after_gag), len(after_gag)),
           bool(after_gag) and all(after_gag))
        after = [nxt[2] for (t, name, _r, _h), nxt in zip(rows_i[:-1], rows_i[1:])
                 if "_release+sign_" in (name or "") and nxt[1] != name]
        ck("after a sign is lowered he rests (%d of %d)" % (sum(after), len(after)), bool(after) and all(after))
        cq = LoopChooser(Catalog.scan(d), rng=random.Random(3))
        ck("no idle sign while his mood is unknown", cq.maybe_idle_sign(at=9e7) is None
           and (cq.on_mood(None) or True) and cq.maybe_idle_sign(at=9e7) is None)
        ck("every ending gets picked across seeds (%d of %d)" % (len(picks), len(BOMB_ENDINGS)),
           len(picks) == len(BOMB_ENDINGS))
        c3.on_hand(("draw", "slot1"))
        ck("a weapon has no cap and is kept", c3.expire(at=c3.held_since + 3600) is None and c3.held)
        # ---- RESTS (J 2026-10-04). Everything below runs on a fake clock. ----
        clk = [1000.0]
        cr = LoopChooser(Catalog.scan(d), rng=random.Random(21), clock=lambda: clk[0])
        cr.apply_prefs({"signs": False, "gags": False})   # these checks are about idles; a sign or a gag is a held
                                                           # sequence of several loops (gags are in calm since 10-04)
        rows = simulate(cr, clk, 2 * 3600.0)
        runs = _runs(rows)
        rest_share = sum(1 for r in rows if r[2]) / float(len(rows))
        acts = [r for r in runs if not r[1]]
        rests = [r for r in runs if r[1]]
        ck("two idle hours: he is standing still most of the time (%.0f%%), yet does %d things"
           % (100 * rest_share, len(acts)), rest_share > 0.7 and len(acts) > 150)
        longest = max((r[2] for r in acts), default=0.0)
        ck("no animation repeats past its bound (longest run %.1f s, bound %d passes of %.1f s)"
           % (longest, ACT_PASSES[1], SIM_PASS_S), longest <= ACT_PASSES[1] * SIM_PASS_S + 0.3)
        ck("every rest lasts REST_S, about (%.0f-%.0f s seen)" % (min((r[2] for r in rests[:-1]), default=0),
                                                               max((r[2] for r in rests[:-1]), default=0)),
           len(rests) > 2 and all(REST_S[0] - 0.2 <= r[2] <= REST_S[1] + 1.2 for r in rests[:-1]))
        ck("a rest is drawn on the mood's standing loop, with no prop",
           all(r[0] == REST_LOOPS["calm"] for r in rests))
        bases = [split_snap(r[0])[0] for r in acts]
        ck("across rests the same idle still never plays twice in a row",
           all(a != b for a, b in zip(bases, bases[1:])) and len(set(bases)) >= 6)
        ck("an animation is always followed by a rest, not by another animation",
           all(runs[i + 1][1] for i, r in enumerate(runs[:-1]) if not r[1]))
        # an event, or a new mood, in the middle of a rest plays at once
        clk = [5000.0]
        ce = LoopChooser(Catalog.scan(d), rng=random.Random(22), clock=lambda: clk[0])
        ce.apply_prefs({"signs": False})          # a sign may replace the gesture; this check is about the gesture
        simulate(ce, clk, 60.0)
        for _ in range(600):                      # bounded: a broken chooser must fail a check, not hang
            if ce.resting:
                break
            simulate(ce, clk, 1.0)
        ck("(set-up) he is resting, with rest time left", ce.resting and ce.rest_until > clk[0] + 2)
        g = ce.on_event("qt_arrived", at=clk[0])
        ck("an event during a rest plays at once", g is not None and g.stem == "quantum_drop_default"
           and not ce.resting and ce.oneshot)
        ck("and when it is over he rests again", ce.on_loop_end(at=clk[0] + 1.4) is not None and ce.resting)
        g = ce.on_mood("hurt", at=clk[0] + 2)
        ck("a mood change during a rest plays the new mood at once",
           g is not None and not ce.resting and ce.current in MOOD_LOOPS["hurt"])
        ce.sim = None                             # driven by hand above: let the simulation pick him up afresh
        rows = simulate(ce, clk, 120.0, mood="hurt")
        ck("a hurt Pico rests on the hurt face, not the calm one",
           {r[1] for r in rows if r[2]} == {REST_LOOPS["hurt"]})
        clk = [6000.0]
        cu = LoopChooser(Catalog.scan(d), rng=random.Random(23), clock=lambda: clk[0])
        rows = simulate(cu, clk, 120.0, mood=None)
        ck("UNKNOWN rests confused, never calm", {r[1] for r in rows} == {"confused_confused"}
           and any(r[2] for r in rows) and any(not r[2] for r in rows))
        # a weapon out: held still, an idle now and then, back to the weapon, and the holster still works
        for part in ("_grab", "_release"):
            (Path(d) / ("weapon_draw_focused" + part + ".gif")).write_bytes(b"GIF89a")
        for cuts in (True, False):
            if not cuts:
                for part in ("_grab", "_release"):
                    (Path(d) / ("weapon_draw_focused" + part + ".gif")).unlink()
            tag = "with put-away cuts" if cuts else "outfit without cuts"
            clk = [9000.0]
            cw = LoopChooser(Catalog.scan(d), rng=random.Random(24), clock=lambda: clk[0])
            cw.on_mood("calm", at=clk[0])
            held = HAND_LOOPS["slot1"]
            cw.on_hand(("draw", "slot1"), at=clk[0])
            rows = simulate(cw, clk, 1800.0)
            runs = _runs(rows)
            names = [r[0] for r in runs]
            calm_bases = {split_snap(n)[0] for n in MOOD_LOOPS["calm"]}
            idles = [i for i, r in enumerate(runs) if split_snap(r[0])[0] in calm_bases and "pistol" not in r[0]]
            still = sum(r[2] for r in runs if r[0] == held and r[1])
            ck("weapon out 30 min (%s): he leaves the held pose for %d idles and comes back each time"
               % (tag, len(idles)),
               len(idles) >= 40 and cw.held == held and all(r[3] == held for r in rows)
               and all(held in names[i + 1:i + 3] for i in idles if i < len(names) - 2))
            ck("weapon out (%s): mostly he holds it still (%.0f%% of the time)" % (tag, 100 * still / 1800.0),
               still / 1800.0 > 0.6)
            ck("weapon out (%s): no loop runs past its bound (longest %.1f s)"
               % (tag, max((r[2] for r in runs if not r[1]), default=0.0)),
               max((r[2] for r in runs if not r[1]), default=0.0) <= ACT_PASSES[1] * SIM_PASS_S + 0.3)
            ck("weapon out (%s): the idle is never a gag, nor the empty-handed weapon pose" % tag,
               not any(n in RARE_LOOPS or split_snap(n)[0] == "weapon_draw_focused" for n in names))
            rel, grab = "weapon_draw_focused_release+pistol_white", "weapon_draw_focused_grab+pistol_white"
            if cuts:
                ck("weapon out: it is put away (release cut) before the idle and taken out (grab cut) after",
                   all(names[i - 1] == rel and names[i + 1] == grab for i in idles if 0 < i < len(names) - 1))
            else:
                ck("an outfit without the cuts still takes its idle breaks", rel not in names and grab not in names)
            for _ in range(3000):                    # holster in the MIDDLE of an idle break
                if cw.break_phase == "idle":
                    break
                simulate(cw, clk, 0.1)
            ck("(set-up, %s) he is in the idle part of a break" % tag, cw.break_phase == "idle")
            back = cw.on_hand(("holster", None), at=clk[0])
            cw.sim = None
            ck("holstering (%s, mid-break) puts the prop away for good" % tag,
               back is not None and cw.held is None and cw.prop_for() is None and cw.break_phase is None)
            rows = simulate(cw, clk, 300.0)
            ck("after the holster the weapon never comes back (%s)" % tag,
               not any("pistol" in (r[1] or "") or r[3] for r in rows))
        clk = [20000.0]
        cw = LoopChooser(Catalog.scan(d), rng=random.Random(25), clock=lambda: clk[0])
        cw.on_mood("calm", at=clk[0]); cw.on_hand(("draw", "slot2"), at=clk[0])
        for _ in range(600):
            if cw.resting:
                break
            simulate(cw, clk, 0.5)
        back = cw.on_hand(("holster", None), at=clk[0])
        ck("holstering while he holds it still puts the prop away",
           back is not None and cw.held is None and cw.prop_for() is None and cw.resting
           and cw.current == REST_LOOPS["calm"])
        clk = [30000.0]
        cb = LoopChooser(Catalog.scan(d), rng=random.Random(26), clock=lambda: clk[0])
        cb.on_mood("calm", at=clk[0]); cb.on_hand(("draw", "bomb"), at=clk[0])
        rows = simulate(cb, clk, 7.5)
        ck("a bomb gets no idle break: held, then the alert, as before",
           {r[1] for r in rows} == {HAND_LOOPS["bomb"], BOMB_ALERT_LOOP} and not any(r[2] for r in rows))
        cp.apply_prefs({"liveliness": "calm"})
        calm_scale = cp.rest_scale
        cp.apply_prefs({"liveliness": "lively"})
        lively_scale = cp.rest_scale
        cp.apply_prefs({"liveliness": 7})
        ck("the How-lively setting scales the rests, and a bad value means normal",
           calm_scale == LIVELINESS["calm"] > 1.0 > LIVELINESS["lively"] == lively_scale and cp.rest_scale == 1.0)
        clk = [40000.0]
        cl = LoopChooser(Catalog.scan(d), rng=random.Random(21), clock=lambda: clk[0])
        cl.apply_prefs({"liveliness": "lively"})
        rows = simulate(cl, clk, 3600.0)
        lively_share = sum(1 for r in rows if r[2]) / float(len(rows))
        ck("lively rests less than normal (%.0f%% of the time vs %.0f%%)" % (100 * lively_share, 100 * rest_share),
           lively_share < rest_share - 0.1)
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
        cp = LoopChooser(Catalog.scan(real), rng=random.Random(11))
        ck("every prop-idle prop is in the snap manifest",
           not cp.snap_props or all(pid in cp.snap_props for pids in PROP_IDLES.values() for pid in pids))
        picks, repeats = [], 0
        for _ in range(3000):
            prev = cp.current
            cp.current = cp._pick("calm")
            if prev and split_snap(prev)[0] == split_snap(cp.current)[0]:
                repeats += 1
            picks.append(cp.current)
        propped = [n for n in picks if split_snap(n)[0] in PROP_IDLES and split_snap(n)[1]]
        kinds = {n for n in propped}
        ck("prop idles turn up, in many kinds, but stay a minority of calm (%d of %d, %d kinds)"
           % (len(propped), len(picks), len(kinds)),
           len(kinds) >= 20 and 0.05 < len(propped) / len(picks) < 0.35)
        ck("the same idle never plays twice in a row, even with a different prop (%d)" % repeats, repeats == 0)
        cr = LoopChooser(Catalog.scan(real), rng=random.Random(5))
        skewer = FOOD_HOLD + SNAP_SEP + "aloprat-skewer"
        if skewer + "_eat_1" in cr.catalog.loops:
            cr.on_hand(("draw", "food"), item="Food_Skewered_Rat_1_a")
            t0 = cr.held_since
            seen = [cr.held]
            for dt in (1.0, 1.6, 2.5, 3.4, 4.3, 5.2):
                cr.expire(at=t0 + dt)
                seen.append(cr.held)
            ck("an eaten skewer: whole first, then bites in order, no stage skipped (%s)"
               % [x.split("+")[-1] if x else x for x in seen],
               seen[0] == skewer and seen[1] == skewer
               and [x for i, x in enumerate(seen) if i == 0 or x != seen[i - 1]]
               == [skewer] + [skewer + "_eat_%d" % i for i in range(1, EAT_STAGES + 1)])
            cr.expire(at=t0 + HAND_MAX_S["food"] + 0.1)
            ck("an eaten food is put away at its cap", cr.held is None)
            cr.on_hand(("draw", "food"), item="Food_hotdog_01_chili_a")
            ck("a hot dog (no bite stages) is just held", cr.expire(at=cr.held_since + 3) is None
               and cr.held == FOOD_HOLD + SNAP_SEP + "hotdog_3")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(selftest())
