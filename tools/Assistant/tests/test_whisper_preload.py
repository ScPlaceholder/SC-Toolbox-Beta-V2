"""The whisper preload added 2026-09-26, and the guarantee that it cannot break arming.

WHY THIS FILE EXISTS. The preload went in and the existing 7 tests stayed green.
That looked like coverage and was not: mutating the preload to re-raise left the
suite at 7/7, which means no test reached the line at all. Green-because-blind and
green-because-correct are indistinguishable until you kill the code and watch.

WHAT IT COSTS, measured 2026-09-26 on this machine: 0.25s import + 1.81s to build
small.en int8 = 2.06s, warm cache. That used to be paid on the FIRST utterance, so
J wore it mid-sentence. arm() now pays it on a worker thread instead.

THE LOAD-BEARING CLAIM is not "it is faster" -- it is that a preload which FAILS
must not cost you your ears. _get_model() will still load lazily on the first
utterance exactly as before, so the worst case is the old behaviour, never a dead
microphone. That is what test_a_failing_preload_still_arms pins down.
"""
import logging
import sys
import types

import pytest

import assistant.voice as voice_mod


@pytest.fixture
def deps_ok(monkeypatch):
    """Satisfy arm()'s dependency gate without needing the real packages.

    The gate runs BEFORE the preload, so on an interpreter missing sounddevice or
    faster-whisper every test below would return False at line one and assert
    nothing -- passing for a reason that has nothing to do with the preload.
    """
    monkeypatch.setattr("assistant.missing_voice_deps", lambda: [])


def _fake_whisper(monkeypatch, ctor):
    mod = types.ModuleType("faster_whisper")
    mod.WhisperModel = ctor
    monkeypatch.setitem(sys.modules, "faster_whisper", mod)


def test_arm_preloads_whisper_off_the_hot_path(deps_ok, monkeypatch):
    built = []

    class FakeModel:
        def __init__(self, name, device=None, compute_type=None, cpu_threads=None):
            built.append((name, device, compute_type, cpu_threads))

    _fake_whisper(monkeypatch, FakeModel)

    e = voice_mod.EarsController()
    e.set_mode("always")
    assert e.arm() is True

    assert e._preload is not None, "arm() did not start a preload at all"
    e._preload.join(timeout=30)
    assert not e._preload.is_alive(), "preload thread never finished"

    assert built == [("small.en", "cpu", "int8", voice_mod._whisper_cpu_threads())]
    assert e._model is not None, "the model was not cached, so the first utterance still pays"


def test_a_failing_preload_still_arms(deps_ok, monkeypatch, caplog):
    """The whole safety argument: a broken preload degrades to the OLD behaviour."""

    class Exploding:
        def __init__(self, *a, **kw):
            raise RuntimeError("no model on this machine")

    _fake_whisper(monkeypatch, Exploding)

    e = voice_mod.EarsController()
    e.set_mode("always")
    with caplog.at_level(logging.WARNING):
        assert e.arm() is True, "a failed preload must NOT cost the player their ears"
    e._preload.join(timeout=30)

    assert e._model is None
    assert "whisper preload failed" in caplog.text, (
        "a preload that fails silently is worse than one that fails loudly -- "
        "the next person sees only that the first utterance is slow again")


def test_the_model_is_built_once_even_though_two_threads_want_it(deps_ok, monkeypatch):
    """_transcribe calls _get_model on its own thread while the preload may still run.

    Without the lock both see self._model is None and construct their own, which
    means two copies of small.en resident and the 2s paid twice.
    """
    built = []

    class SlowModel:
        def __init__(self, name, device=None, compute_type=None, cpu_threads=None):
            import time
            time.sleep(0.2)          # widen the window the lock has to close
            built.append(name)

    _fake_whisper(monkeypatch, SlowModel)

    e = voice_mod.EarsController()
    e.set_mode("always")
    assert e.arm() is True
    e._get_model()                   # races the preload thread, as _transcribe does
    e._preload.join(timeout=30)

    assert built == ["small.en"], "built %d models, expected 1" % len(built)


def test_switching_models_re_enables_the_preload(deps_ok, monkeypatch):
    """set_model clears _model; it must clear _preload too or the preload dies for good."""
    _fake_whisper(monkeypatch, lambda name, device=None, compute_type=None, cpu_threads=None: object())

    e = voice_mod.EarsController()
    e.set_mode("always")
    assert e.arm() is True
    e._preload.join(timeout=30)

    e.set_model("base.en")
    assert e._model is None
    assert e._preload is None, (
        "a finished preload thread left in place makes _preload_model() believe "
        "one is already in flight, so the NEW model is never preloaded")
