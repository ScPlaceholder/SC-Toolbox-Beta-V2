"""Child process: hud_app.main() with a window-state reporter bolted on.

Run by test_reset_position.py in place of hud_app.py, with the SAME argument list the launcher
passes, never imported.  It changes nothing about how the HUD starts; it only wraps
BattleBuddyApp.start so that a timer writes where the windows are to the file named by the
environment variable HUD_PROBE_STATE:

    {"visible": true, "x": 250, "y": 380, "w": 300, "h": 40, "screen": [0, 0, 800, 800],
     "toggle": [x, y, w, h], "options": [x, y, w, h] or null, "moved": false, "ticks": 12}

`screen` is the primary screen's available area, `toggle` the click-through checkbox (a window of
its own that follows the HUD) and `options` the Options window while it is open.

HUD_PROBE_MOVE="x,y", if set, stands in for the user's hand: once the HUD is up it is moved there and
the spot is saved exactly as a drag-and-release saves it (HudWindow._save_position, debounced); the
Options window is opened and moved there too.  `moved` turns true once the save has reached the disk.
"""
from __future__ import annotations

import json
import os
import sys

TOOL = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, TOOL)

import hud_app  # noqa: E402
from ui.options_popup import OptionsPopup  # noqa: E402

STATE = os.environ["HUD_PROBE_STATE"]
MOVE = [int(n) for n in os.environ["HUD_PROBE_MOVE"].split(",")] if os.environ.get("HUD_PROBE_MOVE") else None
_real_start = hud_app.BattleBuddyApp.start


def _rect(widget):
    g = widget.frameGeometry()
    return [g.x(), g.y(), g.width(), g.height()]


def _spy_start(self):
    _real_start(self)
    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QGuiApplication

    hud = self._hud
    seen = {"ticks": 0, "asked": False}

    def report():
        seen["ticks"] += 1
        if MOVE and not seen["asked"] and seen["ticks"] > 3:
            hud.move(*MOVE)
            hud._save_position()
            self._show_options()
            OptionsPopup._instance.move(*MOVE)
            seen["asked"] = True
        options = OptionsPopup._instance
        area = QGuiApplication.primaryScreen().availableGeometry()
        tmp = STATE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"visible": hud.isVisible(), "x": hud.x(), "y": hud.y(),
                       "w": hud.width(), "h": hud.height(),
                       "screen": [area.x(), area.y(), area.width(), area.height()],
                       "toggle": _rect(hud._toggle_window),
                       "options": _rect(options) if options is not None and options.isVisible() else None,
                       "moved": seen["asked"] and hud._pending_save is None,
                       "ticks": seen["ticks"]}, fh)
        os.replace(tmp, STATE)

    timer = QTimer(hud)
    timer.timeout.connect(report)
    timer.start(40)
    hud._probe_timer = timer


hud_app.BattleBuddyApp.start = _spy_start

if __name__ == "__main__":
    hud_app.main()
