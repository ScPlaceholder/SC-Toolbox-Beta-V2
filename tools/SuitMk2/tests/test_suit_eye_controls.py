"""The window's controls for the eyes: four picture sliders with a "never" box each, and the talk dial (J, 2026-10-05).

The real Suit panel, built hidden with the harness of test_disable_companions.py: nothing real starts and the
settings file is a dict.
"""
from __future__ import annotations

import picture_pace as pp
from pacing import LEVEL_NAMES
from test_disable_companions import _Harness, app          # noqa: F401  (app is a fixture)


def _panel(monkeypatch, tmp_path, **settings):
    h = _Harness(monkeypatch, tmp_path, companions_enabled=False, **settings)
    return h, h.panel()


def test_each_activity_has_a_slider_that_opens_on_his_default_with_never_unticked(app, monkeypatch, tmp_path):
    h, w = _panel(monkeypatch, tmp_path)
    assert list(w._pic_sl) == list(pp.ACTIVITIES)
    assert {a: w._pic_lbl[a].text() for a in pp.ACTIVITIES} == {"salvage": "5 min", "mining": "5 min",
                                                               "combat_mission": "2 min", "sandbox": "5 min"}
    for a in pp.ACTIVITIES:
        sl = w._pic_sl[a]
        assert (sl.minimum(), sl.maximum()) == (0, len(pp.STOPS) - 1) and sl.isEnabled()
        assert pp.STOPS[sl.value()] == pp.DEFAULT_EVERY_S[a] and not w._pic_never[a].isChecked()
    assert h.saved == []                                     # building the window writes nothing


def test_the_slider_runs_from_five_seconds_to_two_hours_and_saves_seconds(app, monkeypatch, tmp_path):
    h, w = _panel(monkeypatch, tmp_path)
    sl = w._pic_sl["salvage"]
    sl.setValue(sl.minimum())
    assert h.saved[-1][pp.every_key("salvage")] == 5.0 and w._pic_lbl["salvage"].text() == "5 s"
    sl.setValue(sl.maximum())
    assert h.saved[-1][pp.every_key("salvage")] == 7200.0 and w._pic_lbl["salvage"].text() == "2 h"
    sl.setValue(pp.STOPS.index(120))
    assert h.saved[-1][pp.every_key("salvage")] == 120.0 and w._pic_lbl["salvage"].text() == "2 min"
    assert h.saved[-1][pp.every_key("mining")] == 300.0      # the others are left alone


def test_never_is_saved_greys_the_slider_and_keeps_its_place(app, monkeypatch, tmp_path):
    h, w = _panel(monkeypatch, tmp_path)
    w._pic_never["mining"].setChecked(True)
    assert h.saved[-1][pp.never_key("mining")] is True and h.saved[-1][pp.every_key("mining")] == 300.0
    assert w._pic_lbl["mining"].text() == "never" and not w._pic_sl["mining"].isEnabled()
    w._pic_never["mining"].setChecked(False)
    assert h.saved[-1][pp.never_key("mining")] is False
    assert w._pic_lbl["mining"].text() == "5 min" and w._pic_sl["mining"].isEnabled()


def test_a_saved_never_and_a_hand_edited_interval_open_as_saved(app, monkeypatch, tmp_path):
    h, w = _panel(monkeypatch, tmp_path, **{pp.never_key("sandbox"): True, pp.every_key("combat_mission"): 47.0})
    assert w._pic_lbl["sandbox"].text() == "never" and not w._pic_sl["sandbox"].isEnabled()
    assert w._pic_lbl["combat_mission"].text() == "45 s"     # the nearest stop is shown; the file is not rewritten
    assert h.saved == []


def test_a_running_core_gets_the_new_pace_at_once(app, monkeypatch, tmp_path):
    h, w = _panel(monkeypatch, tmp_path)
    core = type("Core", (), {})()
    core.pace, core.levels = pp.PicturePace(), []
    core.set_eye_chattiness = core.levels.append
    w.core = core
    w._pic_sl["combat_mission"].setValue(0)
    w._pic_never["salvage"].setChecked(True)
    assert core.pace.every("combat_mission") == 5.0 and core.pace.every("salvage") is None
    w._eye_chat.setValue(4)
    assert core.levels == [4]
    w.core = None


def test_the_talk_dial_has_the_five_levels_and_is_saved_separately_from_chattiness(app, monkeypatch, tmp_path):
    h, w = _panel(monkeypatch, tmp_path)
    assert (w._eye_chat.minimum(), w._eye_chat.maximum(), w._eye_chat.value()) == (0, 4, 2)
    assert w._eye_chat_lbl.text() == "normal"
    w._eye_chat.setValue(0)
    assert h.saved[-1]["eyes_chattiness"] == 0 and h.saved[-1]["chattiness"] == 2
    assert w._eye_chat_lbl.text() == LEVEL_NAMES[0]
