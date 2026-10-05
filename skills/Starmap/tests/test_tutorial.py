"""The Star Map tutorial opens, has text on every tab, and names only what the tool has.

Every name set in bold in the tutorial must be a string in the source it
describes (the Star Map, the shared shopping list, the Assistant for the
calibration, the Everything Finder for its tab), an alias for one, or listed
below as prose. See shared/tutorial_guard.py. Rename a button and this goes
red until the tutorial says the new name.
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

TUTORIAL = os.path.join(TOOL, "starmap", "tutorial.py")
SOURCES = [TOOL, os.path.join(ROOT, "shared", "shopping"),
           os.path.join(ROOT, "tools", "Assistant", "assistant"),
           os.path.join(ROOT, "skills", "Everything_Finder", "everything_finder")]

# Bold that is a mouse action, emphasis, a level of the map in the tutorial's own words, or one of its own tabs.
PROSE = [
    "Planet & Moons", "built in", "Left-drag", "Wheel", "Right-drag", "Right-click a system without dragging",
    "Left-click Home", "Right-click Home", "Click the system you are starting from", "click the destination",
    "light years", "Double-click a row", "Drag a row", "Jump Routes",
]
# The tutorial writes a real ellipsis where the tool's text has three dots.
ALIASES = {
    "Search system or location…": "Search system or location",
    "Change home system…": "Change home system",
    "Filter terminals…": "Filter terminals",
    "Search items everywhere…": "Search items everywhere",
}


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _tutorial():
    """starmap/tutorial.py loaded from its file: it needs nothing else from the package, and another tool's
    package is also called ``starmap`` (Trade Hub's), so the name is not a safe way to reach it."""
    import importlib.util
    name = "starmap_tool_tutorial"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, TUTORIAL)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return sys.modules[name]


def _markup() -> str:
    return "".join(html for _title, html in _tutorial()._tabs())


def test_every_tab_has_a_title_and_text():
    tabs = _tutorial()._tabs()
    assert len(tabs) >= 5
    for title, html in tabs:
        assert title.strip() and "&" not in title.replace("&&", "")
        assert len(guard.plain_text(html)) > 200, "tab %r is nearly empty" % title


def test_the_popup_builds(app):
    mod = _tutorial()
    popup = mod.TutorialPopup()
    try:
        tabs = popup.findChild(QtWidgets.QTabWidget)
        assert tabs is not None and tabs.count() == len(mod._tabs())
        for i in range(tabs.count()):
            assert guard.plain_text(tabs.widget(i).widget().text()), "tab %d has no text" % i
    finally:
        popup.close()
        app.processEvents()


def test_the_hotkey_shown_is_the_launchers_not_a_literal(monkeypatch):
    import shared.hotkey_label as hk
    monkeypatch.setattr(hk, "hotkey_label", lambda key, default: "Ctrl+Alt+F9" if key == "hotkey_starmap" else default)
    assert "Ctrl+Alt+F9" in _markup()


def test_every_name_in_bold_exists_in_the_tool():
    markup = _markup()
    assert len(guard.bold_spans(markup)) > 60          # the check below is not passing on an empty list
    strings = guard.source_strings(SOURCES, exclude=[TUTORIAL])
    assert guard.problems(markup, strings, PROSE, ALIASES) == []
