"""The PlayTime tutorial opens, has text on every tab, and names only what the tool has.

Every name set in bold in the tutorial must be a string in the tool's source
or listed below as prose (see shared/tutorial_guard.py). Rename a button, a
tab or a card and this goes red until the tutorial says the new name.
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
SOURCES = [os.path.join(TOOL, "ui"), os.path.join(TOOL, "core")]

# Bold used for emphasis, not for a name on screen.
PROSE = [
    "saved", "Four of them are buttons.", "weekday name", "title in the middle",
    "It starts the first time you open one of these three tabs", "These are counts, not earnings.",
    "facing you", "the log never said",
]
ALIASES: dict = {}


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


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


def test_the_popup_builds(app):
    from ui.tutorial_popup import TutorialPopup, _tabs
    popup = TutorialPopup()
    try:
        tabs = popup.findChild(QtWidgets.QTabWidget)
        assert tabs is not None and tabs.count() == len(_tabs())
        for i in range(tabs.count()):
            assert guard.plain_text(tabs.widget(i).widget().text()), "tab %d has no text" % i
    finally:
        popup.close()
        app.processEvents()


def test_the_hotkey_shown_is_the_launchers_not_a_literal(monkeypatch):
    import shared.hotkey_label as hk
    monkeypatch.setattr(hk, "hotkey_label", lambda key, default: "Ctrl+Alt+F9" if key == "hotkey_playtime" else default)
    assert "Ctrl+Alt+F9" in _markup()


def test_every_name_in_bold_exists_in_the_tool():
    markup = _markup()
    assert len(guard.bold_spans(markup)) > 40          # the check below is not passing on an empty list
    strings = guard.source_strings(SOURCES, exclude=[TUTORIAL])
    assert guard.problems(markup, strings, PROSE, ALIASES) == []
