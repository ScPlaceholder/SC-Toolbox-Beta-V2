"""The launcher's "Reset position for Pico Pals and Battle Buddy", Pico's side.

J, 2026-10-06: "When pressed it will put the pico and battle buddy to center of the monitor in the
case that the user accidentally managed to get one stuck somewhere you cant grab it."

Both states are run for real: the launcher's own reset (core/position_reset.py), the launcher's own
ManagedProcess, the adapter the tile starts (through the probe beside this file, which only adds a
report of where the window is), on Qt's offscreen platform.

    running   Pico is dragged far off the screen and that spot is saved.  The reset is sent through
              his command file: he is back in the middle of the main screen, his settings file holds
              the new spot and every other setting it had, and hiding, showing and quitting him do
              not write the old spot back.
    closed    his saved spot is taken out of the settings file, nothing else, and he opens in the
              middle.  The same start with the spot still saved opens him where it says, which is
              what shows the reset is the thing that centred him.

NOTHING HERE TOUCHES THE USER'S FILES OR DESKTOP, as in test_pico_pals_tile.py: USERPROFILE / HOME /
APPDATA point into tmp_path for the children and, because the reset itself runs in this process, for
this process too.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parents[1]            # tools/Pico
REPO = TOOL.parents[1]
for p in (str(REPO), str(TOOL), str(Path(__file__).resolve().parent)):
    if p not in sys.path:
        sys.path.insert(0, p)

from core import position_reset  # noqa: E402
from test_pico_pals_tile import (  # noqa: E402,F401 - the fixtures are used by name
    PROBE, child_env, fake_home, kill, launcher_registration, log_text, logs_under_tmp, managed,
    read_state, wait_for)

SAVED = {"x": 5, "y": 7, "height": 280, "kept_by_the_test": [1, "two"], "packs_told": True}
FAR = (5000, 5000)


@pytest.fixture()
def home(fake_home, monkeypatch):
    """fake_home, and this process sees it as home too: position_reset works the paths out here."""
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("APPDATA", str(fake_home / "AppData"))
    monkeypatch.delenv("HOMEDRIVE", raising=False)
    monkeypatch.delenv("HOMEPATH", raising=False)
    for path in (position_reset.pico_settings_path(), position_reset.battle_buddy_settings_path()):
        assert os.path.abspath(path).startswith(str(fake_home)), "this test would write to the real %s" % path
    return fake_home


def settings_file(home: Path) -> Path:
    return home / "AppData" / "PicoPal" / "settings.json"


def save(home: Path, data: dict) -> None:
    settings_file(home).parent.mkdir(parents=True, exist_ok=True)
    settings_file(home).write_text(json.dumps(data, indent=2), encoding="utf-8")


def saved(home: Path) -> dict:
    return json.loads(settings_file(home).read_text(encoding="utf-8"))


def off_centre(rect, screen) -> tuple[float, float]:
    """How far the middle of rect [x, y, w, h] is from the middle of screen [x, y, w, h]."""
    return (abs(rect[0] + rect[2] / 2 - (screen[0] + screen[2] / 2)),
            abs(rect[1] + rect[3] / 2 - (screen[1] + screen[3] / 2)))


def centred(state: dict) -> bool:
    dx, dy = off_centre([state["x"], state["y"], state["w"], state["h"]], state["screen"])
    return dx <= 1 and dy <= 1


def start(tmp_path: Path, home: Path, state: Path, **extra_env):
    reg = launcher_registration()
    env = dict(child_env(home, state), **extra_env)
    mp = managed(PROBE, reg["args"], tmp_path, env)
    assert mp.start()
    proc = mp._proc
    first = wait_for(lambda: read_state(state) or (proc.poll() is not None and {"dead": True}),
                     what="Pico's window")
    assert "dead" not in first, "Pico exited on start (rc=%s):\n%s" % (proc.returncode, log_text(tmp_path))
    return mp, first


def test_the_paths_the_launcher_resets_are_the_ones_pico_reads(home):
    import sprite_pal

    here = Path(os.environ["APPDATA"]) / "PicoPal" / "settings.json"
    assert Path(position_reset.pico_settings_path()) == here
    # sprite_pal worked its path out when it was imported, from the real APPDATA: same rule, real folder
    assert sprite_pal.SETTINGS.parts[-2:] == here.parts[-2:]
    assert set(position_reset.TARGETS[0].keys) == {"x", "y"}


def test_a_running_pico_dragged_off_the_screen_comes_back_to_the_middle_and_stays(home, tmp_path):
    save(home, SAVED)
    state = tmp_path / "state.json"
    mp, _first = start(tmp_path, home, state, PICO_PROBE_MOVE="%d,%d" % FAR)
    try:
        proc = mp._proc
        lost = wait_for(lambda: (read_state(state) or {}).get("moved") and read_state(state),
                        what="Pico being dragged off the screen")
        assert (lost["x"], lost["y"]) == FAR and not centred(lost)
        assert (saved(home)["x"], saved(home)["y"]) == FAR, "the far spot was not saved, so the test proves nothing"
        assert lost["dialogs"], "the Customise box should be open"

        got = position_reset.reset_positions(lambda sid: mp if sid == "pico" else None, str(REPO))
        assert got["pico"] == position_reset.MOVED

        back = wait_for(lambda: centred(read_state(state) or lost) and read_state(state), what="Pico in the middle")
        now = saved(home)
        assert (now["x"], now["y"]) == (back["x"], back["y"]), "the settings file does not hold where he is"
        assert {k: v for k, v in now.items() if k not in ("x", "y")} == \
            {k: v for k, v in SAVED.items() if k not in ("x", "y")}, "another setting was changed"
        for box in back["dialogs"]:
            assert max(off_centre(box, back["screen"])) <= 1, "an open box was left behind at %s" % box

        mp.hide()
        wait_for(lambda: (read_state(state) or {}).get("visible") is False, what="hide")
        mp.show()
        shown = wait_for(lambda: (read_state(state) or {}).get("visible") and read_state(state), what="show")
        assert centred(shown)

        mp.stop(timeout=15)
        assert proc.returncode == 0, log_text(tmp_path)
        after = saved(home)
        assert (after["x"], after["y"]) == (back["x"], back["y"]), "quitting wrote the old spot back"
    finally:
        kill(mp)


def test_a_closed_pico_loses_his_saved_spot_and_opens_in_the_middle(home, tmp_path):
    save(home, SAVED)
    state = tmp_path / "state.json"

    # the control: with the spot still saved he opens exactly there, which is not the middle
    mp, first = start(tmp_path, home, state)
    try:
        assert (first["x"], first["y"]) == (SAVED["x"], SAVED["y"]) and not centred(first)
        proc = mp._proc
        mp.stop(timeout=15)
        assert proc.returncode == 0, log_text(tmp_path)
    finally:
        kill(mp)
    assert not mp.running
    assert saved(home) == SAVED, "a start and a quit changed his settings"

    got = position_reset.reset_positions(lambda sid: mp if sid == "pico" else None, str(REPO))
    assert got["pico"] == position_reset.CLEARED
    assert saved(home) == {k: v for k, v in SAVED.items() if k not in ("x", "y")}

    state.unlink()
    mp2, again = start(tmp_path, home, state)
    try:
        assert centred(again), "he opened at %s,%s on screen %s" % (again["x"], again["y"], again["screen"])
        proc = mp2._proc
        mp2.stop(timeout=15)
        assert proc.returncode == 0, log_text(tmp_path)
    finally:
        kill(mp2)
