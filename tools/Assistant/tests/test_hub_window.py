"""The Assistant and SuitMk2 in one window, a tab each (J, 2026-10-04).

"Can we combine toolbox assistant and suit mk2 under the same tool and have a tab for each under the tool"

What is pinned here, and what each test is driving:

    one microphone      switching tabs takes the mic from one tab before the other gets it, with stand-in tabs
                        (the order) and with the two tools' REAL mic methods against fake ears (the effect)
    the voice gate      SuitMk2's companions are silent while the window is hidden and speak while it is open,
                        whichever tab is in front: SuitPanel's real gate, a real window shown and hidden
    Ctrl+2 / Ctrl+3     the launcher's {"type": "show", "tab": ...} for each id selects that tab
    lazy                the tab that is not asked for is not built
    quit                both tabs are shut down, once; with the real SuitPanel._quit and AssistantPanel.shutdown
    the entry script    which tab comes first, a tool disabled in Settings is not a tab, the window's name is the
                        skill.json name, and the two tools' module names do not collide in one process

Nothing here builds the real tools: SuitMk2's constructor starts the companion core and the model service, and
the Assistant's opens a microphone. The real classes are created without their __init__ and given fakes for the
parts each method touches, the way tools/SuitMk2/tests/test_shutdown_once.py does.

Each test was watched failing against a deliberately broken copy of the code (the commit message lists the breaks).
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys

import pytest
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QWidget

HERE = os.path.dirname(os.path.abspath(__file__))
A_ROOT = os.path.normpath(os.path.join(HERE, ".."))
REPO = os.path.normpath(os.path.join(A_ROOT, "..", ".."))

from assistant import hub  # noqa: E402
from assistant import panel as panel_mod  # noqa: E402


@pytest.fixture
def app(monkeypatch):
    a = QApplication.instance() or QApplication([])
    # the window saves its position to <repo>/logs on hide and quit; a test must not
    monkeypatch.setattr(hub, "_save_window_state", lambda _w: None)
    monkeypatch.setattr(hub, "_LIVE", None)
    yield a
    for w in list(a.topLevelWidgets()):
        w.hide()
        w.deleteLater()
    a.processEvents()


# ── stand-in tabs ────────────────────────────────────────────────────────────

class Mic:
    """Who is listening, and the most that ever were at once."""

    def __init__(self):
        self.on = set()
        self.most = 0
        self.log = []

    def take(self, who):
        self.on.add(who)
        self.most = max(self.most, len(self.on))
        self.log.append("+" + who)

    def release(self, who):
        self.on.discard(who)
        self.log.append("-" + who)


class Tab(QWidget):
    def __init__(self, key, mic, listening):
        super().__init__()
        self.key, self.mic = key, mic
        self.shutdowns = 0
        self.visibility = []
        if listening:
            mic.take(key)

    def mic_take(self):
        self.mic.take(self.key)

    def mic_release(self):
        self.mic.release(self.key)

    def host_visibility_changed(self):
        self.visibility.append(self.window().isVisible())

    def shutdown(self):
        self.shutdowns += 1


def make_hub(first="suitmk2", **kw):
    mic, built = Mic(), []

    def spec(key, label):
        def build(listening):
            built.append(key)
            return Tab(key, mic, listening)
        return hub.TabSpec(key, label, build)

    w = hub.HubWindow("Toolbox Assistant", [spec("assistant", "Assistant"), spec("suitmk2", "Suit Mk2")],
                      first=first, **kw)
    return w, mic, built


# ── one microphone ───────────────────────────────────────────────────────────

def test_only_the_tab_asked_for_is_built_and_it_has_the_microphone(app):
    w, mic, built = make_hub(first="suitmk2")
    assert built == ["suitmk2"], "the Assistant was built at start although nobody asked for its tab"
    assert w.page("assistant") is None
    assert mic.on == {"suitmk2"}
    assert w.current_tab() == "suitmk2"


def test_switching_tabs_hands_the_microphone_over_and_two_never_listen(app):
    w, mic, built = make_hub(first="suitmk2")
    w.select("assistant")
    assert built == ["suitmk2", "assistant"]
    assert mic.on == {"assistant"}
    w.select("suitmk2")
    assert mic.on == {"suitmk2"}
    w.select("assistant")
    assert mic.on == {"assistant"}
    assert built == ["suitmk2", "assistant"], "a tab was built a second time"
    assert mic.most == 1, f"two tabs were listening at once: {mic.log}"
    # the order is the point: release first, then take
    assert mic.log == ["+suitmk2", "-suitmk2", "+assistant", "-assistant", "+suitmk2", "-suitmk2", "+assistant"]


def test_selecting_the_tab_already_in_front_changes_nothing(app):
    w, mic, _built = make_hub(first="assistant")
    before = list(mic.log)
    assert w.select("assistant") is True
    assert mic.log == before
    assert w._buttons["assistant"].isChecked() and not w._buttons["suitmk2"].isChecked()


def test_clicking_a_tab_button_is_the_same_as_selecting_it(app):
    w, mic, _built = make_hub(first="assistant")
    w._buttons["suitmk2"].click()
    assert w.current_tab() == "suitmk2" and mic.on == {"suitmk2"}
    assert w._stack.currentWidget() is w.page("suitmk2")
    assert w._buttons["suitmk2"].isChecked() and not w._buttons["assistant"].isChecked()


def test_hiding_the_window_does_not_move_the_microphone(app):
    """Each tool kept listening while its own window was hidden; the tab last in front still does."""
    w, mic, _built = make_hub(first="assistant")
    w.show()
    w.hide()
    assert mic.on == {"assistant"}
    assert w.current_tab() == "assistant"


def test_a_tab_that_cannot_be_built_does_not_take_the_other_down(app):
    def boom(_listening):
        raise RuntimeError("no such model")

    mic = Mic()
    w = hub.HubWindow("T", [hub.TabSpec("assistant", "Assistant", lambda m: Tab("assistant", mic, m)),
                            hub.TabSpec("suitmk2", "Suit Mk2", boom)], first="assistant")
    assert w.select("suitmk2") is True
    assert isinstance(w.page("suitmk2"), QLabel) and "no such model" in w.page("suitmk2").text()
    assert w.select("assistant") is True
    assert mic.on == {"assistant"}


# ── the two tools' real microphone methods ───────────────────────────────────

class FakeEars:
    """Stands in for either tool's EarsController: armed or not, and how often it was armed."""

    def __init__(self, mode="always"):
        self._mode, self._armed = mode, False
        self.arms = 0

    def mode(self):
        return self._mode

    def armed(self):
        return self._armed

    def recording(self):
        return False

    def arm(self):
        self.arms += 1
        self._armed = True
        return True

    def disarm(self):
        self._armed = False

    def shutdown(self):
        self.disarm()


def _suit_module():
    """SuitMk2's real suit_window, loaded by path under a private name (the toolbox root has a `ui` package of
    its own; see test_shutdown_once.py for what importing it as `ui.suit_window` does to a whole-suite run)."""
    name = "_suitmk2_ui_suit_window_for_hub_tests"
    if name in sys.modules:
        return sys.modules[name]
    path = os.path.join(REPO, "tools", "SuitMk2", "ui", "suit_window.py")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class FakeSpeech:
    def __init__(self):
        self.muted = None
        self.closes = 0

    def mute(self, on=True):
        self.muted = bool(on)

    def close(self):
        self.closes += 1


class FakeCore:
    def __init__(self):
        self.stops = 0

    def stop(self):
        self.stops += 1


def real_suit_panel(mic, user_muted=False):
    """The REAL SuitPanel, without the constructor that boots the companion core and the model service."""
    SuitPanel = _suit_module().SuitPanel
    p = SuitPanel.__new__(SuitPanel)
    QWidget.__init__(p)
    p._mic_mine = bool(mic)
    p.s = {"talk_mode": "always", "muted": user_muted}
    p.ears = FakeEars("always")
    p.speech = FakeSpeech()
    p.core = FakeCore()
    p.sidecar = None
    p.setup = None
    p._fb_mon = {}
    p._notice_pending = False
    p._arm_ears()                      # what the constructor's start-up timer does
    return p


def real_assistant_panel(mic):
    """The REAL AssistantPanel's microphone path: its hidden Ears button wired to the real _on_ears_toggled."""
    cls = panel_mod.AssistantPanel
    p = cls.__new__(cls)
    QWidget.__init__(p)
    p._mic_mine = bool(mic)
    p._ears = FakeEars("always")
    p._mouth = type("M", (), {"stops": 0, "stop": lambda self: setattr(self, "stops", self.stops + 1)})()
    p._btn_ears = QPushButton("Ears", p)
    p._btn_ears.setCheckable(True)
    p._btn_ears.toggled.connect(p._on_ears_toggled)
    p._saved = 0
    p._save_state = lambda: setattr(p, "_saved", p._saved + 1)
    p._ensure_ears()                   # the last line of the real constructor
    return p


def real_hub(first):
    made = {}

    def assistant(mic):
        made["assistant"] = real_assistant_panel(mic)
        return made["assistant"]

    def suit(mic):
        made["suitmk2"] = real_suit_panel(mic)
        return made["suitmk2"]

    w = hub.HubWindow("Toolbox Assistant", [hub.TabSpec("assistant", "Assistant", assistant),
                                            hub.TabSpec("suitmk2", "Suit Mk2", suit)], first=first)
    return w, made


def _armed(made):
    out = set()
    if "assistant" in made and made["assistant"]._ears.armed():
        out.add("assistant")
    if "suitmk2" in made and made["suitmk2"].ears.armed():
        out.add("suitmk2")
    return out


def test_with_the_real_panels_only_the_tab_in_front_has_armed_ears(app):
    w, made = real_hub("suitmk2")
    assert _armed(made) == {"suitmk2"}
    w.select("assistant")
    assert _armed(made) == {"assistant"}
    w.select("suitmk2")
    assert _armed(made) == {"suitmk2"}
    w.select("assistant")
    assert _armed(made) == {"assistant"}


def test_a_tab_without_the_microphone_cannot_open_it_by_a_side_door(app):
    """Changing the mic mode or key re-arms the ears in both tools. Not while the other tab is listening."""
    w, made = real_hub("suitmk2")
    w.select("assistant")
    suit, assistant = made["suitmk2"], made["assistant"]
    suit._arm_ears()                                    # what _set_talk_key / _set_talk_mode / the timer call
    assert not suit.ears.armed(), "SuitMk2 opened its mic while the Assistant tab was the one listening"
    w.select("suitmk2")
    assistant._ensure_ears()                            # what _set_mic_mode / _binding_captured call
    assistant._btn_ears.setChecked(True)                # and the button's own path
    assert not assistant._ears.armed(), "the Assistant opened its mic while the SuitMk2 tab was listening"
    assert not assistant._btn_ears.isChecked()
    assert _armed(made) == {"suitmk2"}


def test_each_tool_in_its_own_window_still_arms_as_before(app):
    """The standalone windows never have the microphone taken: _mic_mine is True unless a hub says otherwise."""
    assert _suit_module().SuitWindow._mic_mine is True
    assert panel_mod.AssistantWindow._mic_mine is True
    assert panel_mod.AssistantWindow._owns_window is True and panel_mod.AssistantPanel._owns_window is False
    assert _armed({"suitmk2": real_suit_panel(True), "assistant": real_assistant_panel(True)}) == {"assistant", "suitmk2"}


# ── SuitMk2's voice gate, in a real window ───────────────────────────────────

def test_the_companions_are_silent_while_the_window_is_hidden_and_speak_while_it_is_open(app):
    w, made = real_hub("suitmk2")                       # the preload state: built, never shown
    suit = made["suitmk2"]
    suit._apply_voice_gate()
    assert suit.speech.muted is True, "the companions would talk for a window nobody has opened"
    w.show()
    app.processEvents()
    assert suit.speech.muted is False
    w.hide()
    app.processEvents()
    assert suit.speech.muted is True, "closing the window did not silence the companions"


def test_the_companions_keep_speaking_while_the_assistant_tab_is_the_one_in_front(app):
    """The rule is about the tool being open, not about which tab is showing."""
    w, made = real_hub("suitmk2")
    suit = made["suitmk2"]
    w.show()
    w.select("assistant")
    app.processEvents()
    assert not suit.isVisible(), "this test needs the SuitMk2 tab to be the one that is not in front"
    assert suit.speech.muted is False, "the companions went silent because the other tab is in front"
    w.hide()
    app.processEvents()
    assert suit.speech.muted is True, "a tab that is not in front was not told the window closed"
    w.show()
    app.processEvents()
    assert suit.speech.muted is False, "a tab that is not in front was not told the window opened"


def test_his_mute_button_still_wins_while_the_window_is_open(app):
    made = {}

    def suit(mic):
        made["s"] = real_suit_panel(mic, user_muted=True)
        return made["s"]

    w = hub.HubWindow("T", [hub.TabSpec("suitmk2", "Suit Mk2", suit)], first="suitmk2")
    w.show()
    app.processEvents()
    assert made["s"].speech.muted is True
    assert made["s"].s["muted"] is True


# ── the launcher's commands ──────────────────────────────────────────────────

def test_ctrl_2_and_ctrl_3_open_the_window_on_their_own_tab(app):
    """core/process_manager.py show_tab sends {"type": "show", "tab": <skill id>}."""
    w, _mic, _built = make_hub(first="suitmk2")
    assert w.isHidden()
    w.handle_ipc_command({"type": "show", "tab": "assistant"})          # Ctrl+3
    assert w.isVisible() and w.current_tab() == "assistant"
    assert w._stack.currentWidget() is w.page("assistant")
    w.handle_ipc_command({"type": "show", "tab": "suitmk2"})            # Ctrl+2
    assert w.isVisible() and w.current_tab() == "suitmk2"
    assert w._stack.currentWidget() is w.page("suitmk2")
    w.handle_ipc_command({"type": "hide"})
    assert w.isHidden() and w.current_tab() == "suitmk2"


def test_show_without_a_tab_or_with_an_unknown_one_keeps_the_tab_in_front(app):
    w, mic, built = make_hub(first="assistant")
    w.handle_ipc_command({"type": "show"})
    assert w.isVisible() and w.current_tab() == "assistant"
    w.handle_ipc_command({"type": "show", "tab": "starmap"})
    assert w.isVisible() and w.current_tab() == "assistant"
    assert built == ["assistant"] and mic.on == {"assistant"}


# ── the launcher's hotkeys, against what the user did inside the window ──────
# The launcher cannot see the X or a click on a tab: both happen in this process. Live on J's launcher,
# 2026-10-04 21:10 and 21:13 (the tile is the same launcher call as the hotkey, _toggle_skill):
#     X on the window, then the tile                        -> nothing happened; a second click opened it
#     the tile, a click on "Suit Mk2", then the tile again  -> the window HID instead of going to the Assistant
# In both the launcher had sent {"type": "hide"}, from its own record of the window (shown, on the Assistant tab),
# which the X and the click had made wrong. These drive the launcher's REAL ManagedProcess.toggle_tab and hand what
# it sends straight to the window, as the command file does.

def _launcher_for(w):
    from unittest.mock import MagicMock
    from core.process_manager import ManagedProcess
    mp = ManagedProcess(skill_id="assistant", python_exe="python", script="x.py", cwd=".", args=[], base_dir=".")
    proc = MagicMock(spec=subprocess.Popen)
    proc.poll.return_value = None
    proc.pid = 4242
    mp._proc = proc
    mp._cmd_file = "cmd.jsonl"
    mp._visible = False                         # the preload state: running, hidden
    sent = []

    def send(cmd):
        sent.append(cmd)
        w.handle_ipc_command(cmd)
        return True

    mp._send_unlocked = send
    return mp, sent


def test_the_hotkey_after_the_close_button_opens_the_window_at_the_first_press(app):
    w, _mic, _built = make_hub(first="suitmk2", standalone=False)
    mp, sent = _launcher_for(w)
    mp.toggle_tab("assistant")                              # Ctrl+3
    assert w.isVisible() and w.current_tab() == "assistant"
    w._on_close()                                           # the X: hidden, and the launcher is not told
    assert w.isHidden()
    mp.toggle_tab("assistant")                              # Ctrl+3 again
    assert w.isVisible() and w.current_tab() == "assistant",         "Ctrl+3 after the X did nothing: the launcher sent %r to a window that was already hidden" % (sent[-1],)


def test_the_other_tabs_hotkey_after_a_click_on_a_tab_goes_to_that_tab(app):
    w, mic, _built = make_hub(first="suitmk2", standalone=False)
    mp, sent = _launcher_for(w)
    mp.toggle_tab("assistant")                              # Ctrl+3
    w._buttons["suitmk2"].click()                           # he clicks the Suit Mk2 tab
    assert w.isVisible() and w.current_tab() == "suitmk2"
    mp.toggle_tab("assistant")                              # Ctrl+3: he wants the Assistant back
    assert w.isVisible() and w.current_tab() == "assistant",         "Ctrl+3 from the Suit Mk2 tab hid the window instead of opening the Assistant (sent %r)" % (sent[-1],)
    assert mic.on == {"assistant"} and mic.most == 1


def test_the_front_tabs_hotkey_after_a_click_on_a_tab_hides_at_the_first_press(app):
    w, _mic, _built = make_hub(first="suitmk2", standalone=False)
    mp, sent = _launcher_for(w)
    mp.toggle_tab("assistant")                              # Ctrl+3
    w._buttons["suitmk2"].click()                           # he clicks the Suit Mk2 tab
    mp.toggle_tab("suitmk2")                                # Ctrl+2, the tab he is looking at
    assert w.isHidden(), "Ctrl+2 on the Suit Mk2 tab did not hide the window (sent %r)" % (sent[-1],)
    mp.toggle_tab("suitmk2")
    assert w.isVisible() and w.current_tab() == "suitmk2"


def test_the_hotkeys_with_nothing_done_inside_the_window_are_as_before(app):
    w, mic, _built = make_hub(first="suitmk2", standalone=False)
    mp, _sent = _launcher_for(w)
    mp.toggle_tab("assistant")                              # hidden -> the Assistant
    assert w.isVisible() and w.current_tab() == "assistant"
    mp.toggle_tab("suitmk2")                                # the other tab: switch, do not hide
    assert w.isVisible() and w.current_tab() == "suitmk2"
    mp.toggle_tab("suitmk2")                                # the same tab: hide
    assert w.isHidden() and w.current_tab() == "suitmk2"
    mp.toggle_tab("assistant")                              # hidden -> the Assistant
    assert w.isVisible() and w.current_tab() == "assistant"
    assert mic.most == 1


def test_the_close_button_hides_under_the_launcher_and_quits_by_hand(app, monkeypatch):
    quits = []
    monkeypatch.setattr(hub.QApplication, "quit", staticmethod(lambda: quits.append(1)))
    w, _mic, _built = make_hub(first="assistant", standalone=False)
    w.show()
    w._on_close()
    assert w.isHidden() and not quits and w.page("assistant").shutdowns == 0
    w2, _mic2, _built2 = make_hub(first="assistant", standalone=True)
    w2.show()
    w2._on_close()
    assert quits == [1] and w2.page("assistant").shutdowns == 1


def test_a_tool_inside_the_window_can_bring_another_tab_forward(app):
    """"Open the suit", said to the Assistant: its launch_tool asks for the tab instead of starting SuitMk2."""
    assert hub.hosted_tabs() == [] and hub.show_tab("suitmk2") is False     # no window in this process yet
    w, _mic, _built = make_hub(first="assistant")
    assert hub.hosted_tabs() == ["assistant", "suitmk2"]
    assert hub.show_tab("starmap") is False
    assert hub.show_tab("suitmk2") is True
    app.processEvents()
    assert w.isVisible() and w.current_tab() == "suitmk2"


def test_launch_tool_switches_tabs_instead_of_starting_a_second_suitmk2(app, monkeypatch):
    from assistant import builtin_tools, headless, ipc_bus
    w, _mic, _built = make_hub(first="assistant")
    monkeypatch.setattr(headless, "resolve_skill", lambda base, name: {"id": "suitmk2", "name": "SuitMk2"})
    started = []
    monkeypatch.setattr(ipc_bus, "is_running", lambda sid: False)
    monkeypatch.setattr(ipc_bus, "launcher_cmd_file", lambda: None)
    monkeypatch.setattr(ipc_bus, "spawn_skill", lambda base, sid: started.append(sid) or True)
    monkeypatch.setattr(ipc_bus, "wait_ready", lambda sid, timeout=0: True)
    out = builtin_tools._launch_tool.func(type("Ctx", (), {"base_dir": REPO})(), "suit")
    app.processEvents()
    assert started == [], "the Assistant started a second SuitMk2 beside the one in its own window"
    assert out["via"] == "already running"
    assert w.current_tab() == "suitmk2"


# ── quit ─────────────────────────────────────────────────────────────────────

def test_quit_shuts_down_every_tab_that_exists_once(app, monkeypatch):
    quits = []
    monkeypatch.setattr(hub.QApplication, "quit", staticmethod(lambda: quits.append(1)))
    w, _mic, built = make_hub(first="suitmk2")
    w.select("assistant")
    w.handle_ipc_command({"type": "quit"})
    w._quit()                                           # aboutToQuit calls it again
    w.handle_ipc_command({"type": "quit"})
    assert [w.page(k).shutdowns for k in ("assistant", "suitmk2")] == [1, 1]
    assert quits == [1]


def test_quit_does_not_build_a_tab_nobody_opened(app, monkeypatch):
    monkeypatch.setattr(hub.QApplication, "quit", staticmethod(lambda: None))
    w, _mic, built = make_hub(first="suitmk2")
    w._quit()
    assert built == ["suitmk2"] and w.page("suitmk2").shutdowns == 1


def test_one_tab_failing_to_shut_down_does_not_spare_the_other(app, monkeypatch):
    quits = []
    monkeypatch.setattr(hub.QApplication, "quit", staticmethod(lambda: quits.append(1)))
    w, _mic, _built = make_hub(first="assistant")
    w.select("suitmk2")

    def boom():
        raise RuntimeError("worker would not stop")

    w.page("assistant").shutdown = boom
    w._quit()
    assert w.page("suitmk2").shutdowns == 1
    assert quits == [1]


def test_quit_tears_down_both_real_tools(app, monkeypatch):
    """The real SuitPanel._quit and the real AssistantPanel.shutdown, through the window's quit."""
    from assistant import worker_pool
    pool_stops = []
    monkeypatch.setattr(worker_pool, "shutdown_all", lambda: pool_stops.append(1))
    w, made = real_hub("suitmk2")
    w.select("assistant")
    suit, assistant = made["suitmk2"], made["assistant"]
    core = suit.core

    seen = [0]

    def fuse():                                         # Qt re-emits aboutToQuit from inside quit(); bound it
        seen[0] += 1
        if seen[0] > 10:
            app.aboutToQuit.disconnect(w._quit)

    app.aboutToQuit.connect(fuse)
    app.aboutToQuit.connect(w._quit)                    # as toolbox_assistant_app.main() wires it
    try:
        w.handle_ipc_command({"type": "quit"})
        app.processEvents()
    finally:
        for slot in (fuse, w._quit):
            try:
                app.aboutToQuit.disconnect(slot)
            except (RuntimeError, TypeError):
                pass

    assert core.stops == 1, "SuitMk2's companion core was not stopped (or was stopped more than once)"
    assert suit.speech.closes == 1
    assert not suit.ears.armed()
    assert not assistant._ears.armed(), "the Assistant's mic was left open"
    assert assistant._mouth.stops == 1
    assert assistant._saved == 1, "the Assistant's settings were not saved"
    assert pool_stops == [1], "the Assistant's worker subprocesses were not stopped"


# ── the entry script ─────────────────────────────────────────────────────────

def _entry():
    name = "_toolbox_assistant_app_under_test"
    if name in sys.modules:
        return sys.modules[name]
    saved_path, saved_ui = list(sys.path), sys.modules.get("ui")
    spec = importlib.util.spec_from_file_location(name, os.path.join(A_ROOT, "toolbox_assistant_app.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    finally:                    # the script arranges sys.path for its own process; leave this one as it was
        sys.path[:] = saved_path
        if saved_ui is not None:
            sys.modules["ui"] = saved_ui
    return mod


def test_which_tab_is_built_first():
    e = _entry()
    assert e.first_tab(preload=True, pending=[]) == "suitmk2", "hidden start must not build the Assistant"
    assert e.first_tab(preload=False, pending=[]) == "assistant"
    # Ctrl+2 pressed while the tool was not running: the launcher starts it and queues the tab
    assert e.first_tab(preload=False, pending=[{"type": "show", "tab": "suitmk2"}]) == "suitmk2"
    assert e.first_tab(preload=True, pending=[{"type": "show", "tab": "assistant"}]) == "assistant"
    assert e.first_tab(preload=False, pending=[{"type": "show", "tab": "nope"}, {"type": "hide"}]) == "assistant"


def test_the_tab_keys_are_the_launchers_skill_ids(monkeypatch):
    e = _entry()
    monkeypatch.delenv("SC_TOOLBOX_TABS_OFF", raising=False)
    keys = [t.key for t in e.build_tabs(None)]
    assert keys == ["assistant", "suitmk2"]
    with open(os.path.join(REPO, "tools", "SuitMk2", "skill.json"), encoding="utf-8") as f:
        suit = json.load(f)
    with open(os.path.join(A_ROOT, "skill.json"), encoding="utf-8") as f:
        assistant = json.load(f)
    assert suit["id"] == "suitmk2" and assistant["id"] == "assistant"
    assert suit["tab_of"] == assistant["id"]
    assert assistant["script"] == "toolbox_assistant_app.py"


def test_a_tool_disabled_in_settings_is_not_a_tab(monkeypatch):
    e = _entry()
    monkeypatch.setenv("SC_TOOLBOX_TABS_OFF", "suitmk2")
    assert [t.key for t in e.build_tabs(None)] == ["assistant"]
    monkeypatch.setenv("SC_TOOLBOX_TABS_OFF", "assistant")
    assert [t.key for t in e.build_tabs(None)] == ["suitmk2"]
    monkeypatch.setenv("SC_TOOLBOX_TABS_OFF", "assistant,suitmk2")
    assert [t.key for t in e.build_tabs(None)] == ["assistant", "suitmk2"], "a window with no tabs at all"


def test_the_window_is_named_by_one_field(tmp_path, monkeypatch):
    """The tile name is undecided (2026-10-04): renaming is the "name" in tools/Assistant/skill.json, nothing else."""
    e = _entry()
    with open(os.path.join(A_ROOT, "skill.json"), encoding="utf-8") as f:
        assert e.tool_name() == json.load(f)["name"]
    (tmp_path / "skill.json").write_text(json.dumps({"name": "Companions"}), encoding="utf-8")
    monkeypatch.setattr(e, "HERE", str(tmp_path))
    assert e.tool_name() == "Companions"
    monkeypatch.setattr(e, "HERE", str(tmp_path / "missing"))
    assert e.tool_name() == e.FALLBACK_NAME


_NAMES_PROBE = r"""
import json, os, sys
sys.argv = [sys.argv[1]]
import importlib.util
spec = importlib.util.spec_from_file_location("toolbox_assistant_app", sys.argv[0])
app = importlib.util.module_from_spec(spec)
sys.modules["toolbox_assistant_app"] = app
spec.loader.exec_module(app)
before = app.check_module_names()
SuitPanel = app._suit_panel_class()
import ui, settings, speech
from core.skill_registry import discover_skills
from assistant import headless, ipc_bus, panel
import core
print(json.dumps({
    "before": before, "after": app.check_module_names(),
    "ui": ui.__file__, "core": core.__file__, "settings": settings.__file__, "speech": speech.__file__,
    "suit_panel": sys.modules[SuitPanel.__module__].__file__,
    "panel": panel.__file__,
    "skills": [s.id for s in discover_skills(app.BASE_DIR)],
    "preload_env": os.environ.get("SC_TOOLBOX_PRELOAD"),
}))
"""


def test_the_two_tools_module_names_do_not_collide_in_one_process(tmp_path):
    """A fresh process, set up by the real entry script, importing both tools: `ui` is SuitMk2's, `core` is the
    toolbox's (the Assistant's tool launcher needs core.skill_registry), SuitMk2's bare modules are its own.
    Imports only: no window, no microphone, no model service."""
    env = dict(os.environ)
    env.update({"QT_QPA_PLATFORM": "offscreen", "PYTHONIOENCODING": "utf-8",
                "USERPROFILE": str(tmp_path), "HOME": str(tmp_path), "APPDATA": str(tmp_path)})
    env.pop("PYTHONPATH", None)
    proc = subprocess.run([sys.executable, "-c", _NAMES_PROBE, os.path.join(A_ROOT, "toolbox_assistant_app.py")],
                          cwd=A_ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=180)
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("{")]
    assert proc.returncode == 0 and lines, f"rc={proc.returncode}\n{proc.stdout}\n{proc.stderr[-3000:]}"
    out = json.loads(lines[-1])

    def under(path, *parts):
        return os.path.normcase(os.path.abspath(path)).startswith(os.path.normcase(os.path.join(REPO, *parts)) + os.sep)

    assert out["before"] == [] and out["after"] == [], out
    assert under(out["ui"], "tools", "SuitMk2", "ui"), out["ui"]
    assert under(out["suit_panel"], "tools", "SuitMk2", "ui"), out["suit_panel"]
    assert under(out["core"], "core"), f"`core` is not the toolbox's package: {out['core']}"
    assert under(out["settings"], "tools", "SuitMk2", "core") and under(out["speech"], "tools", "SuitMk2", "core")
    assert under(out["panel"], "tools", "Assistant", "assistant")
    assert {"assistant", "suitmk2", "starmap"} <= set(out["skills"])


def test_check_module_names_says_so_when_the_wrong_ui_is_loaded(monkeypatch):
    e = _entry()
    fake = type(sys)("ui")
    fake.__file__ = os.path.join(REPO, "ui", "__init__.py")              # the launcher's
    monkeypatch.setitem(sys.modules, "ui", fake)
    problems = e.check_module_names()
    assert any("'ui'" in p for p in problems), problems
    with pytest.raises(ImportError):
        e._suit_panel_class()
