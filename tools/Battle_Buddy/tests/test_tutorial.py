# -*- coding: utf-8 -*-
"""The Battle Buddy tutorial opens, has text on every tab, and names only what the tool has.

Every name set in bold in the tutorial must be a string in the tool's source
(see shared/tutorial_guard.py for the rule). Rename a button, a card or an option
and this goes red until the tutorial says the new name.
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

TOOL = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
ROOT = os.path.normpath(os.path.join(TOOL, "..", ".."))
for _p in (ROOT, TOOL):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pytest  # noqa: E402

from shared import tutorial_guard as guard  # noqa: E402

QtWidgets = pytest.importorskip("PySide6.QtWidgets")

TUTORIAL = os.path.join(TOOL, "ui", "tutorial_popup.py")
SOURCES = [os.path.join(TOOL, "ui"), os.path.join(TOOL, "core"), os.path.join(TOOL, "hud_app.py")]

# Bold that is not a name on screen. None: in this tutorial bold is used only for names the tool shows.
PROSE: list = []
ALIASES: dict = {}


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_the_popup_builds_with_text_on_every_tab(app):
    from ui.tutorial_popup import _TAB_BUILDERS, TutorialPopup
    popup = TutorialPopup()
    try:
        tabs = popup.findChild(QtWidgets.QTabWidget)
        assert tabs is not None and tabs.count() == len(_TAB_BUILDERS) >= 4
        for i in range(tabs.count()):
            text = " ".join(lbl.text() for lbl in tabs.widget(i).findChildren(QtWidgets.QLabel))
            assert len(guard.plain_text(text)) > 150, "tab %d is nearly empty" % i
    finally:
        popup.close()
        app.processEvents()


def test_a_tab_title_with_an_ampersand_shows_it(app):
    # "&" alone is Qt's shortcut marker: "Power & Sigs" was drawn as "Power  Sigs".
    from ui.tutorial_popup import TutorialPopup
    popup = TutorialPopup()
    try:
        tabs = popup.findChild(QtWidgets.QTabWidget)
        titles = [tabs.tabText(i) for i in range(tabs.count())]
        assert all("&" not in t.replace("&&", "") for t in titles), titles
    finally:
        popup.close()
        app.processEvents()


def test_the_hotkey_shown_is_the_launchers_not_a_literal(app, monkeypatch):
    import shared.hotkey_label as hk
    monkeypatch.setattr(hk, "hotkey_label", lambda key, default: "Ctrl+Alt+F9" if key == "hotkey_battle_buddy" else default)
    from ui.tutorial_popup import tutorial_text
    assert "Ctrl+Alt+F9" in tutorial_text()


def test_every_name_in_bold_exists_in_the_tool(app):
    from ui.tutorial_popup import tutorial_text
    markup = tutorial_text()
    assert len(guard.bold_spans(markup)) > 15          # the check below is not passing on an empty list
    strings = guard.source_strings(SOURCES, exclude=[TUTORIAL])
    assert guard.problems(markup, strings, PROSE, ALIASES) == []
