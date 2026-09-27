"""Input bindings: keyboard, mouse, joystick and gamepad buttons.

A binding is a small serialisable dataclass (InputBinding) so it can live
in the starmap state JSON. BindingCaptureDialog asks the user to "press
anything" and records the first event from any supported device.
HotkeyMonitor watches one binding and re-emits press / release edges as
Qt signals.

Keyboard + mouse come from pynput; joysticks/gamepads come from pygame
(its event pump runs on a daemon thread, never on the GUI thread).
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict, dataclass
from typing import Optional

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import QDialog, QLabel, QVBoxLayout

from shared.qt.theme import P

ACCENT = P.energy_cyan
_log = logging.getLogger(__name__)

# How long _start_pynput waits for the listener thread to reach its message loop. pynput
# marks itself ready almost immediately; 2 s is a ceiling chosen so a wedged listener
# cannot hang the GUI thread that called arm().
_READY_TIMEOUT_S = 2.0


@dataclass
class InputBinding:
    kind: str = "keyboard"          # keyboard | mouse | joystick | gamepad
    code: object = None             # vk int (keyboard) / button name (mouse) / int (joy)
    label: str = ""
    joy_index: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "InputBinding":
        try:
            return cls(kind=str(d.get("kind", "keyboard")),
                       code=d.get("code"),
                       label=str(d.get("label", "")),
                       joy_index=int(d.get("joy_index", 0)))
        except (TypeError, ValueError):
            return cls()

    def describe(self) -> str:
        return self.label or "%s:%s" % (self.kind, self.code)


class BindingCaptureDialog(QDialog):
    """Modal press-any-input grabber. First event wins; Esc cancels."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.result: Optional[InputBinding] = None
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setModal(True)
        self.setMinimumWidth(430)

        self._listeners = []
        self._joy_thread = None
        self._joy_stop = threading.Event()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        card = QLabel()
        card.setStyleSheet(
            f"background: {P.bg_card}; border: 1px solid {ACCENT}; border-radius: 8px; "
            f"padding: 18px 22px;")
        lay = QVBoxLayout(card)
        lay.setSpacing(8)
        t = QLabel("Press any key, mouse button,\njoystick or gamepad button...")
        t.setStyleSheet(
            f"color: {ACCENT}; font-family: Consolas; font-size: 12pt; font-weight: bold; "
            f"background: transparent;")
        t.setAlignment(Qt.AlignCenter)
        lay.addWidget(t)
        s = QLabel("Esc cancels")
        s.setStyleSheet(f"color: {P.fg_dim}; font-size: 8pt; background: transparent;")
        s.setAlignment(Qt.AlignCenter)
        lay.addWidget(s)
        outer.addWidget(card)

        QTimer.singleShot(0, self._start)

    def _start(self) -> None:
        """Arm every capture device.

        ⛔ 2026-09-26: every listener here was constructed and started inside
          `except Exception: pass`, and `_start` returns nothing, so a dialog that could
          capture NOTHING looked exactly like one waiting for a press. The player is then
          stuck with no way to bind a mic key and no line saying why. Each failure is now
          named; the others still arm, because a keyboard key is a valid binding with no
          mouse hook and vice versa.
        """
        armed = []
        try:
            from pynput import keyboard, mouse
        except ImportError as exc:
            _log.error("key capture: pynput is not installed (%s); no keyboard or mouse "
                       "binding can be captured", exc)
            keyboard = mouse = None

        if keyboard is not None:
            def on_press(key):
                try:
                    if key == keyboard.Key.esc:
                        QTimer.singleShot(0, self.reject)
                        return False
                    vk = getattr(key, "vk", None)
                    if vk is None and hasattr(key, "value"):
                        vk = getattr(key.value, "vk", None)
                    if vk is None:
                        return True
                    char = getattr(key, "char", None)
                    label = str(char) if char else str(key).replace("Key.", "").upper()
                    self._finish(InputBinding(kind="keyboard", code=int(vk), label=label))
                    return False
                except Exception:
                    return True
            try:
                self._listeners.append(keyboard.Listener(on_press=on_press))
                armed.append("keyboard")
            except Exception as exc:
                _log.error("key capture: the keyboard listener could not be created: %s: %s",
                           type(exc).__name__, exc)

        if mouse is not None:
            def on_click(_x, _y, button, pressed):
                if pressed:
                    try:
                        label = str(button).replace("Button.", "").upper()
                        b = InputBinding(kind="mouse", code=str(button),
                                         label="MOUSE " + label)
                        self._finish(b)
                        return False
                    except Exception:
                        return True
                return True
            try:
                self._listeners.append(mouse.Listener(on_click=on_click))
                armed.append("mouse")
            except Exception as exc:
                _log.error("key capture: the mouse listener could not be created: %s: %s",
                           type(exc).__name__, exc)

        # Joystick / gamepad via pygame (polled on a daemon thread).
        try:
            import pygame
            pygame.init()
            pygame.joystick.init()
            if pygame.joystick.get_count() > 0:
                def poll():
                    stop = self._joy_stop
                    while not stop.is_set():
                        try:
                            for ev in pygame.event.get():
                                if ev.type == pygame.JOYBUTTONDOWN:
                                    joy = pygame.joystick.Joystick(ev.joy)
                                    nm = joy.get_name() or ("Joystick %d" % ev.joy)
                                    kind = "gamepad" if "controller" in \
                                        nm.lower() else "joystick"
                                    b = InputBinding(kind=kind, code=int(ev.button),
                                                     label="%s btn %d" % (nm, ev.button),
                                                     joy_index=int(ev.joy))
                                    self._finish(b)
                                    return
                        except Exception:
                            pass
                        stop.wait(0.03)
                self._joy_thread = threading.Thread(target=poll, daemon=True,
                                                    name="JoyCapture")
                self._joy_thread.start()
                armed.append("joystick")
        except ImportError as exc:
            _log.debug("key capture: pygame is not installed (%s); no stick capture", exc)

        started = []
        for lst in self._listeners:
            try:
                lst.start()
                started.append(type(lst).__module__.rsplit(".", 1)[0].rsplit(".", 1)[-1])
            except Exception as exc:
                _log.error("key capture: a listener refused to start: %s: %s",
                           type(exc).__name__, exc)
        if not self._listeners and "joystick" not in armed:
            _log.error("key capture: nothing is listening, so no binding can be captured")
        else:
            _log.info("key capture: waiting for a press (created %s, started %s)",
                      ", ".join(armed) or "nothing", ", ".join(started) or "nothing")

    def _finish(self, binding: InputBinding) -> None:
        self.result = binding
        QTimer.singleShot(0, self.accept)

    def reject(self) -> None:
        self.result = None
        super().reject()

    def done(self, code: int) -> None:
        self._joy_stop.set()
        for lst in self._listeners:
            try:
                lst.stop()
            except Exception:
                pass
        super().done(code)


class HotkeyMonitor(QObject):
    """Watches one InputBinding; emits triggered(pressed: bool) on edges."""

    triggered = Signal(bool)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._binding: Optional[InputBinding] = None
        self._listeners = []
        self._joy_thread = None
        self._joy_stop = threading.Event()
        self._armed_at = 0.0
        self._label = "(no binding)"
        self._seen = 0            # input events of ANY key/button: is the hook delivering?
        self._matched = 0         # events that matched this binding
        self._first_logged = False

    def start(self, binding: InputBinding) -> bool:
        self.stop()
        self._binding = binding
        if binding is None:
            _log.error("hotkey: no binding to watch; the mic trigger is dead")
            return False
        self._label = binding.describe()
        self._armed_at = time.monotonic()
        self._seen = self._matched = 0
        self._first_logged = False
        if binding.kind in ("keyboard", "mouse"):
            return self._start_pynput(binding)
        return self._start_pygame(binding)

    def stop(self) -> None:
        self._joy_stop.set()
        # The joystick path keeps no listener object, so `self._listeners` alone would make
        # a stick binding the one kind of trigger that never gets a verdict.
        had = bool(self._listeners) or self._joy_thread is not None
        self._joy_thread = None
        for lst in self._listeners:
            try:
                lst.stop()
            except Exception as exc:
                # Teardown: the listener is abandoned either way — but a stop() that throws
                # leaves an OS hook installed, which is worth one line rather than none.
                _log.warning("hotkey: could not stop a listener cleanly: %s: %s",
                             type(exc).__name__, exc)
        self._listeners = []
        self._binding = None
        if had:
            _log.info("hotkey: %s", self.verdict())

    def verdict(self) -> str:
        """One sentence separating "nothing delivered" from "wrong key". Once per stop()."""
        held = time.monotonic() - self._armed_at if self._armed_at else 0.0
        if self._seen == 0:
            return ("released %s after %.0fs having seen NO input events at all - the OS "
                    "hook delivered nothing to this process (a window running as "
                    "administrator with focus blocks it), or nothing was pressed"
                    % (self._label, held))
        if self._matched == 0:
            return ("released %s after %.0fs - the hook IS delivering (%d events seen) but "
                    "NONE matched %s, so the bound key is not the key being pressed"
                    % (self._label, held, self._seen, self._label))
        return ("released %s after %.0fs - %d events seen, %d matched it"
                % (self._label, held, self._seen, self._matched))

    def _count(self) -> None:
        self._seen += 1
        if not self._first_logged:
            self._first_logged = True
            _log.info("hotkey: first input event received %.1fs after arming - the OS hook "
                      "IS delivering to this process",
                      time.monotonic() - self._armed_at)

    def _start_pynput(self, binding: InputBinding) -> bool:
        """Install the keyboard/mouse hook, and say by name if it will not go in.

        ⛔ 2026-09-26: this returned a bare False from `except Exception`, so a listener
          that could not start was reported to arm() as a boolean and to the log as
          nothing — and arm() then printed "ears armed" anyway. "The hook refused" and
          "you never pressed the key" were the same empty log. The handlers also each
          swallowed their own exceptions, so a broken vk comparison would have discarded
          every press in silence.
        ⚠ WHAT A TRUE RETURN STILL DOES NOT PROVE: pynput calls `_mark_ready()` BEFORE
          installing the Windows hook, and `SetWindowsHookEx` failing returns NULL without
          raising (pynput does not check it). So a live listener thread proves the code got
          that far; it does not prove Windows accepted the hook, and it cannot prove
          delivery while a higher-integrity window (Star Citizen run as administrator) has
          focus — UIPI drops those at the OS with no error anywhere. That is what `_seen`
          is for: one real press of ANY key is positive proof of delivery, and zero after
          minutes is positive proof of the opposite. Neither is inferable from `running`.
        """
        try:
            if binding.kind == "keyboard":
                from pynput import keyboard
                vk_want = binding.code

                def _vk(key):
                    vk = getattr(key, "vk", None)
                    if vk is None and hasattr(key, "value"):
                        vk = getattr(key.value, "vk", None)
                    return vk

                def on_press(key):
                    self._count()
                    try:
                        vk = _vk(key)
                        if vk is not None and int(vk) == int(vk_want):
                            self._matched += 1
                            self.triggered.emit(True)
                    except (TypeError, ValueError) as exc:
                        # A non-integer vk on either side. Once per session, not per key,
                        # because this runs on the hook thread for every keystroke the
                        # player makes and a flood here would bury the evidence it is.
                        if not getattr(self, "_vk_fail_logged", False):
                            self._vk_fail_logged = True
                            _log.warning("hotkey: cannot compare key %r against binding %r "
                                         "(%s); this key can never trigger the mic",
                                         key, vk_want, exc)

                def on_release(key):
                    try:
                        vk = _vk(key)
                        if vk is not None and int(vk) == int(vk_want):
                            self.triggered.emit(False)
                    except (TypeError, ValueError):
                        pass       # noqa: BLE001 — on_press already reported this binding
                                   # as uncomparable; a second line per keystroke on the
                                   # release edge would only double the flood.

                lst = keyboard.Listener(on_press=on_press, on_release=on_release)
            else:
                from pynput import mouse
                btn_want = str(binding.code)

                def on_click(_x, _y, button, pressed):
                    if pressed:
                        self._count()
                    if str(button) == btn_want:
                        if pressed:
                            self._matched += 1
                        self.triggered.emit(bool(pressed))

                lst = mouse.Listener(on_click=on_click)
            lst.start()
        except Exception as exc:
            _log.error("hotkey: the %s listener for %s did not start: %s: %s",
                       binding.kind, self._label, type(exc).__name__, exc)
            return False

        self._listeners.append(lst)
        deadline = time.monotonic() + _READY_TIMEOUT_S
        while not lst.running and lst.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        if not lst.is_alive():
            # pynput re-raises an exception from the listener thread in whoever joins it.
            # join() is the diagnosis here, not cleanup. A clean exit with nothing to
            # re-raise is itself a finding, so the non-raising branch speaks too.
            try:
                lst.join(0.2)
                _log.error("hotkey: the %s listener for %s exited immediately and raised "
                           "nothing - it is not watching anything", binding.kind, self._label)
            except Exception as exc:
                _log.error("hotkey: the %s listener for %s died on startup: %s: %s",
                           binding.kind, self._label, type(exc).__name__, exc)
            self._listeners = []
            return False
        if not lst.running:
            _log.error("hotkey: the %s listener for %s was still not running after %.1fs; "
                       "treating it as failed", binding.kind, self._label, _READY_TIMEOUT_S)
            self.stop()
            return False
        _log.info("hotkey: %s listener for %s is live (thread %s). A key event of ANY kind "
                  "will be reported once; if none ever is, nothing is reaching this process.",
                  binding.kind, self._label, lst.name)
        return True

    def _start_pygame(self, binding: InputBinding) -> bool:
        """Poll a joystick/gamepad button.

        ⚠ Returns True as soon as the polling THREAD is running, which is weaker than the
          pynput path above: the device is opened inside the thread, so a stick that is
          not plugged in cannot be reported through the return value. It is reported in
          the log instead, by name, and `_seen` still separates "no buttons at all" from
          "buttons, wrong one".
        """
        try:
            import pygame
        except ImportError as exc:
            _log.error("hotkey: pygame is not installed (%s), so the joystick binding %s "
                       "cannot be watched", exc, self._label)
            return False
        idx = int(binding.joy_index or 0)
        btn_want = int(binding.code)
        stop = threading.Event()
        self._joy_stop = stop

        def poll():
            try:
                pygame.init()
                pygame.joystick.init()
                count = pygame.joystick.get_count()
                if count <= idx:
                    _log.error("hotkey: joystick %d is not connected (%d present), so %s "
                               "can never open the mic", idx, count, self._label)
                    return
                joy = pygame.joystick.Joystick(idx)
                joy.init()
                _log.info("hotkey: polling %r button %d for the mic trigger",
                          joy.get_name(), btn_want)
                while not stop.is_set():
                    try:
                        for ev in pygame.event.get():
                            if ev.type == pygame.JOYBUTTONDOWN and ev.joy == idx:
                                self._count()
                                if int(ev.button) == btn_want:
                                    self._matched += 1
                                    self.triggered.emit(True)
                            elif ev.type == pygame.JOYBUTTONUP and \
                                    ev.joy == idx and int(ev.button) == btn_want:
                                self.triggered.emit(False)
                    except Exception as exc:
                        # Per-poll, so it must not flood: one line, then keep pumping. The
                        # loop deliberately survives — a single malformed event must not
                        # silently end the only trigger the player has.
                        if not getattr(self, "_joy_fail_logged", False):
                            self._joy_fail_logged = True
                            _log.warning("hotkey: joystick event pump error (%s: %s); "
                                         "still polling", type(exc).__name__, exc)
                    stop.wait(0.03)
            except Exception as exc:
                _log.error("hotkey: the joystick poller for %s stopped: %s: %s",
                           self._label, type(exc).__name__, exc)

        self._joy_thread = threading.Thread(target=poll, daemon=True, name="JoyMonitor")
        self._joy_thread.start()
        return True
