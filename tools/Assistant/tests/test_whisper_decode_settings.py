"""The two decode settings changed 2026-09-27, and why each assertion is here.

MEASURED, on 28 synthesised SC command utterances (16 kHz mono, the pipeline's
own sample rate), identical bytes for every configuration, 3 repeats each, on a
20-core / 28-thread i7-14700F:

    small.en int8 beam5, 4 threads, timestamps  (before)  1.938 / 1.834 s/utt
    small.en int8 beam5, 16 threads, no stamps   (after)   1.507 / 1.395 s/utt
                                                          -22.3% and -23.9%

Two numbers per row because the machine drifts -- one repeated configuration
moved 26% over an evening. So the claim is the RATIO, measured twice in two
independent passes, never the absolute figure.

WHAT THESE TESTS ARE FOR, and it is not "is it fast". A benchmark cannot live in
a test suite. What can live here is the thing the benchmark would silently lose:
the SETTINGS. Both changes are one keyword each, both look like noise in a diff,
and reverting either costs 17-23% with no visible symptom -- no error, no log
line, nothing but a slower machine. That is exactly the kind of regression that
survives for months. So each one is pinned, and the pin names the measurement.

WHAT IS DELIBERATELY *NOT* CHANGED, pinned below for the same reason:
  * beam_size stays 5. Dropping it to 1 measured only -4% to -11% (the range is
    that wide because it did not replicate cleanly) and on the noisy corpus it
    moved 2 of 14 ship names. Not worth it.
  * compute_type stays int8. On this CPU ctranslate2 offers int8, int8_float32,
    int16 and float32 -- int8 was fastest, int16 +30%, float32 +44%. int8_float16
    is not offered on CPU at all.
  * the initial_prompt stays. With it, all 14 in-prompt command phrases came
    back exact on every model tested including tiny.en. A probe decode without
    it heard "Route to Pyro" as "Root to pyro".
"""
import sys
import types

import pytest

import assistant.voice as voice_mod


@pytest.fixture
def deps_ok(monkeypatch):
    monkeypatch.setattr("assistant.missing_voice_deps", lambda: [])


class _Recorder:
    """A stand-in whisper that records how it was called and returns one segment."""

    def __init__(self, name, device=None, compute_type=None, cpu_threads=None):
        self.ctor = {"name": name, "device": device,
                     "compute_type": compute_type, "cpu_threads": cpu_threads}
        self.calls = []

    def transcribe(self, pcm, **kw):
        self.calls.append(kw)
        seg = types.SimpleNamespace(text="route to pyro")
        return [seg], types.SimpleNamespace()


def _install(monkeypatch, holder):
    mod = types.ModuleType("faster_whisper")

    def ctor(name, device=None, compute_type=None, cpu_threads=None):
        holder.append(_Recorder(name, device, compute_type, cpu_threads))
        return holder[-1]

    mod.WhisperModel = ctor
    monkeypatch.setitem(sys.modules, "faster_whisper", mod)


# --------------------------------------------------------------------------
# cpu_threads
# --------------------------------------------------------------------------

def test_cpu_threads_is_passed_and_is_not_the_library_default(deps_ok, monkeypatch):
    """faster-whisper's default is cpu_threads=0, which ctranslate2 reads as 4.

    Four threads on a twenty-core machine was the single largest free win in the
    whole measurement, so the argument has to actually arrive. Asserting "not
    None" alone would pass if someone wrote cpu_threads=0 and handed the default
    straight back, so the value is checked too.
    """
    holder = []
    _install(monkeypatch, holder)
    e = voice_mod.EarsController()
    e.set_mode("always")
    assert e.arm() is True
    e._preload.join(timeout=30)

    assert holder, "no model was constructed at all"
    got = holder[0].ctor["cpu_threads"]
    assert got is not None, "cpu_threads was not passed; the library default of 4 is back"
    assert got == voice_mod._whisper_cpu_threads()
    assert got > 0, "cpu_threads=0 IS the default -- this change would be a no-op"


def test_cpu_threads_is_floored_at_four_and_capped_at_sixteen(monkeypatch):
    """Measured: 20 threads was WORSE than 16 and 28 was no better than 4.

    The cap is not tidiness. Past the P-core threads on this part the work lands
    on E-cores and the slowest thread sets the pace, so an uncapped cpu_count
    would have shipped the second-worst setting on this very machine. The floor
    keeps a small laptop at the library default rather than starving it.
    """
    cases = {1: 4, 2: 4, 4: 4, 8: 8, 12: 12, 16: 16, 20: 16, 28: 16, 64: 16, None: 4}
    for reported, expected in cases.items():
        monkeypatch.setattr(voice_mod.os, "cpu_count", lambda r=reported: r)
        assert voice_mod._whisper_cpu_threads() == expected, (
            "cpu_count()=%r should give %d threads, got %d"
            % (reported, expected, voice_mod._whisper_cpu_threads()))


# --------------------------------------------------------------------------
# without_timestamps
# --------------------------------------------------------------------------

def test_the_decode_asks_for_no_timestamps(deps_ok, monkeypatch):
    """-17% per utterance for a setting whose output nobody reads.

    _transcribe joins s.text and discards s.start / s.end, so every timestamp
    token the decoder emits is work thrown away. If a future change starts using
    the timestamps this test should be deleted on purpose -- which is the point
    of it being here rather than in a comment.
    """
    holder = []
    _install(monkeypatch, holder)
    e = voice_mod.EarsController()
    e.set_mode("always")
    assert e.arm() is True
    e._preload.join(timeout=30)

    e._transcribe([0.0] * 16000)
    assert holder[0].calls, "_transcribe never called the model"
    kw = holder[0].calls[0]
    assert kw.get("without_timestamps") is True, (
        "timestamps are back on; that is 17%% of the decode spent on tokens "
        "that _transcribe throws away. kwargs were %r" % (kw,))


def test_the_settings_that_were_measured_and_deliberately_kept(deps_ok, monkeypatch):
    """beam_size 5, the English lock, and the ship-name prompt all stay.

    These are pinned because the obvious "make it faster" edits are to drop the
    beam or the prompt, and the measurement says both would cost accuracy for
    little or no speed: beam 1 saved 4-11%% and moved 2 of 14 ship names, and
    without the prompt a probe heard "Route to Pyro" as "Root to pyro".
    """
    holder = []
    _install(monkeypatch, holder)
    e = voice_mod.EarsController()
    e.set_mode("always")
    assert e.arm() is True
    e._preload.join(timeout=30)
    e._transcribe([0.0] * 16000)

    kw = holder[0].calls[0]
    assert kw.get("beam_size") == 5, "beam_size moved; re-measure before accepting it"
    assert kw.get("language") == "en"
    assert kw.get("initial_prompt") == voice_mod._WHISPER_PROMPT
    assert holder[0].ctor["compute_type"] == "int8", (
        "int8 was the fastest compute_type offered on CPU; int16 cost +30%% and "
        "float32 +44%%")
