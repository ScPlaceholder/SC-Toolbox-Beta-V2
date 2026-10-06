"""In-game route setter - the set_route_ai automation, repurposed.

Drives the Star Citizen in-game starmap exactly like the WingmanAI
set_route skill did, but on ``pynput`` (the ears' existing optional
dependency) instead of Wingman's internal mouse/keyboard services:

  F2 (open map) -> click map -> zoom out -> click search bar ->
  paste destination (Win32 clipboard, same as the original) ->
  click the result -> click map -> R x6 (set route) -> F2 (close)

Coordinates come from the pilot's own calibration (``mouse_calibration.json``,
version 1 schema: starmap.search_bar / destination / map_center; see
calibration_path). There are no default positions: until the pilot has
calibrated, nothing is clicked.

Everything runs on a daemon thread with plain sleeps, mirroring the
original timing. Status strings are reported through a callback so the
panel can show progress in the ears status line.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Callable, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QDialog, QLabel, QPushButton, QVBoxLayout

from shared.qt.theme import P

_log = logging.getLogger(__name__)

_TOOLBOX_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
#  route_setter.py -> voice -> starmap -> Starmap -> skills -> SC_Toolbox_Beta_V1.2

SUPPORTED_CALIBRATION_VERSION = 1

# Timing constants from the original implementation.
_HOLD = 0.15
_PAUSE = 1.0

# The three positions a calibration holds. There are NO default values for them: where the game's star map
# puts its search bar depends on the pilot's screen and resolution, and a click at coordinates measured on
# another screen lands on whatever happens to be there.
_POSITIONS = ("search_bar", "destination", "map_center")

#: What the macro reports instead of running when there is no calibration. RouteService says the same thing
#: before it ever starts the macro (service.NOT_CALIBRATED); this is the macro's own refusal.
NOT_CALIBRATED = "the route setter is not calibrated: press Calibrate Route first"


def user_calibration_path() -> str:
    """Where a calibration is saved: ~/.sctoolbox/set_route/, beside the In-Game switch (gate.py).

    Not beside the code. An installed toolbox's folder is replaced whole by every update, so a file kept
    there is lost each time, and a file shipped there is somebody else's screen."""
    return os.path.join(os.path.expanduser("~"), ".sctoolbox", "set_route", "mouse_calibration.json")


def shared_calibration_path() -> str:
    """The WingmanAI set-route skill's own file, when that skill's folder sits beside the toolbox.

    Only ever READ here, and only until the pilot calibrates from this window: a calibration made with that
    skill keeps working. An installed toolbox has no such folder."""
    return os.path.join(_TOOLBOX_ROOT, "tools", "set_route_ai", "data", "mouse_calibration.json")


def calibration_path() -> str:
    """The file the positions are read from: the pilot's own once it exists, else the WingmanAI skill's;
    with neither, the pilot's own, which is where the first calibration will be written."""
    own = user_calibration_path()
    if os.path.isfile(own):
        return own
    shared = shared_calibration_path()
    return shared if os.path.isfile(shared) else own


def load_calibration() -> Optional[dict]:
    """{search_bar: (x, y), destination: (x, y), map_center: (x, y)}, or None when there is no usable
    calibration: no file, a file of another version, or any of the three positions missing or not a number.

    None means nothing may be clicked. InGameRouteSetter refuses to run, and RouteService says why."""
    try:
        with open(calibration_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if int(data.get("version", -1)) != SUPPORTED_CALIBRATION_VERSION:
            return None
        starmap = data["starmap"]
        return {key: (int(starmap[key]["x"]), int(starmap[key]["y"])) for key in _POSITIONS}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


def save_calibration(search_bar, destination, map_center) -> None:
    """Write the pilot's calibration to their own folder (user_calibration_path), never beside the code."""
    data = {
        "version": SUPPORTED_CALIBRATION_VERSION,
        "starmap": {
            "search_bar": {"x": int(search_bar[0]), "y": int(search_bar[1])},
            "destination": {"x": int(destination[0]), "y": int(destination[1])},
            "map_center": {"x": int(map_center[0]), "y": int(map_center[1])},
        },
    }
    path = user_calibration_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def _set_clipboard(text: str) -> None:
    """Win32 clipboard set (CF_UNICODETEXT), same approach as the original.

    The restype / argtypes declarations below are REQUIRED on 64-bit Windows. Without them ctypes
    assumes a C ``int`` return, so GlobalAlloc's HANDLE is truncated to 32 bits and sign-extended,
    GlobalLock is handed a bad handle and returns 0, and this function raises "GlobalLock failed" on
    every call: the destination never reaches the clipboard and no route is plotted. For example:

        default restype   -> -604110840        (truncated, sign-extended)
        c_void_p restype  -> 2000850649112     (the real handle)
        GlobalLock(truncated) -> 0             ->  "GlobalLock failed"

    The text is encoded WITH its terminator ("\\0" appended before encoding) and the size is taken
    from the result. Sizing from the unterminated bytes plus two copies two bytes from past the end of
    the buffer and hands CF_UNICODETEXT an unterminated string.
    """
    import ctypes

    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    # CRITICAL on 64-bit: handles are pointer-width. See the docstring.
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.restype = ctypes.c_void_p
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]

    text_bytes = (text + "\0").encode("utf-16-le")
    size = len(text_bytes)

    h_mem = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
    if not h_mem:
        raise RuntimeError("GlobalAlloc failed")
    p_mem = kernel32.GlobalLock(h_mem)
    if not p_mem:
        kernel32.GlobalFree(h_mem)
        raise RuntimeError("GlobalLock failed")
    ctypes.memmove(p_mem, text_bytes, size)
    kernel32.GlobalUnlock(h_mem)
    if not user32.OpenClipboard(None):
        kernel32.GlobalFree(h_mem)
        raise RuntimeError("OpenClipboard failed")
    user32.EmptyClipboard()
    user32.SetClipboardData(CF_UNICODETEXT, h_mem)
    user32.CloseClipboard()


class InGameRouteSetter:
    """Runs the in-game route sequence on a daemon thread.

    ``status_cb`` receives human-readable progress strings; ``done_cb``
    receives the final message (or an error string prefixed with "error:").
    """

    def __init__(self) -> None:
        self._busy = False
        self._lock = threading.Lock()

    def busy(self) -> bool:
        return self._busy

    def available(self) -> bool:
        try:
            import pynput  # noqa: F401
            return True
        except ImportError:
            return False

    def calibrated(self) -> bool:
        """True when the pilot's three click positions are known (load_calibration)."""
        return load_calibration() is not None

    def set_route(self, destination: str,
                  status_cb: Optional[Callable[[str], None]] = None,
                  done_cb: Optional[Callable[[str], None]] = None) -> bool:
        """Kick off the sequence. Returns False if already busy / no pynput."""
        with self._lock:
            if self._busy:
                return False
            self._busy = True

        def report(msg: str) -> None:
            if status_cb is not None:
                try:
                    status_cb(msg)
                except Exception:
                    pass

        def finish(msg: str) -> None:
            with self._lock:
                self._busy = False
            if done_cb is not None:
                try:
                    done_cb(msg)
                except Exception:
                    pass

        threading.Thread(
            target=self._run,
            args=(destination, report, finish),
            daemon=True, name="InGameRouteSet",
        ).start()
        return True

    # ── the sequence (timings mirror the original) ────────────────────────
    def _run(self, destination: str, report: Callable[[str], None],
             finish: Callable[[str], None]) -> None:
        try:
            from pynput import keyboard, mouse
        except ImportError:
            finish("error: route setter needs pynput (pip install pynput)")
            return

        # Checked here as well as in RouteService.why_not: this is the function that moves the mouse, and it
        # must not depend on every caller having asked first.
        ctl = load_calibration()
        if ctl is None:
            finish("error: " + NOT_CALIBRATED)
            return
        search_x, search_y = ctl["search_bar"]
        dest_x, dest_y = ctl["destination"]
        map_x, map_y = ctl["map_center"]

        kb = keyboard.Controller()
        ms = mouse.Controller()

        def click(x: int, y: int) -> None:
            ms.position = (x, y)
            ms.click(mouse.Button.left, 1)

        try:
            report("opening the in-game map (F2)...")
            kb.press(keyboard.Key.f2)
            time.sleep(_HOLD)
            kb.release(keyboard.Key.f2)
            time.sleep(3.5)

            report("zooming the map out...")
            click(map_x, map_y)
            time.sleep(1.3)
            ms.scroll(0, -60)           # wheel out, as the original
            time.sleep(2.1)

            report("searching for '%s'..." % destination)
            click(search_x, search_y)
            time.sleep(1.5)

            _set_clipboard(destination)
            time.sleep(0.3)
            with kb.pressed(keyboard.Key.ctrl):
                kb.press("v")
                kb.release("v")
            time.sleep(1.0)

            report("clicking the destination result...")
            click(dest_x, dest_y)
            time.sleep(0.5)

            click(map_x, map_y)
            time.sleep(1.3)

            report("pressing Set Route...")
            # Six presses ON PURPOSE, not a bug: when SC lags it drops single R
            # presses, so this retries until one lands.
            for _ in range(6):
                kb.press("r")
                kb.release("r")
            time.sleep(_PAUSE)

            kb.press(keyboard.Key.f2)
            time.sleep(_HOLD)
            kb.release(keyboard.Key.f2)

            finish("route to %s plotted in game" % destination)
        except Exception as exc:
            finish("error: route setter: %s" % exc)


class RouteCalibrationDialog(QDialog):
    """3-step click capture for the in-game route setter, started by voice.

    It opens on a step that only says how to begin; the spoken prompts start
    when the pilot says the phrase, or presses Begin.

      0. "Click on the game and say 'calibrate star map' to begin."
         NOTHING is watched here: the mouse and keyboard listeners do not
         exist yet, so the click that gives the game the focus is not a
         calibration click and cannot become one. begin() ends this step:
         the Assistant calls it when it hears the phrase (panel.py), and the
         Begin button calls it for a pilot with no microphone.
      1. open the in-game map (F2), zoom out, LEFT-CLICK the search bar,
         ENTER to confirm
      2. search a destination and LEFT-CLICK its result
      3. LEFT-CLICK the centre of the map

    Each of 1-3 is SPOKEN as it begins, through *speak* (the Assistant's own
    speaking path, which also warns its ears so the line is not heard back as
    a command), as well as shown: the pilot is looking at the game. Then
    "Calibration saved." or "Calibration cancelled."

    Clicks are captured with a global pynput mouse listener, so the user
    interacts with the GAME while this dialog just watches. Esc cancels.

    *begin* = True starts at step 1 at once (the pilot has already said the
    words; nothing asked for this dialog by hand), and the dialog then opens
    without taking the keyboard focus from the game.
    """


    stepCaptured = Signal()
    clickSeen = Signal(int, int)     # step 1: a click was seen (not yet confirmed)
    enterPressed = Signal()          # step 1: Enter confirms the most recent click

    # Step 1 waits for ENTER. The first click in the game is often only the click that
    # focuses the game window, so taking it as the search-bar position calibrated the wrong spot. Now any
    # number of clicks are allowed in step 1; the most recent one is kept, and Enter confirms it.
    STEP_0 = "Click on the game and say \"calibrate star map\" to begin."

    # Heard while he is looking at the game: short, one action after another, and none of them contains the
    # phrase that starts the calibration.
    SPOKEN = (
        "Step one. Press F2 for the star map and zoom out. Left click the search bar, then press Enter.",
        "Step two. Type a destination in your current system, then left click its result.",
        "Step three. Left click the centre of the map.",
    )
    SAID_SAVED = "Calibration saved."
    SAID_NOT_SAVED = "The calibration could not be saved."
    SAID_CANCELLED = "Calibration cancelled."

    _STEPS = (
        "Step 1 of 3: in the game, press F2 to open the starmap and\n"
        "zoom out with the mouse wheel. Click into the game if you need to,\n"
        "then LEFT-CLICK the search bar and press ENTER to confirm.",
        "Step 2 of 3: in the search bar type a destination in your\n"
        "current system, then LEFT-CLICK its result.",
        "Step 3 of 3: LEFT-CLICK the centre of the map.",
    )

    def __init__(self, parent=None, speak: Optional[Callable[[str], None]] = None, mic_hint: str = "",
                 begin: bool = False) -> None:
        super().__init__(parent)
        self.result_ready = False
        self._points = []
        self._listener = None
        self._kb_listener = None
        self._pending = None          # step 1's most recent click, confirmed by Enter
        self._speak_fn = speak
        self._begun = False           # False = step 0: nothing is watched
        self._closing_said = False
        self._spoken_steps = set()
        if begin:
            # Opened because he SAID it, with the game in front: do not take the keyboard from the game.
            self.setAttribute(Qt.WA_ShowWithoutActivating, True)

        self.setWindowTitle("Calibrate route setter")
        self.setModal(True)
        self.setMinimumWidth(460)


        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(8)
        self._label = QLabel("")
        self._label.setWordWrap(True)
        self._label.setStyleSheet(
            f"color: {P.fg}; font-family: Consolas; font-size: 10pt;")
        lay.addWidget(self._label)
        # step 0 only: how the microphone is set right now, and the way in without one
        self._mic = QLabel(mic_hint or "")
        self._mic.setWordWrap(True)
        self._mic.setStyleSheet(f"color: {P.yellow}; font-size: 9pt;")
        self._mic.setVisible(bool(mic_hint))
        lay.addWidget(self._mic)
        self._btn_begin = QPushButton("Begin")
        self._btn_begin.setToolTip("Start without saying it (no microphone, or the mic is off).")
        self._btn_begin.clicked.connect(lambda _c=False: self.begin())
        lay.addWidget(self._btn_begin)
        hint = QLabel("Esc cancels. Clicks are captured from anywhere on screen.")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {P.fg_dim}; font-size: 8pt;")
        lay.addWidget(hint)

        try:
            from pynput import mouse  # noqa: F401
            self._mouse_mod = mouse
            ok = True
        except ImportError:
            self._mouse_mod = None
            ok = False
        if not ok:
            self._label.setText("Calibration needs pynput (pip install pynput).")
            self._btn_begin.setEnabled(False)
        else:
            self.stepCaptured.connect(self._on_step_captured)
            self.clickSeen.connect(self._on_click_seen)
            self.enterPressed.connect(self._on_enter)
            self._label.setText(self.STEP_0)
            if begin:
                self.begin()

    def begun(self) -> bool:
        """False while step 0 is showing (nothing is being watched yet)."""
        return self._begun

    def begin(self) -> bool:
        """Leave step 0: start watching the mouse and keyboard, show and speak step 1.

        Called when the phrase is heard and by the Begin button. Once: saying it again mid-calibration (or the
        speakers being heard) does nothing. False when it did not start anything."""
        if self._begun or self._mouse_mod is None:
            return False
        self._begun = True
        self._btn_begin.setVisible(False)
        self._mic.setVisible(False)
        if self._start_listener():
            self._show_step()
        return True

    def _say(self, text: str) -> None:
        if self._speak_fn is None:
            return
        try:
            self._speak_fn(text)
        except Exception as exc:       # noqa: BLE001 - a prompt that cannot be spoken is still on screen
            _log.warning("calibration: could not speak %r (%s: %s)", text, type(exc).__name__, exc)

    def _show_step(self) -> None:
        i = len(self._points)
        if i < len(self._STEPS):
            self._label.setText(self._STEPS[i])
            if i not in self._spoken_steps:     # each step's line once, however often its text is redrawn
                self._spoken_steps.add(i)
                self._say(self.SPOKEN[i])

    def _start_listener(self) -> bool:
        mouse = self._mouse_mod

        def on_click(x, y, button, pressed):
            if not pressed:
                return True
            try:
                if button != mouse.Button.left:
                    return True
            except Exception:
                pass
            if not self._points:
                # step 1: remember the most recent click; ENTER confirms it
                self._pending = (int(x), int(y))
                self.clickSeen.emit(int(x), int(y))
                return True
            self._points.append((int(x), int(y)))
            self.stepCaptured.emit()
            return len(self._points) < 3

        try:
            self._listener = mouse.Listener(on_click=on_click)
            self._listener.start()
        except Exception:
            self._listener = None
            self._label.setText("Could not start the global click listener.")
            return False
        # ENTER is pressed in the GAME, which has focus, so it needs a global keyboard listener too.
        try:
            from pynput import keyboard

            def on_press(key):
                if key in (keyboard.Key.enter,) or getattr(key, "vk", None) == 13:
                    self.enterPressed.emit()
                return True

            self._kb_listener = keyboard.Listener(on_press=on_press)
            self._kb_listener.start()
        except Exception:
            self._kb_listener = None
        return True

    def _on_click_seen(self, x: int, y: int) -> None:
        if not self._points:
            self._label.setText(self._STEPS[0] + f"\n\nLast click: ({x}, {y}). Press ENTER if that was the "
                                "search bar, or click it again.")

    def _on_enter(self) -> None:
        """Step 1 only: keep the most recent click as the search-bar position."""
        if self._points or self._pending is None:
            return
        self._points.append(self._pending)
        self._pending = None
        self.stepCaptured.emit()

    def _on_step_captured(self) -> None:
        if len(self._points) >= 3:
            self._finish()
        else:
            self._show_step()

    def _finish(self) -> None:
        search_bar, destination, map_center = self._points[:3]
        try:
            save_calibration(search_bar, destination, map_center)
            self.result_ready = True
        except Exception:
            self.result_ready = False
        self._closing_said = True
        self._say(self.SAID_SAVED if self.result_ready else self.SAID_NOT_SAVED)
        self.accept()

    def keyPressEvent(self, ev) -> None:
        if ev.key() == Qt.Key_Escape:
            self.reject()
            return
        super().keyPressEvent(ev)

    def _stop_listener(self) -> None:
        # pynput's Listener.stop() only posts WM_STOP to its own message loop, so on Windows the reachable
        # failures are AttributeError (the loop was never created) and OSError from the ctypes post. A global
        # input hook that will not let go is worth a line in the log: the next calibration run cannot start a
        # second listener, and the dialog is closing either way.
        if self._listener is not None:
            try:
                self._listener.stop()
            except (AttributeError, OSError, RuntimeError) as exc:
                _log.warning("calibration: the global click listener would not stop (%s)", exc)
            self._listener = None
        if getattr(self, "_kb_listener", None) is not None:
            try:
                self._kb_listener.stop()
            except (AttributeError, OSError, RuntimeError) as exc:
                _log.warning("calibration: the global keyboard listener would not stop (%s)", exc)
            self._kb_listener = None

    def reject(self) -> None:
        self._stop_listener()
        # Said only for a calibration that had begun: closing step 0 has nothing to cancel out loud.
        if self._begun and not self._closing_said:
            self._closing_said = True
            self._say(self.SAID_CANCELLED)
        super().reject()

    def accept(self) -> None:
        self._stop_listener()
        super().accept()
