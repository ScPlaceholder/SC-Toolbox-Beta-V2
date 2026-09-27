"""The assistant's ears had NEVER been entered; this covers why the log could not say so.

`voice.py` logs "ears: key down" on every trigger edge and logs/assistant.crash.log held
none, ever — while the same log said "ears status: ears armed (z, hold to talk)". Both
sentences were compatible with a listener that never started, because `HotkeyMonitor.start`
reported construction rather than arming and `arm()` printed ARMED without consulting it.

Run explicitly (this directory is not in pyproject's testpaths):
    python -m pytest tools/Assistant/tests -q
"""

import logging
import sys
import types

import pytest

from assistant import voice as voice_mod
from assistant.voice_input import HotkeyMonitor, InputBinding, KeyCaptureDialog


class _FakeListener:
    def __init__(self, mode="healthy", **_kw):
        self.mode = mode
        self.running = False
        self.name = "FakeListener"

    def start(self):
        if self.mode == "raise_on_start":
            raise OSError("no hook available")
        if self.mode == "healthy":
            self.running = True

    def is_alive(self):
        return self.mode == "healthy"

    def join(self, _timeout=None):
        if self.mode == "dies_with_reason":
            raise RuntimeError("SetWindowsHookEx refused")

    def stop(self):
        self.running = False


def _fake_pynput(monkeypatch, mode):
    kb = types.ModuleType("pynput.keyboard")
    kb.Listener = lambda **kw: _FakeListener(mode=mode, **kw)
    ms = types.ModuleType("pynput.mouse")
    ms.Listener = lambda **kw: _FakeListener(mode=mode, **kw)
    pkg = types.ModuleType("pynput")
    pkg.keyboard, pkg.mouse = kb, ms
    for name, mod in (("pynput", pkg), ("pynput.keyboard", kb), ("pynput.mouse", ms)):
        monkeypatch.setitem(sys.modules, name, mod)


@pytest.fixture(autouse=True)
def _fast_timeout(monkeypatch):
    monkeypatch.setattr("assistant.voice_input._READY_TIMEOUT_S", 0.05)


def test_a_listener_that_refuses_to_start_is_named_with_its_exception_type(monkeypatch, caplog):
    _fake_pynput(monkeypatch, "raise_on_start")
    m = HotkeyMonitor()
    with caplog.at_level(logging.INFO):
        assert m.start(InputBinding("key", "z")) is False
    assert "did not start" in caplog.text
    assert "OSError" in caplog.text
    assert "no hook available" in caplog.text
    assert "z" in caplog.text


def test_a_listener_that_dies_reports_what_it_raised(monkeypatch, caplog):
    _fake_pynput(monkeypatch, "dies_with_reason")
    m = HotkeyMonitor()
    with caplog.at_level(logging.INFO):
        assert m.start(InputBinding("key", "z")) is False
    assert "died on startup" in caplog.text
    assert "RuntimeError" in caplog.text


def test_a_listener_that_dies_silently_still_produces_a_line(monkeypatch, caplog):
    _fake_pynput(monkeypatch, "dies_quietly")
    m = HotkeyMonitor()
    with caplog.at_level(logging.INFO):
        assert m.start(InputBinding("key", "z")) is False
    assert "exited immediately and raised nothing" in caplog.text


def test_a_healthy_listener_returns_true_and_says_it_is_live(monkeypatch, caplog):
    _fake_pynput(monkeypatch, "healthy")
    m = HotkeyMonitor()
    with caplog.at_level(logging.INFO):
        assert m.start(InputBinding("key", "z")) is True
    assert "is live" in caplog.text


def test_the_verdict_separates_no_events_from_the_wrong_key(monkeypatch):
    _fake_pynput(monkeypatch, "healthy")
    m = HotkeyMonitor()
    assert m.start(InputBinding("key", "z")) is True
    assert "NO input events at all" in m.verdict()
    m._seen = 412
    assert "NONE of them matched" in m.verdict()
    m._matched = 3
    assert "412 events seen, 3 matched it" in m.verdict()


def test_key_capture_that_cannot_listen_refuses_instead_of_waiting_forever(monkeypatch, caplog):
    """"Set Mic Key" used to return True with nothing listening: no key could ever bind."""
    _fake_pynput(monkeypatch, "raise_on_start")
    d = KeyCaptureDialog()
    with caplog.at_level(logging.INFO):
        assert d.start() is False
    assert "no input listener started" in caplog.text


def test_arm_does_not_claim_armed_when_the_trigger_was_not_taken(monkeypatch):
    e = voice_mod.EarsController()
    e.set_mode("push")
    assert e.set_binding(InputBinding("key", "z")) is True
    said = []
    e.statusChanged.connect(said.append)

    monkeypatch.setattr("assistant.voice_input.HotkeyMonitor.start", lambda _s, _b: False)
    assert e.arm() is False
    assert e.armed() is False
    assert any("could not be watched" in s for s in said)
    assert not any("ears armed" in s for s in said)

    said.clear()
    monkeypatch.setattr("assistant.voice_input.HotkeyMonitor.start", lambda _s, _b: True)
    assert e.arm() is True
    assert e.armed() is True
    assert any("ears armed" in s for s in said)
