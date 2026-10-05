"""The Mining Signals tutorial opens, has text on every tab, and names only what the tool has.

Every name set in bold in the tutorial must be a string in the tool's source
or listed below as prose (see shared/tutorial_guard.py). Rename a button or a
tab and this goes red until the tutorial says the new name.
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
SOURCES = [os.path.join(TOOL, "ui"), os.path.join(TOOL, "services"),
           os.path.join(TOOL, "ocr", "sc_ocr", "calibration.py")]

# Bold that is a game setting, a mouse action, emphasis or a path; not a name in this tool's source.
PROSE = [
    "Borderless Windowed", "all possible matches", "Drag empty canvas", "Scroll", "Drag a team",
    "Drop a ship near a team", "Drop a team near another team", "Right-click a ship",
    "Documents/SC Loadouts/mining_roster.json",
]
ALIASES: dict = {}


def _tutorial():
    """The tutorial module, loaded from its file. Not ``import ui.tutorial_popup``: another test file in this
    suite leaves a different package called ``ui`` in sys.modules, and the tutorial needs nothing from ``ui``."""
    import importlib.util
    name = "mining_signals_tutorial_popup"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, TUTORIAL)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return sys.modules[name]


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _markup() -> str:
    return "".join(html for _title, html in _tutorial()._tabs())


def test_every_tab_has_a_title_and_text():
    tabs = _tutorial()._tabs()
    assert len(tabs) >= 8
    for title, html in tabs:
        assert title.strip() and "&" not in title        # a lone & would be eaten as a shortcut mark
        # Ten tabs share one row. Short titles keep them clear of the scroll arrows; whether they fit depends
        # on the real fonts, so it was checked on a rendered picture, not here.
        assert len(title) <= 8, "tab title %r is long for a ten-tab row" % title
        assert len(guard.plain_text(html)) > 200, "tab %r is nearly empty" % title


def test_the_popup_builds(app):
    TutorialPopup, _tabs = _tutorial().TutorialPopup, _tutorial()._tabs
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
    monkeypatch.setattr(hk, "hotkey_label",
                        lambda key, default: "Ctrl+Alt+F9" if key == "hotkey_mining_signals" else default)
    assert "Ctrl+Alt+F9" in _markup()
    assert "Shift + 9" not in _markup()             # the default the old text gave, which was never this tool's


def test_every_name_in_bold_exists_in_the_tool():
    markup = _markup()
    assert len(guard.bold_spans(markup)) > 90          # the check below is not passing on an empty list
    strings = guard.source_strings(SOURCES, exclude=[TUTORIAL])
    assert guard.problems(markup, strings, PROSE, ALIASES) == []


def test_the_scan_region_tip_no_longer_carries_a_developers_note():
    with open(os.path.join(TOOL, "ui", "app.py"), encoding="utf-8") as f:
        assert "previous wording" not in f.read()
