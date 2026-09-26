"""Offscreen smoke tests for the Injuries tab (no window shown, no settings written)."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

PySide6 = pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

from core import injuries as inj  # noqa: E402
from core.fun_stats import FunStats  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _labels(w):
    return [lbl.text() for lbl in w.findChildren(QLabel)]


def test_empty_state(app):
    from ui.injuries_tab import InjuriesTab
    tab = InjuriesTab(on_request_scan=lambda force: None)
    tab.set_stats(FunStats())
    assert any("No injuries logged yet" in t for t in _labels(tab))
    assert tab.body is None
    tab.deleteLater()


def test_with_data_renders_diagram_and_rate(app):
    from ui.injuries_tab import InjuriesTab
    fs = FunStats()
    fs.injuries = inj.aggregate([{"inj": [["2026-09-08T01:17:05.579", "head", 1],
                                          ["2026-09-08T01:20:00.000", "left_arm", 3]]}])
    tab = InjuriesTab(on_request_scan=lambda force: None)
    tab.resize(1000, 900)
    tab.set_stats(fs)
    assert tab.body is not None and tab.stack is not None
    texts = _labels(tab)
    assert "2" in texts                       # total card
    assert "Head" in texts                    # most-hit (tie -> display order)
    tab.body.set_hover("head")
    tab.body.grab()                           # paints hover tooltip without error
    tab.stack.set_hover(0)
    tab.stack.grab()
    tab.deleteLater()
