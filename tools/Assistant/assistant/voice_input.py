"""Trigger bindings for the ears — keyboard + mouse via pynput.

A trimmed counterpart of Starmap's voice/input_devices.py: keyboard keys
and mouse buttons only (no joystick/gamepad — the assistant is a slim
always-on-top panel, and gamepad users can bind a keyboard key).
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import QObject, Signal


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
    """Watches a binding and re-emits pressed/released as Qt signals."""

    triggered = Signal(bool)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._binding: Optional[InputBinding] = None
        self._listeners = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self, binding: InputBinding) -> bool:
        self.stop()
        self._binding = binding
        self._stop.clear()
        try:
            from pynput import keyboard, mouse
        except ImportError:
            return False
        self._listeners = []

        def _on_error(*_a, **_k):
            return False

        if binding.kind == "key":
            lis = keyboard.Listener(on_press=self._press, on_release=self._release,
                                    on_error=_on_error)
        else:
            lis = mouse.Listener(on_click=self._click, on_error=_on_error)
        self._listeners.append(lis)
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="EarsHotkey")
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        for lis in self._listeners:
            try:
                lis.stop()
            except Exception:
                pass
        self._listeners = []
        self._thread = None

    def _run(self) -> None:
        for lis in self._listeners:
            try:
                lis.start()
                lis.wait()
            except Exception:
                pass

    # ── handlers (pynput threads) ────────────────────────────────────────
    def _matches_key(self, key) -> bool:
        b = self._binding
        if b is None or b.kind != "key":
            return False
        name = getattr(key, "name", None) or str(getattr(key, "char", ""))
        return name == b.code or str(key) == b.code

    def _press(self, key) -> None:
        if self._matches_key(key):
            self.triggered.emit(True)

    def _release(self, key) -> None:
        if self._matches_key(key):
            self.triggered.emit(False)

    def _click(self, x, y, button, pressed) -> None:
        b = self._binding
        if b is None or b.kind != "mouse":
            return
        if str(button) == b.code:
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
        self.cancel()
        try:
            from pynput import keyboard, mouse
        except ImportError:
            return False

        def _on_error(*_a, **_k):
            return False

        kl = keyboard.Listener(on_press=self._got_key, on_error=_on_error)
        ml = mouse.Listener(on_click=self._got_click, on_error=_on_error)
        self._listeners = [kl, ml]
        for lis in self._listeners:
            try:
                lis.start()
            except Exception:
                pass
        return True

    def cancel(self) -> None:
        for lis in self._listeners:
            try:
                lis.stop()
            except Exception:
                pass
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
