"""The Star Map keeps its set-route UI and runs the AI Assistant's code behind it.

Set route moved to tools/Assistant/assistant/set_route/ on 2026-10-04 (J: "Can
we move the set route functionality from the starmap to the ai assistant?").
The map kept two things a pilot can see: the typed "navigate to ..." command and
the In-Game button. Each must still work, and each must be the Assistant's copy
underneath, because the map has none. Calibrate did NOT stay (J, later the same
day: "Why not move the calibrate button as well?"): the map must have no way to
open it. That the Assistant has one is tested with the Assistant
(tools/Assistant/tests/test_set_route_window.py).

NOTHING HERE TOUCHES THE GAME, THE MOUSE OR THE KEYBOARD. The in-game macro is
replaced by a fake route setter that records what it was asked to plot (the
real macro on a fake input layer is tested with the code, in
tools/Assistant/tests/test_set_route.py).
"""
import os
import re
import sys

import pytest

from test_no_microphone import PKG_DIR, _pump, audio, panel  # noqa: F401  (fixtures)

TOOLBOX = os.path.normpath(os.path.join(PKG_DIR, "..", "..", ".."))
ASSISTANT_SET_ROUTE = os.path.join(TOOLBOX, "tools", "Assistant", "assistant", "set_route")


class _Setter:
    def __init__(self):
        self.plotted = []

    def available(self):
        return True

    def calibrated(self):
        return True

    def busy(self):
        return False

    def set_route(self, destination, status_cb=None, done_cb=None):
        self.plotted.append(destination)
        return True


@pytest.fixture
def routed(panel, monkeypatch):
    """The panel with the Assistant's real RouteService, real destination list
    and a recording route setter in place of the in-game macro."""
    from starmap import set_route_link
    svc = set_route_link.service()
    setter = _Setter()
    svc._setter_factory = lambda: setter
    monkeypatch.setattr(panel, "_route_svc", svc)
    panel.setter = setter
    return panel


def _type(panel, text):
    """Type into the command bar and press Enter, as a pilot does."""
    bar = panel._voicebar
    bar._input.setText(text)
    bar._input.returnPressed.emit()
    _pump(0.05)
    return bar.status()


def test_the_command_bar_sets_a_route_through_the_assistants_code(routed):
    svc = routed._set_route_service()
    assert type(svc).__module__ == "assistant.set_route.service"
    assert os.path.normpath(sys.modules["assistant.set_route.service"].__file__).startswith(
        os.path.normpath(ASSISTANT_SET_ROUTE)), "the Star Map loaded some other copy"
    routed._btn_game.setChecked(True)
    status = _type(routed, "navigate to Area 18")
    assert routed.setter.plotted == ["area18"]
    assert status == "setting route to area18 in game"
    _type(routed, "set route to Port Tressler")
    assert routed.setter.plotted == ["area18", "port tressler"]


def test_in_game_off_the_command_bar_plots_nothing(routed):
    routed._btn_game.setChecked(False)
    status = _type(routed, "navigate to Area 18")
    assert routed.setter.plotted == []
    assert "toggle 'In-Game'" in status


def test_the_in_game_button_is_the_assistants_switch(routed):
    """One switch, one file. The map's button writes it, and the saved value (not
    the button) decides: turned off from the Assistant's window while the map is
    open, a typed route plots nothing and the button follows."""
    from starmap import set_route_link
    gate = set_route_link.gate()
    assert gate.__name__ == "assistant.set_route.gate"
    routed._btn_game.setChecked(False)
    assert gate.in_game_enabled() is False
    routed._btn_game.setChecked(True)
    assert gate.in_game_enabled() is True
    gate.set_in_game(False)                       # the Assistant's In-Game button
    assert routed._btn_game.isChecked() is True   # the map has not looked yet
    status = _type(routed, "navigate to Area 18")
    assert routed.setter.plotted == []
    assert "toggle 'In-Game'" in status
    assert routed._btn_game.isChecked() is False


def test_the_saved_in_game_choice_survives_the_move(panel):
    """The fixture's saved Star Map state has game_route true (J's file): the
    button comes up on, and the switch file now says so too."""
    from starmap import set_route_link
    assert panel._btn_game.isChecked() is True
    assert set_route_link.gate().in_game_enabled() is True
    assert os.path.isfile(set_route_link.gate().settings_path())


def test_the_star_map_has_no_calibrate_control(panel):
    """No button, no menu entry, no method and no import that opens calibration."""
    from PySide6 import QtGui, QtWidgets
    from PySide6.QtCore import Qt
    from starmap import set_route_link
    texts = [b.text() for b in panel.findChildren(QtWidgets.QAbstractButton)]
    assert texts, "found no buttons at all; this check would pass on an empty panel"
    assert "In-Game" in texts and "Route" in texts            # the fixture really is the map
    labels = texts + [a.text() for a in panel.findChildren(QtGui.QAction)]
    assert [t for t in labels if "calibrat" in t.lower()] == []
    assert not hasattr(panel._voicebar, "_btn_calibrate")
    assert not hasattr(panel, "_calibrate") and not hasattr(panel, "_route_menu")
    # right-clicking Route used to open "Calibrate in-game route setter..."
    assert panel._btn_route.contextMenuPolicy() != Qt.CustomContextMenu
    assert not hasattr(set_route_link, "calibration_dialog_class")
    for name in sorted(os.listdir(PKG_DIR)):
        if name.endswith(".py") and name != "tutorial.py":     # the tutorial says where it went
            with open(os.path.join(PKG_DIR, name), encoding="utf-8") as fh:
                assert "RouteCalibrationDialog" not in fh.read(), name


def test_the_assistant_can_ask_an_open_map_to_show_a_place(routed, monkeypatch):
    """IPC map_goto: the Assistant set a route itself; the map only follows along."""
    shown = []
    monkeypatch.setattr(routed, "goto", shown.append)
    routed._btn_game.setChecked(True)
    routed._on_ipc({"type": "map_goto", "name": "area18"})
    assert shown == ["Area18"]
    assert routed.setter.plotted == [], "a map_goto reached the in-game macro"


def test_without_the_assistant_tool_set_route_says_so_and_stays_off(panel, monkeypatch, tmp_path):
    from starmap import set_route_link
    monkeypatch.setattr(set_route_link, "ASSISTANT_DIR", str(tmp_path / "nowhere"))
    monkeypatch.setattr(panel, "_route_svc", None)
    with pytest.raises(set_route_link.SetRouteUnavailable):
        set_route_link.service()
    ok, reply = panel.run_command("navigate to Area 18")
    assert ok and "AI Assistant tool, which is not installed" in reply
    assert panel._in_game() is False


def test_the_star_map_has_no_set_route_code_of_its_own():
    assert not os.path.exists(os.path.join(PKG_DIR, "set_route")), "starmap/set_route/ is back"
    assert not os.path.exists(os.path.join(PKG_DIR, "data", "set_route")), "its data is back"
    banned = re.compile(r"^class (InGameRouteSetter|DestinationPhoneticEngine|RouteCalibrationDialog)\b"
                        r"|^\s*(?:import|from)\s+pynput\b|\bInGameRouteSetter\s*\("
                        r"|SetClipboardData|keyboard\.Controller|mouse\.Controller", re.MULTILINE)
    offenders, seen = [], 0
    for root, _dirs, files in os.walk(PKG_DIR):
        for name in files:
            if name.endswith(".py"):
                seen += 1
                with open(os.path.join(root, name), encoding="utf-8") as fh:
                    if banned.search(fh.read()):
                        offenders.append(os.path.relpath(os.path.join(root, name), PKG_DIR))
    assert seen > 10, "the scan found almost no files; it would pass on an empty folder"
    assert offenders == [], "the Star Map sends input / matches destinations itself: %s" % offenders
