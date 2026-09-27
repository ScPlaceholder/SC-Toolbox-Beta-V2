"""speakingChanged must track the user's voice, and its sensitivity must not touch hearing.

J, 2026-09-27: *"Go ahead and wire it in and have the option for users to reduce the mic
sensitivity to spawn him but it won't reduce the sensitivity to spawn the whisper."*

Two thresholds, one of which must be inert on the transcription path:

    _VOICE_RMS   drives _voice_ms -> when an utterance is captured   (NOT user-adjustable)
    _show_rms    drives speakingChanged only                         (user-adjustable, upward)

⛔ THE DISCRIMINATING TEST IS `test_raising_show_sensitivity_does_not_deafen_the_ears`.
   Every other test here would still pass if someone "simplified" the two thresholds back into
   one — a single threshold tracks voice activity perfectly well and lights the indicator
   correctly. Only the decoupling test can fail, because it asserts the two DISAGREE: the
   indicator stays dark while the utterance is still captured. That is the whole feature, and it
   is the only assertion that can tell the feature from its absence.
   (Same shape as the July rig lesson: test the case that can only pass if right, not the case
   that looks plausible either way.)
"""
from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

pytest.importorskip("PySide6")

from assistant import voice as V   # noqa: E402


class _Ears(V.EarsController):
    """Construct without Qt parenting, and record what the signal emitted."""

    def __init__(self):
        super().__init__()
        self.emitted = []
        self.speakingChanged.connect(self.emitted.append)
        self.finished = 0

    def _finish(self):
        self.finished += 1
        # do NOT call through: the real one touches audio, whisper and the transcript signal


def _tick(e, rms, n=1):
    """Drive n 100 ms ticks at a given RMS, as the real timer would."""
    e._recording = True
    for _ in range(n):
        e._last_rms = rms
        e._watch_silence()


LOUD = V._VOICE_RMS * 4
QUIET = 0.0


def test_speaking_goes_true_on_voice_and_false_on_the_gap():
    e = _Ears()
    e.set_mode("always")
    _tick(e, LOUD, 5)
    assert e.emitted == [True], "speech should raise it exactly once, not once per tick"
    _tick(e, QUIET, int(V._GAP_MS / 100) + 1)
    assert e.emitted == [True, False]


def test_emits_only_on_change():
    """A per-tick emit would fire 10x a second and restart any consumer's fade forever."""
    e = _Ears()
    e.set_mode("always")
    _tick(e, LOUD, 30)
    assert e.emitted == [True], "got %d emits for one continuous utterance" % len(e.emitted)


def test_raising_show_sensitivity_does_not_deafen_the_ears():
    """★ THE ONE THAT MATTERS. Indicator dark, utterance still captured.

    A single-threshold implementation cannot pass this: it would either light the indicator
    (failing the first assertion) or stop accumulating _voice_ms (failing the second).
    """
    e = _Ears()
    e.set_mode("always")
    e.set_show_sensitivity(LOUD * 10)          # far above anything we will feed it
    _tick(e, LOUD, int(V._MIN_VOICE_MS / 100) + 2)

    assert e.emitted == [], "the indicator lit despite a raised display threshold"
    assert e._voice_ms >= V._MIN_VOICE_MS, \
        "raising the DISPLAY threshold stopped the ears accumulating voice — the two " \
        "thresholds are not decoupled, which is the entire feature"

    # and the utterance must still complete on the same input
    _tick(e, QUIET, int(V._GAP_MS / 100) + 1)
    assert e.finished >= 1, "the utterance never finished; hearing was affected"


def test_sensitivity_cannot_be_lowered_below_the_hearing_threshold():
    """Lowering it under _VOICE_RMS would light up for audio the ears never act on —
    a worse lie than a twitchy indicator, because it says 'I heard that' when nothing
    was captured."""
    e = _Ears()
    e.set_show_sensitivity(0.0)
    assert e.show_sensitivity() == V._VOICE_RMS


def test_the_assistants_own_voice_never_marks_the_user_as_speaking():
    """While the gate holds the mic deaf, the user is by definition not the one talking.
    A companion that animated here would be dancing at the assistant's own voice."""
    e = _Ears()
    e.set_mode("always")
    _tick(e, LOUD, 3)
    assert e.emitted == [True]
    e.note_speaking("a long sentence the assistant is currently saying out loud")
    assert e.deaf(), "precondition: the gate should be holding"
    _tick(e, LOUD, 3)
    assert e.emitted == [True, False], "stayed 'speaking' while the assistant talked"


def test_aborting_the_recording_clears_it():
    e = _Ears()
    e.set_mode("always")
    _tick(e, LOUD, 3)
    e._recording = True
    e._abort_recording()
    assert e.emitted[-1] is False
