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
