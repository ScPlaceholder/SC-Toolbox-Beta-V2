"""In-game route setter - the set_route_ai automation, repurposed.

Drives the Star Citizen in-game starmap exactly like Elah's WingmanAI
set_route skill did, but on ``pynput`` (the ears' existing optional
dependency) instead of Wingman's internal mouse/keyboard services:

  F2 (open map) -> click map -> zoom out -> click search bar ->
  paste destination (Win32 clipboard, same as the original) ->
  click the result -> click map -> R x6 (set route) -> F2 (close)

Coordinates come from the same ``mouse_calibration.json`` the Wingman
skill uses (version 1 schema: starmap.search_bar / destination /
map_center); safe defaults apply when the file is missing.

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
from PySide6.QtWidgets import QDialog, QLabel, QVBoxLayout

from shared.qt.theme import P

_log = logging.getLogger(__name__)

_TOOLBOX_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
#  route_setter.py -> voice -> starmap -> Starmap -> skills -> SC_Toolbox_Beta_V1.2

SUPPORTED_CALIBRATION_VERSION = 1

# Timing constants from the original implementation.
_HOLD = 0.15
_PAUSE = 1.0

# Safe defaults (original's out-of-box values).
_DEFAULTS = {
    "search_bar": (1500, 200),
    "destination": (365, 335),
    "map_center": (1769, 814),
}


def calibration_path() -> str:
    """Same file the Wingman skill writes, so calibration is shared."""
    live = os.path.join(_TOOLBOX_ROOT, "tools", "set_route_ai", "data",
                        "mouse_calibration.json")
    if os.path.isfile(live):
        return live
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", "data", "set_route", "mouse_calibration.json")


def load_calibration() -> dict:
    """Return {search_bar: (x, y), destination: (x, y), map_center: (x, y)}."""
    out = dict(_DEFAULTS)
    try:
        with open(calibration_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if int(data.get("version", -1)) != SUPPORTED_CALIBRATION_VERSION:
            return out
        starmap = data.get("starmap") or {}
        for key in out:
            node = starmap.get(key) or {}
            x, y = int(node.get("x", out[key][0])), int(node.get("y", out[key][1]))
            out[key] = (x, y)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return out


def save_calibration(search_bar, destination, map_center) -> None:
    data = {
        "version": SUPPORTED_CALIBRATION_VERSION,
        "starmap": {
            "search_bar": {"x": int(search_bar[0]), "y": int(search_bar[1])},
            "destination": {"x": int(destination[0]), "y": int(destination[1])},
            "map_center": {"x": int(map_center[0]), "y": int(map_center[1])},
        },
    }
    path = calibration_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def _set_clipboard(text: str) -> None:
    """Win32 clipboard set (CF_UNICODETEXT), same approach as the original.

    ⛔ THIS FUNCTION RAISED "GlobalLock failed" ON EVERY CALL AND THE PORT DROPPED THE
       EXACT LINES THE ORIGINAL LABELS CRITICAL. Found 2026-09-27 by J's dry run: point
       the macro at a blank Notepad and watch what it types. It typed nothing — the
       destination never reached the clipboard, so no route was ever plotted, in Notepad
       or in game.

       `tools/set_route_ai/main.py` carries the comment
       ``# --- CRITICAL: Declare proper 64-bit return types ---`` above eight restype /
       argtypes declarations. The port kept the call sequence and the comment's *shape*
       and dropped the declarations. Without them ctypes assumes a C ``int`` return, so
       on 64-bit Windows GlobalAlloc's HANDLE is truncated to 32 bits and sign-extended.
       Measured on this machine:

           default restype   -> -604110840        (truncated, sign-extended)
           c_void_p restype  -> 2000850649112     (the real handle)
           GlobalLock(truncated) -> 0             ->  "GlobalLock failed"

       ★ The port did not lose the KNOWLEDGE — the original's comment survived into the
         new file as the docstring's "same approach as the original". It lost the four
         lines the comment was ABOUT. A copied reassurance is not a copied safeguard.

    ⚠ SECOND, INDEPENDENT DEFECT in the same body, which the first one masked: it encoded
      ``text`` without a terminator, set ``size = len(text_bytes) + 2``, then memmove'd
      ``size`` bytes out of a buffer holding ``size - 2``. That over-reads the Python
      bytes object by two bytes and never deliberately writes the NUL, so CF_UNICODETEXT
      would be handed an unterminated string plus whatever followed in memory. Fixed the
      way the original does it: append "\\0" BEFORE encoding and size from the result.
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

        ctl = load_calibration()
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
            # (J, 2026-09-25; an audit had flagged it as spam.)
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
    """3-step click capture for the in-game route setter.

    Mirrors the Wingman skill's calibration:
      1. open the in-game map (F2), zoom out, LEFT-CLICK the search bar
      2. search a destination and LEFT-CLICK its result
      3. LEFT-CLICK the centre of the map

    Clicks are captured with a global pynput mouse listener, so the user
    interacts with the GAME while this dialog just watches. Esc cancels.
    """


    stepCaptured = Signal()
    clickSeen = Signal(int, int)     # step 1: a click was seen (not yet confirmed)
    enterPressed = Signal()          # step 1: Enter confirms the most recent click

    # Step 1 waits for ENTER (J, 2026-09-26). The first click in the game is often only the click that
    # focuses the game window, so taking it as the search-bar position calibrated the wrong spot. Now any
    # number of clicks are allowed in step 1; the most recent one is kept, and Enter confirms it.
    _STEPS = (
        "Step 1 of 3: in the game, press F2 to open the starmap and\n"
        "zoom out with the mouse wheel. Click into the game if you need to,\n"
        "then LEFT-CLICK the search bar and press ENTER to confirm.",
        "Step 2 of 3: in the search bar type a destination in your\n"
        "current system, then LEFT-CLICK its result.",
        "Step 3 of 3: LEFT-CLICK the centre of the map.",
    )

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.result_ready = False
        self._points = []
        self._listener = None

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
        else:
            self.stepCaptured.connect(self._on_step_captured)
            self.clickSeen.connect(self._on_click_seen)
            self.enterPressed.connect(self._on_enter)
            self._pending = None          # step 1's most recent click, confirmed by Enter
            self._kb_listener = None
            self._show_step()
            self._start_listener()

    def _show_step(self) -> None:
        i = len(self._points)
        if i < len(self._STEPS):
            self._label.setText(self._STEPS[i])

    def _start_listener(self) -> None:
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
            return
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
        super().reject()

    def accept(self) -> None:
        self._stop_listener()
        super().accept()
