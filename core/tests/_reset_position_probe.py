"""Child process: the REAL launcher window, its REAL Settings popup, and one click on
"Reset position for Pico Pals and Battle Buddy".

Run by test_position_reset.py, never imported.  Like _launcher_grid_probe.py it builds only the
window (ui.main_window.LauncherWindow), not skill_launcher.SCToolboxApp: no tool process is started
and no hotkey is bound.  The callback the launcher would pass is replaced by one that records the
call and returns the result named on the command line, so no settings file is read or written.

Usage:  python _reset_position_probe.py <pico result> <battle_buddy result> [--only-pico]

Prints one JSON object on stdout:
    label         the row's label text, or null when there is no such row
    button        the button's text
    calls         how many times the launcher's callback ran for the one click
    status        the bottom-bar message after the click
    status_color  "green" / "red" / the raw colour
    applied       how many times Apply's callback ran (must stay 0: the reset is not a saved value)
    unchanged     True when the popup's values still equal what it opened with
"""
from __future__ import annotations

import json
import os
import sys

REPO = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
sys.path.insert(0, REPO)

LABEL = "Reset position for Pico Pals and Battle Buddy"


def main() -> int:
    pico_result, bb_result = sys.argv[1], sys.argv[2]
    only_pico = "--only-pico" in sys.argv[3:]

    from PySide6.QtWidgets import QApplication, QLabel

    app = QApplication([])

    from core.skill_registry import discover_skills, resolve_script_path
    from shared import i18n
    from shared.config_models import WindowGeometry
    from shared.qt.theme import P, apply_theme
    from ui.main_window import LauncherWindow

    i18n.init("en", os.path.join(REPO, "locales"))
    apply_theme(app)
    LauncherWindow._maybe_refresh_playtime_summary = lambda self: None   # no background log scan

    skills = discover_skills(REPO)
    if only_pico:
        skills = [s for s in skills if s.id != "battle_buddy"]
    seen = {"calls": 0, "applied": 0}

    def reset():
        seen["calls"] += 1
        return {"pico": pico_result, "battle_buddy": bb_result}

    def applied(_settings):
        seen["applied"] += 1

    window = LauncherWindow(
        geometry=WindowGeometry(x=40, y=40, w=620, h=760, opacity=1.0),
        skills=skills,
        availability={s.id: resolve_script_path(s, REPO) is not None for s in skills},
        launcher_hotkey="<ctrl>+0",
        python_info="probe",
        on_toggle_skill=lambda sid: None,
        on_apply_settings=applied,
        on_shutdown=lambda: None,
        grid_rows=3,
        grid_cols=2,
        grid_layout={},
        on_reset_positions=reset,
    )
    window.show()
    app.processEvents()
    window._open_settings()
    app.processEvents()
    popup = window._settings_popup

    label = next((w.text() for w in popup.findChildren(QLabel) if w.text() == LABEL), None)
    button = getattr(popup, "_reset_pos_btn", None)
    if button is not None:
        button.click()
        app.processEvents()
    style = popup._status_label.styleSheet()
    color = "green" if P.green in style else "red" if P.red in style else style
    print(json.dumps({
        "label": label,
        "button": button.text() if button is not None else None,
        "calls": seen["calls"],
        "status": popup._status_label.text(),
        "status_color": color,
        "applied": seen["applied"],
        "unchanged": popup._collect_and_validate() == popup._baseline,
    }))
    sys.stdout.flush()
    os._exit(0)          # no closeEvent, so nothing is saved for this probe


if __name__ == "__main__":
    main()
