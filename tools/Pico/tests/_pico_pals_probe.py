"""Child process: pico_pals_app.main() with a window-state reporter bolted on.

Run by test_pico_pals_tile.py in place of pico_pals_app.py, with the SAME argument list, never
imported.  It changes nothing about how Pico starts; it only wraps sprite_pal.main so that, once
the window exists, a timer writes what the window is doing to the file named by the environment
variable PICO_PROBE_STATE:

    {"visible": true, "modal": true, "pal_args": [...], "ticks": 12}

`modal` is true while a modal dialog is open (the Customise box, which opens on every start).

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
_real_main = sprite_pal.main


def _spy_main(argv=None, on_ready=None):
    seen = {"ticks": 0}

    def ready(app, pal):
        if on_ready is not None:
            on_ready(app, pal)
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication

        def report():
            seen["ticks"] += 1
            tmp = STATE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"visible": pal.isVisible(),
                           "modal": QApplication.activeModalWidget() is not None, "pal_args": [str(a) for a in (argv or [])],
                           "ticks": seen["ticks"]}, fh)
            os.replace(tmp, STATE)

        timer = QTimer(pal)
        timer.timeout.connect(report)
        timer.start(40)
        pal._probe_timer = timer

    return _real_main(argv, on_ready=ready)


sprite_pal.main = _spy_main

if __name__ == "__main__":
    raise SystemExit(pico_pals_app.main())
