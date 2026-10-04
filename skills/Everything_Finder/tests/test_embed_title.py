"""Inside the Everything Finder, the Item Finder tab must not show its own header.

J, 2026-10-04: the Item Finder tab still showed a "MARKET FINDER" title bar (the
tool's old name) under a tab that says ITEM FINDER. The tab names the tool, so
the embedded bar's icon + name are hidden; what the tool put on that bar for
itself (status line, buttons) has to stay.

No network and no real Item Finder: a stand-in window is built the way
market_finder/ui/app.py builds its title bar (title + icon, then its own labels
inserted after the bar's stretch).
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
EF_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ROOT)
import shared.path_setup  # noqa: E402
shared.path_setup.ensure_path(EF_DIR)

import pytest  # noqa: E402

QtWidgets = pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtCore import QCoreApplication  # noqa: E402


def _app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _fake_item_finder():
    """A window whose title bar is laid out like Item Finder's."""
    from shared.qt.base_window import SCWindow
    from shared.qt.title_bar import SCTitleBar
    win = SCWindow(title="Item Finder", width=700, height=400, min_w=300, min_h=200)
    tb = SCTitleBar(window=win, title="MARKET FINDER", icon_text="\U0001f6d2", show_minimize=False)
    lay = tb.layout()
    status = QtWidgets.QLabel("12,345 items")
    button = QtWidgets.QLabel("\U0001f6d2 Shopping List")
    for i in range(lay.count()):
        if lay.itemAt(i).spacerItem() is not None:
            lay.insertWidget(i + 1, status)
            lay.insertWidget(i + 2, button)
            break
    win.content_layout.addWidget(tb)
    win.content_layout.addWidget(QtWidgets.QLabel("the item table"), 1)
    win._tb, win._status, win._button = tb, status, button
    return win


def _labels(tb):
    return {lbl.text(): lbl for lbl in tb.findChildren(QtWidgets.QLabel)}


def test_hide_title_hides_the_name_and_icon_but_not_the_tools_own_labels():
    _app()
    from everything_finder.embed import embed_window
    inner = _fake_item_finder()
    outer = QtWidgets.QMainWindow()
    central = embed_window(inner, outer, hide_title=True)
    labels = _labels(inner._tb)
    assert labels["MARKET FINDER"].isHidden(), "the old-name header is still shown"
    assert labels["\U0001f6d2"].isHidden(), "the header icon is still shown"
    assert not inner._status.isHidden(), "the tool's status line was hidden with the header"
    assert not inner._button.isHidden(), "the tool's own button was hidden with the header"
    assert central is not None
    outer.deleteLater()


def test_default_embed_keeps_the_header():
    """Trade Hub is embedded without hide_title and keeps its (correct) name."""
    _app()
    from everything_finder.embed import embed_window
    inner = _fake_item_finder()
    outer = QtWidgets.QMainWindow()
    embed_window(inner, outer)
    assert not _labels(inner._tb)["MARKET FINDER"].isHidden()
    outer.deleteLater()


def test_the_item_finder_tab_is_embedded_without_its_header(monkeypatch, tmp_path):
    """The window's real Item Finder factory, with the tool swapped for the stand-in."""
    _app()
    from everything_finder import tool_loader, window as wmod
    monkeypatch.setattr(wmod.EverythingFinderWindow, "_state_path",
                        staticmethod(lambda: str(tmp_path / "window.json")))
    made = []

    def fake_loader(x, y, w, h, opacity):
        made.append(_fake_item_finder())
        return made[-1]
    monkeypatch.setattr(tool_loader, "load_item_finder_window", fake_loader)
    never = lambda: QtWidgets.QLabel("unused")          # noqa: E731
    w = wmod.EverythingFinderWindow(x=10, y=10, w=1000, h=700, initial_tab=wmod.TAB_ITEM,
                                    factories={wmod.TAB_TRADE: never, wmod.TAB_MAP: never})
    w.select_tab(wmod.TAB_ITEM)
    assert len(made) == 1
    assert _labels(made[0]._tb)["MARKET FINDER"].isHidden()
    assert not made[0]._status.isHidden()
    w.hide()
    w.deleteLater()
    for _ in range(5):
        QCoreApplication.processEvents()
