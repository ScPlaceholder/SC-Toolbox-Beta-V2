"""Child process that builds the REAL launcher window and prints what each tile says on hover.

Run by test_tile_tooltips.py, never imported. Built like _launcher_grid_probe.py and safe for the
same reasons: only the window (ui.main_window.LauncherWindow) is built, so no tool process is
started and no global hotkey is bound; no launcher settings file is read or written; the PlayTime
background scan is switched off; it leaves with os._exit so no geometry file is saved.

Usage:  python _tile_tooltip_probe.py [--saved id=<binding>,...] [--rebind id=<binding>,...]
                                      [--keybinds-off id,id] [--disabled id,id] [--show-hidden id,id]
                                      [--no-summary id,id] [--hover id]

    --saved        set skill.hotkey BEFORE the window is built: a binding loaded from the settings file
    --rebind       LauncherWindow.update_hotkey_badges(...) AFTER it is built: a key changed while it is up
    --no-summary   blank these tools' summary before building: a tool whose skill.json has none
    --hover        send a real tooltip event to the NAME LABEL inside that tile and report what Qt shows

Prints one JSON object:
    tiles    {id: {"html": tile.toolTip(), "text": the same as Qt lays it out, one line per line}}
    always   whether the window shows tooltips while it is not the active window
    hover    {"visible": bool, "text": what the tooltip on screen says, "same": it is that tile's}
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


def _pairs(text: str) -> dict[str, str]:
    return dict(t.split("=", 1) for t in _ids(text))


def main() -> int:
    ap = argparse.ArgumentParser()
    for flag in ("--saved", "--rebind", "--keybinds-off", "--disabled", "--show-hidden", "--no-summary", "--hover"):
        ap.add_argument(flag, default="")
    a = ap.parse_args()

    from PySide6.QtCore import QEvent, QPoint, Qt
    from PySide6.QtGui import QHelpEvent, QTextDocument
    from PySide6.QtWidgets import QApplication, QLabel, QToolTip

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
    for s in skills:
        if s.id in _ids(a.no_summary):
            s.summary = ""
        if s.id in _pairs(a.saved):
            s.hotkey = _pairs(a.saved)[s.id]
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
        keybinds_disabled=_ids(a.keybinds_off),
        show_hidden_tiles=_ids(a.show_hidden),
        grid_rows=3,
        grid_cols=2,
        grid_layout={},
    )
    window.show()
    app.processEvents()
    if a.rebind:
        window.update_hotkey_badges("<ctrl>+0", _pairs(a.rebind))
        app.processEvents()

    def as_text(html: str) -> str:
        doc = QTextDocument()
        doc.setHtml(html)
        return doc.toPlainText()

    tiles = {sid: {"html": tile.toolTip(), "text": as_text(tile.toolTip()), "badge": tile._hotkey_label.text()}
             for sid, tile in window._tiles.items()}

    hover = None
    if a.hover:
        tile = window._tiles[a.hover]
        child = next(w for w in tile.findChildren(QLabel) if w.text() == tile.skill.name)
        assert not child.toolTip(), "the label has a tooltip of its own; this would not test the tile's"
        pos = QPoint(4, 4)
        QApplication.sendEvent(child, QHelpEvent(QEvent.ToolTip, pos, child.mapToGlobal(pos)))
        app.processEvents()
        hover = {"visible": QToolTip.isVisible(), "text": QToolTip.text(), "same": QToolTip.text() == tile.toolTip()}
        QToolTip.hideText()

    print(json.dumps({
        "tiles": tiles,
        "always": bool(window.testAttribute(Qt.WA_AlwaysShowToolTips)),
        "hover": hover,
    }))
    sys.stdout.flush()
    os._exit(0)          # no closeEvent, so no saved-geometry file for this probe


if __name__ == "__main__":
    main()
