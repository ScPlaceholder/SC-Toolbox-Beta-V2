"""How often the eyes take a picture depends on what the pilot is doing (J, 2026-10-05).

"Once every 5 minutes for salvage ... For mining 5 by default or trigger when a reliable capture has been fed by the
mining reader with a cooldown period so it's not spamming the player or GPU 85 times in 3 minutes. For combat
missions 2 minutes by default. Sandbox activities also 5 minutes by default. These sliders should be user selected
from 5 seconds to 120 [minutes] maximum with a never checkbox next to each category." And: "There should also be
cooldown periods for chatting about what it sees with a chattiness slider for that as well."

The real CompanionCore, the real eyes.Eyes with its own rules, and the real settings loader, fed a fake clock, a
fake screen, a fake foreground window and a fake vision model. No screen is captured and no model runs.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import eyes as eyes_mod
import pacing
import picture_pace as pp
import settings as st
from _eyes_helpers import FakeEyes, make_core, spoke, tick

BOUNTY = {"mission_id": "m1", "mission_name": "Verified Bounty: Harry Batey | Extreme-Risk Target"}
SALVAGE = {"mission_id": "m2", "mission_name": "Claim #30040: Crusader M2 Hercules Starlifter Salvage Rights"}
MINING = {"mission_id": "m3", "mission_name": "XS Purchase Order: Ship Mined Ore"}
CARGO = {"mission_id": "m4", "mission_name": "Covalex Cargo Haul: Medical Supplies"}


# ---------------------------------------------------------------------------------------------------------------
# A. the settings
# ---------------------------------------------------------------------------------------------------------------
def test_the_defaults_are_the_ones_he_gave():
    d = st.DEFAULTS
    assert [d[pp.every_key(a)] for a in ("salvage", "mining", "combat_mission", "sandbox")] == [300, 300, 120, 300]
    assert all(d[pp.never_key(a)] is False for a in pp.ACTIVITIES)
    assert (pp.MIN_EVERY_S, pp.MAX_EVERY_S) == (5.0, 7200.0)


def test_an_old_settings_file_loads_with_the_defaults_and_nothing_else_changed(tmp_path, monkeypatch):
    old = {"presence": "curious", "vision_glance": True, "chattiness": 4, "muted": True, "volume_elah": 0.7,
           "pilot_id": "someone", "chat": False}                                # a file from before today
    monkeypatch.setattr(st, "PATH", tmp_path / "settings.json")
    (tmp_path / "settings.json").write_text(json.dumps(old), encoding="utf-8")
    s = st.load()
    new = set(pp.defaults()) | {"companions_enabled", "eyes_chattiness", "eyes_pictures_per_hour", "eyes_look_gap_s"}
    assert {k: s[k] for k in new} == {k: st.DEFAULTS[k] for k in new}
    assert all(s[k] == v for k, v in old.items())                              # what the file said still stands
    untouched = {k: v for k, v in s.items() if k not in new and k not in old and k != "talk_key"}
    assert untouched == {k: st.DEFAULTS[k] for k in untouched}
    assert s["companions_enabled"] is True and s["eyes_pictures_per_hour"] is None


@pytest.mark.parametrize("saved, loaded", [(1, 5.0), (4.99, 5.0), (5, 5.0), (120, 120.0), (7200, 7200.0),
                                           (7201, 7200.0), (10 ** 9, 7200.0), (-3, 5.0), ("soon", 300.0),
                                           (None, 300.0), (float("nan"), 300.0)])
def test_an_interval_is_put_inside_five_seconds_to_two_hours_on_load(tmp_path, monkeypatch, saved, loaded):
    monkeypatch.setattr(st, "PATH", tmp_path / "settings.json")
    (tmp_path / "settings.json").write_text(json.dumps({pp.every_key("salvage"): saved, pp.never_key("mining"): "yes"}),
                                            encoding="utf-8")
    s = st.load()
    assert s[pp.every_key("salvage")] == loaded
    assert s[pp.never_key("mining")] is False                 # only a real true is "never"


def test_the_slider_steps_finely_at_the_bottom_and_coarsely_at_the_top():
    stops = pp.STOPS
    assert stops[0] == 5 and stops[-1] == 7200 and list(stops) == sorted(set(stops))
    assert 120 in stops and 300 in stops                      # 2 and 5 minutes are exact stops
    assert stops[1] - stops[0] <= 5 and stops[-1] - stops[-2] >= 900
    assert all(pp.stop_index(v) == i for i, v in enumerate(stops))
    assert pp.STOPS[pp.stop_index(7)] == 5 and pp.STOPS[pp.stop_index(10 ** 6)] == 7200
    assert [pp.label(v) for v in (5, 90, 120, 300, 5400, 7200)] == ["5 s", "90 s", "2 min", "5 min", "1 h 30 min", "2 h"]


# ---------------------------------------------------------------------------------------------------------------
# what the pilot is doing, and what cannot be told
# ---------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("contract, activity", [(BOUNTY, "combat_mission"), (SALVAGE, "salvage"), (MINING, "mining"),
                                                (CARGO, "sandbox")])
def test_an_open_contract_says_what_he_is_doing(contract, activity):
    tr = pp.ActivityTracker()
    assert tr.activity()[0] == "sandbox"
    tr.note_event("contract_accepted", contract)
    assert tr.activity()[0] == activity
    tr.note_event("contract_complete", contract)
    assert tr.activity()[0] == "sandbox"


def test_the_last_contract_accepted_decides_and_a_mining_display_on_screen_decides_over_it():
    tr = pp.ActivityTracker()
    tr.note_event("contract_accepted", SALVAGE)
    tr.note_event("contract_accepted", BOUNTY)
    assert tr.activity("cockpit")[0] == "combat_mission"
    assert tr.activity("mining")[0] == "mining"
    tr.note_event("contract_failed", BOUNTY)
    assert tr.activity()[0] == "salvage"


def test_a_contract_that_ended_in_the_log_or_a_new_session_no_longer_counts():
    tr = pp.ActivityTracker()
    tr.note_event("contract_accepted", BOUNTY)
    tr.on_line("<2025-12-19T02:27:15.983Z> [Notice] <EndMission> Ending mission for player. MissionId[m1] Player[x] "
               "CompletionType[Abandon] Reason[Player left]")
    assert tr.activity()[0] == "sandbox"
    tr.note_event("contract_accepted", SALVAGE)
    tr.note_event("join_pu", {})
    assert tr.activity()[0] == "sandbox"


def test_what_cannot_be_detected_is_sandbox_not_a_guess():
    tr = pp.ActivityTracker()
    for scene in (None, "cockpit", "combat", "trading", "on_foot", "other"):     # a fight on screen is not a mission
        assert tr.activity(scene)[0] == "sandbox"
    tr.note_event("reward_earned", {"amount": 5000})
    tr.note_event("location_change", {"location_name": "a salvage yard"})
    assert tr.activity()[0] == "sandbox"


# ---------------------------------------------------------------------------------------------------------------
# A. the core takes a picture when the activity's interval says so
# ---------------------------------------------------------------------------------------------------------------
def test_a_picture_is_taken_when_the_interval_has_passed_and_not_before():
    clock, eyes = [1000.0], FakeEyes()
    core = make_core(eyes, clock)
    tick(core)
    assert eyes.looks == ["interval"]                         # the first one: no picture yet
    for _ in range(99):
        clock[0] += 3.0
        tick(core)
    assert len(eyes.looks) == 1                               # 297 s: not yet (sandbox is 300)
    clock[0] += 3.0
    tick(core)
    assert len(eyes.looks) == 2


def test_a_combat_mission_is_every_two_minutes_and_salvage_every_five():
    clock, eyes = [1000.0], FakeEyes()
    core = make_core(eyes, clock)

    class Ev:
        event_type, data = "contract_accepted", dict(BOUNTY)
    core.on_event(Ev())
    tick(core)
    clock[0] += 119.0
    tick(core)
    assert len(eyes.looks) == 1 and core.doing_now == "combat_mission"
    clock[0] += 1.0
    tick(core)
    assert len(eyes.looks) == 2
    Ev.event_type = "contract_complete"
    core.on_event(Ev())
    Ev.event_type, Ev.data = "contract_accepted", dict(SALVAGE)
    core.on_event(Ev())
    clock[0] += 299.0
    tick(core)
    assert len(eyes.looks) == 2 and core.doing_now == "salvage"
    clock[0] += 1.0
    tick(core)
    assert len(eyes.looks) == 3


def test_the_eyes_own_count_of_the_last_picture_is_honoured():
    clock, eyes = [1000.0], FakeEyes()
    core = make_core(eyes, clock)
    eyes.state = lambda: {"scene": "cockpit", "transitions": 0, "picture_age_s": 12.0}   # they glanced 12 s ago
    tick(core)
    assert eyes.looks == []
    eyes.state = lambda: {"scene": "cockpit", "transitions": 0, "picture_age_s": 300.0}
    tick(core)
    assert eyes.looks == ["interval"]


def test_never_means_no_picture_of_their_own_accord_whatever_happens():
    clock, eyes = [1000.0], FakeEyes()
    core = make_core(eyes, clock, pace=pp.PicturePace({pp.never_key("sandbox"): True}, now=lambda: clock[0]))

    class Ev:
        event_type, data = "location_change", {"location_name": "Area18"}
    core.on_event(Ev())                                       # an arrival used to look at once
    for _ in range(50):
        clock[0] += 600.0
        tick(core)
    assert eyes.looks == [] and eyes.paces[-1] == (None, True, "")


def test_a_hook_waits_for_the_interval_and_is_dropped_when_its_moment_has_passed():
    clock, eyes = [1000.0], FakeEyes()
    core = make_core(eyes, clock)
    tick(core)

    class Ev:
        event_type, data = "location_change", {"location_name": "Area18"}
    clock[0] += 290.0
    core.on_event(Ev())
    tick(core)
    assert len(eyes.looks) == 1                               # 290 s: the arrival does not jump the interval
    clock[0] += 10.0
    tick(core)
    assert eyes.looks == ["interval", "arrival"]              # asked 10 s ago: still about the arrival
    core.on_event(Ev())
    clock[0] += 300.0
    tick(core)
    assert eyes.looks[-1] == "interval"                       # asked five minutes ago: an ordinary picture now


def test_the_core_tells_the_eyes_the_pace_and_a_fight_holds_every_picture():
    clock, eyes = [1000.0], FakeEyes()
    core = make_core(eyes, clock)
    tick(core)
    assert eyes.paces == [(300.0, False, "")]
    clock[0] += 400.0
    core.gate_state.in_combat = True
    tick(core)
    assert eyes.paces[-1] == (300.0, False, "combat") and len(eyes.looks) == 1     # due, and not taken
    core.gate_state.in_combat = False
    clock[0] += 3.0
    tick(core)
    assert eyes.paces[-1] == (300.0, False, "") and len(eyes.looks) == 2


# ---------------------------------------------------------------------------------------------------------------
# A. the eyes' own rules still stand: the interval is one more condition, never one fewer
# ---------------------------------------------------------------------------------------------------------------
def _eyes(foreground="StarCitizen.exe", headroom="OK", presence="occasional", **kw):
    from PIL import Image
    import random
    calls, clock, rng = [], [0.0], random.Random(3)

    def grab():                                              # a different frame every time: the classifier is unsure
        return Image.frombytes("RGB", (64, 36), bytes(rng.randrange(256) for _ in range(64 * 36 * 3)))
    e = eyes_mod.Eyes(presence=presence, grab=grab, foreground=lambda: foreground, headroom=lambda: headroom,
                      glance=lambda j: calls.append(1) or {"scene": "other", "confidence": 0.2, "notable": "a ship"},
                      now=lambda: clock[0], **kw)
    return e, calls, clock


def test_the_eyes_own_glances_keep_to_the_interval():
    e, calls, clock = _eyes(presence="curious")
    e.set_pace(300.0)
    for _ in range(100):                                     # 500 s of ticks
        clock[0] += 5.0
        e.tick()
    assert len(calls) == 2                                   # at 5 s and at 305 s; unpaced it would be every 45 s
    e2, calls2, clock2 = _eyes(presence="curious")
    for _ in range(100):
        clock2[0] += 5.0
        e2.tick()
    assert len(calls2) == 12                                 # no pace set: exactly as before, one per 45 s gap


@pytest.mark.parametrize("kw, why", [({"foreground": "chrome.exe"}, "the game is not in front"),
                                     ({"headroom": "TIGHT"}, "no room on the card")])
def test_the_shortest_interval_does_not_get_past_the_game_in_front_rule_or_headroom(kw, why):
    e, calls, clock = _eyes(**kw)
    e.set_pace(pp.MIN_EVERY_S)
    for _ in range(60):
        clock[0] += 5.0
        e.tick()
        assert e.look("interval") is None, why
    assert calls == [], why


def test_the_shortest_interval_does_not_get_past_the_hourly_cap():
    e, calls, clock = _eyes(presence="occasional")
    e.set_pace(pp.MIN_EVERY_S)
    for _ in range(200):                                     # 1000 s
        clock[0] += 5.0
        e.tick()
    assert len(calls) == eyes_mod.GLANCES_PER_HOUR["occasional"] == 4


def test_never_and_a_hold_stop_the_eyes_own_glances():
    for pace in ({"never": True}, {"hold": "combat"}, {"hold": "overload"}, {"hold": "something new"}):
        e, calls, clock = _eyes(presence="curious")
        e.set_pace(5.0, **pace)
        for _ in range(60):
            clock[0] += 5.0
            e.tick()
        assert calls == [], pace


def test_the_pace_reaches_the_eyes_in_the_model_service_over_http():
    import companion_service as svc
    e, _, _ = _eyes()
    httpd = svc.serve(svc.Service(realizer=object(), eyes=e, log=lambda m: None), 0)
    try:
        remote = svc.RemoteEyes(f"http://127.0.0.1:{httpd.server_address[1]}", timeout=3.0)
        assert remote.set_pace(120.0, never=False, hold="combat") is True
        assert (e._pace_interval, e._pace_never, e._hold, e._pace_set) == (120.0, False, "combat", True)
        assert remote.set_pace(None, never=True) is True and (e._pace_interval, e._pace_never, e._hold) == (120.0, True, "")
        assert remote.state()["pictures_held"] == ""
    finally:
        httpd.shutdown()
        httpd.server_close()


# ---------------------------------------------------------------------------------------------------------------
# B. the mining reader's early trigger, and its cooldown
# ---------------------------------------------------------------------------------------------------------------
def test_a_reliable_mining_capture_takes_a_picture_ahead_of_the_interval():
    clock, eyes = [1000.0], FakeEyes()
    core = make_core(eyes, clock)
    tick(core)
    clock[0] += 100.0                                        # 200 s before the next one is due
    tick(core)
    assert len(eyes.looks) == 1
    assert core.mining_capture() is True
    tick(core)
    assert eyes.looks == ["interval", "mining_capture"]


def test_eighty_five_captures_in_three_minutes_take_two_pictures():
    clock, eyes = [1000.0], FakeEyes()
    core = make_core(eyes, clock)
    core._last_look_t = clock[0] - 1000.0                    # the last picture was long ago
    for _ in range(85):
        core.mining_capture()
        tick(core)
        clock[0] += 180.0 / 85
    assert eyes.looks.count("mining_capture") == 2 and core.pace.captures_held == 83
    assert st.DEFAULTS[pp.MINING_COOLDOWN_KEY] == 90.0


def test_the_cooldown_between_captures_holds_without_any_other_picture():
    clock = [0.0]
    pace = pp.PicturePace(now=lambda: clock[0])
    got = []
    for _ in range(85):
        got.append(pace.mining_capture())                    # the pace alone: no core, no picture age
        clock[0] += 180.0 / 85
    assert got.count(True) == 2 and got[0] is True and pace.captures_held == 83
    clock[0] += 90.0
    assert pace.mining_capture() is True


def test_a_capture_right_after_any_picture_waits_and_an_unreliable_one_never_counts():
    clock, eyes = [1000.0], FakeEyes()
    core = make_core(eyes, clock)
    tick(core)                                               # a picture, just now
    clock[0] += 30.0
    assert core.mining_capture() is False                    # 30 s after a picture: inside the cooldown
    clock[0] += 100.0
    assert core.mining_capture(reliable=False) is False
    assert core.mining_capture() is True


def test_mining_set_to_never_takes_no_capture_and_a_fight_holds_the_picture():
    clock = [1000.0]
    pace = pp.PicturePace({pp.never_key("mining"): True}, now=lambda: clock[0])
    assert pace.mining_capture() is False
    eyes = FakeEyes()
    core = make_core(eyes, clock)
    core._last_look_t = clock[0] - 1000.0
    core.gate_state.in_combat = True
    assert core.mining_capture() is True                     # asked for
    tick(core)
    assert eyes.looks == []                                  # and not taken while the fight is on


def test_nothing_calls_the_mining_trigger_yet():
    """There is no channel from the mining reader into SuitMk2. If someone wires one, this says so."""
    root = Path(pp.__file__).resolve().parents[1]
    callers = []
    for p in list((root / "core").glob("*.py")) + list((root / "ui").glob("*.py")) + list(root.glob("*.py")):
        src = p.read_text(encoding="utf-8")
        n = src.count(".mining_capture(")
        if p.name == "picture_pace.py":
            n -= src.count("p.mining_capture(") + src.count("PicturePace.mining_capture(")   # its selftest, its docstring
        if p.name == "companion_core.py":
            n -= 1                                           # CompanionCore.mining_capture handing it to the pace
        if n:
            callers.append(p.name)
    assert callers == []


# ---------------------------------------------------------------------------------------------------------------
# G. talking about what they saw is its own dial
# ---------------------------------------------------------------------------------------------------------------
def test_the_eye_talk_dial_is_separate_from_the_picture_interval(tmp_path):
    clock, eyes = [1000.0], FakeEyes()
    core = make_core(eyes, clock, feedback_dir=tmp_path)
    assert core.eye_chattiness == 2 == st.DEFAULTS["eyes_chattiness"] and pacing.eye_talk_gap_s(2) == 240.0
    core.pace.configure({pp.every_key("sandbox"): 5.0})      # a picture every five seconds
    tick(core)
    assert len(core.considered) == 1
    spoke(core, core.considered[0])                          # it was said
    for _ in range(10):
        clock[0] += 5.0
        tick(core)
    assert len(eyes.looks) == 11 and len(core.considered) == 1          # ten more pictures, not one more remark
    clock[0] += 240.0
    tick(core)
    assert len(core.considered) == 2


def test_the_wait_counts_from_a_line_that_was_said_not_one_that_was_only_tried(tmp_path):
    clock, eyes = [1000.0], FakeEyes()
    core = make_core(eyes, clock, feedback_dir=tmp_path)
    core.pace.configure({pp.every_key("sandbox"): 5.0})
    for _ in range(3):
        tick(core)
        clock[0] += 5.0
    assert len(core.considered) == 3                         # none was spoken, so none started the wait


@pytest.mark.parametrize("level, remarks", [(0, 0), (4, 6)])
def test_silent_takes_pictures_and_says_nothing_and_very_chatty_does_not_wait(tmp_path, level, remarks):
    clock, eyes = [1000.0], FakeEyes()
    core = make_core(eyes, clock, feedback_dir=tmp_path, eye_chattiness=level)
    core.pace.configure({pp.every_key("sandbox"): 5.0})
    for _ in range(6):
        tick(core)
        for spec in core.considered:
            spoke(core, spec)
        clock[0] += 5.0
    assert len(eyes.looks) == 6 and len(core.considered) == remarks


def test_the_dial_can_be_moved_while_running_and_out_of_range_is_clamped():
    core = make_core(None)
    core.set_eye_chattiness(99)
    assert core.eye_chattiness == 4
    core.set_eye_chattiness("x")
    assert core.eye_chattiness == 2
    assert [pacing.eye_talk_gap_s(i) for i in range(5)] == [None, 600.0, 240.0, 90.0, 0.0]


# ---------------------------------------------------------------------------------------------------------------
# I. no ceiling chosen for the pilot: the cap and the look gap are settings, with the old values as defaults
# ---------------------------------------------------------------------------------------------------------------
def test_the_hourly_cap_and_the_look_gap_default_to_what_they_were():
    assert st.DEFAULTS["eyes_pictures_per_hour"] is None and st.DEFAULTS["eyes_look_gap_s"] == eyes_mod.LOOK_MIN_GAP_S
    e, calls, clock = _eyes(presence="present")
    assert e._cap() == eyes_mod.GLANCES_PER_HOUR["present"] == 12 and e.look_gap_s == 20.0
    clock[0] += 100.0
    assert e.look("interval") == "a ship"
    clock[0] += 19.0
    e.notable_at = None                                      # not the "just described this frame" shortcut
    assert e.look("interval") is None                        # inside the 20 s gap
    clock[0] += 1.0
    assert e.look("interval") == "a ship"


def test_a_pilot_may_set_both_as_high_as_he_likes():
    e, calls, clock = _eyes(presence="occasional", per_hour=5000, look_gap_s=0.0)
    for _ in range(300):
        clock[0] += 11.0                                     # past the 10 s "just described" shortcut
        assert e.look("interval") == "a ship"
    assert len(calls) == 300                                 # 'occasional' alone would have stopped at 4


@pytest.mark.parametrize("saved, loaded", [(None, None), (500, 500), (0, None), (-4, None), ("lots", None),
                                           (True, None), (12.9, 12)])
def test_the_saved_cap_is_a_number_or_follows_presence(tmp_path, monkeypatch, saved, loaded):
    monkeypatch.setattr(st, "PATH", tmp_path / "settings.json")
    (tmp_path / "settings.json").write_text(json.dumps({"eyes_pictures_per_hour": saved, "eyes_look_gap_s": -1,
                                                        "eyes_chattiness": 9}), encoding="utf-8")
    s = st.load()
    assert s["eyes_pictures_per_hour"] == loaded and s["eyes_look_gap_s"] == 20.0 and s["eyes_chattiness"] == 4
