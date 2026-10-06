"""Child process: pico_pals_app.main() with a window-state reporter bolted on.

Run by test_pico_pals_tile.py in place of pico_pals_app.py, with the SAME argument list, never
imported.  It changes nothing about how Pico starts; it only wraps sprite_pal.main so that, once
the window exists, a timer writes what the window is doing to the file named by the environment
variable PICO_PROBE_STATE:

    {"visible": true, "modal": true, "pal_args": [...], "ticks": 12,
     "x": 260, "y": 158, "w": 280, "h": 483, "screen": [0, 0, 800, 800], "dialogs": [[x, y, w, h]]}

`modal` is true while a modal dialog is open (the Customise box, which opens on every start).

x / y / w / h are the window's geometry, `screen` is the primary screen's available area and `dialogs`
is the geometry of every box of his that is open; test_pico_reset_position.py reads them.

PICO_PROBE_MOVE="x,y", if set, stands in for the user's hand: once the Customise box is open, Pico is
moved there and the spot is saved exactly as a drag-and-release saves it (Pal.remember), and the open
boxes are moved there too. `moved` in the state file turns true when that has happened.

`pal_args` is what the adapter handed to sprite_pal.py after translating the launcher's arguments.
The test reads that file to see show / hide take effect; production code has no such hook.
"""
from __future__ import annotations

import json
import os
import sys

TOOL = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, TOOL)

import pico_pals_app  # noqa: E402
import sprite_pal  # noqa: E402

STATE = os.environ["PICO_PROBE_STATE"]
MOVE = [int(n) for n in os.environ["PICO_PROBE_MOVE"].split(",")] if os.environ.get("PICO_PROBE_MOVE") else None
_real_main = sprite_pal.main


def _spy_main(argv=None, on_ready=None):
    seen = {"ticks": 0, "moved": False}

    def ready(app, pal):
        if on_ready is not None:
            on_ready(app, pal)
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication, QDialog

        def boxes():
            return [w for w in QApplication.topLevelWidgets() if isinstance(w, QDialog) and w.isVisible()]

        def report():
            seen["ticks"] += 1
            if MOVE and not seen["moved"] and QApplication.activeModalWidget() is not None:
                pal.move(*MOVE)
                pal.remember()
                for box in boxes():
                    box.move(*MOVE)
                seen["moved"] = True
            area = app.primaryScreen().availableGeometry()
            tmp = STATE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"visible": pal.isVisible(),
                           "modal": QApplication.activeModalWidget() is not None, "pal_args": [str(a) for a in (argv or [])],
                           "ticks": seen["ticks"], "moved": seen["moved"],
                           "x": pal.x(), "y": pal.y(), "w": pal.width(), "h": pal.height(),
                           "screen": [area.x(), area.y(), area.width(), area.height()],
                           "dialogs": [[g.x(), g.y(), g.width(), g.height()]
                                       for g in (b.frameGeometry() for b in boxes())]}, fh)
            os.replace(tmp, STATE)

        timer = QTimer(pal)
        timer.timeout.connect(report)
        timer.start(40)
        pal._probe_timer = timer

    return _real_main(argv, on_ready=ready)


sprite_pal.main = _spy_main

if __name__ == "__main__":
    raise SystemExit(pico_pals_app.main())
