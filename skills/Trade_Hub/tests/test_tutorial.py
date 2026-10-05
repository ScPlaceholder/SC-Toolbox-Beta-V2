"""The Trade Hub tutorial opens, has text on every tab, and names only what the tool has.

The tutorial's text is TradeHubWindow._tutorial_pages() in trade_hub_app.py. Every
name set in bold in it must be a string somewhere ELSE in the tool's source, or
listed below as prose (see shared/tutorial_guard.py). Rename a button, a view or
a column and this goes red until the tutorial says the new name.

No TradeHubWindow is built (it would start fetching prices): the dialog is
opened on a plain widget that borrows the two methods it needs.
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

# This tool, the ship list it shares ("-- No Ship Cap --"), and the Everything Finder window for its tab's name.
SOURCES = [TOOL, os.path.join(ROOT, "shared", "ships.py"),
           os.path.join(ROOT, "skills", "Everything_Finder", "everything_finder")]
TUTORIAL_DEF = "_tutorial_pages"

PROSE = ["Cards"]               # the tutorial's own tab, referred to from the Views tab
ALIASES: dict = {}


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _pages():
    import trade_hub_app
    return trade_hub_app.TradeHubWindow._tutorial_pages()


def test_every_tab_has_a_title_and_text():
    pages = _pages()
    assert len(pages) >= 5
    for title, html in pages:
        assert title.strip() and "&" not in title
        assert len(guard.plain_text(html)) > 200, "tab %r is nearly empty" % title


def test_the_dialog_builds(app):
    import trade_hub_app

    class Host(QtWidgets.QWidget):
        _tutorial_pages = staticmethod(trade_hub_app.TradeHubWindow._tutorial_pages)
        _open_tutorial = trade_hub_app.TradeHubWindow._open_tutorial

    host = Host()
    host._open_tutorial()
    dlg = host._tutorial_dlg
    try:
        tabs = dlg.findChild(QtWidgets.QTabWidget)
        assert tabs is not None and tabs.count() == len(_pages())
        for i in range(tabs.count()):
            assert guard.plain_text(tabs.widget(i).widget().text()), "tab %d has no text" % i
    finally:
        dlg.close()
        host.close()
        app.processEvents()


def test_the_hotkey_shown_is_the_launchers_not_a_literal(monkeypatch):
    import shared.hotkey_label as hk
    monkeypatch.setattr(hk, "hotkey_label", lambda key, default: "Ctrl+Alt+F9" if key == "hotkey_trade" else default)
    assert "Ctrl+Alt+F9" in "".join(html for _title, html in _pages())


def test_every_name_in_bold_exists_in_the_tool():
    markup = "".join(html for _title, html in _pages())
    assert len(guard.bold_spans(markup)) > 50          # the check below is not passing on an empty list
    # The tutorial's own function is left out: every name it quotes is a string in it.
    strings = guard.source_strings(SOURCES, skip_defs=[TUTORIAL_DEF])
    assert guard.problems(markup, strings, PROSE, ALIASES) == []
