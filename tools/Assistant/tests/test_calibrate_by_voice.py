"""The route calibration starts by voice and is talked through (J, 2026-10-04).

"Step one for calibrate star map should be 'click on game and say calibrate starmap to begin' then when the user
says 'calibrate starmap' it begins the verbal prompts to the user"

What is pinned here:

    step 0          the dialog opens on J's sentence and watches NOTHING: no mouse listener and no keyboard
                    listener exist until it begins, so the click that focuses the game cannot be recorded
    the phrase      "calibrate star map" and the ways it is said and heard begin it: from step 0, and from no
                    dialog at all (he has then already said it, so it opens and starts at once)
    a modal dialog  the transcript still arrives while the dialog's own event loop is the one running: a real
                    exec(), a signal emitted from another thread, the panel's real _on_transcript
    spoken          each step's line is spoken once as the step begins, then "saved" or "cancelled"; they go
                    through the panel's real _speak, which warns the ears before the mouth gets the line
    the old rules   step 1 keeps the most recent click and waits for Enter; steps 2 and 3 take one click each
    no microphone   the Begin button starts it, and step 0 says why he may need it
    cancel          speaks, stops both listeners, saves nothing

The dialog's REAL class runs throughout. pynput is replaced by a stand-in that records the listeners and lets a
test deliver a click or a key, because the real one hooks the mouse and keyboard of the whole machine.
"""
from __future__ import annotations

import sys
import threading
import types

import pytest
from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QWidget

from assistant import panel as panel_mod
from assistant.set_route import phrases, route_setter

D = route_setter.RouteCalibrationDialog
J_SENTENCE = 'Click on the game and say "calibrate star map" to begin.'


@pytest.fixture
def app():
    a = QApplication.instance() or QApplication([])
    yield a
    for w in list(a.topLevelWidgets()):
        w.hide()
        w.deleteLater()
    a.processEvents()


class _Listener:
    def __init__(self, kind, made, on_click=None, on_press=None):
        self.kind, self.on_click, self.on_press = kind, on_click, on_press
        self.started = self.stopped = False
        made.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


@pytest.fixture
def pyn(monkeypatch):
    """A stand-in pynput. ``pyn.made`` is every listener created; click() / enter() deliver input to them."""
    made = []
    left, right, enter = object(), object(), object()
    mouse = types.ModuleType("pynput.mouse")
    mouse.Button = types.SimpleNamespace(left=left, right=right)
    mouse.Listener = lambda on_click=None: _Listener("mouse", made, on_click=on_click)
    keyboard = types.ModuleType("pynput.keyboard")
    keyboard.Key = types.SimpleNamespace(enter=enter)
    keyboard.Listener = lambda on_press=None: _Listener("keyboard", made, on_press=on_press)
    pkg = types.ModuleType("pynput")
    pkg.mouse, pkg.keyboard = mouse, keyboard
    for name, mod in (("pynput", pkg), ("pynput.mouse", mouse), ("pynput.keyboard", keyboard)):
        monkeypatch.setitem(sys.modules, name, mod)
    saved = []
    monkeypatch.setattr(route_setter, "save_calibration", lambda *pts: saved.append(pts))

    def the(kind):
        got = [m for m in made if m.kind == kind]
        assert len(got) == 1, "%d %s listener(s)" % (len(got), kind)
        return got[0]

    def click(x, y, button=left):
        the("mouse").on_click(x, y, button, True)
        the("mouse").on_click(x, y, button, False)
        QApplication.instance().processEvents()

    def press_enter():
        the("keyboard").on_press(enter)
        QApplication.instance().processEvents()

    return types.SimpleNamespace(made=made, saved=saved, click=click, enter=press_enter, the=the, right=right)


def _dialog(**kw):
    said = []
    d = D(None, speak=said.append, **kw)
    return d, said


# ── step 0 ───────────────────────────────────────────────────────────────────

def test_the_dialog_opens_on_js_sentence_and_watches_nothing(app, pyn):
    d, said = _dialog()
    assert d._label.text() == J_SENTENCE
    assert d.begun() is False
    assert pyn.made == [], "a listener exists before he said anything: the click that focuses the game is seen"
    assert d._listener is None and d._kb_listener is None
    assert said == [], "something was spoken before he asked for it"
    assert d._points == [] and d._pending is None and not pyn.saved


def test_closing_step_0_says_nothing_and_saves_nothing(app, pyn):
    d, said = _dialog()
    d.reject()
    assert said == [] and pyn.made == [] and not pyn.saved and d.result_ready is False


def test_step_0_says_how_the_microphone_is_set(app, pyn):
    d, _said = _dialog(mic_hint="No mic key is set, so I cannot hear you. Press Begin instead.")
    d.show()
    assert d._mic.isVisible() and "No mic key is set" in d._mic.text()
    d.begin()
    assert not d._mic.isVisible()
    d2, _said2 = _dialog()
    d2.show()
    assert not d2._mic.isVisible()


# ── beginning ────────────────────────────────────────────────────────────────

def test_begin_starts_watching_and_speaks_step_one_once(app, pyn):
    d, said = _dialog()
    assert d.begin() is True
    assert d.begun() and pyn.the("mouse").started and pyn.the("keyboard").started
    assert d._label.text() == D._STEPS[0]
    assert said == [D.SPOKEN[0]]
    assert d.begin() is False, "saying it a second time started the calibration again"
    assert said == [D.SPOKEN[0]] and len(pyn.made) == 2


def test_the_begin_button_starts_it_without_a_microphone(app, pyn):
    d, said = _dialog()
    d.show()
    btn = [b for b in d.findChildren(QPushButton) if b.text() == "Begin"]
    assert len(btn) == 1 and btn[0].isVisible() and btn[0].isEnabled()
    btn[0].click()
    assert d.begun() and said == [D.SPOKEN[0]] and pyn.the("mouse").started
    assert not btn[0].isVisible()


def test_opened_already_begun_when_he_has_said_the_words(app, pyn):
    from PySide6.QtCore import Qt
    d, said = _dialog(begin=True)
    assert d.begun() and said == [D.SPOKEN[0]] and d._label.text() == D._STEPS[0]
    assert d.testAttribute(Qt.WA_ShowWithoutActivating), "it would take the keyboard from the game as it opened"
    d2, _ = _dialog()
    assert not d2.testAttribute(Qt.WA_ShowWithoutActivating)        # opened by its button: an ordinary dialog


# ── the steps ────────────────────────────────────────────────────────────────

def test_each_step_is_spoken_once_as_it_begins_and_then_saved(app, pyn):
    d, said = _dialog()
    d.begin()
    pyn.click(10, 11)                               # the click that focuses the game
    pyn.click(300, 40)                              # the search bar
    assert said == [D.SPOKEN[0]], "a step-1 click was spoken about, or moved on without Enter"
    assert "Last click: (300, 40)" in d._label.text()
    pyn.enter()
    assert said == [D.SPOKEN[0], D.SPOKEN[1]] and d._label.text() == D._STEPS[1]
    pyn.click(320, 90)
    assert said == [D.SPOKEN[0], D.SPOKEN[1], D.SPOKEN[2]] and d._label.text() == D._STEPS[2]
    pyn.click(960, 540)
    assert said == list(D.SPOKEN) + [D.SAID_SAVED]
    assert pyn.saved == [((300, 40), (320, 90), (960, 540))]
    assert d.result_ready is True
    assert pyn.the("mouse").stopped and pyn.the("keyboard").stopped


def test_enter_still_confirms_step_one_and_only_step_one(app, pyn):
    d, said = _dialog()
    d.begin()
    pyn.enter()                                     # nothing clicked yet: Enter is not a position
    assert d._points == [] and said == [D.SPOKEN[0]]
    pyn.click(1, 1, button=pyn.right)               # a right click is not a calibration click
    pyn.enter()
    assert d._points == []
    pyn.click(5, 5)
    pyn.click(700, 33)
    pyn.enter()
    assert d._points == [(700, 33)], "step 1 did not keep the most recent click"
    pyn.enter()                                     # Enter in step 2 (he pressed it in the search box)
    assert d._points == [(700, 33)] and said == [D.SPOKEN[0], D.SPOKEN[1]]


def test_a_calibration_that_cannot_be_saved_says_so(app, pyn, monkeypatch):
    def boom(*_pts):
        raise OSError("disk")
    monkeypatch.setattr(route_setter, "save_calibration", boom)
    d, said = _dialog(begin=True)
    pyn.click(1, 2)
    pyn.enter()
    pyn.click(3, 4)
    pyn.click(5, 6)
    assert said[-1] == D.SAID_NOT_SAVED and d.result_ready is False and D.SAID_SAVED not in said


def test_the_spoken_lines_do_not_contain_the_phrase_that_starts_it():
    for line in D.SPOKEN + (D.SAID_SAVED, D.SAID_NOT_SAVED, D.SAID_CANCELLED):
        assert not phrases.is_calibrate(line), line
        for part in line.split("."):
            assert not phrases.is_calibrate(part), part


# ── cancel ───────────────────────────────────────────────────────────────────

def test_cancel_says_so_stops_both_listeners_and_saves_nothing(app, pyn):
    d, said = _dialog(begin=True)
    pyn.click(300, 40)
    pyn.enter()
    mouse, keyboard = pyn.the("mouse"), pyn.the("keyboard")
    d.reject()                                      # Esc
    assert said[-1] == D.SAID_CANCELLED and said.count(D.SAID_CANCELLED) == 1
    assert mouse.stopped and keyboard.stopped, "a global input hook was left running"
    assert d._listener is None and d._kb_listener is None
    assert not pyn.saved and d.result_ready is False
    d.reject()
    assert said.count(D.SAID_CANCELLED) == 1


def test_a_speaker_that_fails_does_not_stop_the_calibration(app, pyn):
    def mute(_text):
        raise RuntimeError("no audio device")
    d = D(None, speak=mute)
    d.begin()
    pyn.click(1, 2)
    pyn.enter()
    assert d._label.text() == D._STEPS[1]


# ── the phrase ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("heard", [
    "calibrate star map", "Calibrate starmap.", "Calibrate the Star Map.", "calibrate route", "Calibrate the route.",
    "calibrate start map", "Calibrate star maps", "recalibrate the star map", "calibrate", "Calibrate!",
    "start calibration", "please calibrate the star map", "Star map, calibrate.", "calibrate my star map now",
    "calibrate route setter", "calibrate the in-game star map",
])
def test_what_starts_the_calibration(heard):
    assert phrases.is_calibrate(heard), heard


@pytest.mark.parametrize("heard", [
    "", "how do I calibrate the star map", "set route to Area 18", "navigate to Port Tressler", "open the star map",
    "calibrate mining crops", "is the star map calibrated", "route to Pyro", "calibration saved",
    "step one press f2 for the star map and zoom out", "star map",
])
def test_what_does_not(heard):
    assert not phrases.is_calibrate(heard), heard
    assert phrases.destination("calibrate star map") == ""          # and the command is not a destination


# ── the panel: the real _on_transcript, _calibrate_route and _speak ──────────

class _Ears:
    def __init__(self, mode="push", binding="z", armed=True):
        self._mode, self._binding, self._armed = mode, binding, armed
        self.order = []

    def mode(self):
        return self._mode

    def armed(self):
        return self._armed

    def binding(self):
        if self._binding is None:
            return None
        return types.SimpleNamespace(describe=lambda: self._binding.upper())

    def note_speaking(self, text):
        self.order.append(("ears", text))

    def cancel_speaking(self):
        self.order.append(("cancel", ""))


def _panel(monkeypatch, ears=None):
    """The real AssistantPanel without its constructor (which opens a microphone and starts an agent)."""
    cls = panel_mod.AssistantPanel
    p = cls.__new__(cls)
    QWidget.__init__(p)
    p._mic_mine = True
    p._ears = ears or _Ears()
    order = p._ears.order
    p._mouth = types.SimpleNamespace(speak=lambda text: order.append(("mouth", text)))
    p._btn_replies = QPushButton("Voice Replies", p)
    p._btn_replies.setCheckable(True)
    p._btn_replies.setChecked(True)
    monkeypatch.setattr(panel_mod.Mouth, "available", staticmethod(lambda: True))
    p._lbl_heard, p._lbl_reply, p._lbl_status = QLabel(p), QLabel(p), QLabel(p)
    p.turns = []
    p._run_turn = p.turns.append
    monkeypatch.setattr(panel_mod, "missing_voice_deps", lambda: [], raising=False)
    import assistant
    monkeypatch.setattr(assistant, "missing_voice_deps", lambda: [])
    return p


def test_the_phrase_begins_a_dialog_that_is_waiting_on_step_0(app, pyn, monkeypatch):
    p = _panel(monkeypatch)
    d = D(p, speak=p._speak)
    p._cal_dlg = d
    p._on_transcript("Navigate to Area 18.")        # anything else is an ordinary request, dialog or not
    assert not d.begun() and p.turns == ["Navigate to Area 18."]
    p._on_transcript("Calibrate starmap.")
    assert d.begun(), "he said it and the dialog is still waiting"
    assert p.turns == ["Navigate to Area 18."], "the command was also sent to the assistant as a question"
    assert p._lbl_reply.text() == "AI: " + D.SPOKEN[0]
    p._on_transcript("calibrate star map")          # again, mid-calibration: nothing restarts
    assert [o for o in p._ears.order if o[0] == "mouth"] == [("mouth", D.SPOKEN[0])]


def test_the_prompts_warn_the_ears_before_the_mouth_gets_them(app, pyn, monkeypatch):
    """An always-open mic hears the speakers; EarsController.note_speaking is what stops the Assistant taking its own
    prompt for the pilot's next command (tests/test_assistant_self_hearing.py has the gate itself)."""
    p = _panel(monkeypatch, ears=_Ears(mode="always", binding=None))
    d = D(p, speak=p._speak, begin=True)
    assert p._ears.order == [("ears", D.SPOKEN[0]), ("mouth", D.SPOKEN[0])]
    d.reject()
    assert p._ears.order[-2:] == [("ears", D.SAID_CANCELLED), ("mouth", D.SAID_CANCELLED)]


def test_the_phrase_with_no_dialog_opens_one_already_begun(app, pyn, monkeypatch):
    p = _panel(monkeypatch)
    opened = []

    def fake_exec(self):
        opened.append((self.begun(), self.parent() is p, p._cal_dlg is self))
        return 0

    monkeypatch.setattr(D, "exec", fake_exec)
    p._on_transcript("Calibrate the star map.")
    assert opened == [], "the modal dialog was opened from inside the slot the transcript arrived on"
    app.processEvents()
    assert opened == [(True, True, True)], opened
    assert ("mouth", D.SPOKEN[0]) in p._ears.order
    assert p.turns == [] and getattr(p, "_cal_dlg", None) is None
    assert p._lbl_status.text() == "calibration cancelled"


def test_the_button_opens_on_step_0_and_gives_the_dialog_the_panels_voice(app, pyn, monkeypatch):
    p = _panel(monkeypatch, ears=_Ears(binding=None, armed=False))
    seen = []

    def fake_exec(self):
        seen.append((self.begun(), self._label.text(), self._mic.text(), self._speak_fn == p._speak))
        return 0

    monkeypatch.setattr(D, "exec", fake_exec)
    p._calibrate_route()
    assert seen == [(False, J_SENTENCE, "No mic key is set, so I cannot hear you. Press Begin instead.", True)], seen
    assert pyn.made == [] and p._ears.order == []


def test_what_step_0_says_about_the_microphone(app, monkeypatch):
    import assistant
    assert _panel(monkeypatch, _Ears("push", "z", True))._mic_hint() == "Hold Z while you say it."
    assert _panel(monkeypatch, _Ears("always", None, True))._mic_hint() == ""
    assert "microphone is off" in _panel(monkeypatch, _Ears("always", None, False))._mic_hint()
    assert "microphone is off" in _panel(monkeypatch, _Ears("push", "z", False))._mic_hint()
    assert "No mic key is set" in _panel(monkeypatch, _Ears("push", None, False))._mic_hint()
    other = _panel(monkeypatch)
    other._mic_mine = False
    assert "other tab" in other._mic_hint()
    p = _panel(monkeypatch)
    monkeypatch.setattr(assistant, "missing_voice_deps", lambda: ["faster-whisper", "sounddevice"])
    assert p._mic_hint() == ("Voice input is not installed (missing: faster-whisper, sounddevice). "
                             "Press Begin instead.")


# ── a modal dialog does not hold the transcript back ─────────────────────────

class _Heard(QObject):
    transcript = Signal(str)


def test_a_transcript_reaches_the_panel_while_the_modal_dialog_is_running(app, pyn, monkeypatch):
    """The real exec(): the dialog's own event loop is the one running. The transcript is emitted from another
    thread, as the ears do it (Whisper runs off the GUI thread), and must be delivered inside that loop."""
    p = _panel(monkeypatch)
    heard = _Heard()
    heard.transcript.connect(p._on_transcript)
    seen = {"begun_before": None, "begun_while_open": False, "dialog": None}

    def say_it():
        d = p._cal_dlg
        seen["dialog"] = d
        seen["begun_before"] = d.begun()
        threading.Thread(target=lambda: heard.transcript.emit("Calibrate star map."), daemon=True).start()

    def watch():
        d = p._cal_dlg
        if d is not None and d.begun():
            seen["begun_while_open"] = True
            d.reject()

    poll = QTimer()
    poll.timeout.connect(watch)
    poll.start(20)
    QTimer.singleShot(60, say_it)
    QTimer.singleShot(5000, lambda: p._cal_dlg is not None and p._cal_dlg.reject())     # never hang the suite
    p._calibrate_route()                            # blocks in the real QDialog.exec() until rejected
    poll.stop()
    assert seen["dialog"] is not None and seen["begun_before"] is False
    assert seen["begun_while_open"], "the phrase did not reach the dialog while it was open (modal loop)"
    assert ("mouth", D.SPOKEN[0]) in p._ears.order and ("mouth", D.SAID_CANCELLED) in p._ears.order
    assert pyn.the("mouse").stopped and p._cal_dlg is None
