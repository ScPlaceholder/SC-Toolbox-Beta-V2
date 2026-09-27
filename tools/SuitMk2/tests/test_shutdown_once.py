"""SuitMk2 shutdown must run exactly once (2026-09-26).

THE BUG. Two lines, each sane alone:

    suitmk2_companion_app.py:63   app.aboutToQuit.connect(window._quit)
    suit_window.py:824-826        finally: QApplication.quit()

so ``_quit`` ends by asking Qt to quit, Qt answers by emitting ``aboutToQuit``, and
``aboutToQuit`` calls ``_quit``. Measured in Qt 6.11: ``QApplication.quit()`` re-emits
``aboutToQuit`` even when it is called from inside that very emission, so the pair is an
unbounded loop that ends in ``RecursionError: Stack overflow (used 2912 kB)``.

Production evidence, logs/suitmk2.crash.log: "ears: ears off" and "LogMonitor stopped"
repeat 485 times on MainThread about 9 ms apart, for PIDs 36136 and 188024, and 122-365
times for five more; and the dream-queue session files carry one ``session_end`` line per
re-entry (485, 485, 365, 350, 348, 346, 122).

⚠ THE LOGGING IS A BYSTANDER, NOT THE CYCLE. The first reading of the traceback blamed the
log path (a teardown warning that itself raises, re-entering the logger). It does not: a
control with no logging and no failing teardown step still re-enters 485 times and still
dies with the same "used 2912 kB". The teardown warnings in companion_core.stop() only
*surface* the overflow, because by the time they run the stack is already spent -- which is
also why the captured frames repeat twice rather than hundreds of times.

THE FUSE. These tests cut the aboutToQuit -> _quit link after FUSE re-entries so pytest
survives; without it the test process dies exactly as the app does. The fuse bounds the
observation, it does not touch the code under test.
"""
from __future__ import annotations

import logging
import os
import sys

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMainWindow

FUSE = 25          # generous: the real app reaches 485


# --------------------------------------------------------------------------------------
# fakes: just enough for the real _quit body, and each one counts its own calls
# --------------------------------------------------------------------------------------
class FakeEars:
    def __init__(self):
        self.shutdown_calls = 0
        self.stream_open = True          # stands in for the sounddevice InputStream

    def shutdown(self):
        self.shutdown_calls += 1
        self.stream_open = False         # ears.disarm() -> _abort_recording() -> stream.close()


class FakeCore:
    def __init__(self, raise_on_stop=False):
        self.stop_calls = 0
        self.poll_thread_running = True
        self._raise = raise_on_stop

    def stop(self):
        self.stop_calls += 1
        self.poll_thread_running = False
        if self._raise:
            raise RuntimeError("teardown step failed")


class FakeSpeech:
    def __init__(self):
        self.close_calls = 0

    def close(self):
        self.close_calls += 1


class FakeSidecar:
    def __init__(self):
        self.stop_calls = 0

    def stop(self):
        self.stop_calls += 1


@pytest.fixture
def app():
    a = QApplication.instance() or QApplication([])
    yield a


def _suit_window_class():
    """The real SuitWindow class, loaded by PATH under a private module name.

    ⚠ NOT `from ui.suit_window import SuitWindow`. The toolbox root has a `ui` package of
    its own, so in a whole-suite run whichever test imports `ui` first wins and this one
    then dies with ModuleNotFoundError -- which is how a test file goes dark while looking
    like it merely needs its own runner. Measured: 5 of these 6 failed that way when
    tools/SuitMk2/tests was added to pyproject testpaths.
    """
    import importlib.util
    name = "_suitmk2_ui_suit_window_under_test"
    if name in sys.modules:
        return sys.modules[name].SuitWindow
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "ui", "suit_window.py")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod.SuitWindow


def make_window(core=None):
    """The REAL SuitWindow, with the real _quit, without its 700-line __init__."""
    SuitWindow = _suit_window_class()
    w = SuitWindow.__new__(SuitWindow)
    QMainWindow.__init__(w)
    w._standalone = True
    w.ears = FakeEars()
    w._fb_mon = {}
    w.core = core if core is not None else FakeCore()
    w.speech = FakeSpeech()
    w.sidecar = FakeSidecar()
    w.setup = None
    return w


def run_quit_cycle(app, window):
    """Wire it exactly as suitmk2_companion_app.main() does, then ask Qt to quit.

    Returns how many times aboutToQuit reached _quit.
    """
    seen = [0]

    def fuse():
        seen[0] += 1
        if seen[0] > FUSE:
            app.aboutToQuit.disconnect(window._quit)

    app.aboutToQuit.connect(fuse)                 # connected FIRST, so it runs first
    app.aboutToQuit.connect(window._quit)         # suitmk2_companion_app.py:63
    try:
        QTimer.singleShot(0, QApplication.quit)   # stands in for the IPC "quit" command
        app.exec()
    finally:
        app.aboutToQuit.disconnect(fuse)
        if seen[0] <= FUSE:                       # otherwise the fuse already cut it
            app.aboutToQuit.disconnect(window._quit)
    return seen[0]


# --------------------------------------------------------------------------------------
# tests
# --------------------------------------------------------------------------------------
def test_quit_does_not_re_enter_itself(app):
    """aboutToQuit -> _quit -> QApplication.quit() -> aboutToQuit must not be a loop."""
    w = make_window()
    run_quit_cycle(app, w)
    assert w.speech.close_calls == 1, (
        f"_quit ran its teardown {w.speech.close_calls} times; in the app this reaches 485 "
        f"and ends in RecursionError"
    )


def test_teardown_steps_each_run_exactly_once(app):
    """Every step of the teardown, not just the first, is exactly-once."""
    w = make_window()
    run_quit_cycle(app, w)
    assert (w.ears.shutdown_calls, w.core.stop_calls, w.sidecar.stop_calls) == (1, 1, 1)


def test_microphone_and_poll_thread_are_released(app):
    """The two resources the loop puts at risk: the mic stream and the Game.log poll thread."""
    w = make_window()
    run_quit_cycle(app, w)
    assert w.ears.stream_open is False, "the microphone stream was never closed"
    assert w.core.poll_thread_running is False, "the Game.log poll thread was never stopped"


def test_quit_called_twice_by_hand_tears_down_once(app):
    """The other live re-entry: an IPC "quit" command and then aboutToQuit.

    handle_ipc_command("quit") -> _quit, _on_close -> _quit, and aboutToQuit -> _quit are
    three wirings onto one method, and in a normal shutdown at least two of them fire.
    Teardown is still exactly-once.
    """
    w = make_window()
    w._quit()
    w._quit()
    assert (w.ears.shutdown_calls, w.core.stop_calls, w.speech.close_calls) == (1, 1, 1)


def test_a_failing_teardown_step_does_not_multiply_the_teardown(app, qtbot):
    """A step that raises must not turn one shutdown into hundreds.

    This is the shape of the real 22:19 crash: companion_core.stop()'s own log.warning
    raised on a spent stack, so every level of the loop raised too. The raise itself is
    not the bug; the loop around it is.
    """
    w = make_window(core=FakeCore(raise_on_stop=True))
    with qtbot.capture_exceptions():
        run_quit_cycle(app, w)
    assert w.core.stop_calls == 1, (
        f"a single raising teardown step ran {w.core.stop_calls} times"
    )


def test_companion_core_stop_still_reports_a_failed_teardown(caplog):
    """The real CompanionCore.stop(), with teardown steps that raise, on purpose.

    This is the guard on the ⛔ rule: if a future fix makes a failed teardown go
    unrecorded, this test goes red.
    """
    import threading
    from companion_core import CompanionCore

    class Boom:
        def stop(self):
            raise OSError("device gone")

    class Recorder:
        def close(self):
            raise OSError("disk gone")

    fake_self = type("S", (), {})()
    fake_self._stop = threading.Event()
    fake_self.sound = Boom()
    fake_self._monitor = Boom()
    fake_self.recorder = Recorder()
    fake_self.store = None

    with caplog.at_level(logging.WARNING, logger="suitmk2.core"):
        CompanionCore.stop(fake_self)          # must not raise: every step survivable

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "sound classifier did not stop" in text
    assert "Game.log monitor did not stop" in text
    assert any(r.exc_info for r in caplog.records), "the traceback was dropped"
