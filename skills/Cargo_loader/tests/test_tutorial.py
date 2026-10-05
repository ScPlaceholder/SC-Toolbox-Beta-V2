"""The Cargo Loader tutorial opens, has text on every tab, and names only what the tool has.

The tutorial lives in cargo_app.py with the tool (_CargoTutorialDialog). Every
name set in bold in it must be a string somewhere ELSE in the tool's source,
an alias for one the tool builds at run time, or listed below as prose (see
shared/tutorial_guard.py). Rename a button or a heading and this goes red
until the tutorial says the new name.

Only the tutorial dialog is built: no CargoApp window, no ship data, no files.
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..')))
import shared.path_setup  # noqa: E402
TOOL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
shared.path_setup.ensure_path(TOOL)

import pytest  # noqa: E402

from shared import tutorial_guard as guard  # noqa: E402

QtWidgets = pytest.importorskip("PySide6.QtWidgets")

SOURCES = [os.path.join(TOOL, "cargo_app.py"), os.path.join(TOOL, "crate_ui.py"),
           os.path.join(TOOL, "cargo_engine")]
TUTORIAL_CLASS = "_CargoTutorialDialog"

# Bold that is a heading of the tutorial's own, emphasis, an example, or a key or mouse action.
PROSE = [
    "Quick-start steps:", "Header", "Ship dropdown", "Getting around the view", "Click & drag", "Hover a box",
    "Moving a box that is already placed", "Drag it.", "green", "Legend and overlay", "Two ways to do it",
    "Typing a count switches you to Auto", "While placing (Manual)", "Right-click",
    "Items never count toward SCU", "The size rows", "size button itself is the place tool", "Buttons",
    "Placing an item", "Click a row, then click the grid.", "S2 2×2×3", "its own tab", "Filling a crate",
    "The volume rule is strict", "Commodity brush", "paint brush", "Click with no brush",
    "Assignments overlay", "fade", "Nothing here changes your plan", "Alt+click — hide what is on top",
    "Layer slider — take the hold down a level at a time", "Getting them back",
    "↺ ↻", "▲ ▼",              # two buttons each, named together
    "1/8 · 1 · 2 · 4 · 8 SCU",  # the five crate buttons, read off as a row
]
# Names the tool builds at run time (an icon put in front, a number filled in), and the source text behind each.
ALIASES = {
    "✋ Manual": "Manual", "⚙ Auto": "Auto", "▶ Optimize": "Optimize", "✕ Clear": "Clear", "↺ Reset": "Reset",
    "✕ Clear items": "Clear items",
    "Mission Cargo 1": "Mission Cargo",
    "Alt+click hides the N above": "Alt+click hides the",
    "Up to layer 2/4": "Up to layer",
    "3 boxes hidden ·": "boxes hidden",
}


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _pages():
    import cargo_app
    return cargo_app._CargoTutorialDialog.tutorial_pages()


def test_every_tab_has_a_label_and_text():
    pages = _pages()
    assert len(pages) >= 5
    for label, html in pages:
        assert label.strip()
        assert len(guard.plain_text(html)) > 200, "tab %r is nearly empty" % label


def test_the_dialog_builds(app):
    import cargo_app
    anchor = QtWidgets.QWidget()
    dlg = cargo_app._CargoTutorialDialog(anchor=anchor)
    try:
        tabs = dlg.findChild(QtWidgets.QTabWidget)
        assert tabs is not None and tabs.count() == len(_pages())
        for i in range(tabs.count()):
            text = " ".join(lbl.text() for lbl in tabs.widget(i).findChildren(QtWidgets.QLabel))
            assert guard.plain_text(text), "tab %d has no text" % i
    finally:
        dlg.close()
        anchor.close()
        app.processEvents()


def test_the_hotkey_shown_is_the_launchers_not_a_literal(monkeypatch):
    import shared.hotkey_label as hk
    monkeypatch.setattr(hk, "hotkey_label", lambda key, default: "Ctrl+Alt+F9" if key == "hotkey_cargo" else default)
    text = "".join(html for _label, html in _pages())
    assert "Ctrl+Alt+F9" in text and "{HOTKEY}" not in text


def test_every_name_in_bold_exists_in_the_tool():
    markup = "".join(html for _label, html in _pages())
    assert len(guard.bold_spans(markup)) > 80          # the check below is not passing on an empty list
    # The tutorial's own class is left out: every name it quotes is a string in it.
    strings = guard.source_strings(SOURCES, skip_defs=[TUTORIAL_CLASS])
    assert guard.problems(markup, strings, PROSE, ALIASES) == []


def test_a_tab_label_with_an_ampersand_shows_it(app):
    # "&" alone is Qt's shortcut marker: "Ship & View" was drawn as "Ship  View".
    import cargo_app
    anchor = QtWidgets.QWidget()
    dlg = cargo_app._CargoTutorialDialog(anchor=anchor)
    try:
        tabs = dlg.findChild(QtWidgets.QTabWidget)
        titles = [tabs.tabText(i) for i in range(tabs.count())]
        assert all("&" not in t.replace("&&", "") for t in titles), titles
    finally:
        dlg.close()
        anchor.close()
        app.processEvents()
