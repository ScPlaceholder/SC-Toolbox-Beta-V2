"""A mic trigger that does not arm must SAY so, by name, with the exception type.

The defect these cover: "the listener never started" and "it started fine and you never
pressed the key" used to be the same empty log — `_start_pynput` returned a bare False from
`except Exception`, and `arm()` then emitted "ears armed (...)" regardless of the result.

These tests cover assistant/starmap_ears/ - the Star Map's former ears, moved into the
Assistant on 2026-10-04 (see that package's __init__). The file came with them from
skills/Starmap/tests/test_starmap_hotkey_reporting.py; only the import path changed, and
the HOME redirect went (it protected starmap.data's state path, which is no longer
imported here, and it would have redirected HOME for every other Assistant test too).

Run explicitly (this directory is not in pyproject's testpaths):
    python -m pytest tools/Assistant/tests/test_starmap_ears_hotkey_reporting.py -q
"""

import logging
import os
import sys
import types

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                 '..', '..', '..')))
import shared.path_setup  # noqa: E402
shared.path_setup.ensure_path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from assistant.starmap_ears import ears as ears_mod                          # noqa: E402
from assistant.starmap_ears.input_devices import HotkeyMonitor, InputBinding  # noqa: E402


class _FakeListener:
    """Stands in for a pynput Listener with a chosen failure mode."""

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
    monkeypatch.setitem(sys.modules, "pynput", pkg)
    monkeypatch.setitem(sys.modules, "pynput.keyboard", kb)
    monkeypatch.setitem(sys.modules, "pynput.mouse", ms)


def _binding():
    return InputBinding(kind="keyboard", code=90, label="Z")


@pytest.fixture(autouse=True)
def _fast_timeout(monkeypatch):
    """The real 2 s readiness ceiling is not worth paying eight times over."""
    monkeypatch.setattr("assistant.starmap_ears.input_devices._READY_TIMEOUT_S", 0.05)


def test_a_listener_that_refuses_to_start_is_named_with_its_exception_type(monkeypatch, caplog):
    _fake_pynput(monkeypatch, "raise_on_start")
    m = HotkeyMonitor()
    with caplog.at_level(logging.INFO):
        assert m.start(_binding()) is False
    msg = caplog.text
    assert "did not start" in msg
    assert "OSError" in msg                 # the TYPE, not just the message
    assert "no hook available" in msg
    assert "Z" in msg                       # which binding


def test_a_listener_that_dies_reports_what_it_raised(monkeypatch, caplog):
    _fake_pynput(monkeypatch, "dies_with_reason")
    m = HotkeyMonitor()
    with caplog.at_level(logging.INFO):
        assert m.start(_binding()) is False
    assert "died on startup" in caplog.text
    assert "RuntimeError" in caplog.text
    assert "SetWindowsHookEx refused" in caplog.text


def test_a_listener_that_dies_silently_still_produces_a_line(monkeypatch, caplog):
    """The worst case: nothing to re-raise. Silence here is what the old code shipped."""
    _fake_pynput(monkeypatch, "dies_quietly")
    m = HotkeyMonitor()
    with caplog.at_level(logging.INFO):
        assert m.start(_binding()) is False
    assert "exited immediately and raised nothing" in caplog.text


def test_a_healthy_listener_returns_true_and_says_it_is_live(monkeypatch, caplog):
    _fake_pynput(monkeypatch, "healthy")
    m = HotkeyMonitor()
    with caplog.at_level(logging.INFO):
        assert m.start(_binding()) is True
    assert "is live" in caplog.text


def test_the_verdict_separates_no_events_from_the_wrong_key(monkeypatch):
    _fake_pynput(monkeypatch, "healthy")
    m = HotkeyMonitor()
    assert m.start(_binding()) is True

    assert "NO input events at all" in m.verdict()

    m._seen = 412                       # the hook delivered; none of it matched
    assert "NONE matched" in m.verdict()
    assert "412 events seen" in m.verdict()

    m._matched = 3
    v = m.verdict()
    assert "412 events seen, 3 matched it" in v
    assert "NONE matched" not in v


def test_a_first_event_of_any_kind_is_reported_once(monkeypatch, caplog):
    _fake_pynput(monkeypatch, "healthy")
    m = HotkeyMonitor()
    m.start(_binding())
    with caplog.at_level(logging.INFO):
        m._count()
        m._count()
        m._count()
    assert caplog.text.count("first input event received") == 1
    assert m._seen == 3


def test_arm_does_not_claim_armed_when_the_trigger_was_not_taken(monkeypatch):
    """The load-bearing one: the log line that said ARMED was printed without asking."""
    e = ears_mod.EarsController()
    monkeypatch.setattr(ears_mod, "_log", logging.getLogger("unused"))
    e.set_mode("push")
    e.set_binding(_binding())
    said = []
    e.statusChanged.connect(said.append)

    monkeypatch.setattr(e._monitor, "start", lambda _b: False)
    assert e.arm() is False
    assert e.armed() is False
    assert any("could not be watched" in s for s in said)
    assert not any("ears armed" in s for s in said)

    said.clear()
    monkeypatch.setattr(e._monitor, "start", lambda _b: True)
    assert e.arm() is True
    assert e.armed() is True
    assert any("ears armed" in s for s in said)


def test_push_release_does_not_finalise_a_capture_that_never_opened(monkeypatch):
    """An unguarded _finish() wrote a 0 ms "attempt ended" line for a mic never opened."""
    e = ears_mod.EarsController()
    e.set_mode("push")
    calls = []
    monkeypatch.setattr(e, "_finish", lambda: calls.append("finish"))
    monkeypatch.setattr(e, "_begin", lambda: calls.append("begin"))
    e._on_trigger(False)                 # release with nothing recording
    assert calls == []
    e._recording = True
    e._on_trigger(False)
    assert calls == ["finish"]
