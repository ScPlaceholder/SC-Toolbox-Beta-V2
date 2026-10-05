"""A push-to-talk key each: the key that is held decides which AI hears it (J, 2026-10-05).

"For the assistant and suit Mk 2 can you have individual push to talk buttons which also auto-route to the
right ai?"

What is pinned here:

    the key decides         the Assistant's key held with SuitMk2's tab showing reaches the Assistant's agent and
                            nothing else; SuitMk2's key held with the Assistant's tab showing reaches the
                            companions and nothing else. The tab in front never changes.
    one microphone          both keys held: one capture, the first key's; the second is ignored
    an open mic gives way   a held key takes the microphone from the front tab's always-open mic, which opens
                            again when the key is let go
    the same key twice      two tabs set to one key cannot be told apart: only the tab in front keeps it, and
                            both tabs say so; a different key puts it right
    rebinding               a new key is watched at once and is the key after a restart, in both tools
    old settings            a file saved before there was a default loads, and gets the default
    who is listening        the strip on screen is told which AI, while held and afterwards
    no microphone / no STT  said on screen, and nothing is sent to either AI
    warm                    a tab nobody has opened is built behind the front one and hears its key
    the voice gate          with the window hidden SuitMk2 still says nothing of its own accord; the answer to a
                            question asked with its key is spoken, and only that
    a companion named       "Oh have it decide unless the user specifically says an ai" (J, 2026-10-05): through
                            SuitMk2's key and through its always-open mic, the companion addressed by name
                            answers; a name said to the Assistant's key changes nothing and stays there

How. Both tools' REAL panel classes, made without the constructors that boot an agent, a companion core and a
model service (as tests/test_hub_window.py does), but with their REAL ears, built and wired by each panel's own
_make_ears(), in the REAL HubWindow. Three things are stand-ins, because a test must not touch the machine it
runs on:

    the hotkey layer     HotkeyMonitor.start() installs nothing and records the binding it was given; a key is
                         "pressed" by emitting the monitor's own triggered signal. pynput's Listener.start is
                         also made to fail the test, so no hook can be installed by a path nobody thought of.
    the microphone       `sounddevice` is a stand-in that counts open streams
    speech-to-text       each ears' whisper model is a stand-in that returns the sentence the test "said"

Everything between those is the code that runs in the tool: _on_trigger, the one-microphone floor, _begin,
_finish, _transcribe, the transcript signal, each panel's _on_transcript.

Each test was watched failing with the code it protects reverted (the commit message says which revert).
"""
from __future__ import annotations

import json
import sys
import time
import types
from pathlib import Path

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QWidget

import test_hub_window as H          # the stand-ins there, and the loaders of SuitMk2's window and the entry script
from assistant import hub
from assistant import panel as panel_mod
from assistant import voice as voice_mod
from assistant import voice_input
from shared import mic_floor, ptt_keys

app = H.app                          # the QApplication fixture (it also keeps window positions off the disk)


class FakeSd:
    """Stands in for the sounddevice module: how many input streams are open, and the most there ever were."""

    def __init__(self):
        self.open = 0
        self.most = 0
        self.opened = 0
        self.fail = ""                # set to make opening the microphone fail
        outer = self

        class InputStream:
            def __init__(self, **kw):
                if outer.fail:
                    raise OSError(outer.fail)
                self.live = False

            def start(self):
                self.live = True
                outer.open += 1
                outer.opened += 1
                outer.most = max(outer.most, outer.open)

            def stop(self):
                if self.live:
                    self.live = False
                    outer.open -= 1

            def close(self):
                self.stop()

        self.InputStream = InputStream
        self.default = types.SimpleNamespace(device=(0, 0))

    def query_devices(self, *_a):
        return {"name": "test microphone"}


class Model:
    """Stands in for a loaded whisper model: 'hears' the sentence it was made with."""

    def __init__(self, text="", broken=""):
        self.text, self.broken = text, broken

    def transcribe(self, _pcm, **_kw):
        if self.broken:
            raise RuntimeError(self.broken)
        return [types.SimpleNamespace(text=self.text)], None


class Strip:
    """Stands in for the on-screen strip (assistant/ptt_overlay.py): what it was told to show."""

    def __init__(self):
        self.shown = []

    def show_state(self, who, what, text="", key=""):
        self.shown.append((who, what, text, key))

    def whats(self, who):
        return [what for w, what, _t, _k in self.shown if w == who]


class Core:
    """Stands in for SuitMk2's CompanionCore: what the companions were asked."""

    def __init__(self):
        self.asked = []
        self.who = []                 # which companion each question was given to
        self.notes = []
        self.gate_state = types.SimpleNamespace(pilot_speaking=False)
        self.state = self.volatile = None
        self.feedback = types.SimpleNamespace(press=lambda _n: None)

    def _note(self, msg):
        self.notes.append(msg)

    def voice_command(self, _text):
        return False

    def answer(self, spec, text):
        self.asked.append(text)
        self.who.append(spec["speaker"])

    def stop(self):
        pass


@pytest.fixture
def rig(app, monkeypatch, tmp_path):
    """Everything the two tools' ears touch outside the process, replaced; see the module docstring."""
    suit = H._suit_module()
    r = types.SimpleNamespace(suit=suit, sd=FakeSd(), watched=[], floor=mic_floor.MicFloor(), strip=Strip(),
                              tmp=tmp_path, app=app)

    # the hotkey layer
    def start_assistant(self, binding):
        self._binding = binding
        r.watched.append(("assistant", ptt_keys.ident({"kind": binding.kind, "code": binding.code})))
        return True

    def start_suit(self, binding):
        self._binding = binding
        r.watched.append(("suitmk2", ptt_keys.ident(binding.to_dict())))
        return True

    monkeypatch.setattr(voice_input.HotkeyMonitor, "start", start_assistant)
    monkeypatch.setattr(suit.HotkeyMonitor, "start", start_suit)
    from pynput import keyboard, mouse

    def no_hooks(self, *_a, **_k):
        raise AssertionError("a test tried to install a real input hook")
    monkeypatch.setattr(keyboard.Listener, "start", no_hooks)
    monkeypatch.setattr(mouse.Listener, "start", no_hooks)

    # the microphone and speech-to-text
    monkeypatch.setitem(sys.modules, "sounddevice", r.sd)
    monkeypatch.setattr("shared.mic.stream_device", lambda refresh=True: None)
    monkeypatch.setattr("assistant.missing_voice_deps", lambda: [])
    monkeypatch.setattr(sys.modules["voice_in"], "missing_deps", lambda: [])
    monkeypatch.setattr(voice_mod.EarsController, "_preload_model", lambda self: None, raising=False)
    monkeypatch.setattr(suit.EarsController, "_preload_model", lambda self: None, raising=False)

    # one floor per test, and the two tools' settings in a folder of the test's own
    monkeypatch.setattr(panel_mod, "FLOOR", r.floor)
    monkeypatch.setattr(suit, "FLOOR", r.floor)
    monkeypatch.setattr(panel_mod, "_STATE_PATH", str(tmp_path / "assistant_panel.json"))
    monkeypatch.setattr(suit.st, "DIR", tmp_path / "suitmk2")
    monkeypatch.setattr(suit.st, "PATH", tmp_path / "suitmk2" / "settings.json")
    monkeypatch.setattr(suit, "lane_state_from_core", lambda _s, _v: {})
    return r


def assistant_panel(mic, state=None):
    """The REAL AssistantPanel with its real ears; the agent is a list that records what reached it."""
    cls = panel_mod.AssistantPanel
    p = cls.__new__(cls)
    QWidget.__init__(p)
    p._mic_mine = bool(mic)
    p._state = dict(p._load_state() if state is None else state)
    p._mouth = types.SimpleNamespace(stop=lambda: None)
    p._btn_ears = QPushButton("Ears", p)
    p._btn_ears.setCheckable(True)
    p._btn_ears.toggled.connect(p._on_ears_toggled)
    p._btn_key = QPushButton("Set Mic Key", p)
    p._btn_replies = QPushButton("Voice Replies", p)
    p._btn_replies.setCheckable(True)
    p._mode_btns = {}
    p._lbl_mic, p._lbl_heard, p._lbl_status, p._lbl_reply, p._lbl_notice = (QLabel("", p) for _ in range(5))
    p.turns = []
    p._run_turn = p.turns.append             # where a sentence the Assistant heard goes: its agent
    p._make_ears()                           # REAL: the ears, the floor, and every signal
    p._restore_binding()                     # REAL: the saved key, or the default
    p._ensure_ears()                         # the last line of the real constructor
    return p


def suit_panel(rig, mic, speech=None, **settings):
    """The REAL SuitPanel with its real ears; the companion core records what the companions were asked."""
    suit = rig.suit
    p = suit.SuitPanel.__new__(suit.SuitPanel)
    QWidget.__init__(p)
    p._mic_mine = bool(mic)
    p.s = suit.st.load()                     # REAL: the settings file (in the test's folder), or the defaults
    p.s.update(settings)
    p.speech = speech or H.FakeSpeech()
    p.core = Core()
    p.store = p.sidecar = p.setup = None
    p.lane = types.SimpleNamespace(handle=lambda text, _state, _hist: {"speaker": "elah", "about": text})
    p._fb_mon = {}
    p._notice_pending = False
    p._talk = QPushButton("Talk key", p)
    p._talk_hint = QLabel("", p)
    p._voice_missing = []
    p._make_ears()                           # REAL
    p._show_talk_key()
    p._arm_ears()                            # what the constructor's start-up timer does
    return p


def window(rig, first="suitmk2", assistant_state=None, speech=None, **suit_settings):
    made = {}

    def assistant(mic):
        made["assistant"] = assistant_panel(mic, assistant_state)
        return made["assistant"]

    def suit(mic):
        made["suitmk2"] = suit_panel(rig, mic, speech, **suit_settings)
        return made["suitmk2"]

    w = hub.HubWindow("Toolbox Assistant", [hub.TabSpec("assistant", "Assistant", assistant),
                                            hub.TabSpec("suitmk2", "Suit Mk2", suit)],
                      first=first, overlay=lambda: rig.strip)
    return w, made


def both(rig, front, **kw):
    """The window with *front* showing and the other tab built behind it, as it is a few seconds after start."""
    w, made = window(rig, first=front, **kw)
    for key in w.tab_keys():
        w.warm(key)
    return w, made["assistant"], made["suitmk2"]


def ears(panel):
    return panel._ears if hasattr(panel, "_ears") else panel.ears


def press(panel):
    ears(panel)._monitor.triggered.emit(True)        # what the hotkey layer does when the key goes down


def release(panel, said="", broken=""):
    """Let the key go, having said *said* while it was held. broken: speech-to-text fails with this."""
    e = ears(panel)
    if e.recording() and said:
        e._model = Model(said, broken)
        e._frames = [np.full((1600, 1), 0.1, dtype="float32")] * 6
        e._voice_ms = 900
    e._monitor.triggered.emit(False)


def settle(rig, until=lambda: False, seconds=2.0):
    """Run the event loop until *until*() or the time is up (transcription comes back from a worker thread)."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        rig.app.processEvents()
        if until():
            break
        time.sleep(0.01)
    rig.app.processEvents()


# ── the key decides ──────────────────────────────────────────────────────────

def test_the_assistants_key_reaches_the_assistant_while_the_suit_tab_is_showing(rig):
    w, a, s = both(rig, front="suitmk2")
    assert w.current_tab() == "suitmk2" and a._ears.armed(), "the tab behind is not watching its key"
    press(a)
    assert a._ears.recording() and not s.ears.recording()
    assert rig.sd.open == 1
    release(a, "best trade route for my Caterpillar")
    settle(rig, lambda: a.turns)
    assert a.turns == ["best trade route for my Caterpillar"]
    assert s.core.asked == [], "what was said to the Assistant reached the companions"
    assert w.current_tab() == "suitmk2", "holding a key must not change the tab"
    assert rig.sd.open == 0


def test_the_suits_key_reaches_the_companions_while_the_assistant_tab_is_showing(rig):
    w, a, s = both(rig, front="assistant")
    assert w.current_tab() == "assistant" and s.ears.armed(), "the tab behind is not watching its key"
    press(s)
    assert s.ears.recording() and not a._ears.recording()
    assert rig.sd.open == 1
    release(s, "Elah, where are we?")
    settle(rig, lambda: s.core.asked)
    assert s.core.asked == ["Elah, where are we?"]
    assert a.turns == [], "what was said to the companions reached the Assistant"
    assert w.current_tab() == "assistant", "holding a key must not change the tab"
    assert rig.sd.open == 0


def test_each_key_also_works_on_its_own_tab(rig):
    w, a, s = both(rig, front="assistant")
    press(a)
    release(a, "open the trade hub")
    settle(rig, lambda: a.turns)
    w.select("suitmk2")
    press(s)
    release(s, "Montaigne, how is the ship?")
    settle(rig, lambda: s.core.asked)
    assert a.turns == ["open the trade hub"] and s.core.asked == ["Montaigne, how is the ship?"]


# ── one microphone ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("front", ["assistant", "suitmk2"])
def test_both_keys_held_is_one_capture_and_the_first_key_is_the_one_heard(rig, front):
    w, a, s = both(rig, front=front)
    press(a)
    press(s)                                       # the second key, while the first is still down
    assert rig.sd.open == 1 and rig.sd.most == 1, "two keys opened the microphone twice"
    assert a._ears.recording() and not s.ears.recording()
    release(s, "this must go nowhere")
    assert a._ears.recording(), "letting go of the ignored key ended the other key's capture"
    release(a, "plot a route to Area 18")
    settle(rig, lambda: a.turns)
    assert a.turns == ["plot a route to Area 18"] and s.core.asked == []
    assert rig.sd.opened == 1
    assert "listening" not in rig.strip.whats("Suit Mk2"), "the ignored key was shown as listening"

    press(s)                                       # and the other way round, now the microphone is free
    press(a)
    assert rig.sd.open == 1 and rig.sd.most == 1
    assert s.ears.recording() and not a._ears.recording()
    release(a, "this must go nowhere")
    release(s, "Elah, status")
    settle(rig, lambda: s.core.asked)
    assert s.core.asked == ["Elah, status"] and a.turns == ["plot a route to Area 18"]


def test_a_held_key_takes_the_microphone_from_an_open_mic_and_gives_it_back(rig):
    w, a, s = both(rig, front="suitmk2", talk_mode="always")       # SuitMk2 in front with its mic always open
    settle(rig, lambda: s.ears.recording())
    assert s.ears.recording() and rig.sd.open == 1
    press(a)
    assert a._ears.recording() and not s.ears.recording(), "the open mic kept listening under the held key"
    assert rig.sd.open == 1 and rig.sd.most == 1
    release(a, "what is my balance")
    settle(rig, lambda: a.turns and s.ears.recording())
    assert a.turns == ["what is my balance"] and s.core.asked == []
    assert s.ears.recording() and rig.sd.open == 1, "the open mic did not come back after the key was let go"


def test_an_always_open_mic_still_belongs_only_to_the_tab_in_front(rig):
    w, a, s = both(rig, front="assistant", talk_mode="always")     # SuitMk2 behind, set to always on
    settle(rig)
    assert not s.ears.armed() and not s.ears.recording(), "a tab that is not in front opened an always-on mic"
    assert w.ptt_key_label("suitmk2") == ""
    w.select("suitmk2")
    settle(rig, lambda: s.ears.recording())
    assert s.ears.recording()


# ── the same key on both tabs ────────────────────────────────────────────────

def test_two_tabs_on_one_key_leave_it_with_the_tab_in_front_and_say_so(rig):
    same = {"binding": {"kind": "key", "code": "scroll_lock"}}      # SuitMk2's default
    w, a, s = both(rig, front="suitmk2", assistant_state=same)
    assert s.ears.armed() and not a._ears.armed(), "two tabs are watching one key: which AI answers is chance"
    assert "Suit Mk2" in a._lbl_mic.text() and "Assistant" in s._talk_hint.text()
    w.select("assistant")
    assert a._ears.armed() and not s.ears.armed()

    a._binding_captured(voice_input.InputBinding("key", "f9"))     # he gives the Assistant a key of its own
    assert a._ears.armed() and s.ears.armed()
    assert a._lbl_mic.text() == "" and s._talk_hint.text() == ""


# ── rebinding ────────────────────────────────────────────────────────────────

def test_a_new_assistant_key_is_watched_at_once_and_is_still_the_key_after_a_restart(rig):
    w, a, s = both(rig, front="suitmk2")
    assert ("assistant", ("key", "pause")) in rig.watched
    a._binding_captured(voice_input.InputBinding("key", "f9"))
    assert rig.watched[-1] == ("assistant", ("key", "f9")), "the new key is not the one being watched"
    assert w.ptt_key_label("assistant") == "F9" and "[F9]" in w._buttons["assistant"].text()
    assert json.loads(Path(panel_mod._STATE_PATH).read_text(encoding="utf-8"))["binding"] == \
        {"kind": "key", "code": "f9"}
    press(a)
    release(a, "zoom in")
    settle(rig, lambda: a.turns)
    assert a.turns == ["zoom in"]

    again = assistant_panel(True)                  # a new start reads the file
    assert ptt_keys.ident(again.ptt_binding()) == ("key", "f9")


def test_a_new_suit_key_is_watched_at_once_and_is_still_the_key_after_a_restart(rig, monkeypatch):
    w, a, s = both(rig, front="assistant")
    assert ("suitmk2", ("key", "scroll_lock")) in rig.watched
    f9 = rig.suit.InputBinding(kind="keyboard", code=120, label="F9")
    monkeypatch.setattr(rig.suit, "BindingCaptureDialog",
                        lambda _parent: types.SimpleNamespace(exec=lambda: True, result=f9))
    s._set_talk_key()
    assert rig.watched[-1] == ("suitmk2", ("key", "f9")), "the new key is not the one being watched"
    assert w.ptt_key_label("suitmk2") == "F9" and "[F9]" in w._buttons["suitmk2"].text()
    assert s._talk.text() == "Talk key: F9"
    press(s)
    release(s, "Elah, where are we?")
    settle(rig, lambda: s.core.asked)
    assert s.core.asked == ["Elah, where are we?"]

    assert rig.suit.st.load()["talk_key"]["code"] == 120           # a new start reads the file
    assert ptt_keys.ident(suit_panel(rig, True).ptt_binding()) == ("key", "f9")


# ── old settings ─────────────────────────────────────────────────────────────

def test_settings_saved_before_there_were_default_keys_still_load(rig):
    # the Assistant: a state file with no "binding" at all, with the pre-2026-09-26 "toggle" mode
    Path(panel_mod._STATE_PATH).write_text(json.dumps({"mic_mode": "toggle", "voice_replies": False}),
                                           encoding="utf-8")
    a = assistant_panel(True)
    assert ptt_keys.ident(a.ptt_binding()) == ("key", "pause") and a._ears.mode() == "push"
    assert a._ears.armed() and a._btn_key.text() == "Mic key: pause"

    # SuitMk2: "talk_key": null is what every settings file saved before today holds
    rig.suit.st.DIR.mkdir(parents=True, exist_ok=True)
    rig.suit.st.PATH.write_text(json.dumps({"talk_key": None, "talk_mode": "push", "muted": True,
                                            "chattiness": 4}), encoding="utf-8")
    s = suit_panel(rig, True)
    assert ptt_keys.ident(s.ptt_binding()) == ("key", "scroll_lock")
    assert s.s["muted"] is True and s.s["chattiness"] == 4, "loading an old file lost what it did hold"
    assert s.ears.armed() and s._talk.text() == "Talk key: SCROLL_LOCK"
    rig.suit.st.PATH.write_text(json.dumps({"muted": False}), encoding="utf-8")      # older still: no such key
    assert ptt_keys.ident(suit_panel(rig, True).ptt_binding()) == ("key", "scroll_lock")


def test_a_key_somebody_chose_is_never_replaced_by_the_default(rig):
    Path(panel_mod._STATE_PATH).write_text(json.dumps({"binding": {"kind": "key", "code": "z"}}), encoding="utf-8")
    assert ptt_keys.ident(assistant_panel(True).ptt_binding()) == ("key", "z")
    rig.suit.st.DIR.mkdir(parents=True, exist_ok=True)
    rig.suit.st.PATH.write_text(json.dumps({"talk_key": {"kind": "keyboard", "code": 88, "label": "x"}}),
                                encoding="utf-8")
    assert ptt_keys.ident(suit_panel(rig, True).ptt_binding()) == ("key", "x")


def test_the_two_default_keys_are_different_keys_and_no_launcher_hotkey():
    a, s = ptt_keys.ident(ptt_keys.ASSISTANT_DEFAULT), ptt_keys.ident(ptt_keys.SUIT_DEFAULT)
    assert a and s and a != s
    hotkeys = set()
    for skill in list(Path(H.REPO, "tools").glob("*/skill.json")) + list(Path(H.REPO, "skills").glob("*/skill.json")):
        hotkeys.add(str(json.loads(skill.read_text(encoding="utf-8")).get("hotkey") or "").lower())
    for key in (a[1], s[1]):
        assert not any(key in h.replace("<", "").replace(">", "").split("+") for h in hotkeys), key
    assert H._suit_module().st.DEFAULT_TALK_KEY == ptt_keys.SUIT_DEFAULT, "the two copies of the default differ"


# ── who is listening ─────────────────────────────────────────────────────────

def test_the_strip_names_the_ai_that_is_listening_and_its_key(rig):
    w, a, s = both(rig, front="suitmk2")
    press(a)
    assert rig.strip.shown[-1] == ("Assistant", "listening", "", "Pause")
    assert s.core.gate_state.pilot_speaking is True, "the companions were not told the pilot is talking"
    release(a, "help")
    assert rig.strip.shown[-1][:2] == ("Assistant", "released")
    assert s.core.gate_state.pilot_speaking is False
    settle(rig, lambda: a.turns)
    assert ("Assistant", "heard", "help", "Pause") in rig.strip.shown
    press(s)
    assert rig.strip.shown[-1] == ("Suit Mk2", "listening", "", "Scroll Lock")
    release(s, "Elah?")
    settle(rig, lambda: s.core.asked)
    assert ("Suit Mk2", "heard", "Elah?", "Scroll Lock") in rig.strip.shown


def test_the_real_strip_cannot_take_focus_or_clicks_and_says_who(app):
    from PySide6.QtCore import Qt
    from assistant.ptt_overlay import PttOverlay, strip_text
    o = PttOverlay()
    flags = o.windowFlags()
    for f in (Qt.WindowStaysOnTopHint, Qt.WindowDoesNotAcceptFocus, Qt.WindowTransparentForInput):
        assert flags & f, f
    assert o.testAttribute(Qt.WA_ShowWithoutActivating)
    o.show_state("Suit Mk2", "listening", "", "Scroll Lock")
    assert o.isVisible() and "Suit Mk2 is listening" in o.last[2] and "Scroll Lock" in o.last[2]
    o.show_state("Assistant", "error", "mic error: no device")
    assert o.last[1] == "error" and "Assistant did not hear you" in o.last[2]
    assert strip_text("Assistant", "reply", "") == "" and strip_text("Assistant", "nonsense", "x") == ""
    o.hide()


# ── no microphone, no speech-to-text ─────────────────────────────────────────

@pytest.mark.parametrize("who", ["assistant", "suitmk2"])
def test_a_microphone_that_will_not_open_is_said_on_screen_and_nothing_is_sent_anywhere(rig, who):
    w, a, s = both(rig, front="suitmk2" if who == "assistant" else "assistant")
    mine = a if who == "assistant" else s
    rig.sd.fail = "no input device"
    press(mine)
    release(mine, "this was never heard")
    settle(rig)
    label = "Assistant" if who == "assistant" else "Suit Mk2"
    assert rig.strip.whats(label) == ["error"], rig.strip.shown
    assert "no input device" in rig.strip.shown[-1][2]
    assert a.turns == [] and s.core.asked == [], "a failed capture was sent to an AI"
    assert not a._ears.recording() and not s.ears.recording() and rig.sd.opened == 0
    assert rig.floor.holder() == "", "a capture that never opened kept the microphone"

    rig.sd.fail = ""                               # the other key still works: nothing is left stuck
    other = s if who == "assistant" else a
    press(other)
    assert ears(other).recording()
    release(other)


@pytest.mark.parametrize("who", ["assistant", "suitmk2"])
def test_speech_to_text_failing_is_said_on_screen_and_nothing_is_sent_anywhere(rig, who):
    w, a, s = both(rig, front="suitmk2" if who == "assistant" else "assistant")
    mine = a if who == "assistant" else s
    press(mine)
    release(mine, "words", broken="model file is missing")
    label = "Assistant" if who == "assistant" else "Suit Mk2"
    settle(rig, lambda: "error" in rig.strip.whats(label))
    assert "error" in rig.strip.whats(label), rig.strip.shown
    assert "model file is missing" in rig.strip.shown[-1][2]
    assert a.turns == [] and s.core.asked == []


def test_missing_voice_libraries_are_said_on_the_tab_and_no_key_is_watched(rig, monkeypatch):
    monkeypatch.setattr("assistant.missing_voice_deps", lambda: ["faster-whisper"])
    monkeypatch.setattr(sys.modules["voice_in"], "missing_deps", lambda: ["faster-whisper"])
    w, a, s = both(rig, front="suitmk2")
    assert not a._ears.armed() and not s.ears.armed() and rig.watched == []
    assert "faster-whisper" in a._lbl_status.text()
    assert "faster-whisper" in s._talk_hint.text()


# ── warm ─────────────────────────────────────────────────────────────────────

def test_a_tab_nobody_has_opened_is_built_behind_the_front_one_and_hears_its_key(rig):
    w, made = window(rig, first="suitmk2")         # the hidden start: only SuitMk2 exists
    assert "assistant" not in made and w.ptt_key_label("assistant") == ""
    assert H._entry().warm_tabs(w) == ["assistant"]
    a, s = made["assistant"], made["suitmk2"]
    assert w.current_tab() == "suitmk2" and w.isHidden(), "warming a tab brought it forward or showed the window"
    assert a._mic_mine is False and a._ears.armed()
    assert "[Pause]" in w._buttons["assistant"].text() and "[Scroll Lock]" in w._buttons["suitmk2"].text()
    press(a)
    release(a, "help")
    settle(rig, lambda: a.turns)
    assert a.turns == ["help"]
    assert H._entry().warm_tabs(w) == [], "a tab was built twice"


def test_warm_is_what_the_entry_script_schedules():
    src = Path(H.A_ROOT, "toolbox_assistant_app.py").read_text(encoding="utf-8")
    assert "QTimer.singleShot(WARM_MS, lambda: warm_tabs(window))" in src.split("def main()", 1)[1]


# ── a companion named ────────────────────────────────────────────────────────

def real_lane(rig, panel):
    """Give the panel SuitMk2's real conversation lane (the stand-in elsewhere always says Elah)."""
    panel.lane = rig.suit.ConversationLane()
    return panel


def ask_by_key(rig, s, sentence):
    n = len(s.core.asked)
    press(s)
    release(s, sentence)
    settle(rig, lambda: len(s.core.asked) > n)
    assert s.core.asked[n:] == [sentence], "the question did not reach the companions"
    return s.core.who[-1]


def test_through_the_suits_key_the_companion_named_answers_and_otherwise_the_suit_decides(rig):
    w, a, s = both(rig, front="assistant")
    real_lane(rig, s)
    assert ask_by_key(rig, s, "Where are we?") == "elah"                      # the Suit's own choice
    assert ask_by_key(rig, s, "Montaigne, where are we?") == "montaigne"      # the same question, him named
    assert ask_by_key(rig, s, "Have we been here before?") == "montaigne"     # the Suit's own choice
    assert ask_by_key(rig, s, "Elah, have we been here before?") == "elah"
    assert ask_by_key(rig, s, "Montane, where are we?") == "montaigne"        # as speech-to-text writes him
    assert ask_by_key(rig, s, "What did Montaigne say about where we are?") == "elah"     # only mentioned
    assert a.turns == []


def test_through_the_suits_always_open_mic_it_is_the_same(rig):
    w, a, s = both(rig, front="suitmk2", talk_mode="always")
    real_lane(rig, s)
    settle(rig, lambda: s.ears.recording())
    for sentence, who in (("Where are we?", "elah"), ("Montaigne, where are we?", "montaigne"),
                          ("Ella, have we been here before?", "elah"), ("Have we been here before?", "montaigne")):
        s.ears.transcript.emit(sentence)           # what the open mic's transcription emits
        assert s.core.asked[-1] == sentence and s.core.who[-1] == who, sentence


def test_a_companions_name_said_to_the_assistants_key_stays_with_the_assistant(rig):
    w, a, s = both(rig, front="suitmk2")
    real_lane(rig, s)
    press(a)
    release(a, "Elah, where are we?")
    settle(rig, lambda: a.turns)
    press(a)
    release(a, "Montaigne, how is the ship?")
    settle(rig, lambda: len(a.turns) == 2)
    assert a.turns == ["Elah, where are we?", "Montaigne, how is the ship?"], "the Assistant did not get its sentence whole"
    assert s.core.asked == [] and s.core.who == [], "a name said to the Assistant's key woke a companion"


# ── SuitMk2's voice gate ─────────────────────────────────────────────────────

def real_speech(rig):
    played = []
    sp = rig.suit.Speech(Path("."), synth=lambda text, who: ((who, text), 22050),
                         play=lambda audio, _sr: played.append(audio))
    return sp, played


def test_with_the_window_hidden_only_the_answer_to_a_question_asked_by_key_is_spoken(rig):
    speech, played = real_speech(rig)
    try:
        w, a, s = both(rig, front="suitmk2", speech=speech)
        s._apply_voice_gate()
        assert w.isHidden()
        assert speech.say("Quiet out here.", "elah") is False, "unprompted speech with the window hidden"
        assert speech.say("The answer.", "elah", 0, addressed=True) is False, "an answer nobody asked for"

        press(s)
        release(s, "Elah, where are we?")
        settle(rig, lambda: s.core.asked)
        assert s.core.asked == ["Elah, where are we?"]
        assert speech.say("Quiet out here.", "elah") is False, "the answer pass let unprompted speech through"
        assert speech.say("You are hurt.", "elah", 0) is False, "an urgent unprompted line got through the pass"
        assert speech.say("Lorville, at the gates.", "elah", 0, addressed=True) is True
        settle(rig, lambda: played)
        assert played == [("elah", "Lorville, at the gates.")]

        s._close_answer_pass()                     # what the pass's timer does
        assert speech.say("One more thing.", "elah", 0, addressed=True) is False
        assert speech.muted is True
    finally:
        speech.close()


def test_his_mute_button_silences_the_answer_too_and_an_open_mic_never_opens_the_pass(rig):
    speech, _played = real_speech(rig)
    try:
        w, a, s = both(rig, front="suitmk2", speech=speech, muted=True)
        press(s)
        release(s, "Elah, where are we?")
        settle(rig, lambda: s.core.asked)
        assert speech.say("Lorville.", "elah", 0, addressed=True) is False, "he muted them and they answered"
        assert ("Suit Mk2", "note") == rig.strip.shown[-1][:2] and "muted" in rig.strip.shown[-1][2]

        s.s["muted"] = False
        s._close_answer_pass()
        s.ears.set_mode("always")                  # an always-open mic overhears a question
        s._on_transcript("Elah, where are we?")
        assert s._ptt_pass is False
        assert speech.say("Lorville.", "elah", 0, addressed=True) is False, \
            "an open mic made the hidden companions answer: only a held key is him asking"
    finally:
        speech.close()


def test_the_window_open_is_as_before_everything_is_spoken(rig):
    speech, played = real_speech(rig)
    try:
        w, a, s = both(rig, front="assistant", speech=speech)
        w.show()
        rig.app.processEvents()
        assert speech.say("Quiet out here.", "elah") is True
        settle(rig, lambda: played)
        w.hide()
        rig.app.processEvents()
        assert speech.say("Quiet out here.", "elah") is False
    finally:
        speech.close()
