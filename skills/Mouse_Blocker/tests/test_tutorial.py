"""The Mouse Blocker has a tutorial: a button for it, text on every tab, and only names that exist.

Every name set in bold in the tutorial must be a string in the blocker's
source (or the launcher's Settings, for the two names it points at there), or
listed below as prose. See shared/tutorial_guard.py.

The blocker window is built but never shown: shown, it covers the screen and
starts its z-order timer.
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
TOOL = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
ROOT = os.path.normpath(os.path.join(TOOL, "..", ".."))
# The blocker's folder ahead of the toolbox root: both have a package called ``ui``, and the tool's own
# start-up (shared/app_bootstrap.py) puts its folder first too.
for _p in (ROOT, TOOL):
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)

import pytest  # noqa: E402

from shared import tutorial_guard as guard  # noqa: E402

QtWidgets = pytest.importorskip("PySide6.QtWidgets")

TUTORIAL = os.path.join(TOOL, "ui", "tutorial.py")
SOURCES = [os.path.join(TOOL, "ui"), os.path.join(ROOT, "ui", "settings_panel.py")]

PROSE: list = []
ALIASES: dict = {}


@pytest.fixture
def app():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app
    from shared.qt.tutorial_popup import TutorialPopup
    for popup in list(TutorialPopup._open.values()):
        popup.close()
    app.processEvents()


def test_every_tab_has_a_title_and_text():
    from ui import tutorial
    tabs = tutorial.tabs()
    assert len(tabs) >= 2
    for tab in tabs:
        assert tab.title.strip()
        assert len(guard.plain_text(tab.html)) > 200, "tab %r is nearly empty" % tab.title


def test_the_title_bar_has_a_tutorial_button_that_opens_it(app, monkeypatch):
    monkeypatch.delenv("SC_TOOLBOX_PRELOAD", raising=False)
    from ui import tutorial
    from ui.app import BlockerWindow
    from shared.qt.tutorial_popup import TutorialPopup
    window = BlockerWindow()                    # never shown
    try:
        buttons = [b for b in window._title_bar.findChildren(QtWidgets.QPushButton) if b.text() == "? Tutorial"]
        assert len(buttons) == 1
        buttons[0].click()
        app.processEvents()
        popup = TutorialPopup._open.get("mouse_blocker")
        assert popup is not None and popup.isVisible()
        assert popup.parent() is None, "the popup must not be owned by the blocker (see _show_tutorial)"
        assert popup.tab_widget.count() == len(tutorial.tabs())
        for i in range(popup.tab_widget.count()):
            assert guard.plain_text(popup.tab_text(i))
        assert not window.isVisible(), "opening the tutorial must not put the blocker up"
    finally:
        window.deleteLater()
        app.processEvents()


def test_the_hotkey_shown_is_the_launchers_not_a_literal(monkeypatch):
    import shared.hotkey_label as hk
    monkeypatch.setattr(hk, "hotkey_label",
                        lambda key, default: "Ctrl+Alt+F9" if key == "hotkey_mouse_blocker" else default)
    from ui import tutorial
    assert "Ctrl+Alt+F9" in tutorial.markup()


def test_every_name_in_bold_exists_in_the_tool():
    from ui import tutorial
    markup = tutorial.markup()
    assert len(guard.bold_spans(markup)) >= 5          # the check below is not passing on an empty list
    strings = guard.source_strings(SOURCES, exclude=[TUTORIAL])
    assert guard.problems(markup, strings, PROSE, ALIASES) == []
