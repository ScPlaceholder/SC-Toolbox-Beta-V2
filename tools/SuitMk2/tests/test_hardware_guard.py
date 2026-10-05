"""The hard limit: the companions never cost the game its machine, at any setting (J, 2026-10-05).

"Our hardware monitoring should prevent eyes or chat during important moments and if the card runs too hard disable
them completely." Asked whether a user should be able to switch that off, he said no.

So: while headroom is TIGHT or a fight is on, no picture is taken and no chat model is asked, with every dial at its
loudest and every interval at its shortest; and when TIGHT lasts, eyes and chat are switched off until the PC has
recovered, and the pilot is told why once, in the window.

The real core, the real eyes.Eyes, the real chat_talker.Talker and the real guard, with a fake clock, a fake screen
and a stand-in for the request to the model that fails the test if it is ever made. No model runs.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

import chat_talker as ct
import conversation as conv
import eyes as eyes_mod
import hardware_guard as hg
import picture_pace as pp
import settings as st
from _eyes_helpers import FakeEyes, make_core, tick

LOUDEST = {"eye_chattiness": 4, "chattiness": 4}


def _guard(clock, **kw):
    return hg.OverloadGuard(now=lambda: clock[0], **kw)


def _run(g, clock, reading, seconds, step=5.0):
    for _ in range(int(seconds / step)):
        clock[0] += step
        g.feed(reading)


# ---------------------------------------------------------------------------------------------------------------
# the guard
# ---------------------------------------------------------------------------------------------------------------
def test_tight_that_lasts_switches_eyes_and_chat_off():
    clock = [0.0]
    g = _guard(clock)
    g.feed("TIGHT")
    _run(g, clock, "TIGHT", hg.OVERLOAD_AFTER_S - 5)
    assert g.off is False                                    # 115 s: not yet
    _run(g, clock, "TIGHT", 5)
    assert g.off is True and g.trips == 1
    assert (hg.OVERLOAD_AFTER_S, hg.RECOVER_AFTER_S) == (120.0, 60.0)


@pytest.mark.parametrize("spike_s", [5, 30, 60, 115])
def test_a_spike_does_not(spike_s):
    clock = [0.0]
    g = _guard(clock)
    for _ in range(20):                                      # spike after spike, each with a clear reading between
        g.feed("TIGHT")
        _run(g, clock, "TIGHT", spike_s)
        _run(g, clock, "OK", 5)
    assert g.off is False and g.trips == 0 and g.take_notice() is None


def test_it_comes_back_only_after_the_reading_has_stayed_clear():
    clock = [0.0]
    g = _guard(clock)
    g.feed("TIGHT")
    _run(g, clock, "TIGHT", 130)
    assert g.off
    for _ in range(6):                                       # clear for 55 s, TIGHT again, over and over: no flapping
        _run(g, clock, "OK", 55)
        _run(g, clock, "TIGHT", 5)
        assert g.off
    _run(g, clock, "ROOMY", 55)
    assert g.off is True                                     # 55 s clear: not yet
    _run(g, clock, "ROOMY", 10)
    assert g.off is False
    _run(g, clock, "TIGHT", 60)
    assert g.off is False and g.trips == 1                   # and a spike after recovery is again only a spike


def test_the_pilot_is_told_once_per_switch_off_and_once_when_it_is_back():
    clock = [0.0]
    g = _guard(clock)
    notices = []
    for reading, seconds in (("TIGHT", 400), ("OK", 200), ("TIGHT", 400), ("OK", 200)):
        for _ in range(int(seconds / 5)):
            clock[0] += 5.0
            g.feed(reading)
            n = g.take_notice()
            if n:
                notices.append(n)
    off = hg.OFF_NOTICE.format(mins="2 min")
    assert notices == [off, hg.BACK_NOTICE, off, hg.BACK_NOTICE] and g.trips == 2
    assert "no room to spare for 2 min" in off


def test_no_reading_switches_nothing_off_and_nothing_back_on():
    clock = [0.0]
    g = _guard(clock)
    _run(g, clock, None, 3600)
    assert g.off is False
    g.feed("TIGHT")
    _run(g, clock, "TIGHT", 60)
    _run(g, clock, None, 5)                                  # the service stopped answering: the run is broken
    _run(g, clock, "TIGHT", 115)
    assert g.off is False
    _run(g, clock, "TIGHT", 10)
    assert g.off is True
    _run(g, clock, None, 3600)
    assert g.off is True                                     # not knowing is not recovering


# ---------------------------------------------------------------------------------------------------------------
# J(1). TIGHT and a fight, with everything turned all the way up
# ---------------------------------------------------------------------------------------------------------------
def _real_eyes(headroom):
    from PIL import Image
    import random
    calls, clock, rng = [], [0.0], random.Random(5)

    def grab():
        return Image.frombytes("RGB", (64, 36), bytes(rng.randrange(256) for _ in range(64 * 36 * 3)))
    e = eyes_mod.Eyes(presence="curious", grab=grab, foreground=lambda: "StarCitizen.exe", headroom=headroom,
                      glance=lambda j: calls.append(1) or {"scene": "other", "confidence": 0.9, "notable": "a ship"},
                      now=lambda: clock[0], per_hour=10 ** 6, look_gap_s=0.0)       # the pilot's own, as high as they go
    e.set_pace(pp.MIN_EVERY_S)                                                      # the shortest interval there is
    return e, calls, clock


def test_tight_takes_no_picture_at_the_shortest_interval_and_the_highest_cap():
    head = ["TIGHT"]
    e, calls, clock = _real_eyes(lambda: head[0])
    for reason in ("interval", "arrival", "mining_capture", "pilot_asked"):
        for _ in range(30):
            clock[0] += 11.0
            e.tick()
            assert e.look(reason) is None
    assert calls == []
    head[0] = "OK"                                           # the control: the same eyes do look when there is room
    clock[0] += 11.0
    assert e.look("interval") == "a ship" and calls == [1]


def _never_post(url, body, timeout):
    raise AssertionError(f"the chat model was asked: {url}")


def _talk_spec(core, sentence="Rough day."):
    return conv.ConversationLane().handle(sentence, core.lane_state(), {})


def test_tight_asks_no_chat_model_whatever_the_chattiness():
    core = make_core(None, headroom=lambda: "TIGHT", **LOUDEST)
    core.talker = ct.Talker("gemma3:4b", post=_never_post)
    spec = _talk_spec(core)
    assert core.talker.brief(spec, "Rough day.") is not None            # it IS talk; only the card says no
    assert core._talk(spec, "Rough day.") is None
    assert core.talker.stats["unavailable"] == 1 and core.talker.stats["asked"] == 0


def test_a_fight_takes_no_picture_and_asks_no_chat_model_at_the_loudest_settings():
    clock, eyes = [1000.0], FakeEyes()
    core = make_core(eyes, clock, headroom=lambda: "ROOMY", **LOUDEST)
    core.pace.configure({pp.every_key(a): pp.MIN_EVERY_S for a in pp.ACTIVITIES})
    core.talker = ct.Talker("gemma3:4b", post=_never_post)
    core.gate_state.in_combat = True
    for _ in range(40):
        clock[0] += 5.0
        tick(core)
    assert eyes.looks == [] and eyes.paces[-1][2] == "combat"
    assert core._talk(_talk_spec(core), "Rough day.") is None and core.talker.stats["asked"] == 0
    assert core._look_for({"place": {"look": True}}, lambda text: None) == {"place": {"look": True}}
    assert eyes.looks == []                                  # "look at that" in a fight: no picture either
    core.gate_state.in_combat = False                        # the control: the fight over, the picture is taken
    clock[0] += 5.0
    tick(core)
    assert eyes.looks == ["interval"]


def test_the_audio_fight_detector_counts_as_a_fight_too():
    core = make_core(FakeEyes(), **LOUDEST)
    assert core._picture_hold() == ""
    core.combat.state = "on"                                 # what CombatWatch sets when it hears a fight
    assert core._picture_hold() == "combat"


# ---------------------------------------------------------------------------------------------------------------
# J(2). overloaded for long: eyes and chat off, told once, back by themselves
# ---------------------------------------------------------------------------------------------------------------
def test_a_pc_that_stays_overloaded_switches_eyes_and_chat_off_and_says_so_once():
    clock, eyes, head = [1000.0], FakeEyes(), ["TIGHT"]
    core = make_core(eyes, clock, headroom=lambda: "OK", hardware_reading=lambda: head[0], **LOUDEST)
    core.pace.configure({pp.every_key(a): pp.MIN_EVERY_S for a in pp.ACTIVITIES})
    core.talker = ct.Talker("gemma3:4b", post=_never_post)
    core._hardware_tick()
    for _ in range(30):                                      # 150 s of TIGHT
        clock[0] += 5.0
        core._hardware_tick()
    assert core.overload.off and core._picture_hold() == "overload"
    assert core.hardware_notice == hg.OFF_NOTICE.format(mins="2 min")
    assert sum("Eyes and chat are off" in line for line in core.last) == 1          # told once, in the window's list
    assert core.speech.said == []                                                   # and never out loud
    head[0] = "OK"                                           # a clear reading, but not for long enough yet
    looks_before = len(eyes.looks)
    for _ in range(8):
        clock[0] += 5.0
        core._hardware_tick()
        tick(core)
    assert len(eyes.looks) == looks_before and eyes.paces[-1][2] == "overload"
    assert core._talk(_talk_spec(core), "Rough day.") is None and core.talker.stats["asked"] == 0
    for _ in range(6):
        clock[0] += 5.0
        core._hardware_tick()
    assert not core.overload.off and core.hardware_notice == ""
    tick(core)
    assert len(eyes.looks) == looks_before + 1               # back on their own
    assert sum("Eyes and chat are back" in line for line in core.last) == 1


def test_a_reading_that_cannot_be_taken_switches_nothing_off():
    clock = [1000.0]

    def broken():
        raise OSError("no service")
    core = make_core(None, clock, hardware_reading=broken)
    for _ in range(100):
        clock[0] += 5.0
        core._hardware_tick()
    assert not core.overload.off and core.hardware_notice == ""


# ---------------------------------------------------------------------------------------------------------------
# no setting, key or checkbox gets past any of it
# ---------------------------------------------------------------------------------------------------------------
BYPASS = re.compile(r"overrid|bypass|force|unsafe|ignore|skip|disable_(?:guard|limit|headroom|hardware)|"
                    r"allow_(?:tight|overload|combat)|no_?(?:limit|guard|headroom)|always_(?:look|chat)", re.I)
ROOT = Path(hg.__file__).resolve().parents[1]


def test_no_setting_names_a_way_past_the_hardware_limit():
    assert [k for k in st.DEFAULTS if BYPASS.search(k)] == []
    assert not any(k.startswith(("hardware", "headroom", "overload", "guard")) for k in st.DEFAULTS)


def test_the_guard_reads_no_setting_and_the_core_does_not_hand_it_one():
    src = (ROOT / "core" / "hardware_guard.py").read_text(encoding="utf-8")
    assert "import settings" not in src and "settings.load" not in src and ".get(\"" not in src
    core_src = (ROOT / "core" / "companion_core.py").read_text(encoding="utf-8")
    assert core_src.count("OverloadGuard(") == 1 and "OverloadGuard(now=now)" in core_src     # the defaults, always
    hold = core_src[core_src.index("def _picture_hold"):core_src.index("def _hardware_tick")]
    assert "features" not in hold and "settings" not in hold and ".get(" not in hold


def test_the_window_has_no_control_for_it():
    src = (ROOT / "ui" / "suit_window.py").read_text(encoding="utf-8")
    for line in src.splitlines():
        if "QCheckBox(" in line or ".addItem(" in line or "QPushButton(" in line:
            assert not BYPASS.search(line), line
    assert "overload_after" not in src and "recover_after" not in src and "hardware_reading=hardware_reading" in src
    # what the window may do with the hold is read it, to show it
    assert [ln.strip() for ln in src.splitlines() if "_picture_hold" in ln] == ["hold = c._picture_hold()"]
