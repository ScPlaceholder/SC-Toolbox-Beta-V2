"""The Mining Loadout tutorial opens, has text on every tab, and names only what the tool has.

In this tutorial a name the user can read in the tool is written [[like
this]] (ui/components/tutorial_bubble.py). Every one of them must be a string
in the tool's source, or an alias for one the tool builds at run time (see
shared/tutorial_guard.py). Rename a button or a heading and this goes red
until the tutorial says the new name.
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

TUTORIAL = os.path.join(TOOL, "ui", "components", "tutorial_bubble.py")
SOURCES = [os.path.join(TOOL, d) for d in ("ui", "models", "services")]

PROSE: list = []
# The ship buttons show the ship's name in capitals (ship.upper()).
ALIASES = {"PROSPECTOR": "Prospector", "GOLEM": "Golem"}


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_every_tab_has_a_label_and_text():
    from ui.components.tutorial_bubble import _tabs
    tabs = _tabs()
    assert len(tabs) >= 4
    for tab in tabs:
        assert tab["label"].strip()
        text = "".join(t for _tag, t in tab["content"])
        assert len(text.split()) > 40, "tab %r is nearly empty" % tab["label"]


def test_no_line_is_wider_than_the_bubble_shows():
    from ui.components.tutorial_bubble import _WRAP, _tabs
    for tab in _tabs():
        text = "".join(t for _tag, t in tab["content"])
        for line in text.split("\n"):
            assert len(line) <= max(_WRAP, 52), "%r: %r" % (tab["label"], line)


def test_a_name_is_never_split_across_lines():
    from ui.components.tutorial_bubble import _p
    frags = _p("x " * 30 + "[[CRAFTED PARTS COMBINE:]] " + "y " * 30)
    assert ("ui", "CRAFTED PARTS COMBINE:") in frags


def test_the_bubble_builds_and_shows_each_tab(app):
    from ui.components.tutorial_bubble import TutorialBubble
    parent = QtWidgets.QWidget()
    bubble = TutorialBubble(parent)
    try:
        assert len(bubble._tab_btns) == len(bubble._tabs)
        for i in range(len(bubble._tabs)):
            bubble._select_tab(i)
            assert len(bubble._text_edit.toPlainText().split()) > 40
    finally:
        bubble.close()
        parent.close()
        app.processEvents()


def test_the_hotkey_shown_is_the_launchers_not_a_literal(monkeypatch):
    import shared.hotkey_label as hk
    monkeypatch.setattr(hk, "hotkey_label", lambda key, default: "Ctrl+Alt+F9" if key == "hotkey_mining" else default)
    from ui.components.tutorial_bubble import tutorial_markup
    assert "Ctrl+Alt+F9" in tutorial_markup()


def test_every_on_screen_name_exists_in_the_tool():
    from ui.components.tutorial_bubble import tutorial_markup
    markup = tutorial_markup()
    assert len(guard.bold_spans(markup)) > 20          # the check below is not passing on an empty list
    strings = guard.source_strings(SOURCES, exclude=[TUTORIAL])
    assert guard.problems(markup, strings, PROSE, ALIASES) == []
