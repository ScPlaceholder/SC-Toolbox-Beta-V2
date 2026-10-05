"""shared/qt/tutorial_popup.py: the tabbed tutorial popup the newer tutorials are shown in."""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest  # noqa: E402

QtWidgets = pytest.importorskip("PySide6.QtWidgets")


@pytest.fixture
def app():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app
    from shared.qt.tutorial_popup import TutorialPopup
    for popup in list(TutorialPopup._open.values()):
        popup.close()
    app.processEvents()


def _tabs(calls=None):
    from shared.qt.tutorial_popup import Tab, page
    calls = calls if calls is not None else []
    return [Tab("One", page("<p>first <b>Name</b></p>")),
            Tab("Two & more", page("<p>second</p>"), "Do it", lambda: calls.append("pressed"))]


def test_it_builds_a_tab_for_each_with_its_text(app):
    from shared.qt.tutorial_popup import TutorialPopup
    popup = TutorialPopup.open("t", None, title="Some Tool", accent="#55ddaa", tabs=_tabs(), show=False)
    assert popup.tab_widget.count() == 2
    assert "first" in popup.tab_text(0) and "second" in popup.tab_text(1)
    assert "SOME TOOL" in " ".join(lbl.text() for lbl in popup.findChildren(QtWidgets.QLabel))


def test_a_lone_ampersand_in_a_tab_title_is_shown_not_eaten(app):
    from shared.qt.tutorial_popup import TutorialPopup
    popup = TutorialPopup.open("t", None, title="Some Tool", accent="#55ddaa", tabs=_tabs(), show=False)
    assert popup.tab_widget.tabText(1) == "Two && more"


def test_a_tabs_button_is_drawn_and_calls_back(app):
    from shared.qt.tutorial_popup import TutorialPopup
    calls = []
    popup = TutorialPopup.open("t", None, title="Some Tool", accent="#55ddaa", tabs=_tabs(calls), show=False)
    buttons = [b for b in popup.findChildren(QtWidgets.QPushButton) if b.objectName() == "tutAction"]
    assert [b.text() for b in buttons] == ["Do it"]
    buttons[0].click()
    assert calls == ["pressed"]


def test_opening_it_again_while_it_shows_gives_the_same_window(app):
    from shared.qt.tutorial_popup import TutorialPopup
    first = TutorialPopup.open("same", None, title="Some Tool", accent="#55ddaa", tabs=_tabs())
    again = TutorialPopup.open("same", None, title="Some Tool", accent="#55ddaa", tabs=_tabs())
    other = TutorialPopup.open("other", None, title="Other Tool", accent="#55ddaa", tabs=_tabs())
    assert again is first and other is not first


def test_after_it_is_closed_it_is_made_afresh(app):
    from shared.qt.tutorial_popup import TutorialPopup
    first = TutorialPopup.open("k", None, title="Some Tool", accent="#55ddaa", tabs=_tabs())
    first.close()
    app.processEvents()
    assert "k" not in TutorialPopup._open
    assert TutorialPopup.open("k", None, title="Some Tool", accent="#55ddaa", tabs=_tabs()) is not first
