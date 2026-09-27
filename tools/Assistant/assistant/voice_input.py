"""Trigger bindings for the ears — keyboard + mouse via pynput.

A trimmed counterpart of Starmap's voice/input_devices.py: keyboard keys
and mouse buttons only (no joystick/gamepad — the assistant is a slim
always-on-top panel, and gamepad users can bind a keyboard key).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import QObject, Signal

_log = logging.getLogger(__name__)

# How long start() will wait for the listener thread to reach its message loop. pynput
# marks itself ready almost immediately; 2 s is a generous ceiling chosen so a wedged
# listener cannot hang the GUI thread that called arm().
_READY_TIMEOUT_S = 2.0


@dataclass
class InputBinding:
    """One trigger: a pynput keyboard key string or mouse button."""
    kind: str          # "key" | "mouse"
    code: str          # pynput Key enum name / KeyCode char / Button name

    def describe(self) -> str:
        if self.kind == "mouse":
            return "mouse " + self.code.replace("Button.", "")
        return self.code.replace("Key.", "")

    def refused(self) -> str:
        """Why this binding cannot be the mic key, or "" if it can.

        Left and right click are refused: the listener does not swallow
        the click, so every shot fired or menu clicked in game would also
        open the mic.
        """
        if self.kind == "mouse" and self.code in REFUSED_MOUSE:
            return REFUSED_MSG
        return ""


# pynput Button names that can never be the mic key
REFUSED_MOUSE = ("Button.left", "Button.right")
REFUSED_MSG = ("Left and right mouse buttons can't be the mic key: every click in "
               "game would open the mic. Press a keyboard key (or a side mouse button).")


class HotkeyMonitor(QObject):
    """Watches a binding and re-emits pressed/released as Qt signals.

    ⛔ 2026-09-26: the assistant's ears had never once been ENTERED. `voice.py` logs
      "ears: key down" on every trigger edge and `logs/assistant.crash.log` contained
      none, ever — and this class was the reason the log could not say why. Three
      defects, all of which made "the listener never started" and "it started fine and
      nobody pressed the key" the same empty log:

      1. `start()` returned True as soon as a Listener OBJECT existed. The actual
         `lis.start()` ran later, on a helper thread, inside `except Exception: pass`.
         So the return value reported construction, not arming.
      2. That helper thread was pure loss. A pynput Listener IS a `threading.Thread`;
         `lis.start()` spawns its own message-loop thread, so `_run` existed only to
         call `start()` and then block in `wait()` before exiting. What it really did
         was move the one failure that matters onto a thread with a silent handler —
         and pynput reports a listener's start failure by re-raising it out of
         `join()`, which nothing ever called. The swallow discarded pynput's ONLY
         error channel.
      3. `on_error=_on_error` was never a handler. `pynput.keyboard.Listener.__init__`
         forwards only on_press/on_release/suppress to its base and collects
         prefix-matched kwargs into `_options`; an unknown kwarg is dropped. Checked
         against the installed copy (pynput 2024, site-packages of the 3.14
         interpreter these tools run on): `grep -rn on_error` over the whole pynput
         tree returns nothing, so there is no such concept to hook into. It read like
         error handling and did nothing at all.

    ⇒ `start()` now starts the listener on the calling thread, waits (bounded) for it
      to reach its loop, and on failure logs the binding by name with the exception
      TYPE before returning False.

    ⚠ WHAT THIS STILL CANNOT PROVE, and it must not be read as more than it is: pynput
      calls `_mark_ready()` BEFORE installing the Windows hook, and `SetWindowsHookEx`
      failing returns NULL without raising (pynput's `SystemHook.__enter__` does not
      check it). So a live listener thread proves the code got that far; it does NOT
      prove Windows accepted the hook, and it cannot prove the hook will be delivered
      while a window at higher integrity (Star Citizen launched as administrator) has
      focus — UIPI drops those silently, at the OS, with no error anywhere.
      That hole is why the counters below exist: `_seen` rises on ANY key or click, so
      one real keypress anywhere is positive proof the hook is delivering, and 0 after
      minutes of play is positive proof it is not. Neither is inferable from `running`.

    REJECTED: synthesising a keypress at arm time to prove delivery. It would type into
      whatever has focus — normally the game — so the test would be indistinguishable
      from the player's own input and could fire a weapon. The counters get the same
      evidence from input the player was making anyway.
    """

    triggered = Signal(bool)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._binding: Optional[InputBinding] = None
        self._listeners = []
        self._armed_at = 0.0
        self._seen = 0            # input events of ANY key/button: is the hook delivering?
        self._matched = 0         # events that matched this binding
        self._first_logged = False

    def start(self, binding: InputBinding) -> bool:
        self.stop()
        self._binding = binding
        self._seen = 0
        self._matched = 0
        self._first_logged = False
        self._armed_at = time.monotonic()
        try:
            from pynput import keyboard, mouse
        except ImportError as exc:
            _log.error("hotkey: pynput is not installed (%s), so %s cannot be watched and "
                       "the mic can never open", exc, binding.describe())
            return False

        kind = "keyboard" if binding.kind == "key" else "mouse"
        try:
            if binding.kind == "key":
                lis = keyboard.Listener(on_press=self._press, on_release=self._release)
            else:
                lis = mouse.Listener(on_click=self._click)
            lis.start()
        except Exception as exc:
            # Narrow enough to name: the constructor and start() can raise OSError (no
            # hook available), RuntimeError (already started) or ImportError from a
            # platform backend. Caught broadly anyway because whatever it is, the ears
            # are dead and the player is owed the reason — but it is REPORTED, with the
            # type, which is the whole point of this handler existing.
            _log.error("hotkey: the %s listener for %s did not start: %s: %s",
                       kind, binding.describe(), type(exc).__name__, exc)
            return False

        self._listeners.append(lis)
        deadline = time.monotonic() + _READY_TIMEOUT_S
        while not lis.running and lis.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        if not lis.is_alive():
            # pynput passes an exception raised inside the listener thread to whoever
            # joins it, and re-raises it there. join() is therefore the diagnosis, not
            # merely cleanup — this is the channel the old `except Exception: pass` threw
            # away. A clean exit with nothing to re-raise is itself a finding, so the
            # else branch speaks too.
            try:
                lis.join(0.2)
                _log.error("hotkey: the %s listener for %s exited immediately and raised "
                           "nothing - it is not watching anything",
                           kind, binding.describe())
            except Exception as exc:
                _log.error("hotkey: the %s listener for %s died on startup: %s: %s",
                           kind, binding.describe(), type(exc).__name__, exc)
            self._listeners = []
            return False
        if not lis.running:
            _log.error("hotkey: the %s listener for %s was still not running after %.1fs; "
                       "treating it as failed", kind, binding.describe(), _READY_TIMEOUT_S)
            self.stop()
            return False

        _log.info("hotkey: %s listener for %s is live (thread %s). A key event of ANY kind "
                  "will now be reported once; if none ever is, nothing is reaching this "
                  "process.", kind, binding.describe(), lis.name)
        return True

    def stop(self) -> None:
        had = bool(self._listeners)
        for lis in self._listeners:
            try:
                lis.stop()
            except Exception as exc:
                # Teardown: the listener is being abandoned either way, so this cannot be
                # made to matter — but a stop() that throws leaves an OS hook installed,
                # which is worth one line rather than none.
                _log.warning("hotkey: could not stop a listener cleanly: %s: %s",
                             type(exc).__name__, exc)
        self._listeners = []
        if had:
            _log.info("hotkey: %s", self.verdict())

    def verdict(self) -> str:
        """One sentence separating "never armed" / "armed, nothing delivered" / "wrong key".

        Bounded by construction: emitted once per stop(), never per event.
        """
        held = time.monotonic() - self._armed_at if self._armed_at else 0.0
        name = self._binding.describe() if self._binding is not None else "(no binding)"
        if self._seen == 0:
            return ("released %s after %.0fs having seen NO input events at all - the OS "
                    "hook delivered nothing to this process (a window running as "
                    "administrator with focus blocks it), or nothing was pressed"
                    % (name, held))
        if self._matched == 0:
            return ("released %s after %.0fs - the hook IS delivering (%d events seen) but "
                    "NONE of them matched %s, so the mic key is wrong, or it was pressed "
                    "with shift/ctrl held (which changes the reported character)"
                    % (name, held, self._seen, name))
        return ("released %s after %.0fs - %d events seen, %d matched it"
                % (name, held, self._seen, self._matched))

    # ── handlers (pynput threads) ────────────────────────────────────────
    def _count(self) -> None:
        self._seen += 1
        if not self._first_logged:
            self._first_logged = True
            _log.info("hotkey: first input event received %.1fs after arming - the OS hook "
                      "IS delivering to this process",
                      time.monotonic() - self._armed_at)

    def _matches_key(self, key) -> bool:
        b = self._binding
        if b is None or b.kind != "key":
            return False
        name = getattr(key, "name", None) or str(getattr(key, "char", ""))
        return name == b.code or str(key) == b.code

    def _press(self, key) -> None:
        self._count()
        if self._matches_key(key):
            self._matched += 1
            self.triggered.emit(True)

    def _release(self, key) -> None:
        if self._matches_key(key):
            self.triggered.emit(False)

    def _click(self, x, y, button, pressed) -> None:
        if pressed:
            self._count()
        b = self._binding
        if b is None or b.kind != "mouse":
            return
        if str(button) == b.code:
            if pressed:
                self._matched += 1
            self.triggered.emit(bool(pressed))


class KeyCaptureDialog(QObject):
    """One-shot 'press anything' capture (keyboard or mouse).

    Emits captured(InputBinding) then stops listening. A refused press
    (left or right click) emits refused(message) and keeps listening, so
    the user can press a key straight after.
    """

    captured = Signal(object)
    refused = Signal(str)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._listeners = []

    def start(self) -> bool:
        """True only if at least one listener is really running.

        ⛔ 2026-09-26: this used to return True unconditionally and start each listener
          inside `except Exception: pass`, so "Set Mic Key" that could never capture
          anything looked identical to one waiting patiently for a press. The player is
          then stuck: no key can be bound, and nothing says so. `on_error=` is gone for
          the reason given on HotkeyMonitor — pynput drops unknown kwargs, so it was
          never a handler.
        """
        self.cancel()
        try:
            from pynput import keyboard, mouse
        except ImportError as exc:
            _log.error("key capture: pynput is not installed (%s); no mic key can be bound",
                       exc)
            return False

        started = []
        for label, make in (("keyboard", lambda: keyboard.Listener(on_press=self._got_key)),
                            ("mouse", lambda: mouse.Listener(on_click=self._got_click))):
            try:
                lis = make()
                lis.start()
            except Exception as exc:
                # Broad but named: either device's listener failing still leaves the other
                # usable (a keyboard key is a valid binding with no mouse hook, and vice
                # versa), so this must not abort the capture — only report it.
                _log.error("key capture: the %s listener did not start: %s: %s",
                           label, type(exc).__name__, exc)
                continue
            self._listeners.append(lis)
            started.append(label)
        if not started:
            _log.error("key capture: no input listener started; nothing can be captured")
            return False
        _log.info("key capture: listening for a press on %s", " and ".join(started))
        return True

    def cancel(self) -> None:
        for lis in self._listeners:
            try:
                lis.stop()
            except Exception as exc:
                _log.warning("key capture: could not stop a listener cleanly: %s: %s",
                             type(exc).__name__, exc)
        self._listeners = []

    def _finish(self, binding: InputBinding) -> None:
        self.cancel()
        self.captured.emit(binding)

    def _got_key(self, key) -> None:
        name = getattr(key, "name", None)
        if name:
            self._finish(InputBinding("key", name))
            return
        ch = getattr(key, "char", None)
        if ch:
            self._finish(InputBinding("key", ch))

    def _got_click(self, x, y, button, pressed) -> None:
        if not pressed:
            return
        binding = InputBinding("mouse", str(button))
        why = binding.refused()
        if why:
            self.refused.emit(why)
            return
        self._finish(binding)
