"""The Mission Database tutorial opens, has text on every tab, and names only what the tool has.

Every name set in bold in the tutorial must be a string in the tool's source,
an alias for one the tool builds at run time, or listed below as prose (see
shared/tutorial_guard.py). Rename a button, a page or a filter heading and
this goes red until the tutorial says the new name.
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

TUTORIAL = os.path.join(TOOL, "ui", "modals", "tutorial.py")
SOURCES = [os.path.join(TOOL, "ui"), os.path.join(TOOL, "services"), os.path.join(TOOL, "config.py")]

PROSE = ["30 minutes"]
# Button texts the pages make from lower-case keys with .title().
ALIASES = {
    "Career": "career", "Story": "story", "Asd": "asd",
    "Weapons": "weapons", "Armour": "armour", "Ammo": "ammo",
}


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _markup() -> str:
    from ui.modals.tutorial import _tabs
    return "".join(html for _title, html in _tabs())


def test_every_tab_has_a_title_and_text():
    from ui.modals.tutorial import _tabs
    tabs = _tabs()
    assert len(tabs) >= 5
    for title, html in tabs:
        assert title.strip()
        assert len(guard.plain_text(html)) > 200, "tab %r is nearly empty" % title


def test_the_tutorial_builds(app, monkeypatch):
    from ui.modals.base import ModalBase
    from ui.modals.tutorial import TutorialModal, _tabs
    # The popup list is shared by every popup of the tool. Another test file leaves popups in it whose
    # windows are already destroyed, and opening any popup then trips over them; start from an empty list.
    monkeypatch.setattr(ModalBase, "_open_dialogs", [])
    monkeypatch.setattr(ModalBase, "_pinned_dialogs", [])
    modal = TutorialModal()
    try:
        tabs = modal.findChild(QtWidgets.QTabWidget)
        assert tabs is not None and tabs.count() == len(_tabs())
        for i in range(tabs.count()):
            assert guard.plain_text(tabs.widget(i).widget().text()), "tab %d has no text" % i
    finally:
        modal.close()
        app.processEvents()


def test_the_hotkey_shown_is_the_launchers_not_a_literal(monkeypatch):
    import shared.hotkey_label as hk
    monkeypatch.setattr(hk, "hotkey_label", lambda key, default: "Ctrl+Alt+F9" if key == "hotkey_missions" else default)
    from ui.modals.tutorial import _tab_getting_started
    assert "Ctrl+Alt+F9" in _tab_getting_started()


def test_every_name_in_bold_exists_in_the_tool():
    markup = _markup()
    assert len(guard.bold_spans(markup)) > 60          # the check below is not passing on an empty list
    strings = guard.source_strings(SOURCES, exclude=[TUTORIAL])
    assert guard.problems(markup, strings, PROSE, ALIASES) == []
