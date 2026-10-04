"""Child process that builds the REAL launcher window from the REAL registry and prints its tiles.

Run by test_hidden_tiles.py, never imported.  A separate process because one test wants the
offscreen platform and a screenshot wants the real one, and Qt picks its platform once.

Usage:  python _launcher_grid_probe.py [--show-hidden id,id] [--disabled id,id] [--cols N]
                                       [--shot <png>]

Prints one JSON object on stdout:
    registry   every id discover_skills() returned, in order
    hidden     the ids among them declared hidden
    tiles      [{"id", "name", "row", "col"}] for every tile actually in the grid

What it deliberately does NOT do, so it is safe to run on a machine where the toolbox is in use:
it builds only the window (ui.main_window.LauncherWindow), not skill_launcher.SCToolboxApp, so no
tool process is registered, started or killed and no global hotkey is bound; it reads no launcher
settings file (rows / columns / disabled come from the arguments); the PlayTime summary refresh,
which would start a background log scan, is switched off; and it leaves with os._exit so the
window's closeEvent never saves a geometry file.  With --shot the window is rendered to a PNG
through WA_DontShowOnScreen, so nothing appears on the desktop.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

REPO = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
sys.path.insert(0, REPO)


def _ids(text: str) -> list[str]:
    return [t for t in (text or "").split(",") if t]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--show-hidden", default="")
    ap.add_argument("--disabled", default="")
    ap.add_argument("--cols", type=int, default=2)
    ap.add_argument("--shot", default="")
    a = ap.parse_args()

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QGridLayout

    app = QApplication([])

    from core.skill_registry import discover_skills, resolve_script_path
    from shared import i18n
    from shared.config_models import WindowGeometry
    from shared.qt.theme import apply_theme
    from ui.main_window import LauncherWindow

    i18n.init("en", os.path.join(REPO, "locales"))
    apply_theme(app)
    LauncherWindow._maybe_refresh_playtime_summary = lambda self: None   # no background log scan

    skills = discover_skills(REPO)
    availability = {s.id: resolve_script_path(s, REPO) is not None for s in skills}
    window = LauncherWindow(
        geometry=WindowGeometry(x=40, y=40, w=620, h=760, opacity=1.0),
        skills=skills,
        availability=availability,
        launcher_hotkey="<ctrl>+0",
        python_info="probe",
        on_toggle_skill=lambda sid: None,
        on_apply_settings=lambda d: None,
        on_shutdown=lambda: None,
        disabled_skills=_ids(a.disabled),
        keybinds_disabled=[],
        show_hidden_tiles=_ids(a.show_hidden),
        grid_rows=3,
        grid_cols=a.cols,
        grid_layout={},
    )
    if a.shot:
        window.setAttribute(Qt.WA_DontShowOnScreen, True)
    window.show()
    app.processEvents()

    grid = window._tiles_container.findChild(QGridLayout)
    tiles = []
    for sid, tile in window._tiles.items():
        row, col, _rs, _cs = grid.getItemPosition(grid.indexOf(tile))
        tiles.append({"id": sid, "name": str(tile.skill.name), "row": row, "col": col})
    tiles.sort(key=lambda t: (t["row"], t["col"]))

    if a.shot:
        app.processEvents()
        os.makedirs(os.path.dirname(os.path.abspath(a.shot)), exist_ok=True)
        window.grab().save(a.shot)

    print(json.dumps({
        "registry": [s.id for s in skills],
        "hidden": [s.id for s in skills if s.hidden],
        "tiles": tiles,
        "window": [window.width(), window.height()],
    }))
    sys.stdout.flush()
    os._exit(0)          # no closeEvent, so no saved-geometry file for this probe


if __name__ == "__main__":
    main()
