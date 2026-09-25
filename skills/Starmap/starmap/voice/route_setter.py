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
import os
import threading
import time
from typing import Callable, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QDialog, QLabel, QVBoxLayout

from shared.qt.theme import P

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
    """Win32 clipboard set (CF_UNICODETEXT), same approach as the original."""
    import ctypes

    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    text_bytes = text.encode("utf-16-le")
    size = len(text_bytes) + 2

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

    _STEPS = (
        "Step 1 of 3: in the game, press F2 to open the starmap,\n"
        "zoom out with the mouse wheel, then LEFT-CLICK the search bar.",
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
            self._points.append((int(x), int(y)))
            self.stepCaptured.emit()
            return len(self._points) < 3

        try:
            self._listener = mouse.Listener(on_click=on_click)
            self._listener.start()
        except Exception:
            self._listener = None
            self._label.setText("Could not start the global click listener.")

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
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:
                pass
            self._listener = None

    def reject(self) -> None:
        self._stop_listener()
        super().reject()

    def accept(self) -> None:
        self._stop_listener()
        super().accept()
