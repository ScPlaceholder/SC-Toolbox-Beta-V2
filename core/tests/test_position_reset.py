"""Settings > Tools > "Reset position for Pico Pals and Battle Buddy", the launcher's side.

J, 2026-10-06: "in the settings add a 'Reset position for Pico Pals and Battle Buddy'. When pressed it
will put the pico and battle buddy to center of the monitor in the case that the user accidentally
managed to get one stuck somewhere you cant grab it."

What is held here (core/position_reset.py, and the button in ui/settings_panel.py):

    a closed tool     its position keys leave its settings file; every other setting, and their
                      order, stays.  Missing, empty, corrupt and not-a-dict files are left as they are
    a running tool    it is sent {"type": "reset_position"} through the command file and its settings
                      file is NOT edited (Battle Buddy saves its position on hide; an edit under a
                      running tool would be overwritten with the old spot)
    one tool only     the other one's absence breaks nothing
    the button        the real Settings popup, opened by the real launcher window, has the row; one
                      click runs the launcher's callback once and reports per tool

The two tools' own halves are in tools/Pico/tests/test_pico_reset_position.py and
tools/Battle_Buddy/tests/test_reset_position.py.

NOTHING HERE TOUCHES THE USER'S FILES.  USERPROFILE and APPDATA point into tmp_path for every test,
and the paths the module works out are asserted to be under it before anything is written.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

REPO = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
sys.path.insert(0, REPO)

from core import position_reset as pr  # noqa: E402

PROBE = os.path.join(REPO, "core", "tests", "_reset_position_probe.py")


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "AppData").mkdir(parents=True)
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("APPDATA", str(home / "AppData"))
    monkeypatch.delenv("HOMEDRIVE", raising=False)
    monkeypatch.delenv("HOMEPATH", raising=False)
    for path in (pr.pico_settings_path(), pr.battle_buddy_settings_path()):
        assert os.path.abspath(path).startswith(str(home)), "this test would write to the real %s" % path
    return home


class FakeProcess:
    """What reset_positions needs of core.process_manager.ManagedProcess."""

    def __init__(self, running=True, delivers=True):
        self.running = running
        self.delivers = delivers
        self.sent = []

    def send(self, cmd):
        self.sent.append(cmd)
        return self.delivers


def write(path, data):
    os.makedirs(os.path.dirname(str(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        if isinstance(data, str):
            fh.write(data)
        else:
            json.dump(data, fh, indent=2)


def read(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def raw(path):
    with open(path, "rb") as fh:
        return fh.read()


PICO = {"outfit": "pack:drake", "x": 9000, "height": 320, "y": -4000, "gags": False, "packs_told": True}
BUDDY = {"log_path": "D:/Game.log", "orientation": "vertical", "opacity": 0.8,
         "window_x": 9000, "window_y": 9000, "click_through": True}


# ── a closed tool: the settings file ─────────────────────────────────────────

def test_the_paths_are_the_ones_the_tools_read(fake_home):
    assert pr.pico_settings_path() == os.path.join(str(fake_home), "AppData", "PicoPal", "settings.json")
    assert pr.battle_buddy_settings_path() == os.path.join(str(fake_home), ".sctoolbox", "battle_buddy",
                                                           "settings.json")


def test_only_the_position_keys_leave_and_the_rest_keeps_its_order(tmp_path):
    path = tmp_path / "settings.json"
    write(path, PICO)
    assert pr.forget_position(str(path), ("x", "y")) is True
    after = read(path)
    assert after == {"outfit": "pack:drake", "height": 320, "gags": False, "packs_told": True}
    assert list(after) == ["outfit", "height", "gags", "packs_told"]
    assert sorted(os.listdir(tmp_path)) == ["home", "settings.json"], "a temporary file was left behind"


def test_a_missing_file_is_fine_and_is_not_created(tmp_path):
    path = tmp_path / "nowhere" / "settings.json"
    assert pr.forget_position(str(path), ("x", "y")) is True
    assert not path.exists() and not path.parent.exists()


@pytest.mark.parametrize("content", ["", "   \n", "{\"x\": 12, \"y\"", "[1, 2, 3]", "\"x\"", "null"])
def test_an_empty_corrupt_or_odd_file_is_left_exactly_as_it_is(tmp_path, content):
    path = tmp_path / "settings.json"
    write(path, content)
    before = raw(path)
    assert pr.forget_position(str(path), ("x", "y")) is True
    assert raw(path) == before


def test_a_file_with_no_saved_position_is_not_rewritten(tmp_path):
    path = tmp_path / "settings.json"
    write(path, "{\"outfit\":\"a\",   \"height\": 300}")      # the user's own formatting
    before = raw(path)
    assert pr.forget_position(str(path), ("x", "y")) is True
    assert raw(path) == before


def test_one_key_of_the_two_is_enough_to_be_removed(tmp_path):
    path = tmp_path / "settings.json"
    write(path, {"window_y": 5, "opacity": 0.5})
    assert pr.forget_position(str(path), ("window_x", "window_y")) is True
    assert read(path) == {"opacity": 0.5}


def test_a_failed_write_says_so_and_the_old_file_is_whole(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    write(path, BUDDY)
    before = raw(path)

    def refuse(src, dst):
        raise PermissionError("read-only profile")

    monkeypatch.setattr(pr.os, "replace", refuse)
    assert pr.forget_position(str(path), ("window_x", "window_y")) is False
    assert raw(path) == before
    assert sorted(os.listdir(tmp_path)) == ["home", "settings.json"], "the temporary file was left behind"


def test_a_legacy_file_about_to_be_migrated_does_not_bring_the_position_back(tmp_path):
    """Battle Buddy reads tools/Battle_Buddy/battle_buddy_settings.json once when it has no settings of
    its own.  With no new file to edit, the reset writes the legacy settings there without the position."""
    path = tmp_path / "new" / "settings.json"
    legacy = tmp_path / "battle_buddy_settings.json"
    write(legacy, BUDDY)
    before = raw(legacy)
    assert pr.forget_position(str(path), ("window_x", "window_y"), str(legacy),
                              "battle_buddy_settings.json") is True
    assert read(path) == {"log_path": "D:/Game.log", "orientation": "vertical", "opacity": 0.8,
                          "click_through": True}
    assert raw(legacy) == before, "the legacy file is not this module's to change"


def test_the_users_own_file_wins_over_the_legacy_one(tmp_path):
    path = tmp_path / "new" / "settings.json"
    legacy = tmp_path / "battle_buddy_settings.json"
    write(path, {"opacity": 0.3, "window_x": 1, "window_y": 2})
    write(legacy, BUDDY)
    assert pr.forget_position(str(path), ("window_x", "window_y"), str(legacy)) is True
    assert read(path) == {"opacity": 0.3}


# ── both tools, running or not ───────────────────────────────────────────────

def both_saved():
    write(pr.pico_settings_path(), PICO)
    write(pr.battle_buddy_settings_path(), BUDDY)


def test_closed_tools_lose_their_saved_position_and_nothing_else():
    both_saved()
    assert pr.reset_positions(lambda sid: None, "") == {"pico": pr.CLEARED, "battle_buddy": pr.CLEARED}
    assert read(pr.pico_settings_path()) == {k: v for k, v in PICO.items() if k not in ("x", "y")}
    assert read(pr.battle_buddy_settings_path()) == {k: v for k, v in BUDDY.items()
                                                     if k not in ("window_x", "window_y")}


def test_running_tools_are_told_to_move_and_their_files_are_not_edited():
    both_saved()
    before = raw(pr.pico_settings_path()), raw(pr.battle_buddy_settings_path())
    procs = {"pico": FakeProcess(), "battle_buddy": FakeProcess()}
    assert pr.reset_positions(procs.get, "") == {"pico": pr.MOVED, "battle_buddy": pr.MOVED}
    assert procs["pico"].sent == [{"type": "reset_position"}]
    assert procs["battle_buddy"].sent == [{"type": "reset_position"}]
    assert (raw(pr.pico_settings_path()), raw(pr.battle_buddy_settings_path())) == before


def test_one_running_and_one_closed_each_get_their_own_treatment():
    both_saved()
    procs = {"pico": FakeProcess(running=False), "battle_buddy": FakeProcess()}
    assert pr.reset_positions(procs.get, "") == {"pico": pr.CLEARED, "battle_buddy": pr.MOVED}
    assert procs["pico"].sent == [], "a command file is not written for a tool that is not running"
    assert "x" not in read(pr.pico_settings_path())
    assert read(pr.battle_buddy_settings_path()) == BUDDY


def test_a_command_that_cannot_be_delivered_falls_back_to_the_file():
    both_saved()
    procs = {"pico": FakeProcess(delivers=False), "battle_buddy": FakeProcess(delivers=False)}
    assert pr.reset_positions(procs.get, "") == {"pico": pr.CLEARED, "battle_buddy": pr.CLEARED}
    assert "x" not in read(pr.pico_settings_path())
    assert "window_x" not in read(pr.battle_buddy_settings_path())


def test_the_command_is_a_copy_so_a_caller_cannot_change_it_for_the_next_one():
    proc = FakeProcess()
    pr.reset_positions({"pico": proc}.get, "")
    proc.sent[0]["type"] = "quit"
    assert pr.RESET_CMD == {"type": "reset_position"}


def test_only_pico_installed():
    write(pr.pico_settings_path(), PICO)
    assert pr.reset_positions(lambda sid: None, "") == {"pico": pr.CLEARED, "battle_buddy": pr.CLEARED}
    assert "x" not in read(pr.pico_settings_path())
    assert not os.path.exists(pr.battle_buddy_settings_path())


def test_only_battle_buddy_installed():
    write(pr.battle_buddy_settings_path(), BUDDY)
    assert pr.reset_positions(lambda sid: None, "") == {"pico": pr.CLEARED, "battle_buddy": pr.CLEARED}
    assert "window_x" not in read(pr.battle_buddy_settings_path())
    assert not os.path.exists(pr.pico_settings_path())


def test_neither_file_exists_and_no_process_table_is_given():
    assert pr.reset_positions() == {"pico": pr.CLEARED, "battle_buddy": pr.CLEARED}


def test_one_file_that_cannot_be_written_does_not_stop_the_other(monkeypatch):
    both_saved()
    real = os.replace

    def refuse_pico(src, dst):
        if "PicoPal" in str(dst):
            raise PermissionError("read-only")
        return real(src, dst)

    monkeypatch.setattr(pr.os, "replace", refuse_pico)
    assert pr.reset_positions(lambda sid: None, "") == {"pico": pr.FAILED, "battle_buddy": pr.CLEARED}
    assert read(pr.pico_settings_path()) == PICO
    assert "window_x" not in read(pr.battle_buddy_settings_path())


def test_battle_buddys_legacy_file_in_the_toolbox_folder_is_found(tmp_path):
    root = tmp_path / "toolbox"
    write(root / "tools" / "Battle_Buddy" / "battle_buddy_settings.json", BUDDY)
    assert pr.reset_positions(lambda sid: None, str(root))["battle_buddy"] == pr.CLEARED
    assert read(pr.battle_buddy_settings_path()) == {k: v for k, v in BUDDY.items()
                                                     if k not in ("window_x", "window_y")}


# ── the launcher hands the button its process table ──────────────────────────

def test_the_launcher_resets_through_its_own_process_manager(monkeypatch):
    import skill_launcher

    class PM:
        def get(self, skill_id):
            return None

    seen = {}

    def fake_reset(process_for, root):
        seen["process_for"], seen["root"] = process_for, root
        return {"pico": pr.MOVED}

    monkeypatch.setattr(pr, "reset_positions", fake_reset)
    app = skill_launcher.SCToolboxApp.__new__(skill_launcher.SCToolboxApp)
    app._pm = PM()
    assert app._reset_tool_positions() == {"pico": pr.MOVED}
    assert seen["process_for"] == app._pm.get
    assert os.path.normpath(seen["root"]) == REPO


# ── the button ───────────────────────────────────────────────────────────────

def _click(fake_home, *args: str) -> dict:
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("QT_SCALE_FACTOR", None)
    proc = subprocess.run([sys.executable, PROBE, *args], cwd=str(fake_home), env=env,
                          capture_output=True, text=True, encoding="utf-8", timeout=180)
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("{")]
    assert proc.returncode == 0 and lines, (
        f"probe failed rc={proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")
    return json.loads(lines[-1])


def test_settings_has_the_row_and_one_click_resets_once_and_says_what_happened(fake_home):
    got = _click(fake_home, pr.MOVED, pr.CLEARED)
    assert got["label"] == "Reset position for Pico Pals and Battle Buddy"
    assert got["button"] == "Reset"
    assert got["calls"] == 1
    assert "Pico Pals: centred now" in got["status"]
    assert "Battle Buddy: centred on next start" in got["status"]
    assert got["status"].startswith("\u2713") and got["status_color"] == "green"
    assert got["applied"] == 0 and got["unchanged"] is True, "the reset must not count as a changed setting"


def test_a_tool_that_could_not_be_reset_is_named_in_red(fake_home):
    got = _click(fake_home, pr.FAILED, pr.MOVED)
    assert "Pico Pals: could not be reset" in got["status"]
    assert "Battle Buddy: centred now" in got["status"]
    assert got["status"].startswith("\u2717") and got["status_color"] == "red"


def test_a_tool_that_is_not_installed_is_not_reported_on(fake_home):
    got = _click(fake_home, pr.MOVED, pr.CLEARED, "--only-pico")
    assert got["calls"] == 1
    assert "Pico Pals: centred now" in got["status"]
    assert "Battle Buddy" not in got["status"]
