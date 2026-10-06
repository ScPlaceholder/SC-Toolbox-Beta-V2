"""Child process that builds the REAL launcher window and prints where its title bar put everything.

Run by test_launcher_title.py, never imported.  Built like _launcher_grid_probe.py and safe for the
same reasons: only the window (ui.main_window.LauncherWindow) is built, so no tool process is
started and no global hotkey is bound; no launcher settings file is read or written; the PlayTime
background scan is switched off; it leaves with os._exit so no geometry file is saved.

A separate process because the UI scale is QT_SCALE_FACTOR, which Qt reads once when the
QApplication is built: the caller sets it in the environment, exactly as skill_launcher.py does.

Usage:  python _launcher_title_probe.py [--w N] [--h N] [--hotkey <binding>] [--rebind <binding>]
                                        [--shot <png>]

    --w / --h   the launcher's size in device-independent pixels (it is started with 500 x 550)
    --hotkey    the launcher hotkey the window is built with
    --rebind    LauncherWindow.update_hotkey_badges(...) AFTER it is built: a key changed while it is up
    --shot      render the title bar alone to this PNG

Prints one JSON object:
    version   shared.update_checker's current version, the one the title has to show
    dpr       the device pixel ratio the window got (the UI scale, as Qt applied it)
    window    [w, h]
    bar       [w, h] of the title bar
    items     every widget in the title bar, left to right:
              {"cls", "text", "x", "w", "need", "visible", "pt", "spacing"}; "need" is the widget's
              own sizeHint width, "pt" and "spacing" the font it is drawn in
"""
from __future__ import annotations

import argparse
import json
import os
import sys

REPO = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
sys.path.insert(0, REPO)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--w", type=int, default=500)
    ap.add_argument("--h", type=int, default=550)
    ap.add_argument("--hotkey", default="<ctrl>+0")
    ap.add_argument("--rebind", default=None)
    ap.add_argument("--shot", default="")
    a = ap.parse_args()

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    app = QApplication([])

    from core.skill_registry import discover_skills, resolve_script_path
    from shared import i18n
    from shared.config_models import WindowGeometry
    from shared.qt.theme import apply_theme
    from shared.update_checker import get_current_version
    from ui.main_window import LauncherWindow

    i18n.init("en", os.path.join(REPO, "locales"))
    apply_theme(app)
    LauncherWindow._maybe_refresh_playtime_summary = lambda self: None   # no background log scan

    skills = discover_skills(REPO)
    availability = {s.id: resolve_script_path(s, REPO) is not None for s in skills}
    window = LauncherWindow(
        geometry=WindowGeometry(x=100, y=100, w=a.w, h=a.h, opacity=0.95),
        skills=skills,
        availability=availability,
        launcher_hotkey=a.hotkey,
        python_info="probe",
        on_toggle_skill=lambda sid: None,
        on_apply_settings=lambda d: None,
        on_shutdown=lambda: None,
        disabled_skills=[],
        keybinds_disabled=[],
        show_hidden_tiles=[],
        grid_rows=3,
        grid_cols=2,
        grid_layout={},
    )
    window.setAttribute(Qt.WA_DontShowOnScreen, True)
    window.show()
    app.processEvents()
    if a.rebind is not None:
        window.update_hotkey_badges(a.rebind, {})
        app.processEvents()

    bar = window._title_bar
    lay = bar.layout()
    items = []
    for i in range(lay.count()):
        w = lay.itemAt(i).widget()
        if w is None:
            continue                       # the stretch
        text = w.text() if hasattr(w, "text") else ""
        items.append({"cls": type(w).__name__, "text": text, "x": w.x(), "w": w.width(),
                      "need": w.sizeHint().width(), "visible": w.isVisible(),
                      "pt": w.font().pointSizeF(), "spacing": w.font().letterSpacing()})

    if a.shot:
        os.makedirs(os.path.dirname(os.path.abspath(a.shot)), exist_ok=True)
        bar.grab().save(a.shot)

    print(json.dumps({
        "version": get_current_version(),
        "dpr": window.devicePixelRatioF(),
        "window": [window.width(), window.height()],
        "bar": [bar.width(), bar.height()],
        "items": items,
    }))
    sys.stdout.flush()
    os._exit(0)          # no closeEvent, so no saved-geometry file for this probe


if __name__ == "__main__":
    main()
