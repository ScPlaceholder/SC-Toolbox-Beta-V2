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

import threading
from dataclasses import asdict, dataclass
from typing import Optional

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import QDialog, QLabel, QVBoxLayout

from shared.qt.theme import P

ACCENT = P.energy_cyan


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
        try:
            from pynput import keyboard, mouse
        except ImportError:
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
            except Exception:
                pass

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
            except Exception:
                pass

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
        except ImportError:
            pass

        for lst in self._listeners:
            try:
                lst.start()
            except Exception:
                pass

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

    def start(self, binding: InputBinding) -> bool:
        self.stop()
        self._binding = binding
        if binding is None:
            return False
        if binding.kind in ("keyboard", "mouse"):
            return self._start_pynput(binding)
        return self._start_pygame(binding)

    def stop(self) -> None:
        self._joy_stop.set()
        for lst in self._listeners:
            try:
                lst.stop()
            except Exception:
                pass
        self._listeners = []
        self._binding = None

    def _start_pynput(self, binding: InputBinding) -> bool:
        try:
            if binding.kind == "keyboard":
                from pynput import keyboard
                vk_want = binding.code

                def on_press(key):
                    try:
                        vk = getattr(key, "vk", None)
                        if vk is None and hasattr(key, "value"):
                            vk = getattr(key.value, "vk", None)
                        if vk is not None and int(vk) == int(vk_want):
                            self.triggered.emit(True)
                    except Exception:
                        pass

                def on_release(key):
                    try:
                        vk = getattr(key, "vk", None)
                        if vk is None and hasattr(key, "value"):
                            vk = getattr(key.value, "vk", None)
                        if vk is not None and int(vk) == int(vk_want):
                            self.triggered.emit(False)
                    except Exception:
                        pass

                lst = keyboard.Listener(on_press=on_press, on_release=on_release)
            else:
                from pynput import mouse
                btn_want = str(binding.code)

                def on_click(_x, _y, button, pressed):
                    try:
                        if str(button) == btn_want:
                            self.triggered.emit(bool(pressed))
                    except Exception:
                        pass

                lst = mouse.Listener(on_click=on_click)
            lst.start()
            self._listeners.append(lst)
            return True
        except Exception:
            return False

    def _start_pygame(self, binding: InputBinding) -> bool:
        try:
            import pygame
        except ImportError:
            return False
        idx = int(binding.joy_index or 0)
        btn_want = int(binding.code)
        stop = threading.Event()
        self._joy_stop = stop

        def poll():
            try:
                pygame.init()
                pygame.joystick.init()
                if pygame.joystick.get_count() <= idx:
                    return
                pygame.joystick.Joystick(idx).init()
                while not stop.is_set():
                    try:
                        for ev in pygame.event.get():
                            if ev.type == pygame.JOYBUTTONDOWN and \
                                    ev.joy == idx and int(ev.button) == btn_want:
                                self.triggered.emit(True)
                            elif ev.type == pygame.JOYBUTTONUP and \
                                    ev.joy == idx and int(ev.button) == btn_want:
                                self.triggered.emit(False)
                    except Exception:
                        pass
                    stop.wait(0.03)
            except Exception:
                pass

        self._joy_thread = threading.Thread(target=poll, daemon=True, name="JoyMonitor")
        self._joy_thread.start()
        return True
