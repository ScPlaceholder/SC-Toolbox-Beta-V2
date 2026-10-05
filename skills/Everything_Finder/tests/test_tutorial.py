"""The Everything Finder has a tutorial: a button for it, text on every tab, and only names that exist.

Every name set in bold in the tutorial must be a string in the source of the
tool it belongs to (this window, the three tools in it, the shared shopping
list, the Assistant for the route calibration), an alias for one, or listed
below as prose. See shared/tutorial_guard.py. Rename a button in any of them
and this goes red until the tutorial says the new name.

No tool is loaded: the window is built with stand-in tabs, and its state file
is a temp file.
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
EF_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ROOT)
import shared.path_setup  # noqa: E402
shared.path_setup.ensure_path(EF_DIR)

import pytest  # noqa: E402

from shared import tutorial_guard as guard  # noqa: E402

QtWidgets = pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtCore import QCoreApplication  # noqa: E402

TUTORIAL = os.path.join(EF_DIR, "everything_finder", "tutorial.py")
SKILLS = os.path.join(ROOT, "skills")
SOURCES = [
    os.path.join(EF_DIR, "everything_finder"),
    os.path.join(ROOT, "shared", "shopping"),
    os.path.join(SKILLS, "Market_Finder", "market_finder"),
    os.path.join(SKILLS, "Trade_Hub", "trade_hub_app.py"),
    os.path.join(SKILLS, "Starmap", "starmap"),
    os.path.join(ROOT, "tools", "Assistant", "assistant"),
]
# Left out of the search: the tutorials themselves (their text is not anybody's UI).
NOT_UI = [TUTORIAL, os.path.join(SKILLS, "Market_Finder", "market_finder", "ui", "tutorial.py"),
          os.path.join(SKILLS, "Starmap", "starmap", "tutorial.py")]

# Keys on the keyboard, and a tab of the tutorial itself.
PROSE = ["Ctrl+Tab", "Ctrl+Shift+Tab", "Set Route"]
ALIASES: dict = {}


def _app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _pump(n=5):
    for _ in range(n):
        QCoreApplication.processEvents()


@pytest.fixture
def window(monkeypatch, tmp_path):
    _app()
    from everything_finder import window as wmod
    monkeypatch.setattr(wmod.EverythingFinderWindow, "_state_path",
                        staticmethod(lambda: str(tmp_path / "window.json")))
    facs = {k: (lambda k=k: QtWidgets.QLabel("tool " + k)) for k in (wmod.TAB_ITEM, wmod.TAB_TRADE, wmod.TAB_MAP)}
    w = wmod.EverythingFinderWindow(x=10, y=10, w=1000, h=700, factories=facs)
    yield w
    from shared.qt.tutorial_popup import TutorialPopup
    for popup in list(TutorialPopup._open.values()):
        popup.close()
    w.hide()
    w.deleteLater()
    _pump()


def test_every_tab_has_a_title_and_text():
    from everything_finder import tutorial
    tabs = tutorial.tabs()
    assert len(tabs) >= 5
    for tab in tabs:
        assert tab.title.strip()
        assert len(guard.plain_text(tab.html)) > 200, "tab %r is nearly empty" % tab.title


def test_the_title_bar_has_a_tutorial_button_that_opens_it(window):
    buttons = [b for b in window._title_bar.findChildren(QtWidgets.QPushButton) if b.text() == "? Tutorial"]
    assert len(buttons) == 1
    buttons[0].click()
    _pump()
    from everything_finder import tutorial
    from shared.qt.tutorial_popup import TutorialPopup
    popup = TutorialPopup._open.get("everything_finder")
    assert popup is not None and popup.isVisible()
    assert popup.tab_widget.count() == len(tutorial.tabs())
    for i in range(popup.tab_widget.count()):
        assert guard.plain_text(popup.tab_text(i)), "tab %d has no text" % i


def test_opening_the_tutorial_loads_none_of_the_three_tools(window):
    window._show_tutorial()
    _pump()
    assert window.tabs.built_keys() == []


def test_the_star_map_tab_offers_the_star_maps_own_tutorial(window, monkeypatch):
    opened = []
    monkeypatch.setattr(window, "_show_star_map_tutorial", lambda: opened.append(True))
    popup = window._show_tutorial()
    buttons = [b for b in popup.findChildren(QtWidgets.QPushButton) if b.objectName() == "tutAction"]
    assert [b.text() for b in buttons] == ["Open the Star Map tutorial"]
    buttons[0].click()
    assert opened == [True]


def test_the_star_maps_own_tutorial_really_opens_from_here(window):
    popup = window._show_star_map_tutorial()
    try:
        assert popup is not None and type(popup).__name__ == "TutorialPopup"
        assert type(popup).__module__ == "ef_starmap.tutorial"      # the Star Map's, under this window's alias
        assert popup.findChild(QtWidgets.QTabWidget).count() >= 5
    finally:
        if popup is not None:
            popup.close()
        _pump()


def test_the_hotkey_shown_is_the_launchers_not_a_literal(monkeypatch):
    import shared.hotkey_label as hk
    monkeypatch.setattr(hk, "hotkey_label",
                        lambda key, default: "Ctrl+Alt+F9" if key == "hotkey_everything_finder" else default)
    from everything_finder import tutorial
    assert "Ctrl+Alt+F9" in tutorial.markup()


def test_every_name_in_bold_exists_in_the_tool_it_belongs_to():
    from everything_finder import tutorial
    markup = tutorial.markup()
    assert len(guard.bold_spans(markup)) > 40          # the check below is not passing on an empty list
    strings = guard.source_strings(SOURCES, exclude=NOT_UI, skip_defs=["_tutorial_pages"])
    assert guard.problems(markup, strings, PROSE, ALIASES) == []
