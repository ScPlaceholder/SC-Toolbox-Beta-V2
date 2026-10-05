"""The Item Finder tutorial opens, has text on every tab, and names only what the tool has.

Every name set in bold in the tutorial must be a string in the tool's source
(or in the shared shopping list, or the Everything Finder window, for the
names it borrows from them), or be listed below as prose. See
shared/tutorial_guard.py. Rename a button or a tab and this goes red until the
tutorial says the new name.
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PKG = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))      # market_finder/
TOOL = os.path.normpath(os.path.join(PKG, ".."))                                            # Market_Finder/
ROOT = os.path.normpath(os.path.join(TOOL, "..", ".."))
for _p in (ROOT, TOOL):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pytest  # noqa: E402

from shared import tutorial_guard as guard  # noqa: E402

QtWidgets = pytest.importorskip("PySide6.QtWidgets")

TUTORIAL = os.path.join(PKG, "ui", "tutorial.py")
SOURCES = [PKG, os.path.join(ROOT, "shared", "shopping"),
           os.path.join(ROOT, "skills", "Everything_Finder", "everything_finder")]

PROSE = ["Drag a row"]
ALIASES: dict = {}


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _markup() -> str:
    from market_finder.ui.tutorial import _tabs
    return "".join(html for _title, html in _tabs())


def test_every_tab_has_a_title_and_text():
    from market_finder.ui.tutorial import _tabs
    tabs = _tabs()
    assert len(tabs) >= 4
    for title, html in tabs:
        assert title.strip() and "&" not in title
        assert len(guard.plain_text(html)) > 200, "tab %r is nearly empty" % title


def test_the_tutorial_builds(app):
    from market_finder.ui.tutorial import TutorialBubble, _tabs
    bubble = TutorialBubble()
    try:
        tabs = bubble.findChild(QtWidgets.QTabWidget)
        assert tabs is not None and tabs.count() == len(_tabs())
        for i in range(tabs.count()):
            assert guard.plain_text(tabs.widget(i).widget().text()), "tab %d has no text" % i
    finally:
        bubble.close()
        app.processEvents()


def test_the_hotkey_shown_is_the_launchers_not_a_literal(monkeypatch):
    import shared.hotkey_label as hk
    monkeypatch.setattr(hk, "hotkey_label", lambda key, default: "Ctrl+Alt+F9" if key == "hotkey_market" else default)
    assert "Ctrl+Alt+F9" in _markup()


def test_every_name_in_bold_exists_in_the_tool():
    markup = _markup()
    assert len(guard.bold_spans(markup)) > 30          # the check below is not passing on an empty list
    strings = guard.source_strings(SOURCES, exclude=[TUTORIAL])
    assert guard.problems(markup, strings, PROSE, ALIASES) == []


def test_the_item_table_no_longer_calls_the_shopping_list_the_grocery_list():
    with open(os.path.join(PKG, "ui", "virtual_table.py"), encoding="utf-8") as f:
        assert "Grocery List" not in f.read()
