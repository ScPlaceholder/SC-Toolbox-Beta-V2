"""The launcher's "Reset position for Pico Pals and Battle Buddy", Battle Buddy's side.

J, 2026-10-06: "When pressed it will put the pico and battle buddy to center of the monitor in the
case that the user accidentally managed to get one stuck somewhere you cant grab it."

Battle Buddy is more than one window: the HUD, the click-through checkbox (a window of its own that
follows the HUD), and the Options and Tutorial windows.  And it saves its position whenever the HUD
is hidden, so a reset that only edited the settings file of a running HUD would be undone by the
next hide.  Held here:

    in this process     the HUD centres on the primary screen when the command arrives, the spot is
                        on disk at once (no debounce), no other setting changes, the checkbox window
                        is where the HUD is, an open Options or Tutorial window comes back too, and
                        a later hide saves the centred spot, not the old one
    as the launcher     the launcher's own reset and process class against the real hud_app.py (via
    runs it             the probe beside this file): running, it moves and stays moved across hide,
                        show and quit; closed, its saved position leaves the file and it opens centred

NOTHING HERE TOUCHES THE USER'S FILES OR DESKTOP.  Qt runs offscreen.  In-process tests point the
settings file into tmp_path; the child processes get USERPROFILE / HOME / APPDATA under tmp_path.
"""
import importlib.util
import json
import os
import subprocess
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

TOOL = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
ROOT = os.path.normpath(os.path.join(TOOL, "..", ".."))
for _p in (ROOT, TOOL):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pytest  # noqa: E402

pytest.importorskip("PySide6")
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

PROBE = os.path.join(TOOL, "tests", "_hud_probe.py")
FAR = (5000, 5000)
OTHER = {"orientation": "vertical", "opacity": 0.8, "click_through": False, "auto_show_on_join": False}


def _launcher_module(name):
    """A module of the LAUNCHER's core package, loaded by path: inside this folder `core` is Battle
    Buddy's own package of the same name."""
    alias = "toolbox_core_" + name
    if alias not in sys.modules:
        spec = importlib.util.spec_from_file_location(alias, os.path.join(ROOT, "core", name + ".py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[alias] = module
        spec.loader.exec_module(module)
    return sys.modules[alias]


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def settings(tmp_path, monkeypatch):
    """Battle Buddy's settings file under tmp_path, with a log path that exists so nothing goes
    looking for a real Game.log."""
    from ui import options_popup

    log = tmp_path / "Game.log"
    log.write_text("", encoding="utf-8")
    path = tmp_path / "bb" / "settings.json"
    path.parent.mkdir()
    data = dict(OTHER, log_path=str(log).replace("\\", "/"), window_x=FAR[0], window_y=FAR[1])
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    monkeypatch.setattr(options_popup, "_SETTINGS_FILE", str(path))
    monkeypatch.setattr(options_popup, "_LEGACY_SETTINGS_FILE", str(tmp_path / "no_legacy.json"))
    return path


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _centre_of_primary(w):
    g = QGuiApplication.primaryScreen().availableGeometry()
    return (g.center().x() - w.frameGeometry().width() // 2,
            g.center().y() - w.frameGeometry().height() // 2)


def _buddy(tmp_path, monkeypatch=None, argv=None):
    """A BattleBuddyApp built from the launcher's own argument list (x y w h opacity cmd_file) by
    hud_app's own parser, not started: no log thread, no timers."""
    import hud_app

    cmd = tmp_path / "cmd.jsonl"
    cmd.write_text("", encoding="utf-8")
    argv = ["100", "100", "1300", "800", "0.95", str(cmd)] if argv is None else argv + [str(cmd)]
    old = sys.argv
    sys.argv = ["hud_app.py"] + argv
    try:
        cfg = hud_app._parse_args()
    finally:
        sys.argv = old
    assert cfg["cmd_file"] == str(cmd)
    return hud_app.BattleBuddyApp(cfg), str(cmd)


def _send_reset(cmd_file):
    from shared.ipc import ipc_write          # the launcher's writer (core/process_manager.py)
    assert ipc_write(cmd_file, {"type": "reset_position"})


def _close(buddy):
    from ui.options_popup import OptionsPopup
    from ui.tutorial_popup import TutorialPopup

    for popup in (OptionsPopup._instance, TutorialPopup._instance):
        if popup is not None:
            popup.close()
    OptionsPopup._instance = TutorialPopup._instance = None
    buddy._hud._save_timer.stop()
    buddy._hud._pending_save = None
    buddy._hud.close()


# ── in this process ──────────────────────────────────────────────────────────

def test_centre_on_primary_centres_a_window_that_is_far_off_screen(app):
    from hud_app import _centre_on_primary
    w = QWidget()
    w.resize(300, 40)
    w.move(*FAR)
    _centre_on_primary(w)
    assert (w.x(), w.y()) == _centre_of_primary(w)


def test_the_reset_command_centres_the_hud_and_saves_it_at_once(app, settings, tmp_path):
    buddy, cmd = _buddy(tmp_path)
    try:
        hud = buddy._hud
        hud.show()
        hud.move(*FAR)                               # dragged off the screen while running
        assert (hud.x(), hud.y()) == FAR
        _send_reset(cmd)
        buddy._poll_ipc()
        assert (hud.x(), hud.y()) == _centre_of_primary(hud)
        # on disk already: nothing here ran the event loop, so a debounced save would not be
        now = _read(settings)
        assert (now["window_x"], now["window_y"]) == (hud.x(), hud.y())
        assert {k: now[k] for k in OTHER} == OTHER, "another setting was changed"
        assert now["log_path"].endswith("Game.log")
        assert not hud._save_timer.isActive() and hud._pending_save is None
    finally:
        _close(buddy)


def test_hiding_the_hud_after_a_reset_saves_the_centre_not_the_old_spot(app, settings, tmp_path):
    buddy, cmd = _buddy(tmp_path)
    try:
        hud = buddy._hud
        hud.show()
        hud.move(*FAR)
        hud._save_position()                         # the old spot is waiting in the debounce
        _send_reset(cmd)
        buddy._poll_ipc()
        centre = (hud.x(), hud.y())
        hud.hide()                                   # hideEvent saves the position
        hud._flush_save()
        now = _read(settings)
        assert (now["window_x"], now["window_y"]) == centre == _centre_of_primary(hud)
    finally:
        _close(buddy)


def test_the_click_through_checkbox_window_comes_with_the_hud(app, settings, tmp_path):
    buddy, cmd = _buddy(tmp_path)
    try:
        hud = buddy._hud
        hud.show()
        hud.move(*FAR)
        _send_reset(cmd)
        buddy._poll_ipc()
        toggle = hud._toggle_window
        assert (toggle.x(), toggle.y()) == (hud.x() + 1, hud.y() + 27)
    finally:
        _close(buddy)


def test_open_options_and_tutorial_windows_come_back_too(app, settings, tmp_path):
    from ui.options_popup import OptionsPopup
    from ui.tutorial_popup import TutorialPopup

    buddy, cmd = _buddy(tmp_path)
    try:
        buddy._hud.show()
        buddy._show_options()
        buddy._show_tutorial()
        options, tutorial = OptionsPopup._instance, TutorialPopup._instance
        options.move(*FAR)
        tutorial.move(-9000, 40)
        _send_reset(cmd)
        buddy._poll_ipc()
        assert (options.x(), options.y()) == _centre_of_primary(options)
        assert (tutorial.x(), tutorial.y()) == _centre_of_primary(tutorial)
    finally:
        _close(buddy)


def test_a_reset_with_no_popup_ever_opened_is_fine(app, settings, tmp_path):
    from ui.options_popup import OptionsPopup
    from ui.tutorial_popup import TutorialPopup

    OptionsPopup._instance = TutorialPopup._instance = None
    buddy, cmd = _buddy(tmp_path)
    try:
        buddy._hud.move(*FAR)                        # hidden HUD: it is moved all the same
        _send_reset(cmd)
        buddy._poll_ipc()
        assert (buddy._hud.x(), buddy._hud.y()) == _centre_of_primary(buddy._hud)
    finally:
        _close(buddy)


def test_the_keys_the_launcher_clears_are_the_ones_the_hud_saves(app, settings, tmp_path):
    reset = _launcher_module("position_reset")
    target = {t.skill_id: t for t in reset.TARGETS}["battle_buddy"]
    buddy, _cmd = _buddy(tmp_path)
    try:
        buddy._hud.move(31, 47)
        buddy._hud.save_position_now()
        assert {k: _read(settings)[k] for k in target.keys} == {"window_x": 31, "window_y": 47}
    finally:
        _close(buddy)


def test_a_launcher_command_is_read_on_the_first_poll_not_ten_seconds_later(app, settings, tmp_path):
    """The launcher's writer leaves its lock file behind; Battle Buddy's own reader took that for a
    held lock and gave up for 10 s after every command."""
    buddy, cmd = _buddy(tmp_path)
    try:
        hud = buddy._hud
        hud.show()
        from shared.ipc import ipc_write
        assert ipc_write(cmd, {"type": "hide"})
        assert os.path.exists(cmd + ".lock"), "the launcher's writer no longer leaves a lock file"
        began = time.monotonic()
        buddy._poll_ipc()
        assert not hud.isVisible(), "the command was not read on the first poll"
        assert time.monotonic() - began < 1.0
    finally:
        _close(buddy)


def test_started_by_wingman_it_keeps_its_own_reader_and_still_obeys(app, settings, tmp_path):
    """main.py passes named flags and writes with core/ipc.py, whose lock is a file that exists only
    while held.  That pair must stay a pair."""
    from core import ipc as own

    buddy, cmd = _buddy(tmp_path, argv=["--x", "60", "--y", "880", "--cmd-file"])
    try:
        assert buddy._ipc_read is own.ipc_read_and_clear
        buddy._hud.move(*FAR)
        assert own.ipc_write(cmd, {"type": "reset_position"})
        buddy._poll_ipc()
        assert (buddy._hud.x(), buddy._hud.y()) == _centre_of_primary(buddy._hud)
    finally:
        _close(buddy)


# ── as the launcher runs it ──────────────────────────────────────────────────

@pytest.fixture()
def home(tmp_path, monkeypatch):
    """A home folder for the child and, because the reset runs in this process, for this one."""
    from shared import logging_config

    home = tmp_path / "home"
    (home / "AppData").mkdir(parents=True)
    (home / "Game.log").write_text("", encoding="utf-8")
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("APPDATA", str(home / "AppData"))
    monkeypatch.delenv("HOMEDRIVE", raising=False)
    monkeypatch.delenv("HOMEPATH", raising=False)
    (tmp_path / "logs").mkdir()
    monkeypatch.setattr(logging_config, "_LOG_DIR", str(tmp_path / "logs"))
    reset = _launcher_module("position_reset")
    for path in (reset.pico_settings_path(), reset.battle_buddy_settings_path()):
        assert os.path.abspath(path).startswith(str(home)), "this test would write to the real %s" % path
    return home


def _home_settings(home):
    return home / ".sctoolbox" / "battle_buddy" / "settings.json"


def _save_home(home, **position):
    data = dict(OTHER, log_path=str(home / "Game.log").replace("\\", "/"), **position)
    _home_settings(home).parent.mkdir(parents=True, exist_ok=True)
    _home_settings(home).write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data


def _state(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _wait(cond, what, timeout=30.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        got = cond()
        if got:
            return got
        time.sleep(0.05)
    raise AssertionError("timed out after %.0fs waiting for %s" % (timeout, what))


def _log(tmp_path):
    try:
        return (tmp_path / "logs" / "battle_buddy.log").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _start(tmp_path, home, state, **extra_env):
    pm = _launcher_module("process_manager")
    env = dict({"QT_QPA_PLATFORM": "offscreen", "QT_SCALE_FACTOR": "", "USERPROFILE": str(home),
                "HOME": str(home), "APPDATA": str(home / "AppData"), "HOMEDRIVE": "", "HOMEPATH": "",
                "PYTHONIOENCODING": "utf-8", "HUD_PROBE_STATE": str(state)}, **extra_env)
    # x y w h opacity, as skill_launcher.py registers every tool; the command file is added at start
    mp = pm.ManagedProcess(skill_id="battle_buddy", python_exe=sys.executable, script=PROBE, cwd=TOOL,
                           args=["100", "100", "1300", "800", "0.95"], base_dir=str(tmp_path), env=env)
    assert mp.start()
    proc = mp._proc
    first = _wait(lambda: _state(state) or (proc.poll() is not None and {"dead": True}), "the HUD")
    assert "dead" not in first, "Battle Buddy exited on start (rc=%s):\n%s" % (proc.returncode, _log(tmp_path))
    return mp, first


def _kill(mp):
    proc = mp._proc
    if proc is not None and proc.poll() is None:
        proc.kill()
        proc.wait(timeout=10)
    if mp._log_file not in (None, subprocess.DEVNULL):
        mp._log_file.close()


def _off_centre(rect, screen):
    return max(abs(rect[0] + rect[2] / 2 - (screen[0] + screen[2] / 2)),
               abs(rect[1] + rect[3] / 2 - (screen[1] + screen[3] / 2)))


def _centred(state):
    return _off_centre([state["x"], state["y"], state["w"], state["h"]], state["screen"]) <= 1


def test_a_running_hud_dragged_off_the_screen_comes_back_and_stays(home, tmp_path):
    reset = _launcher_module("position_reset")
    saved = _save_home(home, window_x=20, window_y=30)
    state = tmp_path / "state.json"
    mp, first = _start(tmp_path, home, state, HUD_PROBE_MOVE="%d,%d" % FAR)
    try:
        proc = mp._proc
        assert (first["x"], first["y"]) == (20, 30), "the saved on-screen spot should have been kept"
        lost = _wait(lambda: (_state(state) or {}).get("moved") and _state(state), "the HUD dragged away")
        assert (lost["x"], lost["y"]) == FAR and lost["options"][:2] == list(FAR)
        on_disk = _read(_home_settings(home))
        assert (on_disk["window_x"], on_disk["window_y"]) == FAR, "the far spot was not saved"

        began = time.monotonic()
        got = reset.reset_positions(lambda sid: mp if sid == "battle_buddy" else None, ROOT)
        assert got["battle_buddy"] == reset.MOVED

        back = _wait(lambda: _centred(_state(state) or lost) and _state(state), "the HUD in the centre")
        assert time.monotonic() - began < 5.0, "the HUD moved, but not when the button was pressed"
        now = _read(_home_settings(home))
        assert (now["window_x"], now["window_y"]) == (back["x"], back["y"])
        assert {k: v for k, v in now.items() if k not in ("window_x", "window_y")} == \
            {k: v for k, v in saved.items() if k not in ("window_x", "window_y")}, "another setting changed"
        assert back["toggle"][:2] == [back["x"] + 1, back["y"] + 27]
        assert _off_centre(back["options"], back["screen"]) <= 1, "the Options window was left behind"

        mp.hide()                                    # Battle Buddy saves its position on hide
        _wait(lambda: (_state(state) or {}).get("visible") is False, "hide")
        mp.show()
        shown = _wait(lambda: (_state(state) or {}).get("visible") and _state(state), "show")
        assert _centred(shown)

        mp.stop(timeout=15)
        assert proc.returncode == 0, _log(tmp_path)
        after = _read(_home_settings(home))
        assert (after["window_x"], after["window_y"]) == (back["x"], back["y"]), "quitting wrote the old spot back"
    finally:
        _kill(mp)


def test_a_closed_hud_loses_its_saved_spot_and_opens_centred(home, tmp_path):
    reset = _launcher_module("position_reset")
    saved = _save_home(home, window_x=20, window_y=30)
    state = tmp_path / "state.json"

    # the control: with the spot still saved it opens exactly there, which is not the centre
    mp, first = _start(tmp_path, home, state)
    try:
        proc = mp._proc
        assert (first["x"], first["y"]) == (20, 30) and not _centred(first)
        mp.stop(timeout=15)
        assert proc.returncode == 0, _log(tmp_path)
    finally:
        _kill(mp)
    assert not mp.running

    got = reset.reset_positions(lambda sid: mp if sid == "battle_buddy" else None, ROOT)
    assert got["battle_buddy"] == reset.CLEARED
    assert _read(_home_settings(home)) == {k: v for k, v in saved.items()
                                           if k not in ("window_x", "window_y")}

    os.remove(state)
    mp2, again = _start(tmp_path, home, state)
    try:
        assert _centred(again), "it opened at %s,%s on screen %s" % (again["x"], again["y"], again["screen"])
    finally:
        _kill(mp2)
