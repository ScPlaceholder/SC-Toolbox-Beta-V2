"""A sequence of words, static and gaps, played as ONE line (J, 2026-10-06).

J heard Montaigne's rare line through the Suit's own voice: "he reads the brzzrs rather than it sounding like
static", and "MONT-AI-GN-3" came out mangled. "We need to use an audio sound snip." He approved a rendering with
generated static where the buzz words were and the model number spelled out. So speech has one new way in,
say_sequence, and the rare line is such a sequence (data/canon_montaigne.json, glitch.sequence).

These tests hold: the audio put together for the canon sequence has static at the three places and of the stored
lengths, by sample count; it is one queue item, so mute stops it and nothing further plays, a muted (hidden) voice
refuses it, a stale one is dropped, and an event line goes ahead of it; the character and the level are applied
to the words and not to the static; without voice_fx / scipy it still plays, with unfiltered static at half the
level; and a synth that does not return arrays (the other tests') still works.

The real Speech with an injected synth and play: no voice model, no sound device. The band-passed static needs
scipy, which the suite's Python 3.13 does not have: those two tests skip there and run under the toolbox's 3.14.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

import rare_line as rl
import speech as sp

SR = 22050
VOICE = {0: 4000, 1: 2500, 2: 5200}            # samples the fake synth returns for the 1st, 2nd and 3rd words


def _n(seconds):
    return int(seconds * SR)


def _wait(cond, timeout=3.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.01)
    return False


class _Rig:
    """A Speech whose synth returns a tone of a known length per `say`, and whose play keeps what it is handed."""

    def __init__(self, hold=False, **kw):
        self.played, self.asked, self.gate = [], [], threading.Event()
        if not hold:
            self.gate.set()
        self.clock = [100.0]
        kw.setdefault("fx_presets", {"elah": "clean", "montaigne": "clean"})
        self.s = sp.Speech(Path("."), synth=self._synth, play=self._play, now=lambda: self.clock[0], **kw)

    def _synth(self, text, speaker):
        n = VOICE[len(self.asked) % 3] if speaker == "montaigne" else 1000
        self.asked.append(text)
        return (0.2 * np.sin(2 * np.pi * 220 * np.arange(n) / SR)).astype(np.float32), SR

    def _play(self, audio, sr):
        self.started = True
        self.gate.wait(3)
        self.played.append(audio)

    def close(self):
        self.gate.set()
        self.s.close()


@pytest.fixture
def rig():
    r = _Rig()
    yield r
    r.close()


def _parts(seq):
    """(kind, start, stop) in samples for each item of a sequence rendered with the fake synth."""
    out, at, said = [], 0, 0
    for kind, value in seq:
        n = VOICE[said % 3] if kind == "say" else _n(value)
        said += kind == "say"
        out.append((kind, at, at + n))
        at += n
    return out


def _rms(x):
    return float(np.sqrt(np.mean(np.asarray(x, np.float64) ** 2))) if len(x) else 0.0


# ---------------------------------------------------------------------------------------------------------------
# the canon sequence, as audio
# ---------------------------------------------------------------------------------------------------------------
def test_the_canon_sequence_is_one_line_with_static_at_the_three_places_and_of_the_stored_lengths(rig):
    seq = rl.sequence()
    assert [k for k, _ in seq] == ["static", "gap", "say", "gap", "static", "gap", "say", "static", "gap", "say"]
    assert [v for k, v in seq if k == "static"] == [0.55, 0.32, 0.18] and [v for k, v in seq if k == "gap"] == [0.07] * 4
    assert rig.s.say_sequence(seq, "montaigne", sp.PRIORITY_AMBIENT, text=rl.line()) is True
    assert _wait(lambda: len(rig.played) == 1)
    audio = rig.played[0]
    assert rig.asked == [v for k, v in seq if k == "say"]                 # the words, word for word, in order
    assert not any("brzz" in t.lower() or "bzzt" in t.lower() for t in rig.asked)
    parts = _parts(seq)
    assert isinstance(audio, np.ndarray) and audio.dtype == np.float32
    assert len(audio) == parts[-1][2] == sum(VOICE.values()) + _n(0.55) + _n(0.32) + _n(0.18) + 4 * _n(0.07)
    voice_level = _rms(audio[parts[2][1]:parts[2][2]])
    for kind, a, b in parts:
        seg = audio[a:b]
        if kind == "gap":
            assert not seg.any()                                          # silence, exactly
        elif kind == "say":
            assert _rms(seg) == pytest.approx(0.2 / np.sqrt(2), rel=0.02)
        else:
            assert 0.2 * voice_level < _rms(seg) < 1.2 * voice_level and np.abs(seg).max() <= 1.0     # static, and not silence
            assert abs(float(seg[0])) < 0.01 and abs(float(seg[-1])) < 0.01    # faded in and out: no click
    assert parts[7][1] == parts[6][2]                                     # the third burst starts where his words stop
    assert [(who, text) for _, who, text in rig.s.spoken] == [("montaigne", rl.line())]     # one line in the status window


def test_the_static_is_the_same_every_time_and_each_burst_is_its_own(rig):
    seq = rl.sequence()
    rig.s.say_sequence(seq, "montaigne")
    rig.s.say_sequence(seq, "montaigne")
    assert _wait(lambda: len(rig.played) == 2)
    assert np.array_equal(rig.played[0], rig.played[1])
    a, _ = sp.static_burst(0.3, SR, sp.static_seed(0), 0.1)
    b, _ = sp.static_burst(0.3, SR, sp.static_seed(1), 0.1)
    assert len(a) == len(b) == _n(0.3) and not np.array_equal(a, b)
    assert [sp.static_seed(i) for i in range(5)] == [3, 7, 11, 15, 19]
    assert len(sp.static_burst(0.0, SR, 3, 0.1)[0]) == 0 and len(sp.static_burst(0.001, SR, 3, 0.1)[0]) == _n(0.001)


def test_band_passed_static_at_eight_tenths_of_the_voices_speech_level(rig):
    if sp.voice_fx is None:
        pytest.skip("needs scipy (voice_fx); runs under the toolbox's Python 3.14")
    burst, filtered = sp.static_burst(0.55, SR, 3, 0.1)
    assert filtered and _rms(burst) == pytest.approx(0.08, rel=0.01)
    spectrum = np.abs(np.fft.rfft(burst)) ** 2
    freqs = np.fft.rfftfreq(len(burst), 1 / SR)
    inside = spectrum[(freqs >= 300) & (freqs <= 4000)].sum() + spectrum[(freqs > 100) & (freqs < 140)].sum()
    assert inside / spectrum.sum() > 0.80                                 # the band, and the 120 Hz buzz under it
    rig.s.say_sequence(rl.sequence(), "montaigne")
    assert _wait(lambda: len(rig.played) == 1)
    parts = _parts(rl.sequence())
    first = rig.played[0][parts[2][1]:parts[2][2]]
    want = sp.STATIC_LEVEL * sp.voice_fx.speech_rms(first, SR)
    for kind, a, b in parts:
        if kind == "static":
            assert _rms(rig.played[0][a:b]) == pytest.approx(want, rel=0.03)


def test_the_approved_numbers_are_the_ones_in_the_code():
    assert sp.STATIC_BAND_HZ == (350.0, 3600.0) and sp.STATIC_LEVEL == 0.8 and sp.STATIC_SEEDS == (3, 7, 11)
    assert sp.STATIC_LEVEL_UNFILTERED == 0.4


# ---------------------------------------------------------------------------------------------------------------
# without voice_fx or scipy it still plays
# ---------------------------------------------------------------------------------------------------------------
def test_without_voice_fx_the_static_is_unfiltered_at_half_the_level_and_it_plays(monkeypatch):
    monkeypatch.setattr(sp, "voice_fx", None)
    burst, filtered = sp.static_burst(0.55, SR, 3, 0.1)
    assert not filtered and len(burst) == _n(0.55) and _rms(burst) == pytest.approx(0.04, rel=0.01)
    rig = _Rig()
    try:
        assert rig.s.say_sequence(rl.sequence(), "montaigne")
        assert _wait(lambda: len(rig.played) == 1)
        parts = _parts(rl.sequence())
        assert len(rig.played[0]) == parts[-1][2]
        voice = _rms(rig.played[0][parts[2][1]:parts[2][2]])
        for kind, a, b in parts:
            if kind == "static":
                assert _rms(rig.played[0][a:b]) == pytest.approx(sp.STATIC_LEVEL_UNFILTERED * voice, rel=0.03)
    finally:
        rig.close()


def test_with_voice_fx_there_and_scipy_failing_to_import_it_falls_back_and_never_raises(monkeypatch):
    class _Fx:                                           # voice_fx is "there", scipy is not
        @staticmethod
        def speech_rms(x, sr):
            raise RuntimeError("no scipy underneath")

        @staticmethod
        def _limit(x, sr):
            raise RuntimeError("no scipy underneath")
    monkeypatch.setattr(sp, "voice_fx", _Fx)
    monkeypatch.setitem(sys.modules, "scipy", None)
    monkeypatch.setitem(sys.modules, "scipy.signal", None)
    burst, filtered = sp.static_burst(0.2, SR, 3, 0.1)
    assert not filtered and _rms(burst) == pytest.approx(0.04, rel=0.01)
    rig = _Rig()
    try:
        assert rig.s.say_sequence(rl.sequence(), "montaigne")
        assert _wait(lambda: len(rig.played) == 1) and len(rig.played[0]) == _parts(rl.sequence())[-1][2]
        assert np.abs(rig.played[0]).max() <= 1.0 and rig.s._thread.is_alive()
    finally:
        rig.close()


def test_if_the_static_itself_fails_there_is_a_gap_of_its_length_and_the_words_are_still_said(monkeypatch, rig):
    def boom(*a, **k):
        raise MemoryError("no")
    monkeypatch.setattr(sp, "static_burst", boom)
    assert rig.s.say_sequence(rl.sequence(), "montaigne")
    assert _wait(lambda: len(rig.played) == 1)
    parts = _parts(rl.sequence())
    assert len(rig.played[0]) == parts[-1][2]
    assert all(not rig.played[0][a:b].any() for kind, a, b in parts if kind == "static")
    assert all(rig.played[0][a:b].any() for kind, a, b in parts if kind == "say")


def test_a_synth_that_does_not_return_arrays_still_works():
    """The other tests' synth returns a string. play() is then handed the parts in order, once."""
    played = []
    s = sp.Speech(Path("."), synth=lambda text, speaker: (f"{speaker}:{text}", SR), play=lambda audio, sr: played.append(audio))
    try:
        assert s.say_sequence([("static", 0.5), ("say", "one"), ("gap", 0.07), ("say", "two")], "montaigne")
        assert s.say("a plain line", "elah")
        assert _wait(lambda: len(played) == 2)
        assert played == [[("static", 0.5), "montaigne:one", ("gap", 0.07), "montaigne:two"], "elah:a plain line"]
    finally:
        s.close()


# ---------------------------------------------------------------------------------------------------------------
# one queue item: every rule a line obeys
# ---------------------------------------------------------------------------------------------------------------
def test_mute_during_the_sequence_stops_it_and_nothing_further_plays():
    rig = _Rig(hold=True)
    try:
        assert rig.s.say_sequence(rl.sequence(), "montaigne")
        assert _wait(lambda: getattr(rig, "started", False))             # it is playing (held by the test)
        assert rig.s.say("queued behind it", "elah") and rig.s.say_sequence(rl.sequence(), "montaigne")
        assert rig.s.pending() == 2 and rig.s._current is not None and rig.s._current.sequence is not None
        rig.s.mute(True)
        assert rig.s._abort.is_set() and rig.s.pending() == 0             # the device is told to stop; the queue is empty
        rig.gate.set()
        time.sleep(0.3)
        assert len(rig.played) == 1                                       # the one that was cut; nothing after it
        assert rig.s.say_sequence(rl.sequence(), "montaigne") is False
    finally:
        rig.close()


def test_a_muted_voice_which_is_what_a_hidden_window_is_refuses_it(rig):
    rig.s.mute(True)
    assert rig.s.say_sequence(rl.sequence(), "montaigne") is False
    rig.s.allow_addressed(True)                                           # the answer pass is for answers only
    assert rig.s.say_sequence(rl.sequence(), "montaigne") is False
    assert rig.s.say_sequence(rl.sequence(), "montaigne", addressed=True) is True
    rig.s.mute(False)
    assert _wait(lambda: len(rig.played) == 1)


@pytest.mark.parametrize("items", [
    [], [("static", 0.5)], [("gap", 0.1), ("static", 0.2)], [("say", "")], [("say", "one"), ("hiss", 0.2)],
    [("say", "one"), ("static", 0)], [("say", "one"), ("static", 99)], [("say", "one"), ("static", "long")], None,
    [("say", "one"), "static"],
])
def test_a_sequence_with_no_words_or_an_item_that_is_not_one_of_the_three_is_refused(rig, items):
    assert rig.s.say_sequence(items, "montaigne") is False
    assert rig.s.say_sequence([("say", "one")], "ghost") is False and rig.s.pending() == 0


def test_it_waits_its_turn_is_dropped_when_stale_and_never_overlaps_a_line():
    rig = _Rig(hold=True)
    try:
        rig.s.say("first", "elah", sp.PRIORITY_AMBIENT)
        assert _wait(lambda: getattr(rig, "started", False))
        rig.s.say_sequence(rl.sequence(), "montaigne", sp.PRIORITY_AMBIENT, text="the sequence")
        rig.s.say("you are hurt", "elah", sp.PRIORITY_EVENT)              # jumps ahead of the queued sequence
        rig.gate.set()
        assert _wait(lambda: len(rig.s.spoken) == 3)
        assert [text for _, _, text in rig.s.spoken] == ["first", "you are hurt", "the sequence"]
        assert len(rig.played) == 3                                       # three plays, one after another
        rig.gate.clear()
        rig.s.say("holds the device", "elah")
        assert _wait(lambda: len(rig.asked) == 6)                         # it has been synthesised and holds the device
        rig.s.say_sequence(rl.sequence(), "montaigne", sp.PRIORITY_AMBIENT, text="too late")
        rig.clock[0] += sp.MAX_AGE_S[sp.PRIORITY_AMBIENT] + 1             # it waited longer than a line may
        rig.gate.set()
        assert _wait(lambda: rig.s.dropped_stale == 1)
        time.sleep(0.2)
        assert "too late" not in [text for _, _, text in rig.s.spoken] and len(rig.played) == 4
    finally:
        rig.close()


def test_it_waits_for_the_games_own_dialogue_as_a_line_does():
    class _Ducker:
        def __init__(self):
            self.waits = []

        def wait_clear(self, max_wait_s, abort=lambda: False):
            self.waits.append(max_wait_s)
            return 0.5

        def volume_scale(self):
            return 1.0
    d = _Ducker()
    rig = _Rig(ducker=d)
    try:
        rig.s.say_sequence(rl.sequence(), "montaigne", sp.PRIORITY_AMBIENT)
        assert _wait(lambda: len(rig.played) == 1)
        assert d.waits == [sp.HOLD_MAX_S[sp.PRIORITY_AMBIENT]] and rig.s.held_s == 0.5
    finally:
        rig.close()


# ---------------------------------------------------------------------------------------------------------------
# the character and the level are the words', not the static's
# ---------------------------------------------------------------------------------------------------------------
def test_the_character_and_the_level_are_applied_to_each_of_the_words_and_never_to_the_static(rig):
    fx, lv = [], []

    def character(audio, sr, speaker):
        fx.append(len(audio))
        return audio * 0.5

    def level(audio, sr, speaker):
        lv.append(len(audio))
        return audio * 0.5
    rig.s._character, rig.s._level = character, level
    seq = rl.sequence()
    rig.s.say_sequence(seq, "montaigne")
    assert _wait(lambda: len(rig.played) == 1)
    assert fx == lv == [VOICE[0], VOICE[1], VOICE[2]]                     # once per `say`, with that item's audio only
    parts = _parts(seq)
    voice = _rms(rig.played[0][parts[2][1]:parts[2][2]])
    assert voice == pytest.approx(0.25 * 0.2 / np.sqrt(2), rel=0.02)      # both were applied to the words
    for kind, a, b in parts:
        if kind == "static":                                              # ... and the static follows the words down
            assert 0.2 * voice < _rms(rig.played[0][a:b]) < 1.2 * voice


def test_a_character_turned_right_down_takes_its_static_down_with_it(rig):
    rig.s.set_level("montaigne", 0.0)
    rig.s.say_sequence(rl.sequence(), "montaigne")
    assert _wait(lambda: len(rig.played) == 1)
    assert not rig.played[0].any()
