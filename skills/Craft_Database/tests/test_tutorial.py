"""The Craft Database tutorial opens, has text on every tab, and names only what the tool has.

The second half is the guard against the tutorial going stale: every name set
in bold in the tutorial must be a string in the tool's source, an alias for
one, or listed below as prose (see shared/tutorial_guard.py for the rule).
Rename a button in ui/ and this goes red until the tutorial says the new name.
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

TOOL = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
ROOT = os.path.normpath(os.path.join(TOOL, "..", ".."))
sys.path.insert(0, ROOT)
import shared.path_setup  # noqa: E402
shared.path_setup.ensure_path(TOOL)

import pytest  # noqa: E402

from shared import tutorial_guard as guard  # noqa: E402

QtWidgets = pytest.importorskip("PySide6.QtWidgets")

TUTORIAL = os.path.join(TOOL, "ui", "tutorial_popup.py")
# Where the names the tutorial quotes are defined: this tool, and the launcher's Settings for the row it points at.
SOURCES = [os.path.join(TOOL, d) for d in ("ui", "data", "domain", "services")] + [
    os.path.join(ROOT, "ui", "settings_panel.py")]

# Bold that is emphasis, an example, or something Qt or the game supplies; not a name in this tool's source.
PROSE = [
    "crafting blueprint", "Left panel", "Center", "ingredient names", "Blueprint name", "craft time",
    "ingredient pills", "mission reward pools", "On by default.", "5 detail popups", "0 to 1000", "sni",
    "mission drop lists are not",
    "Yes",                      # the standard Yes button of the confirmation box
]
# Names the tool builds at run time, and the piece of source text each stands for.
ALIASES = {
    "INVENTORY (N)": "INVENTORY (",
    "DROPS (N MISSIONS)": "DROPS (",
    "(any 2 of 3)": "any",
    "Weapons / Sniper": "Weapons /",
    "Ship Components / Shield": "Ship Components /",
}


def _markup() -> str:
    from ui.tutorial_popup import _tabs
    return "".join(html for _title, html in _tabs())


def test_every_tab_has_a_title_and_text():
    from ui.tutorial_popup import _tabs
    tabs = _tabs()
    assert len(tabs) >= 4
    for title, html in tabs:
        assert title.strip()
        assert len(guard.plain_text(html)) > 200, "tab %r is nearly empty" % title


def test_the_popup_builds():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from ui.tutorial_popup import TutorialPopup, _tabs
    popup = TutorialPopup()
    try:
        tabs = popup.findChild(QtWidgets.QTabWidget)
        assert tabs is not None and tabs.count() == len(_tabs())
        for i in range(tabs.count()):
            label = tabs.widget(i).widget()
            assert guard.plain_text(label.text()), "tab %d has no text" % i
    finally:
        popup.close()
        app.processEvents()


def test_the_hotkey_shown_is_the_launchers_not_a_literal(monkeypatch):
    import shared.hotkey_label as hk
    monkeypatch.setattr(hk, "hotkey_label", lambda key, default: "Ctrl+Alt+F9" if key == "hotkey_craft_db" else default)
    from ui.tutorial_popup import _tab_getting_started
    assert "Ctrl+Alt+F9" in _tab_getting_started()


def test_every_name_in_bold_exists_in_the_tool():
    strings = guard.source_strings(SOURCES, exclude=[TUTORIAL])
    assert guard.problems(_markup(), strings, PROSE, ALIASES) == []
