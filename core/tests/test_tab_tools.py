"""A tool that is a TAB of another tool's window (SkillConfig.tab_of).

J, 2026-10-04: "Can we combine toolbox assistant and suit mk2 under the same tool and have a tab for each
under the tool".  SuitMk2 is now a tab of the Toolbox Assistant window.  For the launcher that means:

    the tile        one tile (Toolbox Assistant); SuitMk2 is declared hidden
    the process     ONE process, the host's; SuitMk2 gets none of its own (two would be two companions
                    reading the same Game.log and fighting over one model service)
    Ctrl+3          the window, on the Assistant tab           {"type": "toggle", "tab": "assistant"}
    Ctrl+2          the same window, on the SuitMk2 tab        {"type": "toggle", "tab": "suitmk2"}
                    (the window shows, switches or hides from what it really is; what it does with each is
                    tested with the real window in tools/Assistant/tests/test_hub_window.py)
    preload         SuitMk2 still asks for it; what starts hidden is the window it is a tab of
    Settings        a tool switched off there does not come back as a tab (env SC_TOOLBOX_TABS_OFF)
    IPC             launch_skill / toggle_skill naming SuitMk2 reach the host, on its tab

Nothing here starts a process: the launcher is built without its constructor (which kills orphan tools and
binds global hotkeys) and the process manager is a recorder, as in test_hidden_tiles.py.

Each test was watched failing against a deliberately broken copy of the code (see the commit message).
"""
from __future__ import annotations

import os
import subprocess
import sys
from unittest.mock import MagicMock

REPO = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
sys.path.insert(0, REPO)

from core.process_manager import ManagedProcess  # noqa: E402
from core.skill_registry import discover_skills, resolve_script_path, tile_skills  # noqa: E402
from shared.config_models import LauncherSettings, SkillConfig  # noqa: E402


# ── the property ─────────────────────────────────────────────────────────────

def test_tab_of_defaults_to_nothing_and_round_trips():
    plain = SkillConfig.from_dict({"id": "a", "name": "A", "script": "a.py"})
    assert plain.tab_of == ""
    assert "tab_of" not in plain.to_dict(), "an ordinary tool's metadata must not grow a tab_of key"
    tab = SkillConfig.from_dict({"id": "a", "name": "A", "script": "a.py", "tab_of": "b"})
    assert tab.tab_of == "b"
    assert tab.to_dict()["tab_of"] == "b"
    assert SkillConfig.from_dict(tab.to_dict()).tab_of == "b"


def test_suitmk2_is_a_hidden_tab_of_the_toolbox_assistant():
    by_id = {s.id: s for s in discover_skills(REPO)}
    suit, host = by_id["suitmk2"], by_id["assistant"]
    assert suit.tab_of == "assistant"
    assert suit.hidden is True, "SuitMk2 still has a tile of its own"
    assert host.hidden is False and host.tab_of == ""
    assert suit.preload is True, "nothing asks for the hidden start that lets the companions follow Game.log"
    assert os.path.basename(resolve_script_path(host, REPO)) == "toolbox_assistant_app.py"
    # both tools still run on their own from their old entry scripts
    assert os.path.isfile(os.path.join(REPO, "tools", "Assistant", "assistant_app.py"))
    assert os.path.basename(resolve_script_path(suit, REPO)) == "suitmk2_companion_app.py"
    tiles = [s.id for s in tile_skills(list(by_id.values()))]
    assert "assistant" in tiles and "suitmk2" not in tiles
    assert suit.hotkey == "<ctrl>+2" and host.hotkey == "<ctrl>+3"


# ── the launcher ─────────────────────────────────────────────────────────────

class _MP:
    def __init__(self, pm, skill_id):
        self._pm, self.skill_id = pm, skill_id
        self.running = False
        self.visible = False

    def set_on_exit(self, cb):
        pass

    def toggle(self):
        self._pm.calls.append((self.skill_id, "toggle", None))

    def toggle_tab(self, tab, near=None):
        self._pm.calls.append((self.skill_id, "toggle_tab", tab))
        self._pm.near.append(near)

    def show(self):
        self._pm.calls.append((self.skill_id, "show", None))

    def show_tab(self, tab, near=None):
        self._pm.calls.append((self.skill_id, "show_tab", tab))
        self._pm.near.append(near)

    def stop(self):
        self._pm.calls.append((self.skill_id, "stop", None))

    def start_with_env(self, extra_env, visible_after=True):
        self._pm.calls.append((self.skill_id, "preload", dict(extra_env)))
        self.running = True
        return True


class _RecordingPM:
    def __init__(self):
        self.registered: dict[str, dict] = {}
        self.procs: dict[str, _MP] = {}
        self.calls: list = []
        self.near: list = []        # where the launcher said it was, per toggle_tab / show_tab

    def register(self, skill_id, python_exe, script, cwd, args, base_dir, env=None):
        self.registered[skill_id] = {"script": script, "env": dict(env or {})}
        self.procs[skill_id] = _MP(self, skill_id)

    def get(self, skill_id):
        return self.procs.get(skill_id)


class _NoWindow:
    def __init__(self):
        self.tiles = []

    def update_tile(self, sid, *a):
        self.tiles.append(sid)


def _launcher(settings: dict | None = None, skills=None):
    import skill_launcher

    app = skill_launcher.SCToolboxApp.__new__(skill_launcher.SCToolboxApp)
    app._skills = skills if skills is not None else discover_skills(REPO)
    app._settings = LauncherSettings.from_dict(settings or {}, app._skills)
    app._launcher_hotkey = "<ctrl>+0"
    app._python = sys.executable
    app._availability = {}
    app._pm = _RecordingPM()
    app._window = _NoWindow()
    app._hotkey_conflicts = []
    app._enqueue = lambda fn: fn()
    app._auto_hide_check = lambda: None
    app._register_skills({})
    return app


def test_suitmk2_gets_no_process_of_its_own():
    app = _launcher()
    assert "assistant" in app._pm.registered
    assert os.path.basename(app._pm.registered["assistant"]["script"]) == "toolbox_assistant_app.py"
    assert "suitmk2" not in app._pm.registered, "a second SuitMk2 process would be registered beside the window"
    assert app._availability["suitmk2"] is True


def test_ctrl_2_and_ctrl_3_reach_one_process_each_on_its_own_tab():
    app = _launcher()
    by_id = {s.id: s for s in app._skills}
    bindings = app._build_hotkey_bindings()
    assert by_id["suitmk2"].hotkey in bindings, f"Ctrl+2 is not bound; conflicts: {app._hotkey_conflicts}"
    assert by_id["assistant"].hotkey in bindings
    bindings[by_id["suitmk2"].hotkey]()         # Ctrl+2
    bindings[by_id["assistant"].hotkey]()       # Ctrl+3
    assert app._pm.calls == [("assistant", "toggle_tab", "suitmk2"), ("assistant", "toggle_tab", "assistant")]
    assert app._window.tiles == ["assistant", "assistant"], "the tile that lights up must be the one that exists"


def test_a_tool_without_tabs_is_toggled_exactly_as_before():
    app = _launcher()
    app._toggle_skill("dps")
    assert app._pm.calls == [("dps", "toggle", None)]


def test_the_tile_click_opens_the_assistant_tab():
    app = _launcher()
    app._toggle_skill("assistant")              # what LauncherWindow's on_toggle_skill calls for the tile
    assert app._pm.calls == [("assistant", "toggle_tab", "assistant")]


def test_ipc_that_names_suitmk2_reaches_the_window_on_its_tab():
    app = _launcher()
    app._dispatch({"type": "launch_skill", "skill_id": "suitmk2"})
    app._dispatch({"type": "toggle_skill", "skill_id": "suitmk2"})
    app._dispatch({"type": "launch_skill", "skill_id": "assistant"})
    app._dispatch({"type": "launch_skill", "skill_id": "dps"})
    assert app._pm.calls == [("assistant", "show_tab", "suitmk2"), ("assistant", "toggle_tab", "suitmk2"),
                             ("assistant", "show_tab", "assistant"), ("dps", "show", None)]


def test_the_preload_starts_the_window_suitmk2_is_a_tab_of():
    app = _launcher()
    app._preload_skills()
    preloads = [c for c in app._pm.calls if c[1] == "preload"]
    assert ("assistant", "preload", {"SC_TOOLBOX_PRELOAD": "1"}) in preloads, preloads
    assert not [c for c in preloads if c[0] == "suitmk2"]


def test_suitmk2_switched_off_in_settings_is_not_preloaded_and_is_not_a_tab():
    app = _launcher({"disabled_skills": ["suitmk2"]})
    assert app._pm.registered["assistant"]["env"].get("SC_TOOLBOX_TABS_OFF") == "suitmk2"
    app._preload_skills()
    assert not [c for c in app._pm.calls if c[0] == "assistant" and c[1] == "preload"], \
        "SuitMk2 is switched off, and the launcher still started it hidden inside the Assistant window"
    by_id = {s.id: s for s in app._skills}
    assert by_id["suitmk2"].hotkey not in app._build_hotkey_bindings()


def test_nothing_switched_off_means_no_tabs_are_off():
    app = _launcher()
    assert app._pm.registered["assistant"]["env"].get("SC_TOOLBOX_TABS_OFF") == ""     # "" = unset in the child
    assert "SC_TOOLBOX_TABS_OFF" not in app._pm.registered["dps"]["env"]


def test_a_tab_whose_host_is_not_installed_runs_on_its_own():
    skills = [s for s in discover_skills(REPO) if s.id != "assistant"]
    app = _launcher(skills=skills)
    assert "suitmk2" in app._pm.registered
    app._toggle_skill("suitmk2")
    assert app._pm.calls == [("suitmk2", "toggle", None)]


# ── where the launcher is, sent with the request ─────────────────────────────
# The window opens beside the launcher the first time (tools/Assistant/toolbox_assistant_app.py); only the launcher
# knows where the launcher is, and only at the moment of the press.

class _Pt:
    def __init__(self, x, y):
        self._x, self._y = x, y

    def x(self):
        return self._x

    def y(self):
        return self._y

    width, height = x, y


class _PlacedWindow(_NoWindow):
    def pos(self):
        return _Pt(100, 120)

    def size(self):
        return _Pt(500, 550)


def test_the_launcher_tells_a_tab_window_where_the_launcher_is():
    app = _launcher()
    app._window = _PlacedWindow()
    app._toggle_skill("suitmk2")                # Ctrl+2
    app._toggle_skill("assistant")              # Ctrl+3, and the tile
    app._dispatch({"type": "launch_skill", "skill_id": "suitmk2"})
    assert app._pm.near == [[100, 120, 500, 550]] * 3, app._pm.near


def test_a_launcher_stashed_off_screen_gives_the_place_it_will_come_back_to():
    app = _launcher()
    app._window = _PlacedWindow()
    app._autohide_stashed, app._autohide_pos = True, _Pt(300, 40)
    assert app._launcher_rect() == [300, 40, 500, 550]


def test_a_launcher_that_cannot_say_where_it_is_still_opens_the_tool():
    app = _launcher()                           # _NoWindow has no pos()
    app._toggle_skill("suitmk2")
    assert app._pm.calls == [("assistant", "toggle_tab", "suitmk2")] and app._pm.near == [None]


# ── the process: show / hide per tab ─────────────────────────────────────────

def _running_mp():
    mp = ManagedProcess(skill_id="assistant", python_exe="python", script="x.py", cwd=".", args=[],
                        base_dir=".")
    proc = MagicMock(spec=subprocess.Popen)
    proc.poll.return_value = None
    proc.pid = 4242
    mp._proc = proc
    mp._cmd_file = "cmd.jsonl"
    mp._visible = False                         # the preload state: running, hidden
    sent = []
    mp._send_unlocked = lambda cmd: sent.append(cmd) or True
    return mp, sent


def test_toggle_tab_asks_the_window_and_keeps_its_own_guess_of_the_result():
    mp, sent = _running_mp()
    mp.toggle_tab("assistant")                  # Ctrl+3: hidden -> shown on Assistant
    assert sent == [{"type": "toggle", "tab": "assistant"}] and mp.visible
    mp.toggle_tab("suitmk2")                    # Ctrl+2 while the Assistant tab is up: switch, do NOT hide
    assert sent[-1] == {"type": "toggle", "tab": "suitmk2"} and mp.visible
    mp.toggle_tab("suitmk2")                    # Ctrl+2 again: now it hides
    assert sent[-1] == {"type": "toggle", "tab": "suitmk2"} and not mp.visible
    mp.toggle_tab("assistant")
    assert sent[-1] == {"type": "toggle", "tab": "assistant"} and mp.visible
    assert len(sent) == 4


def test_toggle_tab_never_decides_to_hide_from_its_own_record():
    """The launcher's record says "shown, on the Assistant tab"; the user may have closed the window with its X or
    clicked the other tab since, and nothing told the launcher. A "hide" sent on that record did nothing in the
    first case and hid the window in the second (seen live, 2026-10-04)."""
    mp, sent = _running_mp()
    mp._visible, mp._tab = True, "assistant"
    mp.toggle_tab("assistant")
    assert sent == [{"type": "toggle", "tab": "assistant"}], sent


def test_show_tab_never_hides():
    mp, sent = _running_mp()
    mp.show_tab("suitmk2")
    mp.show_tab("suitmk2")
    assert sent == [{"type": "show", "tab": "suitmk2"}] * 2 and mp.visible


def test_a_hotkey_pressed_while_the_tool_is_not_running_starts_it_on_that_tab():
    mp = ManagedProcess(skill_id="assistant", python_exe="python", script="x.py", cwd=".", args=[], base_dir=".")
    sent, started = [], []

    def start():
        started.append(1)
        proc = MagicMock(spec=subprocess.Popen)
        proc.poll.return_value = None
        mp._proc = proc
        mp._visible = True
        return True

    mp._start_unlocked = start
    mp._send_unlocked = lambda cmd: sent.append(cmd) or True
    mp.toggle_tab("suitmk2")
    assert started == [1]
    assert sent == [{"type": "show", "tab": "suitmk2"}], "the new window was not told which tab was asked for"
    mp.toggle_tab("suitmk2")
    assert sent[-1] == {"type": "toggle", "tab": "suitmk2"}
