"""The companions speak only while the SuitMk2 window is open (J, 2026-10-04).

"Can you make sure that suitmk2 only have the AI's talk while it is launched?" The launcher preloads this
tool hidden when the launcher starts, and closing the window only hides it, so the companions talked for a
tool the user had never opened or had already closed.

These tests drive the real ``SuitWindow`` methods against a stand-in for ``self``: building the real window
boots the companion core, the log monitor and the voice service, none of which this rule depends on.
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from ui import suit_window  # noqa: E402


class _Speech:
    def __init__(self):
        self.muted = False
        self.calls = []

    def mute(self, on=True):
        self.muted = bool(on)
        self.calls.append(bool(on))


class _Win:
    """Just what the gate reads: the saved settings, the speech queue, and whether the window is showing."""

    def __init__(self, visible, user_muted):
        self.s = {"muted": user_muted}
        self.speech = _Speech()
        self._visible = visible

    def isVisible(self):
        return self._visible

    _apply_voice_gate = suit_window.SuitWindow._apply_voice_gate


def _gate(visible, user_muted):
    w = _Win(visible, user_muted)
    w._apply_voice_gate()
    return w


def test_hidden_window_is_silent():
    # the preload case: the launcher started it, the user never opened it
    assert _gate(visible=False, user_muted=False).speech.muted is True


def test_open_window_speaks():
    assert _gate(visible=True, user_muted=False).speech.muted is False


def test_open_window_respects_the_mute_button():
    assert _gate(visible=True, user_muted=True).speech.muted is True


def test_closing_and_reopening_does_not_change_his_mute_setting():
    w = _Win(visible=True, user_muted=False)
    w._apply_voice_gate()
    w._visible = False
    w._apply_voice_gate()
    assert w.speech.muted is True
    assert w.s["muted"] is False            # the gate never writes his setting
    w._visible = True
    w._apply_voice_gate()
    assert w.speech.muted is False


def test_the_mute_toggle_saves_his_choice_and_goes_through_the_gate(monkeypatch):
    saved = []
    monkeypatch.setattr(suit_window.st, "save", lambda s: saved.append(dict(s)))
    w = _Win(visible=False, user_muted=True)
    suit_window.SuitWindow._toggle_mute(w, False)      # he un-mutes while the window is hidden
    assert w.s["muted"] is False and saved and saved[-1]["muted"] is False
    assert w.speech.muted is True                      # still silent: the window is not open


def test_show_and_hide_events_apply_the_gate():
    src = open(suit_window.__file__, encoding="utf-8").read()
    for name in ("def showEvent", "def hideEvent"):
        body = src.split(name, 1)[1].split("    def ", 1)[0]
        assert "_apply_voice_gate()" in body, name


# -- push-to-talk (J, 2026-10-05) -------------------------------------------------------------------------------
# "For the assistant and suit Mk 2 can you have individual push to talk buttons which also auto-route to the right
# ai?" Holding SuitMk2's talk key asks the companions something from anywhere, the window hidden included, and they
# answer. The rule above is about UNPROMPTED speech and must hold exactly as it did: with the window hidden nothing
# is said except the answer to a question asked with the key. These use the real Speech (its synth and playback
# replaced), the real gate, and the core's real answer helpers.

import inspect   # noqa: E402
import time      # noqa: E402
from pathlib import Path   # noqa: E402

import companion_core   # noqa: E402
import speech as speech_mod   # noqa: E402


def _real_speech():
    played = []
    return speech_mod.Speech(Path("."), synth=lambda text, who: ((who, text), 22050),
                             play=lambda audio, _sr: played.append(audio)), played


def _wait(cond, seconds=2.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end and not cond():
        time.sleep(0.01)
    return cond()


class _RealWin(_Win):
    """The gate's stand-in window, with the real Speech behind it and the answer pass a key press opens."""

    def __init__(self, visible, user_muted, answer_pass):
        super().__init__(visible, user_muted)
        self.speech, self.played = _real_speech()
        self._ptt_pass = answer_pass
        self._apply_voice_gate()


def test_a_hidden_window_refuses_every_unprompted_line_even_while_the_answer_pass_is_open():
    w = _RealWin(visible=False, user_muted=False, answer_pass=True)
    try:
        for priority in (speech_mod.PRIORITY_AMBIENT, speech_mod.PRIORITY_EVENT, speech_mod.PRIORITY_URGENT):
            assert w.speech.say("Nobody asked.", "montaigne", priority) is False, priority
        assert w.speech.muted is True                  # what every unprompted path in the core reads
        assert w.speech.say("Lorville.", "elah", speech_mod.PRIORITY_URGENT, addressed=True) is True
        assert _wait(lambda: w.played) and w.played == [("elah", "Lorville.")]
    finally:
        w.speech.close()


def test_a_hidden_window_with_no_answer_pass_says_nothing_at_all():
    w = _RealWin(visible=False, user_muted=False, answer_pass=False)
    try:
        assert w.speech.say("Nobody asked.", "elah") is False
        assert w.speech.say("Lorville.", "elah", speech_mod.PRIORITY_URGENT, addressed=True) is False
    finally:
        w.speech.close()


def test_his_mute_button_refuses_the_answer_too():
    for visible in (True, False):
        w = _RealWin(visible=visible, user_muted=True, answer_pass=True)
        try:
            assert w.speech.say("Lorville.", "elah", speech_mod.PRIORITY_URGENT, addressed=True) is False, visible
        finally:
            w.speech.close()


def test_closing_the_answer_pass_drops_an_answer_that_was_still_waiting():
    w = _RealWin(visible=False, user_muted=False, answer_pass=True)
    try:
        hold = []
        w.speech._play = lambda audio, _sr: (hold.append(audio), time.sleep(0.3))
        assert w.speech.say("First.", "elah", 0, addressed=True) is True
        assert _wait(lambda: hold)
        assert w.speech.say("Second.", "elah", 0, addressed=True) is True
        w._ptt_pass = False
        w._apply_voice_gate()                          # the pass's timer
        assert w.speech.pending() == 0
        time.sleep(0.5)
        assert hold == [("elah", "First.")]
    finally:
        w.speech.close()


def test_hiding_the_window_drops_the_chatter_and_keeps_the_answer_he_is_waiting_for():
    w = _RealWin(visible=True, user_muted=False, answer_pass=True)
    try:
        w.speech._play = lambda audio, _sr: time.sleep(0.3)             # a line is being spoken
        assert w.speech.say("Now playing.", "elah") is True              # open window: everything is spoken
        time.sleep(0.05)
        assert w.speech.say("Chatter.", "elah") is True
        assert w.speech.say("Lorville.", "elah", 0, addressed=True) is True
        w._visible = False
        w._apply_voice_gate()
        with w.speech._cv:
            left = [(i.text, i.addressed) for i in w.speech._q]
        assert left == [("Lorville.", True)], left
    finally:
        w.speech.close()


def test_the_cores_answers_use_the_pass_and_nothing_else_in_the_core_can():
    sp, played = _real_speech()
    try:
        sp.mute(True)
        sp.allow_addressed(True)                       # the window hidden, a question asked with the key
        core = type("Core", (), {"speech": sp})()
        assert companion_core.CompanionCore._answer_muted(core) is False
        assert companion_core.CompanionCore._say_answer(core, "Lorville.", "elah", 0) is True
        assert _wait(lambda: played)
        sp.allow_addressed(False)
        assert companion_core.CompanionCore._answer_muted(core) is True
        assert companion_core.CompanionCore._say_answer(core, "Lorville.", "elah", 0) is False
    finally:
        sp.close()
    cls = companion_core.CompanionCore
    assert "_answer_muted()" in inspect.getsource(cls.answer)
    assert "_say_answer(" in inspect.getsource(cls._answer_worker)
    src = inspect.getsource(cls)
    code = [ln for ln in src.splitlines() if not ln.strip().startswith("#")]
    assert sum("addressed=True" in ln for ln in code) == 1, "a path other than _say_answer marks its lines as answers"
    assert src.count("_say_answer(") == 3, "_say_answer has a caller besides the answer and the spoken toggle"
    assert src.count("_answer_muted()") == 2


def test_a_speech_with_no_answer_pass_still_works_for_the_core():
    said = []
    old = type("Old", (), {"muted": False, "say": lambda self, text, who, pri: said.append(text) or True})()
    core = type("Core", (), {"speech": old})()
    assert companion_core.CompanionCore._answer_muted(core) is False
    assert companion_core.CompanionCore._say_answer(core, "Lorville.", "elah", 0) is True and said == ["Lorville."]
